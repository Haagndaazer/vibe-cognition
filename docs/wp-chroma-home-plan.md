# WP-Chroma-Home — ChromaDB moves out of the repo into the plugin data dir

**Status:** scoping approved by Colton (2026-08-10), peer-reviewed (sonnet, two passes:
full scoping + post-decision delta). No implementation yet.
**Decisions (Colton):** fresh re-embed migration (no vector carry-over); plugin-data
location is the DEFAULT, not env-gated; migration note rides the WP-WhatsNew-1
what's-new notice.

## Goal

The ChromaDB vector store is machine-local and fully regenerable from
`.cognition/journal.jsonl` (server startup sync re-embeds missing nodes). Today it
lives at `<repo>/.cognition/chromadb/`, kept out of git only by the auto-written
`.cognition/.gitignore` (git_hygiene). Move it out of the repo entirely so it can
never be source-controlled, never blocks repo operations (Windows file handles),
and never churns cloud sync (repos under OneDrive-managed `Documents`).

## Location: `${CLAUDE_PLUGIN_DATA}/chromadb/<project-key>/`

NOT the Claude Code transcript dir (`~/.claude/projects/<munged-path>/`) that was
the original suggestion: writing there requires re-implementing Claude Code's
undocumented path-munging exactly, and squats in a namespace Claude Code owns and
may prune. `CLAUDE_PLUGIN_DATA` (`~/.claude/plugins/data/vibe-cognition-<marketplace>/`)
is already the plugin's persistent data channel (holds `.venv`, `update-check.json`,
`whats-new-seen`), survives plugin updates, and has working `${CLAUDE_PLUGIN_DATA}`
substitution precedent in `plugin.json` (`UV_PROJECT_ENVIRONMENT`).

**Project key:** `<repo-dir-name>-<sha256(os.path.normcase(str(resolved_repo_path)))[:12]>`
— readable slug + stable hash. `normcase` handles Windows case-insensitivity.
Sanitize the slug (or drop it, hash-only) if the dir name has filesystem-hostile
characters (trailing dot/space, reserved chars). Same repo at two paths → two DBs:
accepted (regenerable, ~7 MB each). Sibling keyed dirs under one parent share no
ChromaDB locks/state (verified) — no multi-project collision.
**HARD REQUIREMENT:** one shared key-derivation helper used by EVERY entry point
(server lifespan, dashboard CLI, `cognition_load_project`) — plugin-launch and
dev-CLI convergence to the same dir depends on identical derivation, not just the
same base path.

## Resolution order (config.py `cognition_chromadb_path`)

1. `VIBE_CHROMADB_DIR` explicit override (tests, power users).
2. `VIBE_DATA_DIR` (new `plugin.json` env entry, `${CLAUDE_PLUGIN_DATA}`, explicit
   like the `REPO_PATH` precedent) — the normal plugin-launch path. The explicit
   plugin.json entry is REQUIRED for correctness: only `${CLAUDE_PLUGIN_DATA}`
   *substitution* in plugin.json is a proven mechanism (UV_PROJECT_ENVIRONMENT);
   there is no evidence Claude Code auto-injects a bare `CLAUDE_PLUGIN_DATA` env
   var into spawned MCP servers. Checking the bare var too is a harmless defensive
   second path, never the primary one. `Settings`' `env_ignore_empty` already
   treats `""` as absent; any future non-Settings direct reader must use the
   guarded pattern (`resolve_repo_path_env` precedent), never a bare
   `os.environ.get`.
3. Best-effort discovery for env-less launches (standalone dashboard CLI, dev):
   glob `~/.claude/plugins/data/vibe-cognition-*`; exactly one match → use it, so
   plugin and dev-CLI launches of the same project resolve to the SAME keyed dir.
   Zero or 2+ matches → fall through.
4. Last resort: legacy `<repo>/.cognition/chromadb` (CI, tests without override,
   machines with no plugin install).

## Migration: always fresh (Colton — "vector safety")

- No copy/move of legacy data, ever. New keyed dir starts empty; existing startup
  sync re-embeds everything from the journal (`_sync_cognition_embeddings` diffs
  journal node IDs against the collection — an empty collection reads every node
  as missing; no marker file involved, verified). One-time cost per project:
  sidecar model load + full re-embed — node count varies per project (THIS repo is
  already ~600 live nodes; do not hardcode an estimate in user-facing copy).
- No new concurrency machinery: existing `_retry_chromadb_open` / collection
  locking already covers concurrent first-runs (verified — the earlier draft's
  migration lockfile is moot under always-fresh).
- Legacy `<repo>/.cognition/chromadb` is left in place untouched — shipped code
  never deletes it (shipped-code safety invariant). The what's-new notice tells
  users it is safe to delete manually.
- This removes the copy-verify-delete complexity and the migration-concurrency
  lockfile from the earlier draft entirely. It also removes the need to verify
  ChromaDB tolerates a moved persist dir.
- Note: rule 4 means a repo whose env-less launches used the legacy dir keeps
  WRITING to it on those launches until the plugin env/discovery path is available
  — acceptable; both stores are independently regenerable.

## Affected code (complete consumer list, verified twice)

- `config.py:391` `cognition_chromadb_path` — the resolution order above.
- `server.py:571` lifespan open — no change beyond logging the resolved path.
- `dashboard/cli.py:32` standalone dev CLI — inherits new resolution; add
  `--data-dir` flag for explicitness; docs note in the vibe-dashboard skill.
- `tools/cognition_tools.py:2384` — `cognition_load_project` HARDCODES
  `resolved/.cognition/chromadb` for foreign project B. Compute B's keyed path
  with the same helper; read-only fallback (`open_existing`) to B's legacy in-repo
  dir if the keyed dir is absent (B not yet run since upgrade).
- `git_hygiene.py` — keep writing the `chromadb/` ignore line indefinitely
  (teammates on older plugin versions in shared repos; pre-migration windows).
- `whats_new.py` changelog notice — static text: new location, old dir safe to
  delete. stdlib-only, no dependency on the resolved path. The notice is
  per-MACHINE with no visibility into which repos have a stale dir — copy must
  stay generic ("your projects' in-repo `.cognition/chromadb` dirs are now safe
  to delete"), never name specific paths.
- `get_status` — surface the resolved chromadb path ("where did my vectors go" is
  the first debugging question post-change).
- Tests: `tests/test_config.py:149` pins the old path unconditionally — becomes a
  test of the resolution order (the SOLE existing break; every other test file
  constructs `ChromaDBStorage(persist_directory=...)` directly and bypasses
  config.py — verified across ~15 files). `tests/conftest.py:104` hardcodes tmp
  paths (unaffected by construction). NEW pin required: conftest must set
  `VIBE_CHROMADB_DIR` (or monkeypatch `Path.home`) globally — the discovery glob
  hits a REAL `~/.claude/plugins/data/vibe-cognition-*` dir on dev machines
  (confirmed live on this one). NEW coverage required: tests for all four
  resolution branches, not just a fix to the one legacy-path test.
- `readme.py:211` + README/docs wording that says chroma lives under `.cognition/`.

## Risks / accepted trade-offs

- **Plugin-data blast radius:** all projects' vector stores now live under one
  root; a plugin-data wipe takes out every cache at once (vs one repo's today).
  Harmless — regenerable — but a real behavior-domain change, documented here.
- **Orphans:** moved/deleted repos leave small keyed dirs behind. Future GC could
  list/prune via get_status; out of scope now.
- **Key divergence:** symlink/subst path spellings produce different keys.
  Accepted; regenerable.
- **Glob ambiguity (rule 3):** 2+ `vibe-cognition-*` marketplaces → silent fall
  through to legacy. Rare; get_status path surfacing makes it diagnosable.

## Out of scope

- `journal.jsonl` and `documents/` stay in-repo by design (git-shared team state).
- Legacy-dir GC / auto-cleanup.

## Release

User-facing → version bump in `pyproject.toml` + `.claude-plugin/plugin.json`,
CHANGELOG entry (feeds the what's-new notice), ping Loki with the code-commit SHA
per the release procedure.
