# WP-Lifecycle (P1): orphan servers must die with their parent

Status: rev 2 — Vince, 2026-07-06. Rev 1 review FAIL (1 BLOCKER / 2 MAJOR /
2 MINOR: uv-intermediary topology, OpenProcess semantics, loop-independent
EOF detection, os._exit guarantees, harness pin) folded in.
Implementer: Vorpid, AFTER WP-Wedge-2 gates. Branch: `wp-lifecycle` in a
worktree at `C:\Users\colto\Documents\Projects\Worktrees\vibe-cognition`.
Graph task: a54b0191e362 (raised to high).

## 1. Why this is now P1, not hygiene

2026-07-06 evidence: orphaned server processes are not just leaked RAM — they
are the AMPLIFIER of the import-wedge P0. Each orphan sits mid-torch-import
hammering the disk; that sustained I/O pressure is what wedges the NEXT
server's import. Observed: 7+ orphan pairs before the reboot (some 2.5-2.9GB);
after a clean reboot AND a plugin fix (v0.15.4), a single session re-
accumulated 4 wedged orphan pairs within 50 minutes purely through
hang→reconnect churn — a self-sustaining spiral. Two reap-failure paths seen
in client logs:
- "Terminating MCP server process tree" logged, yet the pair survived
  (observed at the 10:53 pair; earlier at the 9:04/9:08 pairs which outlived
  their sessions by an hour).
- Connection drops (user cancel / config-swap "marking stale") where the
  client abandons the pipe without killing the tree at all.
Clean instance close DOES currently reap (verified post-restart: zero
survivors) — the leaks come from the mid-session churn paths, which are
exactly the paths a wedged server produces.

## 2. Direction — belt and suspenders, both server-side

The client's reap cannot be trusted (it's not our code and demonstrably
misses). The server must guarantee its own exit two independent ways:

1. **Ancestor-death watch (primary; works even mid-wedge).** CRITICAL
   topology fact (rev-1 BLOCKER): `plugin.json` launches `uv run ... python
   -m vibe_cognition.server` — on Windows there is no exec, so the server's
   direct parent is **uv, which waits on the server and never dies first**.
   Watching the direct parent deadlocks the pair forever (uv waits on
   python, python waits on uv) — this circular wait IS why orphans come in
   pairs. Therefore: from the server, walk up via
   `NtQueryInformationProcess(ProcessBasicInformation)` — own parent (uv),
   then uv's parent (the client) — open handles on BOTH, and in a daemon
   thread `WaitForMultipleObjects` on both; either dying → `os._exit(0)`.
   The ancestor-walk depth is a parameter (direct-parent mode must remain
   available: WP-Sidecar reuses this with parent=server, no intermediary).
   OpenProcess specifics (all requirements, not suggestions):
   - Rights: `SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION` only.
   - NULL return because the pid is already gone → the ancestor is dead;
     exit NOW. ACCESS_DENIED on a live process → fall back to slow polling
     of pid liveness; do NOT exit and do NOT crash the watch.
   - PID-reuse guard: validate via `GetProcessTimes` that the opened
     process's creation time is EARLIER than our own start time (a reused
     pid is necessarily younger than us); validation logic unit-tested even
     though the race itself is hard to drive.
   `os._exit` is deliberate: a wedged bg import holds locks that make any
   graceful path (joins, atexit, lifespan cleanup) unreliable; a daemon
   thread + `os._exit` needs none of them. The watch thread is started BEFORE
   the bg import thread (so its OS thread exists before any loader-lock wedge
   can block thread creation — same reasoning as WP-Wedge's pre-yield warm
   spawn).
2. **Pipe-closure exit (secondary; loop-independent by requirement).** The
   MCP-conventional stdin-EOF path rides the event loop (the `to_thread`
   readline's `""` must be PROCESSED on the loop before shutdown starts) —
   on a mode-(b) frozen loop it never fires, and the frozen state is exactly
   what the leaking servers are in. Requirement: a dedicated daemon thread
   polls the stdin handle via `PeekNamedPipe` (detects ERROR_BROKEN_PIPE on
   client close without consuming data), armed pre-yield alongside the
   ancestor watch; on broken pipe → give graceful shutdown a 5s grace →
   `os._exit(0)`. Coordinate with WP-Wedge-2's transport work — do not
   double-build; if INV-1 changes how stdin is read, hook the same place,
   but the loop-independence property is non-negotiable.

Windows-first (the fleet is Windows); POSIX degrades gracefully (parent-death
watch via polling `os.getppid() != original` at a slow interval is acceptable
there — do not over-engineer).

## 3. Scope

- §L-a: ancestor-death watch as above, breadcrumbed (`parent_watch_armed`,
  `parent_death_exit` — the latter is a stderr line; it will rarely reach the
  breadcrumb file, and that's fine, `os._exit` is the point).
- §L-b: loop-independent pipe-closure watch with the 5s grace-then-exit.
- §L-c: startup ordering guarantee (watch thread armed pre-yield, before the
  bg import thread starts), asserted by a test.
- §L-d: breadcrumb-retention sweep already exists (v0.15.3); verify it still
  bounds the directory with the higher file churn this incident produced, and
  bump the cap if needed. No new retention machinery.

## 4. Acceptance criteria (WPL-AC*)

- **WPL-AC1:** spawn the server FROM A REAL `uv run` INTERMEDIARY, exactly
  as plugin.json does (disposable-parent → uv → python topology; spawning
  python directly makes the test vacuous-by-topology — rev-1 BLOCKER); kill
  the disposable ancestor; the server exits within 5s. (Integration test,
  subprocess-isolated, Windows-real — no mocks of the Win32 wait.)
- **WPL-AC2:** same topology, but with the server's bg thread wedged via the
  import-block harness (pin the exact fixture/file name from the landed
  WP-Wedge-2 tree at dispatch time — it may have been renamed): ancestor
  dies → server STILL exits within 5s (this is the entire point; a
  graceful-only implementation fails here).
- **WPL-AC3:** close the server's stdin without killing the ancestor: exit
  within the 5s grace bound, graceful path preferred, `os._exit` fallback
  taken if graceful stalls — verified with the loop deliberately busied so
  the loop-riding EOF path cannot be what passed the test.
- **WPL-AC4:** both watch threads demonstrably armed before the bg import
  thread starts (ordering assertion; a wedge cannot prevent the watchers'
  existence). PID-reuse creation-time validation unit-tested.
- **WPL-AC5:** zero regression — full suite green, ruff clean; no change to
  handshake latency (pre-yield additions are microseconds-cheap: a handle
  open and a thread start).

## 5. Known-intentional / constraints

- No new runtime dependency (ctypes over pywin32).
- `os._exit(0)` on the death paths is intentional — do not "improve" it into
  graceful shutdown; graceful is what wedges. Exit code 0: the parent is
  gone; nobody meaningful reads the code, and non-zero codes trigger crash
  telemetry noise.
- Journal/chroma safety on hard exit — the real guarantees (a hard exit can
  land mid-`cognition_record`, not just mid-idle): (1) journal replay SKIPS
  torn/malformed trailing lines with a warning (storage.py:1033-1038), so a
  torn write costs one entry, never startup; (2) an in-flight chroma write is
  a sqlite transaction and rolls back; (3) a committed-journal-but-missing-
  vector state is self-healed by the additive startup backfill sync. Add (or
  cite, if present) a torn-trailing-line replay test. Do not add flush
  choreography to the death path.
- All WP-Wedge / WP-Wedge-2 standing constraints apply (worktree, no journal
  commits, DEVNULL subprocess rule, exact-SHA report, voiding clause,
  `uv run python -m pytest`).

## 6. Out of scope

- The sidecar epic (separate brief).
- Client-side reap fixes (not our code; evidence filed in the graph).
- Cross-machine/cross-user process management.
