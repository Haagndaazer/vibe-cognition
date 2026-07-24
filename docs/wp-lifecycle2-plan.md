# WP-Lifecycle-2 (high): the shipped self-exit watches are vacuous under the uv-trampoline topology

Status: rev 3 — Vince, 2026-07-24. Scoping for graph task d2a62f38cc88
(MCP server process leak, 13 trees / ~3.5GB since 7/13). Rev 1 Fable peer
review: FAIL (1 BLOCKER / 5 MAJOR / 3 MINOR) — folded into rev 2. Rev 2
sonnet verification pass: PASS, 9/9 folds confirmed, +4 MAJOR / 2 MINOR
fresh findings — folded into this rev 3.
Implementer: Vorpid. Branch: `wp-lifecycle2` in a worktree under
`C:\Users\colto\Documents\Projects\Worktrees\vibe-cognition`.

## 1. Field evidence (2026-07-24, Colton's machine, scoping session)

- **11 live server trees** at scoping time; at least 5 are stale leaks
  (7/20 x2, 7/22 x2, 7/23 x1 — all v0.28.0, which SHIPS lifecycle.py).
  Today's trees may be live sessions; not assumed leaked.
- **The real topology has one more intermediary than WP-Lifecycle knew:**
  `claude.exe → uv.exe → venv-trampoline python.exe → real python.exe (server)`.
  The plugin venv's `Scripts\python.exe` is a **uv trampoline**: it stays
  resident and spawns the uv-managed base interpreter as its child. Reproduced
  by direct spawn of `plugins\data\vibe-cognition-coltondyck\.venv\Scripts\python.exe`
  (launcher stays alive, real `cpython-3.12.11...\python.exe` child appears).
  The sidecar chain repeats it: `server → trampoline → real sidecar python`.
- **Trampoline kill semantics (Fable-review empirical check, production
  binary):** killing the trampoline kills the real python underneath within
  ~3s — the uv trampoline holds a kill-on-job-close **Job Object** around its
  child (no breakaway flags observed). So the supervisor's kill+respawn path
  is reaped today by an *undocumented uv internal*, with the depth-1 parent
  watch as belt-and-suspenders — not by the watch alone.
- **Smoking gun:** the 7/20 15:42 tree's claude.exe (pid 11800) is GONE, yet
  uv → trampoline → server → sidecar are all alive 4 days later. Client death
  did not reap the tree, the ancestor watch did not fire, and the stdin-pipe
  watch did not fire either — a write-end handle copy apparently survives
  somewhere (I-1 resolves who holds it).
- **Breadcrumbs** (leaked server pid 39980, 7/23): `parent_watch_armed` and
  `stdin_watch_armed`, both NON-degraded — the watch armed cleanly and is
  watching the wrong two processes.
- **Pipe-peer APIs validated (Fable-review empirical check):**
  `GetNamedPipeServerProcessId`/`GetNamedPipeClientProcessId` on an inherited
  stdin handle succeed for anonymous CreatePipe pipes, a pass-through
  intermediary, a real `uv run` chain (peer = top creator — **uv passes stdio
  handles through, it does not re-pipe**), and a Node.js/libuv parent
  (claude.exe's pipe machinery). Both variants return the pid of the process
  that **created** the pipe, recorded at creation time.
- **Upstream (claude-code-guide research, 2026-07-24):** Claude Code does not
  reap MCP stdio process trees on session end (issues #1935, #15211 Windows,
  #22612), does not use Windows Job Objects for MCP children, and does not
  follow the MCP spec's close-stdin → wait → SIGTERM → SIGKILL escalation.
  Subagent-reported, unverified in detail, but consistent with our field
  evidence. Conclusion stands regardless: **self-termination is the only
  reliable path; upstream will not save us.**

## 2. Root causes (four distinct gaps)

- **G1 — server ancestor watch is vacuous.** `DEFAULT_ANCESTOR_DEPTH = 2`
  watches parent (trampoline) + grandparent (uv). claude.exe sits at depth 3,
  unwatched. Trampoline and uv both wait DOWN the chain (no exec on Windows),
  so they never die first — the exact circular-wait failure mode the rev-1
  WP-Lifecycle review caught for uv, reproduced one level deeper by the
  trampoline.
- **G2 — sidecar watch is vacuous on the os._exit path.** sidecar.py arms
  `arm_ancestor_watch(depth=1)` on the documented assumption "the sidecar's
  real parent IS the server process" (sidecar.py:28-32). In production the
  real sidecar's parent is its own trampoline. When the server dies via
  `os._exit` (every death-watch path), the trampoline survives — its parent's
  death doesn't propagate — so the sidecar's watch never fires and the
  sidecar pair leaks. (The supervisor's *graceful* kill path is covered
  today by uv's trampoline job — see §1 — but that is an undocumented
  third-party internal, not a guarantee we may lean on.)
- **G3 — stdin-pipe watch did not fire on actual client death (UNRESOLVED).**
  On the 7/20 tree, claude.exe died; the kernel should have closed its pipe
  write end; `PeekNamedPipe` should have hit `ERROR_BROKEN_PIPE`; the server
  should have exited within the 5s grace. It is still alive. Either a write-
  end handle copy survives in uv/trampoline (inheritance), or the watch has a
  blind spot. Needs handle-level forensics (I-1) before we design around it.
- **G4 — observability gap that hid all of this.** Only the DEGRADED arm path
  breadcrumbs the resolved ancestor chain. A healthy arm logs just
  `parent_watch_armed`. One line of `grandparent_image=uv.exe` (instead of
  claude.exe) in the normal path would have exposed G1 on day one.

**Timing hypothesis (I-2 verifies):** WP-Lifecycle gated green 7/6; leaks
began 7/13. If a uv upgrade or venv recreation introduced the trampoline in
that window, WPL-AC1's green was real at gate time and silently went vacuous
a week later. Whether or not this holds, the lesson is the same: **a
fixed-depth ancestor walk is fragile against topology changes we don't
control. The fix must be topology-independent.**

## 3. Direction — one direction per fix

- **F1 (server → client watch, topology-independent).** PRIMARY (empirically
  validated in §1): resolve the stdin pipe peer via
  `GetNamedPipeServerProcessId`/`GetNamedPipeClientProcessId`, open a handle
  on that pid (existing OpenProcess + creation-time PID-reuse validation,
  unchanged), and add it to the `WaitForMultipleObjects` set — alongside, not
  replacing, the existing parent watch. **Spike success criteria (pinned —
  the spike must not false-positive):** resolved peer pid is not self, not
  the direct parent, and not the grandparent (a peer inside the depth-≤2
  ancestor set means an intermediary re-piped stdio → engage fallback); peer
  creation time earlier than ours (existing guard); peer pid + image
  breadcrumbed (F3). A pre-dead creator surfaces as OpenProcess failure →
  existing degrade rule applies — EXCEPT a pre-dead PEER, which is pinned to
  **exit now** by name (a dead pipe creator is unambiguous orphaning, the
  direct-parent-NULL tier — NOT the grandparent-degrade tier, which exists
  to protect legitimately-transient launch shims; the peer is the resolved
  real client, so the transient-shim protection does not apply). **Exit
  reason pinned:** the peer-death exit fires with a NEW distinct reason
  string `client_watch_exit` — the current `_watch()` loop fires one shared
  `parent_death_exit` for ANY signaled handle in the wait set, so adding the
  peer handle requires per-entry attribution in the loop (which entry
  signaled decides the reason string); AC1's breadcrumb assertion is
  unsatisfiable without this. FALLBACK (only if the spike fails on the
  real topology): image-name-aware ancestor walk with pinned semantics —
  walk upward while the ancestor's image basename ∈ {python.exe, pythonw.exe,
  uv.exe, uvx.exe}; the FIRST foreign image is the presumed client; watch it
  and the intermediaries below it; watch NOTHING above it (a shell/terminal/
  extension-host dying while claude lives must never exit us — the churn
  class the WPL rev-1 review killed); if the allowlist never terminates
  within depth 5 (renamed/copied interpreter), cap, degrade, breadcrumb —
  never exit on ambiguity. (claude under node.exe is safe: node.exe is
  foreign → treated as the client.) The spike outcome and chosen mechanism
  must be recorded in the PR body.
- **F2 (sidecar → server watch, kill the guessing — ADDITIVE, not a
  re-point).** The server passes its own pid explicitly at sidecar spawn
  (env var `VIBE_SUPERVISOR_PID`, plain inheritance survives the trampoline
  hop — verified); the sidecar opens a creation-time-validated handle on
  THAT pid and waits on it, **in addition to keeping the existing depth-1
  parent watch**. Rationale: the depth-1 watch backstops the supervisor's
  kill+respawn path (`_SidecarProcess.kill()` kills only the trampoline,
  sidecar_client.py:243-253, with the server still alive); today that path
  is reaped by uv's trampoline job (§1) — an undocumented internal that a
  future uv could drop, at which point every load-timeout retry would leak
  one wedged, disk-hammering real sidecar while the server lives: the
  original P0 amplifier, recreated. Both arms stay. The trampoline-job
  finding also de-risks the optional job-object enhancement: if the
  explicit-pid watch alone leaves any gap, a server-held Job Object
  (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) around the sidecar spawn is
  API-compatible with uv's nested job (no breakaway flags observed;
  nested jobs fine on Win10+) — but it is an enhancement, not the committed
  direction.
- **F3 (observability, mandatory, cheap).** Every arm — degraded or not —
  breadcrumbs the full resolved chain with image names, one line. Same for
  the sidecar's both arms and for F1's pipe-peer pid + image.
- **F4 (stale-sibling sweep, log-only, machine-wide).** Pinned matching
  mechanism: count live `vibe_cognition.server` processes machine-wide,
  **deduped to real interpreters only** (image path under the uv-managed
  base-interpreter dir, e.g. `AppData\Roaming\uv\python\...\python.exe` —
  each tree otherwise triple-counts via uv + trampoline + real). Per-graph
  attribution is NOT attempted: no process's command line carries the repo
  path (verified on the live trees — the uv line carries only the shared
  version-pinned plugin cache dir; REPO_PATH is env-only and cross-process
  env reads need PROCESS_VM_READ + PEB walking, out of ctypes budget). A
  pidfile registry in `.cognition/` was considered and REJECTED for this WP:
  `os._exit(0)` on every death path guarantees stale entries, forcing
  liveness + creation-time validation on every read — machinery the
  log-only goal doesn't justify (revisit only if an auto-kill ruling later
  requires per-graph attribution). Surface the count + oldest-age in
  `get_status`, labeled explicitly as machine-wide. **Execution site
  pinned:** the sweep runs inside the existing bg thread
  (`_load_embeddings_and_sync`), writing its result into context for
  get_status — never pre-yield (enumeration costs 100ms-2s: AC5
  handshake-latency regression) and never a new post-yield thread
  (WP-Wedge INV-1: no Thread.start after the load window opens). **No
  auto-kill — that needs an explicit ruling from Colton and is out of scope
  here.** This sweep is the only detection for the mode no watch can catch:
  client alive but session logically dead (/exit'd claude.exe lingering,
  /reload-plugins leaving the old pair running). Adding a get_status field
  triggers the recurring tool-surface self-sufficiency audit workflow:
  the implementer runs it (via cognition_get_workflow) before the gate and
  records the outcome in the PR body.

## 4. Pre-implementation investigation (evidence dies on reboot — do these first)

- **I-1 — pipe-owner forensics on the LIVE leaked trees.** Who holds the
  stdin pipe write end for a leaked server (7/20 tree ideally)? SysInternals
  `handle.exe` is not on PATH; either get approval to download it, or use a
  read-only ctypes `NtQuerySystemInformation(SystemHandleInformation)`
  script. Answers G3 and confirms F1's mechanism on the real leaked
  topology (not just the review's synthetic repros).
  **Colton must not reboot or kill the stale trees until this is captured.**
- **I-2 — timeline.** uv changelog / installed-version history for the
  trampoline's introduction; venv creation/mtime; correlate with the 7/13
  leak onset. Re-examine WPL-AC1's test venv: did its topology include the
  trampoline? If the green was vacuous-at-gate, that's ledger-worthy; if
  vacuous-later, record the topology-drift lesson instead.
- **I-3 — upstream issue filing.** Consolidate our evidence (tree listings,
  breadcrumbs, repro) into a GitHub issue on anthropics/claude-code (Windows,
  plugin-declared uv-launched MCP server, tree not reaped; /reload-plugins
  leaving the old pair). Outward-facing: **draft in-repo, file only on
  explicit human go.**

## 5. Acceptance criteria (WPL2-AC*) — every new test must FAIL on current main first

- **AC1 (F1, harness shape pinned — Fable BLOCKER fold):** the claude-stand-in
  process itself CREATES the stdin pipes and spawns the real uv → trampoline
  → server chain (a harness where pytest owns the pipes tests nothing: the
  pipe-peer APIs return the creating process's pid, so F1 would resolve to
  pytest and never be exercised). The test harness (pytest side) holds a
  **duplicated write-end handle** so the pipe survives the stand-in's death —
  deliberately reproducing the G3 field mode (surviving write-end copy) and
  keeping the shipped stdin watch from firing. Kill ONLY the stand-in pid
  (no tree-kill). Real server exits ≤5s, and uv + trampoline unwind with it.
  **Assert the exit breadcrumb/stderr reason is F1's** (not
  `stdin_pipe_closed_exit`, not the old parent-watch reason). Must fail on
  current main — and with the duplicated-write-end harness it fails on main
  by construction, not by a 0.5s timing margin.
- **AC2 (F2, os._exit path):** hard-kill the REAL server python (no graceful
  path): real sidecar AND its trampoline exit ≤5s. Must fail on main.
- **AC2b (F2, kill+respawn path — Fable MAJOR-2 fold):** supervisor
  kill+respawn while the server stays alive → the OLD real sidecar + its
  trampoline are gone ≤5s, the replacement comes up, and BOTH sidecar watch
  arms (depth-1 + supervisor-pid) are asserted present in breadcrumbs. (This
  path passes on main today only via uv's trampoline job — the test exists
  so a future uv dropping that job cannot silently reopen the leak.)
- **AC2c (F2, fire-logic unit test — sonnet MAJOR-2 fold):** because uv's
  trampoline job can reap the pair in AC2b regardless of our watch (making a
  broken supervisor-pid watch pass AC2b armed-but-inoperative), the
  supervisor-pid watch's wait/fire logic gets a UNIT test via the existing
  `_raw_open_process`/handle mock seam — wrong-pid, creation-time-mismatch,
  and signaled-handle-fires cases — independent of the OS race. (Same
  precedent as WPL's PID-reuse guard: "validation logic unit-tested even
  though the race itself is hard to drive.")
- **AC2d (F1 fallback walk unit test — sonnet MAJOR-3 fold):** the fallback
  walk's semantics (allowlist membership, first-foreign-image selection,
  nothing-above-the-client rule, depth-5 cap-and-degrade, renamed-
  interpreter non-termination → degrade-never-exit) are unit-tested in
  isolation via the same mock seam — the fallback may never engage in CI or
  production if the primary spike holds, and untested-but-shipped is how G1
  happened.
- **AC3 (regression):** stdin write-end closed (all copies) → exit within
  grace bound; existing WPL-AC1..AC4 suite stays green (the old tests keep
  their meaning: parent/grandparent death still exits).
- **AC4:** arming breadcrumbs contain the full chain with image names
  (asserted); F1's watched-peer pid + image breadcrumbed.
- **AC5:** full suite green via `uv run python -m pytest` (whole-repo, exact
  gate command), ruff clean, no handshake-latency regression (arming stays
  pre-yield, handle-opens only — INV-1-safe).
- **AC6 (field, human gate — scoped to what the fixes deliver, Fable
  MAJOR-4 fold):**
  - (i) /exit where claude.exe actually exits → tree gone within seconds.
  - (ii) /reload-plugins → old tree **detected and surfaced** by F4 in
    get_status. Reaping this mode is explicitly NOT this WP's promise —
    it is carried by the upstream filing (I-3) and any future auto-kill
    ruling. (If I-1 shows claude closes the old pipe on reload, then the
    F1/stdin paths must additionally reap it — verify at the gate.)
  - (iii) the stale 0.28.0 trees killed manually at upgrade time and
    documented. Install-mechanics constraint applies: releases gate on a
    human machine.

## 6. Known-intentional / constraints

- `os._exit(0)` on death paths stays — do not soften into graceful shutdown.
- ctypes only; no pywin32, no new runtime deps.
- Degrade-don't-abort arming philosophy stays (a watch failure must never
  break startup).
- POSIX stays a simple poll; do not over-engineer the platform we don't
  ship. **New-mechanism pin (sonnet MAJOR-4 fold):** F1 (pipe-peer) is
  Windows-only — POSIX keeps the existing `os.getppid()` poll unchanged and
  gains nothing from this WP. F2's supervisor-pid watch is Windows-only too
  (the env var is portable, but the wait-on-pid machinery is the Win32
  handle path; POSIX sidecar behavior is unchanged). The fleet is Windows.
- Dashboard on the `os._exit` death paths: nothing to do, and nothing CAN be
  done (`os._exit` never returns to cleanup code). This is correct — the
  dashboard is an in-process daemon thread on a local TCP socket, reclaimed
  by the OS with the process; it is not a subprocess and cannot leak the way
  the sidecar does. `stop_dashboard()` remains a graceful-shutdown-only
  concern.
- The sidecar keeps its depth-1 watch AND gains the supervisor-pid watch
  (F2 is additive). Update sidecar.py's docstring claim (sidecar.py:28-32),
  which is factually wrong in production, to describe both arms and why.
- **Accepted risk (F1):** if Claude Code ever moves MCP spawning into a
  transient helper child, the pipe creator becomes that helper and its exit
  would kill live servers. F3's peer-image breadcrumb is the detection for
  that topology change; accepted, recorded here.
- F2 must not preclude the future shared cross-session daemon epic
  (9873b833aafb): the sidecar protocol stays transport-agnostic; the
  supervisor-pid watch is a per-spawn concern, not a protocol one.
- Journal protocol: no `.cognition/journal.jsonl` commits on the WP branch;
  manager flushes via temp worktree; flush-then-push before merge.
- Standing WP constraints: DEVNULL subprocess rule (sidecar stdio PIPEs
  remain the sanctioned exception), exact-SHA report, voiding clause,
  worktree isolation.

## 7. Out of scope

- Auto-kill of stale sibling servers (needs Colton's ruling; F4 is log-only).
- Per-graph attribution in F4 (pidfile registry rejected for this WP — see
  F4; revisit with any auto-kill ruling).
- Idle-timeout self-exit ("no MCP request for N hours → exit") — REJECTED:
  false-positive risk on long-idle-but-live sessions outweighs the benefit
  while F1/F2/F4 cover the observed modes.
- Client-side reap fixes (not our code; I-3 files the evidence upstream).
- The shared embedding daemon epic (separate task 9873b833aafb).
- The stale-first-session/--no-sync task (43d3c3dab10f) — but NOTE the
  coupling: leaked servers hold venv DLLs open, which is exactly what makes
  dropping `--no-sync` dangerous; landing this WP unblocks that one.
- chromadb flake bucket (7ec1e5929309) — leaked servers are the standing
  multi-process load plausibly feeding it; expect (don't assume) pressure to
  drop after this lands; note the linkage in both tasks at closeout.
