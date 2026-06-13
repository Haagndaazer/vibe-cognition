# WP-R2 Execution Plan — fix/wp-r-074 (v0.7.4 release commit)

Branch: `fix/wp-r-074` off main @ `92312b1` (pending Vince's align). Same shape as WP-R (v0.7.3), 4-file scope pre-applied. No code.

## Changes (one "Release v0.7.4" commit)
1. `pyproject.toml` — `version = "0.7.3"` → `"0.7.4"`.
2. `.claude-plugin/plugin.json` — `"version": "0.7.3"` → `"0.7.4"`.
3. `uv.lock` — `uv lock` so the vibe-cognition package version line tracks pyproject (else CI `uv sync --locked` fails). **Verify the lock hunk is EXACTLY that one version line** — abort/flag otherwise.
4. `CHANGELOG.md` — rename `## [Unreleased]` (holding the WP-5 detection fix + the WP-4 C-1/C-3/H-2 entries) to `## [0.7.4] — 2026-06-11`, add a fresh empty `## [Unreleased]` stub above.

## To verify at execution (drift since WP-R)
- Grep the repo for `0.7.3` — confirm the ONLY tracked occurrences are the two manifests + uv.lock's package version line (WP-R confirmed this for 0.7.2; re-confirm for 0.7.3). Nothing else to bump.
- `__version__` already deleted (WP-1) — no stale source version.
- CHANGELOG `[Unreleased]` currently contains the WP-5 + WP-4 entries; confirm structure before the cut. No link-reference footers (WP-R confirmed none).

## Acceptance
- Diff touches exactly: pyproject.toml, plugin.json, uv.lock, CHANGELOG.md (4 files).
- Version strings agree (0.7.4 in both manifests + uv.lock + the CHANGELOG header).
- `uv lock --check` clean → CI's `--locked` satisfied; CI green on the PR.
- No CLAUDE.md, no marketplace.json, no code. Journal off-branch.

## Date
Section header `2026-06-11` (today — the release commit lands today). Flagged to Vince (he typed 06-10).

## Post-merge
Vince queues the Loki pin (lands on Colton's go in the morning); WP-4 install-mechanics (hook routing, cross-platform lock) gate on Colton's machine test post-pin.
