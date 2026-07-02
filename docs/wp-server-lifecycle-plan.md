# WP-Server-Lifecycle — orphan self-exit (PLAN)

> Status: **DRAFT plan, peer-reviewed + revised twice. NOT dispatched, NOT implemented.**
> Target: ~v0.13.0 (independent of the v0.12.1 git-identity hotfix).
> Owner TBD. Author: Vince (manager). Created during the 2026-06-25 P0 firefight.

---

## 0. HARD CONSTRAINTS (overriding — set by Colton)

These outrank minimalism, elegance, and any cleanup ambition. A mechanism that violates one is rejected outright.

- **C1 — Preserve current functionality / paradigm EXACTLY.** Anything that could alter a working
  path, change how the server starts, or affect a **live** server is a **massive NO**. Zero behavior
  change to anything that works today.
- **C2 — Concurrent same-repo sessions are FIRST-CLASS valid.** Multiple Claude Code sessions on the
  same project, each with its own live server on the same `.cognition` store, is a **supported**
  scenario — not a degradation. No single-instance gate, no refusing a second server, **never kill a
  live server.**
- **C3 — Safe failure direction.** The only acceptable failure mode is **leaving an orphan** (exactly
  today's status quo). **Disrupting a live session is never acceptable.** Every mechanism is judged
  first by: "what's its worst case, and can it ever harm a live server?" If yes → rejected.

These constraints **kill** the original framing (single-instance lock, kill-the-old, cross-process
reaper). What survives is the one mechanism that is self-regarding and additive: **self-exit.**

---

## 1. Motivation / evidence

Observed on Colton's Windows machine during the v0.12.0 firefight:

- **~11 orphaned `vibe_cognition.server` processes** spanning 3 days (+~24 teammate-comms),
  ~46 python procs total; several holding **0.5–1 GB** RAM (embedding model loaded).
- Claude Code spawns a fresh server per session; **old ones are never reaped on Windows.**

Harms: unbounded resource leak (RAM/handles/processes); update-time corruption risk (running servers
hold venv DLLs during `/plugin update` — ledger 19, cf. v0.7.3 incident).

**Additional angle (fable-audit, bab6c5431a62):** an orphan that had launched the local
dashboard (`cognition_dashboard`) doesn't just leak RAM/handles — it keeps a LIVE,
token-gated, DELETE-capable HTTP listener on `127.0.0.1` for the orphan's entire
lifetime (`dashboard/server.py`, a daemon thread with no liveness tie-in to its parent
session). Since orphans have been observed persisting up to 3 days, this is a live
attack surface window (bound to localhost and token-protected, so the practical risk is
low, but it is a listener that can delete graph nodes, sitting unsupervised) — not just
a resource leak. Any self-exit mechanism designed here should close this listener along
with the process, not just free memory.

> NOTE: the `add_task` hang in this firefight was the **git subprocess** bug (fixed v0.12.1, PR #33),
> **not** this. The pileup is a separate reliability bug the firefight exposed.

---

## 2. The mechanism: SELF-EXIT on own-parent death (the ONLY survivor)

An orphan is, by definition, a server whose launching Claude Code is **gone**. A live server has a
**live** parent. So the discriminator is entirely **self-regarding**: *"is the process that launched
ME still alive?"* Each server answers that about **itself only** and exits if its own parent is gone.

Why this is the only design that satisfies §0:
- **C2 ✓** — a server only ever acts on itself based on its OWN parent; a concurrent live session
  (with its own live parent) is structurally untouchable. No cross-process decisions, no enumeration
  of "who else is running," no killing anyone.
- **C3 ✓** — failure direction is provably safe: a recycled/ambiguous parent PID can only make a
  **dead** parent look **alive** → the server **doesn't** exit → an orphan lingers (status quo). It
  can **never** make a live parent look dead, because a live parent's PID *is* alive. So self-exit
  **cannot** disrupt a live session — its worst case is "failed to clean up," never "killed a live one."
- **C1 ✓** — additive: a self-exit backstop touches no tool, no store, no journal, no startup path.
  The only observable change is that an *orphan* now exits. Every working path is byte-identical.

---

## 3. Phase 0 — INVESTIGATION (read-only; gates the mechanism's IMPLEMENTATION, not the strategy)

The strategy (self-exit) is fixed by §0. Phase 0 only determines the cleanest **additive** way to
implement it, and whether an even-more-additive path exists. Findings folded from the sonnet review
(verified against the installed MCP/fastmcp SDK):

- **Q0.1 (answered):** the MCP SDK `stdio_server()` already loops `async for line in stdin:` → exits
  on stdin-EOF. So the server *should* already self-exit when its stdin pipe closes.
- **Q0.2 — why doesn't EOF reach it on Windows?** Candidates: (a) Claude Code's Job-Object kill path
  needs `pywin32`; absent → it only kills `uv run`, not the python grandchild. (b) `uv run` gives
  python a **detached/non-pipe stdin**, so closing `uv run`'s stdin never delivers EOF to python.
  Inspect the live PPID chain + whether python's stdin is the pipe from `uv run`.
- **Q0.3 — is the launch path relevant?** If (b), launching the venv python **directly** would fix
  EOF delivery for free. **BUT under C1 this is paradigm-risk** (changes how the server starts; env
  substitution + update-safety surface) → treat as a *last resort*, only if proven byte-equivalent in
  behavior, and even then likely **deferred** in favor of the additive in-process backstop below.

**Deliverable:** a short note stating the root cause (a / b / both) and confirming the in-process
backstop is sufficient — so we do NOT need to touch the launch path.

---

## 4. Phase 1 — the additive self-exit backstop (the fix)

Default mechanism (most paradigm-preserving): an **in-process daemon thread** that, on an interval,
checks whether the original launching parent is still alive and, if not, logs and exits the process.
- Self-regarding; no store/journal/tool/startup interaction; pure addition.
- Implementation detail decided by Phase 0 (parent-PID liveness poll; possibly augmented by a stdin
  reader IF and only if it cannot steal bytes from the protocol's stdin loop — likely it can't, so
  parent-PID poll is the safe default).
- **Tunable, conservative defaults** so it never fires early: only after the parent has been gone for
  a confirmed interval. Erring toward "exit a little late" (orphan lingers briefly) over "exit early"
  (C3).

Explicitly NOT in scope (rejected by §0):
- ✗ Single-instance lockfile / startup gate (would gate/refuse a valid concurrent server — C2).
- ✗ Cross-process reaper that kills *other* servers (could hit a live one; PID-recycle risk — C3).
- ✗ Launch-method change as the primary fix (paradigm-risk — C1); only a deferred last resort.

---

## 5. Phase 2 — pre-existing orphans: ONE-TIME MANUAL cleanup only

The ~11 existing orphans are a cleanup chore, not a design driver, and an **auto-reaper is rejected**
(C3: it would have to judge *other* processes live-or-dead, the exact thing that risks a live session).
Ship a **documented manual command** — the surgical one used in this firefight:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'vibe_cognition\.server' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

(Once the Phase 1 backstop ships, new orphans self-clean and this is only needed once, for the backlog.)

---

## 6. Phase 3 — never-hang store writes: DESCOPED to a principle-audit (no behavior change)

- chromadb 1.5.5 exposes **no** SQLite busy_timeout / pragma hook, and with self-exit keeping the
  process count sane, multi-writer Chroma contention is rare. **Do not build a Chroma busy_timeout.**
- Audit tool-reachable store-write paths for unbounded blocking and **document** residuals (POSIX
  `flock` still blocks by design; the journal append is already Windows-bounded). Treat "a tool call
  must be bounded" as a standing review rule. **No change to any current write path** (C1).

---

## 7. Decisions

- **D1 — concurrent same-repo sessions: RESOLVED → VALID / first-class supported** (Colton). Drives §0 C2.
- **D2 — pre-existing-orphan auto-reaper: RESOLVED → NO** (violates C3). Manual command only (§5).
- **D3 — scope/split:** likely a single small WP (Phase 0 investigation → additive self-exit backstop
  + the manual-cleanup doc), with the principle-audit as a tiny follow-up. *Open for Colton.*

## 8. Risk register (under §0)

- **Self-exit fires while the session is live** — would violate C3. Mitigate: parent-liveness check
  whose only ambiguity is "dead-looks-alive" (safe), conservative confirm-interval, extensive unit
  tests asserting it never signals exit while the recorded parent PID is alive.
- **Launch-path change (deferred)** — if ever revisited, must be proven byte-equivalent in behavior
  and update-safety before any consideration (C1).
- **Suite can't reproduce detached-process orphaning** (pytest is console-attached, single process) —
  same lesson as the git bug. The suite proves the *liveness/exit LOGIC* with fake PIDs (alive →
  never exits; dead → exits after the interval; recycled → safe "dead-looks-alive"); the real
  behavior needs a **mandatory manual Windows check** (start N servers, restart Claude Code → the
  current session's server exits, a *second live session's* server is untouched).

## 9. Interactions to preserve (C1)

- **Journal lock / post-commit hook / manager worktree-flush** — self-exit touches only the calling
  process; it never touches the journal file, the lock, or the store. The destructive-op ban near the
  journal is unaffected. No reaping of other processes = nothing to disturb.

---

### Provenance
Peer-reviewed by an independent sonnet agent (decorrelated review, ledger 7) which verified claims
against the installed MCP/fastmcp SDK and overturned the original premises (kill-the-old, watchdog
pre-commitment, Chroma busy_timeout). Then re-scoped under Colton's two directives: concurrent
same-repo sessions are valid (C2), and current functionality/paradigm must not change (C1/C3). Result:
the design collapses to a single additive, self-regarding self-exit backstop + a one-time manual cleanup.
