# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

WP-1 — Tier 1 mechanical cleanup (from the 2026-06-10 audit).

### Added
- `LICENSE` (MIT) — was declared in both manifests but no file existed (H-6).
- `CHANGELOG.md` — this file.
- `.gitattributes` rule `merge=union` for `.cognition/journal.jsonl` — correct
  merge semantics for an append-only, globally-unique-ID JSONL log (defense in
  depth; resolves textual conflicts only, not C-3 replay order).
- Regression test for ChromaDB telemetry being disabled.

### Fixed
- **E-1:** ChromaDB anonymized (PostHog) telemetry is now disabled — it was on
  by default and phoned home from every project on each server start.

### Changed
- Unified authorship to "Colton Dyck" across `pyproject.toml` and
  `plugin.json` (was "BlckLvls" / "ColtonDyck") (H-6/d).
- Documented why `einops` is a runtime dependency (nomic's `trust_remote_code`
  model code imports it) (H-6).
- Ruff baseline cleanup: fixed 20 of 23 findings (UP017×8, F401×3, I001,
  SIM102×2, SIM105×2, E741×4). Deferred: 2× UP042 (StrEnum changes `str()`
  semantics) and 1× UP017 in `hooks/post-commit.py` (runs on system Python,
  kept 3.10-compatible) (§8.1).
- Corrected stale comments in `server.py` (REPO_PATH source), the
  `session-start.sh` header (it removes, not configures, per-project MCP), and
  the `post-commit.py` docstring (actual `hooks/hooks.json` wiring) (T-10, H-6, H-2).

### Removed
- Dead direct dependency `httpx` (zero imports; satisfied transitively) (H-6).
- Duplicated `[project.optional-dependencies].dev` block — `[dependency-groups].dev`
  is the one `uv sync` reads (H-6).
- Stale `__version__ = "0.1.0"` from `vibe_cognition/__init__.py` (read by
  nothing; real version lives in the manifests) (T-10).
- `.ruff_cache/` from version control (added to `.gitignore`) (H-6).
