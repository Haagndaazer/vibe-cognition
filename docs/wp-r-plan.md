# WP-R Execution Plan — fix/wp-r-073 (v0.7.3 release commit)

Branch: `fix/wp-r-073` off main @ `f67b0a2` (pending Vince's branch-switch align). Deliberately tiny: version bump + CHANGELOG cut. No code.

## Changes (one "Release v0.7.3" commit)
1. `pyproject.toml:3` — `version = "0.7.2"` → `"0.7.3"`.
2. `.claude-plugin/plugin.json:3` — `"version": "0.7.2"` → `"0.7.3"`.
3. `uv.lock:2682` — re-lock via `uv lock` so the vibe-cognition package version tracks pyproject (else CI `uv sync --locked` fails). **Verify the lock diff is EXACTLY that one version line** — abort/flag if `uv lock` changes anything else. [4-file scope pending Vince's confirm.]
4. `CHANGELOG.md` — rename `## [Unreleased]` (with the WP-1/2/3 content) to `## [0.7.3] — 2026-06-10`, and add a fresh empty `## [Unreleased]` stub above it.

## Verified
- Only tracked `0.7.2` strings: pyproject.toml:3, plugin.json:3, uv.lock:2682 (grep). Nothing else to bump.
- `__version__` already deleted in WP-1 — no stale source version.

## Acceptance
- CI green on the PR.
- Diff touches exactly: pyproject.toml, plugin.json, uv.lock, CHANGELOG.md (4 files — uv.lock is the required addition over Vince's "3").
- Version strings match each other AND the CHANGELOG header: 0.7.3 in all four.
- No CLAUDE.md, no marketplace.json, no code (per release procedure + standing standards).
- Anything else spotted → node + flag, not a commit.

## Post-merge (Vince/others)
- Vince pings Loki with SHA + version for the marketplace pin.
- Colton gets the human release-test ask (H-1/S-1/B-3 install mechanics gate on his machine — b8ec24fe9107).
