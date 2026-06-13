# WP-5 Execution Plan — fix/wp-5-venv-detect (upgrade-resilience detection)

Branch: `fix/wp-5-venv-detect` off main @ `ba10ba0` (pending Vince's align). Incident: v0.7.3 dep-swap (torch PyPI→CPU-index) fails mid-uninstall under running servers holding DLLs (Windows) → half-installed torch → every new server start dies on `ImportError torch.Tensor`. Self-heals on a clean start. Today's symptom is a *cryptic connection failure*; goal is a *clear, actionable message*.

## Root cause of the cryptic failure
`session-start.sh:54`: `uv sync ... 2>/dev/null` under `set -euo pipefail`, no guard. A failed swap → `set -e` kills the hook before any JSON → Claude Code sees a broken hook (the "cryptic connection failure"). (This is audit H-3's territory; WP-5's fix necessarily closes it.)

## (a) Probe + guard in session-start.sh
Replace the Step-2 sync block (53-57) with:
- Guard the sync: `uv sync ... 2>/dev/null || true` + `SYNCED=1` (don't let a failed sync kill the hook).
- **Step 2b — probe ONLY after a sync** (the brick only follows a dep swap; steady-state healthy starts skip this entirely → zero added happy-path cost):
  - `uv run --no-sync --project "$PLUGIN_ROOT" python -c "import torch, chromadb"` (the heavy native deps; torch is the one that bricked).
  - **import OK** → `mkdir -p VENV_DIR; echo "$HASH" > "$STAMP"` (stamp ONLY a verified-importable venv).
  - **import FAILS** → emit a clear SessionStart `additionalContext` ("a dependency update didn't finish… close ALL Claude Code sessions, start ONE, it self-heals") and `exit 0`. Do NOT write the stamp → broken venv re-syncs + re-warns every start until healed; do NOT proceed to migrate/prime (they'd fail cryptically too).
- Net behavior: healthy steady state (stamp matches) → no probe, no cost. Upgrade/install/broken → probe runs; broken → clear guidance instead of a dead hook.

## (b) CHANGELOG + release-procedure habit
- Under `## [0.7.3]`, add a **Known upgrade note**: upgrading 0.7.2→0.7.3 while other Claude Code sessions run can leave the shared venv half-swapped (torch); close all sessions and start one to self-heal. (User-facing.)
- Release-procedure habit ("dep-swap releases ship a 'close sessions before updating' line"): the procedure lives in CLAUDE.md, which is **off-limits without Colton's permission** → FLAG for Colton, do not edit. Note it in the report.

## (c) Seam check — ledger 18 (a fix-for-an-incident gets reviewed for which OTHER ledger rule it could itself violate)
WP-5 is the embodiment of **ledger 19** (an upgrade is also a write to the running copy; when a failure self-heals, ship detection + a clear message, not a code fix that adds its own seams). Seeding the seam check (ledger 18) against the rest of the ledger:
- **ledger 17 (green-detector must measure the real thing, not what survived a pipe):** the probe's gate is `if uv run --no-sync … python -c "import torch, chromadb" 2>/dev/null; then` — the `if` reads uv run's exit, which propagates python's exit. `2>/dev/null` redirects stderr only (no stdout pipe), so the exit code is the program's own, not a pipe's. SATISFIED — no `| head`/pipe masking.
- **Startup cost** (Vince candidate 1): MITIGATED — probe gated to post-sync only; the steady-state happy path never runs it. Measure the post-sync probe cost (import torch+chromadb via uv run) and report; it's paid only on upgrade/install, where a full sync already dominates.
- **Hook output must survive probe failure** (Vince candidate 2 / the `{}`-on-failure discipline): SATISFIED — `|| true` on sync, probe in an `if`, the broken branch emits valid JSON + `exit 0`. No `set -e` death (capture-then-print, like the rest of the script).
- **ledger 5 (the defense is also a write to the defended thing):** the probe is READ-ONLY on the venv (an import); it writes only the stamp file, never the venv it checks — so the detector can't corrupt what it detects. SATISFIED.
- **New seam — stamp semantics change** (stamp now means "synced AND importable", was "synced"): benefit = self-healing retry; risk = a transient false-negative import re-syncs+re-warns once more (idempotent, informational — acceptable).

## Acceptance
- Probe verified under its FAILURE condition (**ledger 3**): rename a torch dir in a THROWAWAY venv copy → probe must catch it and emit the clear message (not the cryptic death). Demonstrated, not asserted. Also run the happy-path (intact venv) → probe passes silently and the stamp is written.
- CI green (no Python changes; suite unaffected). Baselines hold (ruff/pyright/pytest).
- Install-mechanics → report ends "gated on human release test" (b8ec24fe9107) — the real upgrade-collision path only reproduces on a human machine.
- Journal off-branch; branch-switch via Vince; no version bump; no CLAUDE.md; no marketplace.json.

## Commits
1. `WP-5: detect a half-swapped venv on session start, guide the user to self-heal` — session-start.sh.
2. `WP-5: CHANGELOG known-upgrade note for 0.7.3` — CHANGELOG.md.
