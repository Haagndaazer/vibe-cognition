v0.7.3 release commit — version bump + CHANGELOG cut. No code.

## Changes (4 files)
- pyproject.toml, .claude-plugin/plugin.json: version 0.7.2 -> 0.7.3
- uv.lock: vibe-cognition package version line only (re-locked so `uv sync --locked` stays green — verified the lock diff is exactly that one line)
- CHANGELOG.md: cut `[Unreleased]` into `[0.7.3] — 2026-06-10`, fresh empty `[Unreleased]` stub above

## Acceptance
- Diff touches exactly those 4 files; all version strings match (0.7.3 in all three manifests + the CHANGELOG header).
- CI green (this PR's run).

Covers WP-1 (cleanup/telemetry/LICENSE), WP-2 (CI + CPU-torch install shrink), WP-3 (hook H-1/utf-8/B-3, S-1).

Note (not actioned, per scope): the [0.7.3] section keeps the WP-1/2/3 sub-groupings, so it has repeated ### category headings — cosmetically non-canonical Keep-a-Changelog; left as-is since the task was "cut, nothing else."

Post-merge: Loki pin (SHA + version); human release-test ask for the H-1/S-1/B-3 install mechanics (b8ec24fe9107).
