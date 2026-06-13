Upgrade-resilience detection (ledger 19) for the v0.7.3 venv-corruption incident.

## Incident
Updating the plugin (0.7.3's torch PyPI→CPU-index swap) while other Claude Code sessions hold torch's DLLs (Windows) fails the uv-sync uninstall mid-way → half-installed torch → every new MCP server start dies on ImportError, surfaced only as a cryptic connection failure. It SELF-HEALS once all sessions close and one clean start finishes the swap. Per ledger 19, ship detection + a clear message, not a code fix.

## Change (hooks/session-start.sh, Step 2)
- Guard the sync: `uv sync … || true` — a failed swap no longer kills the hook under `set -e` (that death was the cryptic-failure root).
- Probe the heavy native deps (`import torch, chromadb`) ONLY after a sync (install/upgrade/broken-retry). Stamp is written only for a verified-importable venv, so a broken one re-warns every start until healed.
- On probe failure: a clear SessionStart message — close ALL sessions, start ONE, it self-heals.
- Steady-state happy path matches the stamp and skips the probe entirely (zero added cost). Post-sync probe ~7.5s, dwarfed by the sync.

## Seam check (ledger 18)
- ledger 17: the `if` reads python's own exit (no pipe; `2>/dev/null` is stderr-only).
- ledger 5: probe is read-only on the venv it checks (writes only the stamp file).
- hook still emits valid JSON on probe failure (capture-then-print idiom).
- new seam: stamp now means "synced AND importable" — enables self-healing retry; a broken start re-attempts the full sync (acceptable, transient).

## Verified (ledger 3 — under the failure condition)
- Probe catches a torch-ABSENT venv AND a faithful present-but-broken one (torch/__init__.py raising ImportError) → exit 1 → message branch. Throwaway temp venvs; the real venv is never mutated (ledger 5).
- Happy path: real venv → exit 0, stamp written. Message JSON validated.
- bash -n clean. No Python changed → ruff/pyright/pytest unaffected (CI confirms).

## Flag (not actioned)
CLAUDE.md's release procedure should gain a "close sessions before a dep-swap update" habit line — CLAUDE.md is off-limits without Colton's permission, so flagging, not editing.

Install-mechanics: the true upgrade-collision only reproduces on a real machine — gated on human release test (b8ec24fe9107).
