v0.7.4 release commit — version bump + CHANGELOG cut. No code (4-file diff).

## Changes
- pyproject.toml + .claude-plugin/plugin.json: 0.7.3 -> 0.7.4
- uv.lock: vibe-cognition package version line only (re-locked; verified one-line hunk)
- CHANGELOG.md: cut [Unreleased] -> [0.7.4] — 2026-06-11, fresh empty [Unreleased] stub

## Covers (data-integrity fixes, shipped promptly per Vince; document feature is 0.8.0 later)
- WP-5: half-swapped venv detection + clear self-heal message
- WP-4: journal append atomicity (C-1), replacement/merge detection (C-3), post-commit hook routed through the shared locked helper (H-2)

## Acceptance
4-file diff exactly; versions agree (0.7.4 in both manifests + uv.lock + CHANGELOG header); uv lock --check clean; CI green.

Post-merge: Loki pin (Colton's go in the morning); WP-4 install-mechanics gate on Colton's machine test post-pin (b8ec24fe9107).
