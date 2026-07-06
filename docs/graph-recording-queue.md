# TRANSIENT: deferred graph-recording queue (Vince, 2026-07-06)

Delete this file (committed removal) once executed. Execute against a healthy
post-0.15.6 server (embeddings ready); author=Vince unless noted; then
journal-flush commit on main + /vibe-curate.

## Nodes (cognition_record)

1. incident/high: v0.15.3 shipped a mid-session tool-freeze — bg heavy-import
   (sentence_transformers/torch/scipy) wedges in native DLL load under load,
   holds Windows loader lock + import locks, dispatch hangs forever (client
   never times out); pre-0.15.3 the same wedge was a visible 30s
   connect-timeout at launch. refs: 581c7c3, 9ce361b.
2. incident/high: v0.15.4 post-fix wedges same day — probe ok 7.3s yet
   in-process load 1,463s (pid 35220, loop-alive mode: get_status hung 21min
   post-watchdog-fire, cancel answered instantly); pid 50760 loop-frozen mode
   py-spy'd mid-wedge: MainThread in Thread.start() from fastmcp dispatch, bg
   thread in create_module under scipy; pid 2604 same wedge, loop alive.
   refs: 61b52bb.
3. discovery: a subprocess import probe does NOT immunize the in-process
   import (separate process = separate loader state) — WP-Wedge §3a premise
   falsified in production. refs: 9ce361b, 61b52bb.
4. decision: WP-Wedge design — DEVNULL-only probe + 120s watchdog with late
   recovery + warm anyio workers. Rejected: reverting WP-C lazy import (loses
   the launch-stability win); building the sidecar immediately (too big for
   the P0 clock). refs: 9fb55c2, 9ce361b.
5. decision: WP-Wedge-2 — INV-1 spawn-free event loop (dedicated prewarmed
   ThreadPoolExecutor dispatch seam; stdio transport keeps warm-pool/
   heartbeat) + INV-2 import-free tool surface (function-body-import AST
   guard) + 300s watchdog + dispatch-stall self-forensics. §W2-a forensics
   NEGATIVE ratified: pure Python cannot reproduce either production mode
   (per-module import lock ≠ OS loader lock); WP2-AC2 replaced by the
   production stack-dump. refs: 916ca3a, 61b52bb.
6. decision: full-fix program ordering Wedge-2 → Lifecycle → Sidecar;
   Lifecycle promoted P1 because orphans amplify the wedge (disk pressure);
   Sidecar = the durable fix. refs: 8024ca2.
7. episode: WP-Wedge → v0.15.4 — RCA (py-spy pid 78496, breadcrumbs, client
   logs) → brief rev3 9fb55c2 (2 peer reviews; rev-1 PIPE-probe defect
   caught) → impl ffed2ed → 0f7a446 → d0f5902 (AC3 near-vacuous HOLD +
   comment fix rounds) → gate green 682 → merge 39ba4df → ship 9ce361b →
   marketplace pin f7089ba. refs: 9fb55c2, d0f5902, 39ba4df, 9ce361b.
8. episode: WP-Wedge-2 → v0.15.5 — briefs 413b373/916ca3a (2 reviews: rev-1
   FAIL 2-BLOCKER incl. AC-fidelity; rev-2 FAIL incl. the
   stdio-transport-rides-anyio-pool BLOCKER) → impl 9274a4d → gate (708
   green + chromadb failure proven pre-existing at base; tautology drives;
   adversarial PASS w/ 1 MAJOR ordering-pin gap) → fix 1fd8a49 → merge
   680bf88 → ship 61b52bb → pin d72468a. refs: 413b373, 916ca3a, 1fd8a49,
   680bf88, 61b52bb.
9. episode: WP-Lifecycle → v0.15.6 — brief revs (rev-1 FAIL: uv-intermediary
   BLOCKER — a direct-parent watch would deadlock the pair, THE reason
   orphans come in pairs) → impl 3a6f82f → gate (737 green; adversarial PASS
   w/ WAIT_FAILED MAJOR found independently by manager + reviewer) → fix
   ba1e4dd (740 green; failure-drive: stripped fix → regression test hangs)
   → merge 1789233 → ship 419a616 → pin cbf36a1. refs: 3a6f82f, ba1e4dd,
   1789233, 419a616.
10. discovery (author Vorpid): Python default args bind at def time —
    monkeypatching module constants after def has no effect on already-bound
    defaults; parameterize signatures for testability. (WP-Wedge FTR.)
11. fail (author Vorpid): WP-Wedge AC1 first cut — monkeypatched probe whose
    fake re-imported the patched name → infinite recursion; fix: the fake
    returns directly. (FTR.)
12. discovery (author Vorpid): fastmcp Context.request_context rides a
    contextvar that anyio's to_thread propagates but bare
    ThreadPoolExecutor.submit does NOT — the dispatch seam needs
    contextvars.copy_context().run(); only caught by real-dispatch testing,
    mock_mcp can never see it. (WP-Wedge-2 FTR.)
13. discovery (author Vorpid): WaitForMultipleObjects rejects
    GetCurrentProcess() pseudo-handles (ERROR_INVALID_HANDLE); also uv's
    .venv Scripts\python.exe is a trampoline binary that adds an unplanned
    process tier when invoked directly. (WP-Lifecycle FTR.)
14. fail (author Vorpid): several WP-Lifecycle test doubles "passed" BECAUSE
    of the WAIT_FAILED silent busy-loop bug — pseudo-handle waits never fired
    exit_fn; fixed to real OpenProcess handles when the bug was fixed. (FTR.)
15. pattern: mock-based tool tests structurally cannot catch dispatch-layer
    defects (threading/contextvars/schema bypassed) — real in-memory fastmcp
    Client dispatch is mandatory for dispatch-adjacent ACs; twice-proven
    (contextvars bug; WP-Wedge AC3 passing on the very build that hung
    production).
16. discovery: sessions stay pinned to stale pre-update wedged servers until
    a manual /mcp reconnect; the client's "Terminating MCP server process
    tree" sometimes fails to reap (3 same-morning hangs all traced to
    pre-venv-sync servers; venv synced 09:08:36, last old spawn 09:08:33);
    clean instance close DOES reap.
17. discovery: concurrent model loads are the wedge trigger — 27s healthy
    load vs 1,463s under stampede; orphan churn is self-sustaining (each
    wedged orphan adds the disk pressure that wedges the next load).

## Task ops

a. cognition_update_task a54b0191e362 (WP-Server-Lifecycle) → done, note
   refs 419a616 + evidence above.
b. new task (parent 77a17aeb314e, normal): "Shared cross-session embedding
   daemon (one torch heap per machine)" — follow-up epic per
   wp-sidecar-plan §S-e; per-server sidecars still cost a heap per session
   and time-to-ready under fleet load is unbounded; protocol kept
   transport-agnostic for this.
c. new task (parent 77a17aeb314e, low): "dashboard run_in_threadpool spawn
   risk on its own uvicorn loop" — residual outside INV-1 scope
   (dashboard/api.py:259), opt-in feature, separate loop.
d. append to task e09d4f4a9a23 (chromadb flake): fails as AttributeError
   "'RustBindingsAPI' object has no attribute 'bindings'" under concurrent
   PersistentClient opens — evades _retry_chromadb_open (InternalError-only);
   LOAD-dependent (3/3 fail under stampede, passes on a calm box); surfaced
   in a second test (test_e3_doc_prefix reconciler-parity) same signature.
e. append to task 76eb74437d89 (CI guards): pyproject floats fastmcp>=2.0.0
   unbounded while the WP-Wedge-2 dispatch seam depends on 3.1.1 behavior
   (call_sync_fn_in_threadpool routing) — a silent uv sync drift invalidates
   the WP premise; add an upper bound or pin discipline.
f. new task (normal): "glb-splitter-c: MSVC C1128 Debug fix via
   $<$<CXX_COMPILER_ID:MSVC>:/bigobj>, coordinate with Gregovich" —
   provenance: subordinate-reported by Gina via Gabrielle; pre-existing;
   belongs to the glb-splitter-c project (recorded here as a relay note).

## Then

Journal-flush commit on main; /vibe-curate; soak report to Loki after a real
fleet session on 0.15.5+ (tool_served_degraded stamps, DISPATCH STALL dumps,
wedge frequency); delete this file.
