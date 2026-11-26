# WP-Memory-Lifecycle: cut per-session RAM and make the server tree close with its client

Status: rev 3, 2026-11-26. Stage A shipped as v0.49.3 (50f6833), field test
pending. Rulings R1 to R3 answered (section 4). Rev 2 (rev 1 sonnet peer review: PASS WITH CHANGES,
1 blocker, 3 major, 5 minor, all folded in). Solo work on `main`, one release per stage.
Each stage is: implement, gate, sonnet peer review of the diff, release,
Colton tests on his machine with a plugin update and reload, then the next
stage starts. A stage that fails its field test is fixed before the next
one begins.

Graph tasks: epic b2970b15de79, leak task d2a62f38cc88, shared-daemon task
9873b833aafb. Findings this plan rests on: discoveries e2f760b0bc84 (memory)
and 4d3d542bcbd0 (lifecycle evidence).

## 1. Evidence (Colton's machine, 24 logical cores, 64 GB, 2026-11-26)

- 13 live sessions, 13 server trees, none orphaned. Each tree is
  `claude.exe -> uv.exe -> venv python.exe (launcher) -> python3.13.exe
  (server) -> venv python.exe (launcher) -> python3.13.exe (sidecar)`.
- Private commit per session: server about 900 MB, sidecar about 2.5 GB.
  Across 13 sessions that is about 45 GB; system commit was 138 of 164 GB.
- `import numpy` alone commits 789 MB. With `OPENBLAS_NUM_THREADS=1` it
  commits 50 MB. OpenBLAS reserves a buffer per thread at load, and both
  numpy and scipy bundle a copy.
- Full sidecar (model loaded, one encode): 2596 MB default, 1114 MB with
  only `OPENBLAS_NUM_THREADS=1`. Encode time is unchanged (109 ms against
  122 ms for one text, 623 ms against 640 ms for eight). Torch's own thread
  pool is not the cost and stays untouched.
- Stage-1 identification logs in `%TEMP%\vibe-cognition-startup`: 26 of 26
  servers resolved the stdin pipe peer to claude.exe (5 of them as
  `claude.exe.old.<n>` after a Claude Code self-update), 27 of 27 sidecars
  resolved the supervisor pid to the server's interpreter.
- `get_status.stale_server_sweep.server_count` reads 0 with 13 servers
  alive. Two filters in `stale_sweep.py` cause it: enumeration keeps only
  images named `python.exe` or `pythonw.exe` (the Store interpreter is
  `python3.13.exe`), and classification skips any image whose path lacks
  `\uv\python\` (the venv launcher lives under `.venv\Scripts`).
- uv is 0.9.27. docs/wp-lifecycle2-plan.md rev 4 assumes 0.9.28 or later
  for the launcher's documented Job Object.
- Not reproduced: killing a Claude Code process and watching the tree. The
  July evidence (docs/wp-lifecycle2-plan.md section 1) shows it surviving.

## 2. What a virtual environment does and does not do

The plugin already runs in its own venv (`UV_PROJECT_ENVIRONMENT`). A venv
selects packages; it does not contain processes. On Windows it adds one
resident launcher process per interpreter, which is the tier that hid
claude.exe from the shipped depth-2 ancestor watch. No stage changes the
venv arrangement.

## 3. Stages

### Stage A (v0.49.3): cap OpenBLAS threads

Change: `src/vibe_cognition/__init__.py` sets
`os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")` before anything else
is imported. The package `__init__` runs first for every entry point
(`-m vibe_cognition.server`, `-m vibe_cognition.embeddings.sidecar`, hooks,
dashboard, CLI tools), on both harnesses, and the sidecar also inherits the
value through its spawn environment. `setdefault` leaves a user's own value
alone.

The session-start hook's dependency probe (`hooks/session-start.sh`, the
`python -c "import torch, numpy, sqlite3"` line, and the Codex hook if it
has the same probe) imports numpy without the package, so it commits the
uncapped amount for a moment at every session start. The hook sets the
variable on that one command. Adding `import vibe_cognition` to the probe
was rejected: the probe's job is to test native dependencies, and a package
import failure there would raise a false broken-environment warning.

Rejected: putting the variable in the manifests' `env`. Codex registers the
server through `codex mcp add` with a managed env list, so a manifest-only
change needs a re-registration and still misses hooks and CLI entry points.

Acceptance:
- A1. A subprocess that imports `vibe_cognition` then reads the variable
  sees `1`. With the variable preset to `3` it still sees `3`.
  The first case is the regression guard; the second passes on reverted
  code and only pins the override.
- A2. Windows, 8 or more logical cores: a subprocess that imports
  `vibe_cognition` then numpy commits less than half the private bytes of a
  subprocess that imports numpy alone. `OPENBLAS_NUM_THREADS` is removed
  from both subprocess environments first, so a developer shell that sets
  it cannot make the test vacuous. Skipped elsewhere with a stated reason.
  Fails on the reverted code.
- A3. `vibe_cognition/__init__.py` imports nothing but `os`, and after
  `import vibe_cognition` in a fresh subprocess numpy is not in
  `sys.modules`.
- A4. Search results are unchanged: existing vector-store and search tests
  pass untouched.
- A5. Gates: ruff, render checks, full pytest, release-sync.

Field test (Colton): `/plugin update`, restart one session, run one
`cognition_search`. Pass when that session's real server process commits
under 250 MB and its sidecar under 1.4 GB, and search returns hits.
Sessions not restarted keep their old numbers until they are.

### Stage B (v0.50.0): arm the self-exit watches (WP-Lifecycle-2 Stage 2)

Technical spec of record: docs/wp-lifecycle2-plan.md, fixes F1 and F2,
acceptance AC1 to AC5, with rev 4's containment rules. This stage arms what
Stage 1 has been logging.

- B-F1. The server opens a handle on the resolved stdin pipe peer and adds
  it to the `WaitForMultipleObjects` set. Peer death exits with reason
  `client_watch_exit`. Wiring: `server.py` calls `arm_ancestor_watch()`
  before `resolve_stdin_pipe_peer()` today and the wait set is fixed before
  the watch thread starts, so the resolve moves ahead of the arm and its
  handle is passed in. Each wait-set entry carries its own exit reason; the
  loop fires one shared reason today. Verdict table:
  - `ok`: arm the watch.
  - every other verdict, including `peer_gone` and
    `peer_younger_than_self`: arm nothing, breadcrumb the verdict.
  Amendment at implementation (differs from lifecycle2 plan F1 and from rev
  2 of this plan, which exited at once on a pre-dead peer): the client is
  INFERRED from the pipe, and the only field evidence is Claude Code on one
  machine (no Codex session is in the logs). A wrong inference on another
  harness or a future client version must cost a missed watch, never a
  server that refuses to start. The stdin watch and the ancestor watch
  still cover a client that is really gone. The sidecar side keeps
  exit-at-once, because its supervisor pid is handed over explicitly.
  Second amendment, from the Stage B diff review: the client watch arms on
  Claude Code only (`harness.is_codex()` skips it). The pipe CREATOR is
  what gets watched, and a client whose pipes are created by a helper that
  exits while the client lives would lose a healthy server. Codex keeps
  logging the resolved peer; arm it there once its logs show the peer is
  the client. The client entry also has no polling fallback: a denied
  re-open arms nothing.
- B-F2. The sidecar opens a handle on `VIBE_SUPERVISOR_PID` and waits on it
  in addition to its depth-1 parent watch. Exit reason
  `supervisor_watch_exit`. A supervisor already dead at arm time exits at
  once.
- B-F3. The stale-server sweep admits any `python*.exe` image, drops the
  `\uv\python\` path gate, carries the parent pid out of the Toolhelp
  entry (discarded today), and counts only the leaf: a process whose
  command line has the server marker and which has no child with the same
  marker. `uv.exe` is never counted (its command line carries the marker
  too). `tests/test_stale_sweep.py` pins the old path gate and changes.
- B-F4 (DROPPED by ruling R2). A Job Object with kill-on-close, created and held
  by the server, around the sidecar. `subprocess.Popen` cannot place a
  child in a job at creation, and assigning afterwards races the launcher
  spawning the real interpreter outside the job. So either the spawn is
  created suspended, assigned, then resumed, or this item is dropped.

Every mechanism is self-closing: a process watches a read-only handle and
calls `os._exit(0)` on itself. Constraint 8f49b174c660 holds. Kill commands
appear only in tests and only against stand-ins the test spawned
(tests/test_containment_gate.py enforces this).

As built: `lifecycle.client_watch_entry` and
`lifecycle.supervisor_watch_entry` return entries for
`arm_ancestor_watch(extra_entries=...)`; AC1 and AC2 are the two
`test_stage_b_*` tests in tests/test_wp_lifecycle_integration.py (the
stand-in dies by `os._exit` on a sentinel and a holder keeps the pipe's
write end open, so no test ends a process it did not spawn); AC2b's
both-arms assertion is inside the AC2 test and the kill-and-respawn path
stays covered by tests/test_wp_sidecar.py; AC2c and B2 are
tests/test_lifecycle_owner_watch.py; B3 is tests/test_stale_sweep.py.

Acceptance: AC1, AC2, AC2b, AC2c, AC3, AC4, AC5 of
docs/wp-lifecycle2-plan.md as written. Dropped on purpose: AC2d (the
fallback ancestor walk is not built, because the pipe-peer resolution held
on every logged session; a non-`ok` verdict arms nothing instead) and AC6
(replaced by the field test below). Plus:
- B1. Each new test fails on the Stage A commit.
- B2. A `claude.exe.old.<n>` peer image arms the watch (the verdict is
  `ok`; the image name is logged, never matched).
- B3. Sweep unit test: a process table shaped like this machine's (Store
  interpreter, two launchers per tree) counts one server per tree.
- B4. Five consecutive green runs of the lifecycle integration tests
  (ledger rule 6).

Field test (Colton): `/plugin update`, open a throwaway session in a
scratch project, note its server and sidecar pids, close that terminal tab
without `/exit`. Pass when both pids are gone within 10 seconds. Then
repeat with `/exit`. Then `/reload-plugins` in a session that stays open:
pass when the session's tools still work and `get_status` counts every
live server. Reload leaves claude.exe alive, so an old server left behind
by a reload is detected by the sweep, not closed by the watch.

### Stage C (v0.51.0): stop the sidecar when idle (10 minutes, ruling R3)

Change: the supervisor stops its sidecar after a quiet period with no
embedding request, and starts it again on the next request. The server
stays up; only the model process goes away. The stop is the supervisor's
existing kill of its own child (the one sanctioned exception in constraint
8f49b174c660). `embedding_status` gains the value `idle`.

Design points to settle in the Stage C brief, after Stage B is in the
field: which existing thread owns the idle timer (no new thread after the
load window, WP-Wedge INV-1); how a request that arrives during the stop is
serialised; that a restart goes through the same load mutex; that a failed
restart degrades through the existing recovery loop.

Acceptance:
- C1. With the timeout set to a few seconds in a test, the launcher AND the
  real sidecar interpreter are gone after the quiet period
  (`_SidecarProcess.kill()` ends only the launcher; the interpreter goes by
  its own depth-1 watch).
- C2. `embedding_status` reads `idle` in `get_status` and in the dashboard.
  Both compute the status themselves from `embedding_ready`
  (`service_tools.py`, `dashboard/api.py`), so a new value in
  `SidecarSupervisor.status()` alone changes nothing.
- C3. The next `generate` call returns correct vectors and the status
  returns to `ready`.
- C4. A request racing the stop never returns an error and never marks the
  supervisor degraded. `generate` reads the process under the lock and
  sends outside it, and its error path kills and degrades, so this needs an
  in-flight count or a restart-on-ended path, not only a lock.
- C5. Concurrent callers during a restart spawn exactly one sidecar.
- C6. A cold restart does not run under `sidecar_request_timeout`, has a
  stated latency bound, and a test shows the dispatch pool is not saturated
  by callers waiting on it.
- C7. The brief states whether INV-1 permits the two threads each respawn
  starts (the recovery respawn already does this) and which existing thread
  owns the idle timer at what granularity.
- C8. Timeout 0 disables the feature and behaviour matches Stage B.
- C9. Tool-surface audit (workflow 67751ebc39bd) for the new status value.

Field test (Colton): leave a session idle past the timeout, confirm its
sidecar pid is gone, run a search, confirm hits and a new sidecar pid.

## 4. Rulings (Colton, 2026-11-26, graph decision recorded)

- R1. Arming the Stage B watches is approved. This is the explicit approval
  epic b2970b15de79 requires.
- R2. No Job Object around the sidecar. B-F4 is dropped; Stage B relies on
  the two self-exit watches.
- R3. Stage C is in. The sidecar stops after 10 minutes without an
  embedding request and restarts on the next one.

## 5. Known-intentional

- `os._exit(0)` on every death path stays.
- ctypes only, no new runtime dependency.
- The shipped depth-2 ancestor watch and stdin watch stay; Stage B adds to
  them.
- An idle timeout that exits the SERVER stays rejected (lifecycle2 plan
  section 7). Stage C stops only the sidecar, which restarts on demand.
- One BLAS thread in the server is not a search regression: the search is
  one matrix-vector product over the in-memory matrix, bound by memory
  bandwidth, fine at tens of thousands of rows.
- POSIX behaviour is unchanged in Stages B and C's Windows-only parts.
- The shared embedding daemon (task 9873b833aafb) and an ONNX model are out
  of scope. Nothing here blocks either.

## 6. Release mechanics per stage

Release procedure workflow 65f43ed83cf1: version bump in the three
manifests, `uv lock`, CHANGELOG and whats-new, gates, sonnet review of the
diff, commit and push to `main`, journal flush, pin request to Loki with the
code-commit SHA, then Colton's `/plugin update`. Stages B and C touch
`get_status`, so the tool-surface audit runs for them.
