# WP-Wedge (P0): stop the mid-session server freeze from the lazy heavy-import

**Manager:** Vince · **Implementer:** Vorpid · **Branch:** `wp-wedge` off `main` @ `052c767`
**Target release:** v0.15.4 (human releases; version bump in both `pyproject.toml` and `.claude-plugin/plugin.json` happens at release, not in this WP)
**Rev 3** — peer-reviewed twice (rev 1: FAIL, 9 findings — probe spec reproduced the
v0.12.1 pipe-drain wedge class, heartbeat could freeze the event loop; rev 2:
PASS-WITH-CHANGES, 5 findings — warm-pool LIFO decay, clobber-guard atomicity, both
folded in here).

---

## 1. Incident and root cause (gated fact, reproduced live 2026-07-05)

Since v0.15.3 shipped, agents intermittently freeze mid-session on a vibe-cognition tool
call that previously worked, and stay frozen until the human runs `/mcp` reconnect.
Root-caused with client logs + py-spy on two wedged servers (pids 72208, 78496):

- WP-C moved the `sentence_transformers`/torch/scipy import chain into the background
  thread (`SentenceTransformersBackend.__init__`, `src/vibe_cognition/embeddings/generator.py:67`,
  called from `server.py:330 _load_embeddings_and_sync`).
- That import chain intermittently wedges **inside a native DLL load** (`create_module`,
  observed in scipy's extension) on Windows under multi-agent load — 30+ min at ~0 CPU.
- While wedged, the thread holds the **Windows loader lock** (new OS-thread creation
  blocks process-wide → AnyIO worker spawn for tool dispatch blocks) and **Python
  per-module import locks** for the whole chain (any lazy import in a tool path blocks).
- Claude Code never times out a tool call → the call hangs forever
  (observed: `cognition_search` 24ms at minute 2, then 1,268s hang at minute 5, killed
  only by manual reconnect).
- Secondary symptom: `embedding_ready` never sets, so even non-colliding gated tools
  return "still loading" forever.

Evidence artifacts: client log
`…\claude-cli-nodejs\Cache\C--Users-colto-Documents-Projects-vibe-cognition\mcp-logs-plugin-vibe-cognition-vibe-cognition\2026-07-06T01-10-07-669Z.jsonl`
(lines 8–54), py-spy dumps (bg thread at `create_module` under `generator.py:67`),
breadcrumb logs in `%TEMP%\vibe-cognition-startup\` (≥8 servers never reached
`bg_model_loaded` since ship).

Pre-v0.15.3 the same wedge hit at module import, pre-handshake → 30s connect timeout →
visible launch failure. v0.15.3 converted that into an unbounded invisible mid-session
freeze. **We are NOT reverting WP-C** (see known-intentional); we are bounding the wedge
and degrading gracefully.

## 2. Direction (one direction; the why)

Bound the wedge out-of-process, signal it in-process, and shrink the tool-surface
collision window. The full fix (sidecar embedding process — the only thing that truly
eliminates loader-lock sharing) is a follow-up epic, too big for a P0 patch. Rejected
alternatives: revert WP-C (reintroduces launch-timeout failures the epic just fixed);
watchdog-kills-thread (a thread stuck in `LoadLibrary` cannot be killed and its locks
cannot be released — only avoidance and signaling work).

**Honesty bar for the PR body:** this WP reduces the loader-lock collision
probabilistically (probe shrinks the in-process wedge window to ~ms; warm workers cover
the serial-call case). It does not eliminate the class — concurrent tool calls needing a
fresh worker thread during a residual wedge can still block. Say so; don't claim
elimination. Full elimination is the sidecar epic.

## 3. Scope — four items

### 3a. Subprocess import probe before the in-process import
In `_load_embeddings_and_sync`, BEFORE the `bg_model_load_start` stamp and before
constructing the generator: run `[sys.executable, "-c", "import sentence_transformers"]`
as a subprocess with a **300s timeout** and `stdin=DEVNULL`, `stdout=DEVNULL`,
`stderr=DEVNULL`, `CREATE_NO_WINDOW` on Windows. **DEVNULL on all three is
load-bearing:** only the return code is consumed. This repo was already burned by the
PIPE variant — `src/vibe_cognition/cognition/git_identity.py:8-25` records that a
piped subprocess in this detached server context blocked forever **in the pipe drain,
where `timeout=` cannot fire**, and the v0.12.1 fix removed that subprocess entirely.
Do not reintroduce pipes here for any reason, including "we might want the error text."
- Probe succeeds → stamp, then proceed to the in-process import (now warm: OS file
  cache + Defender scan already paid out-of-process; the in-process `LoadLibrary`
  window shrinks from minutes to milliseconds).
- Probe times out → `kill()` the subprocess (then `wait()`), stamp, and **retry once**
  after a 60s sleep (still in the bg thread; transient machine-wide contention —
  Defender scan queues, disk pressure — clears on this scale, and a permanent
  degrade on one flaky probe is worse than the incident for a session that would
  have recovered). Second timeout → set `embedding_error`
  ("embedding import wedged twice (probe killed at 300s); search degraded — reduce
  concurrent sessions or reconnect"), set `embedding_ready`, **do not attempt the
  in-process import this session**, stamp + flush. Session survives degraded.
- Probe runs `sys.executable` directly (same interpreter/venv). No uv wrapping.
- Ordering contract (also §3d): `import_probe_start` → [`import_probe_ok` |
  `import_probe_killed` (×2 max)] → `bg_model_load_start` → in-process import. The
  watchdog clock (§3b) starts at `bg_model_load_start`, so a legitimately slow probe
  never trips it.
- **Chosen trade-off, stated:** the probe window is unsignaled — worst case
  300s + 60s + 300s ≈ **11 minutes** during which gated tools return "still loading"
  with no watchdog coverage. Bounded and honest beats the incident's forever-hang;
  do not "fix" this by starting the watchdog clock before `bg_model_load_start`.

### 3b. Watchdog on the event loop (signal, not kill) + the state contract
An asyncio timer on the main loop (loop stays alive when the bg thread wedges —
verified by py-spy on the incident processes): armed when the lifespan starts, it
checks whether `embedding_ready` is set **120s after `bg_model_load_start`** (read the
stamp's existence/time via the context, not by racing the bg thread; if
`bg_model_load_start` hasn't happened yet — probe still running — re-arm and check
again later). On firing: set `context["watchdog_fired"] = True`, set
`embedding_error` ("embedding load slow/wedged; search temporarily degraded"), set
`embedding_ready`. The watchdog never touches disk and is cancelled on normal
completion.

**Late-completion semantics — owner, clobber-guard, and the atomicity rule:**
the *bg thread's existing success path* (`server.py:334-339`) is the sole owner of
install-and-clear. On reaching it after the watchdog fired
(`context.get("watchdog_fired")` is true): install the generator, clear
`embedding_error` **only if `watchdog_fired` is set** (never clear an error written by
the except block at `server.py:400-406` — genuine failures win; the except path runs
regardless of `watchdog_fired` and its error stands), clear `watchdog_fired`, stamp
`bg_late_recovery` + flush, log one INFO line.

**Atomicity rule (mandatory):** the watchdog's test-and-fire ("ready not set →
set error + ready + `watchdog_fired`") and the bg thread's complete/except paths
("install generator + ready.set() + check-and-clear") each run under one shared
`threading.Lock`. Without it there is a stranding interleaving: bg reads
`watchdog_fired`=False → watchdog fires in that instant (ready not yet set) → bg
proceeds without clearing → a healthy, generator-installed session fails every gated
call forever on a stale `embedding_error`. Critical sections are tiny and bounded, so
holding the lock briefly on the event loop is acceptable. This interleaving is an AC2
variant.

**State contract fix (pre-existing TOCTOU, weaponized by the 120s window — must land
in this WP):** `_record_node` (`tools/cognition_tools.py:189`) and `_add_task`
(`cognition_tools.py:1174`) capture `lc["embedding_generator"]` once, early, then gate
on a later fresh `_embeddings_ready(lc)` re-check (`:254`, `:1233`). With a
watchdog-fired → late-recovery sequence, a call can pass the gate with a stale
`generator=None` → `AttributeError` inside the tool. Fix the pattern: re-read
`lc["embedding_generator"]` at/after the readiness gate, AND make the readiness check
treat `(ready set, no error, generator is None)` as not-ready — in **BOTH** gate
functions: `require_embeddings` (`tools/utils.py:22-31`) **and its boolean twin
`_embeddings_ready` (`tools/cognition_tools.py:57-62`)**, which is the gate the two
cited call sites actually flow through — patching only utils.py does not close AC4.
The inconsistent tuple must be unobservable by construction. Audit all gated tools
for the same capture-early pattern, not just these two.

### 3c. Shrink the tool-dispatch collision window (warm workers, honestly scoped)
AnyIO worker threads idle out (~10s `MAX_IDLE_TIME`), so a one-shot pre-warm is a
false fix; but a heartbeat that itself calls `to_thread` when no worker is idle would
`Thread.start()` **on the event-loop thread** — under a held loader lock that freezes
the loop and disables the §3b watchdog with it. Two mechanics to respect: AnyIO
reuses idle workers **LIFO**, so a single-submission tick keeps exactly one worker
warm while the rest idle out; and a tick that finds the warm worker(s) busy with a
real tool call will spawn on the loop thread. Sequence:
1. Pre-yield (before the bg import thread starts — the wedge window is not yet open):
   await **4 concurrent** no-op `to_thread` calls to force-spawn 4 workers while
   spawning is safe.
2. Start the heartbeat task: every **3s** (< the 10s idle timeout, with margin for
   loop lag), submit a **batch of 4 concurrent** no-op `to_thread` calls — matching
   the pre-spawn count, so the whole pool stays warm, not just the LIFO head. Guard
   applies to the batch: if the previous batch hasn't fully completed (worker
   starvation / possible wedge), **skip** this tick rather than stacking
   spawn-triggering submissions.
3. Heartbeat stops when `embedding_ready` sets (either path); it exists only for the
   load window.
4. Start the bg import thread only after step 1 completes.
Residual risk stated in the PR body per §2: this keeps ~4 workers warm, not ∞ — a
tick overlapping >4 in-flight tool calls, or a missed idle window under loop lag, can
still trigger a spawn on the loop thread during a residual wedge; §3a makes that
wedge window ~ms-scale, and the sidecar epic closes the class entirely.

**Import-collision regression test (AC3):** a test installing a `sys.meta_path` hook
that BLOCKS (a `threading.Event`, released in teardown) any NEW import of
`torch|scipy|sentence_transformers|transformers|sklearn` submodules, armed from a fake
bg thread; then invoke **every registered tool** through the existing no-client harness
(`tests/test_tool_wrappers.py` pattern: `mock_mcp`/`build_lc`/`make_ctx`), **each
invocation in its own thread with `join(timeout=10)`** so a deadlock is an
attributable per-tool assertion failure, not a process-wide `pytest-timeout`
`os._exit` (which would also torpedo the AC5 whole-repo run). Gated tools are expected
to return their error dicts; the assertion is "returns within 10s," per tool.

### 3d. Breadcrumb visibility for the wedge window
`flush_to_disk()` immediately after the `bg_model_load_start` stamp (bg thread —
allowed; the existing HEISENBUG GUARD only forbids disk I/O on the pre-yield path).
Today a wedged server's breadcrumb file is indistinguishable from a just-started one.
New stamps, each followed by a bg-thread flush where they can be the last event a
wedged process ever writes: `import_probe_start`, `import_probe_ok`,
`import_probe_killed`, `watchdog_fired` (stamped from the event loop is acceptable —
`stamp()` is stderr-only, no disk; its persistence rides the next bg flush or, if the
bg thread is wedged, is visible in captured stderr), `bg_late_recovery`.

## 4. Acceptance criteria (pre-committed; fix + proof in the same commit)

- **AC1 (probe kill path, failure-condition test):** probe command injected to block
  forever → killed at (parameterized) timeout, one retry after (parameterized)
  backoff, second kill → `embedding_error` + `embedding_ready` set, in-process import
  **never attempted** (assert via marker/mock), all tools respond < 2s after. Also the
  recovery variant: first probe killed, second succeeds → normal load proceeds.
- **AC2 (watchdog + late recovery):** in-process import blocked via hook, probe
  bypassed → watchdog fires at parameterized T, gated tools return the error dict;
  release the hook → generator installs, error clears, `watchdog_fired` cleared,
  INFO line + `bg_late_recovery` stamp present (assert the specific message/stamp).
  Two variants: (i) clobber-guard — bg thread raises a genuine error after watchdog
  fired → except-path error stands, is NOT cleared; (ii) stranding interleaving —
  watchdog fires concurrently with normal completion (drive both sides of the §3b
  lock) → session ends ready + error-free, never stranded on a stale error.
- **AC3 (import-collision):** as §3c — every registered tool returns within 10s,
  per-tool thread + join, while the heavy-chain import is blocked mid-flight.
- **AC4 (state contract):** with `ready` set, `embedding_error=None`, and
  `embedding_generator=None` (the late-recovery race tuple), `_record_node`,
  `_add_task`, and every other gated tool return the loading/degraded error dict —
  no `AttributeError` — via BOTH gate functions (§3b: `require_embeddings` and
  `_embeddings_ready`).
- **AC6 (heartbeat lifecycle):** warm-worker pre-spawn happens before the bg thread
  starts; heartbeat batches ≥2 ticks during a (simulated) load window, skips when the
  prior batch is incomplete, and stops after ready/error (both paths).
- **AC5 (gate, pinned command — run exactly this, whole repo, from repo root):**
  `uv run python -m pytest -q` green and `uv run ruff check .` clean. I will re-run
  both at your pinned SHA in an isolated worktree with an import-provenance check;
  a green at any narrower scope does not count.
- **Tautology check is mine at the gate:** I run AC1–AC4 and AC6 against the reverted
  fix in a throwaway worktree; each must fail for the *specific* reason (hang/timeout/
  AttributeError), so don't write assertions that pass vacuously.

## 5. Known-intentional — do NOT "fix" these

- WP-C lazy import stays at `generator.py:67` — no revert to module-top import
  (decision 9022f7de94e9; the launch-stability win is settled).
- `_venv_guard`: torch is presence-checked only, NEVER real-imported there — load-bearing.
- chromadb stays a real module-top import; WP-A retry wrapper stays as-is.
- Tools return "still loading"/error dicts instead of blocking on `embedding_ready` — settled.
- HEISENBUG GUARD: no disk I/O on the synchronous pre-yield path — §3d flushes are
  bg-thread only; the §3c pre-warm is pre-yield but touches no disk.
- `git_identity.py`'s no-subprocess design is settled — the §3a probe does not change it.
- `--no-sync` + hook-owns-healing topology — untouched.
- fastmcp stays pinned at its current version in this WP (no opportunistic upgrade).

## 6. Out of scope (tracked separately)

- Sidecar embedding process (the durable loader-lock elimination) — follow-up epic;
  task node to be opened once the graph is writable again.
- Orphan-server self-exit (`docs/wp-server-lifecycle-plan.md`, existing open task) —
  priority raised to high; it is the amplifier (~80 leftover python processes observed,
  several at 2.5–2.9 GB).
- OS-level wedge forensics (Defender exclusions etc.) — user-side mitigation note, not code.

## 7. Standing constraints

- Never commit `.cognition/journal.jsonl` on the WP branch (shared-worktree protocol).
- **Do not call vibe-cognition MCP tools for durable recording during this WP** — the
  live server may be wedged (this bug); relay durable facts via `For-the-record:`
  instead. Task-status updates: attempt them, but treat a >30s stall as this bug —
  abandon the call and note it in the handoff rather than hanging your session.
- Post your plan as non-blocking FYI and proceed; the diff is the gate.
- Handoff must carry a `For-the-record:` field (durable facts; I record to the graph —
  currently unwritable because this very bug wedged the session's server, which is
  also why this brief is a committed doc rather than a document node).
- Sign-off will name an exact commit SHA; any commit after approval voids it.
