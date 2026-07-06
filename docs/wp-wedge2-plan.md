# WP-Wedge-2 (P0): tool dispatch must survive an in-process import wedge

Status: rev 3 — Vince, 2026-07-06. Rev 1 review FAIL (7 findings) and rev 2
re-review FAIL (8 findings, incl. the stdio-transport BLOCKER) both folded in.
§2 is built on mid-wedge py-spy captures that settle both hypotheses.
Implementer: Vorpid. Branch: `wp-wedge2` in a worktree at
`C:\Users\colto\Documents\Projects\Worktrees\vibe-cognition`.

## 1. What happened (post-v0.15.4 evidence, same day as the ship)

Three v0.15.4 servers (venv confirmed 0.15.4; breadcrumbs show new-code stamps)
wedged in production on 2026-07-06. The probe succeeded on all of them; the
in-process import wedged anyway.

**Incident A — pid 35220 (09:29), "loop-alive" mode.** Probe ok 7.3s;
`bg_model_load_start` → `bg_model_loaded` took **1,463.5s (24.4 min)**;
watchdog fired at +120.0s and late recovery worked. A `get_status` call at
+3.5min **hung 1,261s** until user cancel — but the server answered the cancel
INSTANTLY, so the event loop was pumping the whole time: the tool's worker
thread was started and then blocked INSIDE the dispatch/handler path. The same
tool had answered in 13ms two minutes earlier (pre-watchdog-fire).

**Incident B — pid 50760 (10:00), "loop-frozen" mode, py-spy'd mid-wedge**
(dump: scratchpad `pyspy-50760-loop-frozen.txt`; copy whatever you need into
the repo before relying on it — scratchpads are session-bound):

- Bg thread: stuck in `create_module` (`importlib._bootstrap_external:1293`,
  the native-DLL load) under `scipy.interpolate._fitpack`, reached from
  `generator.py:67` ← `server.py:536`. The real wedge, live.
- **MainThread: frozen in `Thread.start()` → `wait(threading.py:355)`, called
  from `run_sync_in_worker_thread (anyio)` ← `call_sync_fn_in_threadpool
  (fastmcp)` ← `call_tool` — i.e., MCP TOOL DISPATCH spawned a fresh worker
  thread from the event-loop coroutine and the new OS thread never signals
  started (blocked at thread-attach under the loader lock held by the wedged
  DLL load).**
- Only ONE AnyIO worker thread remained: with the loop frozen, the heartbeat
  coroutine stopped ticking and the warm pool idled out (anyio worker
  MAX_IDLE_TIME ~10s). The freeze is self-reinforcing.

**Incident C — pid 2604 (10:00), same wedge, loop still healthy** (dump:
`pyspy-2604-wedged-loop-alive.txt`): bg thread stuck in `create_module` under
`scipy.special`; loop pumping; 5 warm workers alive. One unlucky dispatch away
from Incident B.

Breadcrumb caveat discovered via Incident B: pid 50760's breadcrumb FILE ends
at `handshake_yield` even though its stack proves `import_probe_*` and
`bg_model_load_start` happened — stamps buffer in memory and flush only at
certain points, so mid-wedge files under-report. See §3e.

(Three OTHER hangs the same morning were sessions pinned to pre-update
v0.15.3 servers — the known old-code wedge, out of scope. Old-code specimens
pids 11596/10380 may still be alive for comparison py-spy; do not kill any
running server.)

## 2. Root-cause reading — both mechanisms are now OBSERVED, not hypothesized

1. **The §3a probe premise is falsified.** A successful subprocess import does
   not immunize the in-process import (separate process, separate loader
   state). Wedges of 24+ minutes occur after `import_probe_ok`. The probe
   stays (it converts some wedge classes to throwaway-subprocess cost and is
   harmless otherwise) but nothing may rely on it.
2. **Mode (b) — dispatch-spawn loop freeze (Incident B, proven by stack):**
   anyio's `to_thread.run_sync` spawns a fresh `WorkerThread` via
   `Thread.start()` **synchronously on the event-loop thread** whenever no
   idle worker is available; under the wedge the new OS thread can't attach,
   `Thread.start()` never returns, the LOOP freezes, the heartbeat dies, the
   pool evaporates. WP-Wedge's warm pool + batch-of-4 heartbeat narrowed this
   window but cannot close it: any instant where the pool is saturated (e.g.,
   a tool call landing inside a heartbeat tick, or >4 concurrent calls) forces
   a spawn. Note the predecessor's own lifespan comment already described this
   exact path; rev 1's suggestion that a dispatch-spawn could block "just that
   dispatch slot" was wrong — `Thread.start()` runs on the loop thread and a
   blocked one freezes everything.
3. **Mode (a) — in-worker block with the loop alive (Incident A):** the
   worker was started (no spawn needed at that instant) and blocked inside the
   dispatch/handler path. The precise blocking site is NOT yet pinned. Known
   candidate class: imports executed at call time — `sys.modules`-cached
   imports don't block, but `from X import Y` where X is mid-initialization by
   the wedged thread DOES block on X's import lock, and any first-time import
   blocks on the extension-load path. Known function-body imports reachable
   from tools today (must be eliminated or proven-cached by §3b):
   - `tools/service_tools.py:87` — `from .project_registry import
     LoadedProjects` inside `get_status` itself,
   - `tools/cognition_tools.py:438` — `import json as _json`,
   - `dashboard/api.py:182` — `from starlette.concurrency import
     run_in_threadpool`,
   - `dashboard/server.py:107,146-147` — local `webbrowser`/`time`.
   Third-party conditional imports (chromadb telemetry/embedding-function
   paths and similar first-use imports) are part of this class and cannot be
   pre-enumerated by grep alone.
4. What v0.15.4 got right and keeps: instant handshake, watchdog + late
   recovery + `_wedge_lock` atomicity (all fired correctly in Incident A).
   The failing layer is the degraded-mode SERVICE guarantee.

## 3. Direction

Two invariants, each closing one observed mode:

- **INV-1 (kills mode b): NOTHING running on the event loop may execute
  `Thread.start()` after `handshake_yield` — not tool dispatch, and not
  anything else.** This is deliberately broader than "tool dispatch": the
  stdio TRANSPORT rides the same spawn-on-demand pool. Verified in the pinned
  venv: `mcp/server/stdio.py` wraps stdin/stdout via `anyio.wrap_file`, whose
  `AsyncFile` routes every `readline`/`write`/`flush` through
  `to_thread.run_sync` (`anyio/_core/_fileio.py:99,117,140`); the stdin
  reader permanently occupies one worker, so EVERY response write needs
  another — an empty anyio pool turns each tool RESPONSE into a fresh
  `Thread.start()` on the loop. Scope therefore includes a mandatory
  **inventory of all post-handshake `to_thread`/loop-side spawn users**
  (stdio reader + writer at minimum) and covering every one of them.
  For tool dispatch, the known-viable first-party seam (no fastmcp fork): re-
  register sync tools as `async def` wrappers routing the body to a dedicated
  pre-started `concurrent.futures.ThreadPoolExecutor` via `run_in_executor` —
  TPE threads are created before `handshake_yield`, never idle out, and
  `submit()` beyond `max_workers` QUEUES without spawning. Note this seam
  fixes DISPATCH only, not `wrap_file`: the transport needs its own cover
  (pre-spawn + keep-alive of the anyio default pool sized for reader+writer,
  or an equivalent Vorpid judges cleaner). The WP-Wedge warm-pool/heartbeat
  machinery may be removed ONLY once the transport path is independently
  covered — removing it before that is a net REGRESSION vs v0.15.4 (today the
  heartbeat incidentally keeps response-write workers warm).
- **INV-2 (kills mode a): the tool surface is import-free at runtime.** After
  `handshake_yield`, nothing a tool handler or the dispatch path executes may
  trigger import machinery. Pre-import AND pre-exercise before the handshake;
  the heavy chain (torch/scipy/sentence_transformers & co.) stays lazy in its
  sanctioned site and is exempt (it's the wedge source, not a dispatch
  dependency).

The **sidecar embedding process** (heavy chain never imported into the serving
process at all) remains the durable fix — explicitly OUT of this WP, filed as
an epic. This WP makes the current architecture honest about degraded mode.

## 4. Scope

### §W2-a — Pin mode (a)'s blocking site (bounded forensics, first)
Incident A's exact block point is unknown; INV-2's design should be informed,
not guessed. Build the faithful repro (see AC fidelity note below), drive
`get_status` and a representative tool set in loading AND post-watchdog-fired
states, capture the blocking stack. If live wedged specimens still exist,
py-spy them for corroboration. Deliverable: the stack + which import/lock, as
a For-the-record item. If the finding contradicts INV-2 (i.e., the block is
NOT import-machinery), HOLD and report to Vince before implementing §W2-c.

**OUTCOME (2026-07-06, Vorpid, ratified by Vince):** NEGATIVE. A fidelity-
compliant repro (subprocess-isolated, real FastMCP in-memory dispatch, bg
thread blocked mid-`exec_module` of a real not-yet-imported module) could NOT
hang `get_status`/`cognition_search`/`cognition_add_task`/
`cognition_get_history` — all returned ~30ms. The four known function-body
import sites test clean under the only simulation buildable in pure Python
(per-module import lock). The native OS-loader-lock class — the likely
Incident-A mechanism — is not reproducible from pure Python for mode (a) any
more than for mode (b). Consequence: Incident A's site stays unpinned;
WP2-AC2 is REPLACED by §W2-f (production self-forensics) so the next real
occurrence pins itself. INV-1/INV-2 proceed as defense-in-depth on the
strength of Incident B (photographed) plus the eliminated import-machinery
class.

### §W2-b — INV-1: spawn-free event loop
As in §3 Direction: dispatch seam + transport cover + post-handshake
`to_thread` inventory. Enforcement is WP2-AC1/WP2-AC3, not inspection.

### §W2-c — INV-2: import-free tool surface
Mandatory audit, not vibes: an AST walk over every module reachable from a
registered tool flagging every function-body `Import`/`ImportFrom` (this is a
DIFFERENT guard from WP-Wedge's heavy-chain AST test, which only watches five
package names — that one stays untouched). Fix the four known sites (§2.3).
For third-party conditional imports: pre-EXERCISE the call paths tools use
(one real chroma count/get cycle against the opened collections — a query
needs a dummy embedding vector since no generator exists yet, so prefer
count/get) before `handshake_yield`, budgeted at <200ms total, and state in
the PR body which paths were exercised and which residual first-use paths
remain unproven. (This does not violate the HEISENBUG GUARD: chroma init
already does pre-yield disk I/O; the guard as practiced constrains the
breadcrumb flush path, not startup work per se.)

### §W2-d — Watchdog margin
120s fired on a healthy 119.7s load (pid 44288; harmless via late recovery but
noisy, and it needlessly puts the degraded branch in play). Raise the default
to **300s** (observed healthy max 119.7s × 2.5) and make it env-overridable
per existing config conventions. Late-recovery semantics unchanged.

### §W2-f — Dispatch-stall self-forensics (added at the §W2-a negative)
As specified in WP2-AC2(i): a loop-side monitor (the existing watchdog task is
a natural home) that, when a dispatched tool call is in flight past the
threshold during the load window or degraded state, dumps all-thread stacks
to stderr once per process. This converts the next un-reproducible production
stall into a pinned stack in the client MCP logs. Detection mechanism
(in-flight registry around the dispatch seam vs. sampling) is implementer's
craft; the once-per-process bound and stderr-only rule are not.

### §W2-e — Breadcrumb fidelity
1. Flush after every stamp **made from the bg thread** (Incident B's file hid
   `import_probe_*`/`bg_model_load_start` that had demonstrably happened;
   mid-wedge files must be readable forensics). Bounded cost: startup-window
   stamps number ~a dozen per process. **Thread-context rule (safety-
   critical): inline flushes are bg-thread-context ONLY. Loop-side stamps
   (`watchdog_fired`, server.py:217) and dispatch/worker-side stamps stay
   stderr-only and ride the next bg flush — a disk flush on the event loop or
   inside a tool call mid-wedge is a new freeze/latency source (same class as
   WP-Wedge rev-1's PIPE defect).**
2. One `tool_served_degraded` stamp, first occurrence per process, when a tool
   call is served while `embedding_error`/`watchdog_fired` is set — so fleet
   logs distinguish "degraded but serving" from "hung". Never flushes inline
   (see rule above).

## 5. Acceptance criteria (cite as WP2-AC*; WP-Wedge's AC numbers are taken)

**Repro-fidelity note (binding on WP2-AC1/WP2-AC3):** WP-Wedge's existing
meta_path dispatch test passed on the very build that hung in production —
that harness (synthetic module name, `Event.wait` in `find_spec`, handlers
called directly via `mock_mcp`) models a per-name Python import-lock hold
over a dispatch path that isn't even executed. It is NOT sufficient evidence
for these ACs. Requirements:
1. **Real dispatch:** WP2-AC1 and WP2-AC3 must invoke tools through actual
   FastMCP dispatch (fastmcp's in-memory client/transport or equivalent that
   executes `call_sync_fn_in_threadpool` / the new seam) — NOT `mock_mcp`.
2. **Real names, isolated process:** the wedge simulation blocks inside
   import machinery (`create_module`/`exec_module` level) on a REAL
   not-yet-imported module that the exercised path actually reaches, driven
   from a background thread mirroring `_load_embeddings_and_sync`. Because
   evicting/re-importing a real C extension mid-suite destabilizes the shared
   pytest process (the predecessor's stated reason for going synthetic), this
   repro RUNS SUBPROCESS-ISOLATED (its own pytest process or a spawned
   python), keeping the main suite stable.
3. **Fails on unfixed main:** the repro must reproduce the mode-(a)
   signature (in-worker import-lock block of a dispatched tool) before the
   fix. **Mode (b)'s production signature — `Thread.start()` blocking at
   thread-attach — CANNOT be reproduced from pure Python (a Python-level
   import hook holds import locks, never the OS loader lock); do not burn
   time trying. Mode (b) is covered INDIRECTLY by WP2-AC3's zero-spawn
   invariant: if nothing on the loop ever calls `Thread.start()`, the
   unreproducible block has nothing to block.**

- **WP2-AC1 (the point of the WP):** with the faithful wedge simulation in
  flight, EVERY registered tool returns within 10s, in BOTH the loading and
  the post-watchdog-fired degraded states. Both states are required because
  production hung in the post-fire window; if §W2-a shows the states share one
  mechanism, say so in the test docstring rather than silently merging them.
  Issuance condition: serial, or at most pool-capacity concurrent calls
  (the ≥2×-capacity storm belongs to WP2-AC3, whose bound is completion +
  zero spawns, not wall clock); `cognition_dashboard` patched as in the
  existing dispatch test so the 10s bound can't fail for unrelated reasons.
- **WP2-AC2 (replaced after the §W2-a negative — see outcome note):** two
  parts. (i) **Stall self-forensics (§W2-f):** when any in-flight tool
  dispatch exceeds a threshold (default 30s, env-overridable) while the load
  window or degraded state is active, dump all-thread stacks via
  `sys._current_frames()`/`faulthandler` to STDERR, once per process
  (bounded). Stderr lands in the client MCP logs, so the next production
  mode-(a) occurrence delivers its own pinned blocking stack. Thread-context
  rule applies: stderr only — NEVER an inline breadcrumb-file flush from the
  loop or a worker. Tested in-suite with a deliberately-blocked tool.
  (ii) **Regression coverage for the import-lock class:** the §W2-a repro
  script is wired into the suite (subprocess-isolated, real dispatch, real
  module name). It passes on current main for the four cleared sites — its
  docstring must say it guards the plain-import-lock CLASS (it fails if
  someone adds a colliding function-body import later), and must NOT claim to
  reproduce Incident A.
- **WP2-AC3 (INV-1):** under a dispatch storm (≥ 2× pool capacity concurrent
  tool calls) during the wedge simulation, zero `Thread.start()` calls are
  executed from the event-loop thread (instrument `Thread.start` and record
  the calling thread — a bare thread census is contaminated by legitimate
  test-side threads), and all calls complete. Response WRITES during the
  storm are part of the assertion surface (the stdio-transport path, §3
  INV-1), to whatever extent the chosen harness exercises real writes.
- **WP2-AC4 (INV-2):** the function-body-import AST audit runs as a test and
  passes; the four known sites are gone; the heavy-chain guard from WP-Wedge
  still passes unmodified.
- **WP2-AC5:** watchdog does not fire on loads up to 2.5× the observed healthy
  max (i.e., ≤300s); timeout env-overridable; all existing watchdog/
  late-recovery tests pass unmodified apart from the constant.
- **WP2-AC6:** zero regression — full suite green (`uv run python -m pytest`),
  `uv run ruff check .` clean. No WP-Wedge test weakened; if §W2-b removes the
  warm-pool/heartbeat machinery, its tests are REPLACED by WP2-AC3 coverage
  with the replacement called out explicitly in the commit message.
- **WP2-AC7:** §W2-a forensic finding delivered as For-the-record (blocking
  stack, which import/lock, which mode).

## 6. Honesty bar for the PR body

State plainly: this WP bounds DISPATCH during a wedge; the embedding backend
itself remains unavailable for the wedge's full duration (24+ min observed),
degraded responses are the ceiling until the sidecar epic lands, and a fixed-
size dispatch pool bounds concurrency at N simultaneous in-flight calls — a
storm beyond N queues (bounded latency), it does not scale. No "fixed the
hang" language without these qualifiers.

## 7. Known-intentional — do not relitigate

- The subprocess probe STAYS (converts some wedge classes to throwaway cost;
  harmless otherwise) — but no logic may treat `import_probe_ok` as safety.
- Watchdog + late recovery + `_wedge_lock` atomicity rule stay as designed.
- Heavy chain stays lazy in the sanctioned site (`embeddings/generator.py`);
  WP-Wedge's static heavy-chain AST guard stays and must keep passing (§W2-c's
  pre-imports cover the TOOL surface, never torch/scipy/sentence_transformers/
  transformers/sklearn).
- DEVNULL-only subprocess rule (v0.12.1 pipe-drain class) for any subprocess.
- Error-dict pattern for degraded tools; HEISENBUG GUARD; `--no-sync` launch
  topology; fastmcp pin (if INV-1 requires a fastmcp version bump, HOLD and
  report — pin changes are a Vince decision). **The same HOLD applies to any
  monkeypatch of third-party internals (anyio, fastmcp, mcp — e.g., patching
  `WorkerThread.MAX_IDLE_TIME` or private worker machinery): version-fragile
  private-API reachware is a Vince decision, not implementer's craft. The
  async-wrapper + dedicated-`ThreadPoolExecutor` seam (§3) is first-party and
  needs no HOLD.**

## 8. Out of scope

- **Sidecar embedding process** — the durable fix; separate epic, priority
  high. Do not start it here.
- **WP-Server-Lifecycle** (orphan self-exit) — separate task; today's orphan
  evidence goes there.
- OS-level forensics of WHY Windows DLL loads wedge under load.
- Client-side (Claude Code) timeout behavior.

## 9. Standing constraints (unchanged from WP-Wedge)

- Worktree under `C:\Users\colto\Documents\Projects\Worktrees\vibe-cognition`,
  branch `wp-wedge2`; never touch the main tree.
- NOBODY commits `.cognition/journal.jsonl` on a WP branch (manager flushes on
  main).
- Do not rely on vibe-cognition MCP tools for durable writes mid-WP; hand
  durable facts to Vince as For-the-record items. (Given live wedge risk,
  prefer NOT calling cognition tools at all mid-WP.)
- Do not kill any running server process — live specimens are evidence and
  other sessions' infrastructure.
- Tests via `uv run python -m pytest` (never bare pytest); ruff clean before
  reporting done.
- Report done at an exact SHA; merge-pin voiding clause cuts both ways (you
  HOLD if you find a reason the pin shouldn't merge).

## 10. Gate (Vince's side, stated so the ACs are honest)

At the reported SHA, in an isolated worktree with import provenance: full
suite + ruff; strong-form tautology check (targeted reversion so WP2 tests
fail on assertions, not collection); independent rerun of the §W2-a repro
script and the WP2-AC2 stall-forensics test; adversarial subagent review of
the diff. Any commit after the pinned SHA voids approval.
