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
- **E-1:** Disable ChromaDB anonymized (PostHog) telemetry. Defense-in-depth:
  inert at our pinned chromadb 1.5.5 (no-op stub), but chromadb 0.5–0.6.x —
  permitted by our `>=0.5.0` floor — actively phoned home gated on this flag.

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

WP-2 — CI + slim install.

### Added
- GitHub Actions CI (`.github/workflows/ci.yml`): runs ruff, pyright, and pytest
  on every PR and push to `main`. pyright uses a baseline-count ratchet that
  fails on new type errors and tightens as the count drops.

### Changed
- **Smaller install:** torch now resolves from PyTorch's CPU wheel index,
  removing the multi-gigabyte CUDA stack (18 GPU-only packages) from installs —
  a large first-install size reduction for Linux users, who previously pulled
  the full nvidia/CUDA toolchain this CPU-inference tool never uses (audit B-4).
  - Technical note: torch is declared a direct dependency pinned exactly to
    `==2.11.0` (uv ignores index sources for transitive deps; the exact pin
    guarantees zero drift at adoption). A future `sentence-transformers` bump
    requiring newer torch will hard-conflict at re-lock by design — fail loud,
    decide deliberately, loosen only when forced.
- `.cognition/journal.jsonl` marked `-text` in `.gitattributes` so git stores
  it verbatim (byte-determinism for the journal's byte-offset replay; C-3 defense).

WP-3 — post-commit hook + skill correctness.

### Fixed
- **H-1:** The post-commit hook no longer runs on a bare `python` (which fails
  silently where python isn't on PATH — macOS default, many Windows installs).
  It now runs through `uv run` via a `hooks/post-commit.sh` wrapper; uv is a
  guaranteed plugin dependency.
- Commit messages with non-ASCII characters are no longer mangled in the
  cognition journal (e.g. "§" → "Â§"): the hook now decodes git output as UTF-8
  instead of the system locale codepage.
- **B-3 (Windows):** the hooks' `CLAUDE_PLUGIN_DATA` fallback no longer mis-strips
  a backslash path, which had placed the venv back inside the version-pinned
  cache dir (where a `/plugin update` could lock/wipe it). Fixed across all three
  bash hooks.
- **S-1:** The `vibe-curate` skill now references its subagent prompt files from
  the skill's own directory, so they resolve when the plugin is installed (they
  previously used repo-relative paths that only worked from a checkout).
