# WP-R3 Execution Plan — v0.8.0 release commit (document storage)

The release commit for the completed document-storage feature (D1a→D4, all merged; main at `6939c99`). **No code** — version bump + CHANGELOG only. Same shape as WP-R2.

## Scope — exactly 4 files (the WP-R2 lesson: CI runs `uv` with `--locked`, so uv.lock's version must move with the manifests or `--locked` fails)
1. `pyproject.toml` — `version = "0.7.4"` → `"0.8.0"` (line 3).
2. `.claude-plugin/plugin.json` — `"version": "0.7.4"` → `"0.8.0"` (line 3; the plugin system reads version from here).
3. `uv.lock` — the `vibe-cognition` package entry `version = "0.7.4"` → `"0.8.0"` (line ~2682). Hand-edit the single line (the local package has no content hash in the lock); then verify with `uv lock --check` (or `uv sync --frozen`) that the lock is consistent so CI's `--locked` passes.
4. `CHANGELOG.md` — the R2-style **cut-and-stub**, NOT a blind add: rename the current `## [Unreleased]` content to `## [0.8.0] — 2026-06-13`, and leave a fresh empty `## [Unreleased]` stub above it. **Reconcile against the real release tip:** check `[Unreleased]` AT `6939c99` (the release branches off it) — none of the D1a→D4 commits touched the CHANGELOG, so it's expected to be empty, in which case AUTHOR the `[0.8.0]` body from the feature prose below; if it somehow carries entries, those BECOME the `[0.8.0]` body (don't hand-retype/double-count). Verify against the actual file after the branch-switch align, not against this stale branch.

**Verified scope is complete:** a repo-wide scan for `0.7.4` shows only these manifests + the lock + the CHANGELOG's historical `[0.7.4]` section (kept) + historical docs/journal (untouched). No `__version__` in code; pyproject version is static (not dynamic). Nothing else references the version.

## CHANGELOG [0.8.0] content (the feature, operator-facing)
`### Added` — first-class **document storage**:
- Store documents (client docs, PDFs, specs) as first-class `document` nodes — **reference mode** (path + metadata + content sha256; bytes stay in place) by default, opt-in **copy mode** (`store_copy`, content-addressed blob; `local_only` keeps it out of git).
- **Agent-extracted text**, searchable — chunked into the embedding store; `cognition_search` returns documents with a matched excerpt.
- Descriptor nodes link to a document by citing its `doc:<hash>` in their references (auto `part_of`); the **`/vibe-document` skill** makes this the default workflow.
- `cognition_store_document` / `cognition_get_document` tools (freshness: `unchanged|modified|missing`).
- Dashboard: a **document list** + **token-gated, path-safe download**.
- Refcounted deletion reclaims the managed sidecar/blob/chunks (never the referenced original).

`### Fixed` — sub-bullet:
- **N1 ghost-search fix:** `cognition_search` (and the dashboard) no longer serve hits for nodes deleted on another machine (cross-process `remove_node` replay never un-embedded); a startup sweep reclaims orphan vectors.

(Privacy note worth including: a git-committed copy-mode blob persists in history after a node is deleted — deletion does not un-publish it.)

## Binding rules
No code → no pyright/test changes expected, but RUN the full suite + ruff + pyright after the bump to confirm nothing reads the version at import in a way that breaks, and that `uv --locked` is satisfied. Journal protocol unchanged. SHA-pinned merge gate.

## Pin gating (NOT tonight — feature release)
v0.8.0 is a FEATURE release; the marketplace pin is NOT covered by the delegated patch-pin authority (that was v0.7.4). We LAND v0.8.0 on `main` tonight (version-bump PR gated + merged). Vince QUEUES the Loki pin request flagged **"hold for Colton's morning go + the D-4 vendored-libs RENDER check + the owed v0.7.4 non-ASCII-commit journal test."** Main at 0.8.0 un-pinned publishes nothing; Colton pins after his two human-eyes-on checks.

## Verification gate
Edit 4 files → **confirm `git diff --stat` is EXACTLY those 4 files** (the whole bar for a no-code release — the WP-R2 acceptance gate) → full pytest + ruff + pyright (≤29) + `uv lock --check` → push → CI green 3 legs (esp. the `--locked` job) → ping Vince the tip SHA → SHA-pinned merge gate.
