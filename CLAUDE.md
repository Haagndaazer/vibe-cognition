# Vibe Cognition

MCP server plugin for Claude Code that maintains a project knowledge graph.

## MCP Setup

The MCP server is **declared by the plugin** in `.claude-plugin/plugin.json` (`mcpServers`), launched via `uv run --directory ${CLAUDE_PLUGIN_ROOT}`. There is **no per-project `.mcp.json`** — the server learns the project root from `${CLAUDE_PROJECT_DIR}` (Claude Code injects it into the server env; `REPO_PATH` is also set to it explicitly, and `config.py` falls back to it). The uv virtualenv lives in `${CLAUDE_PLUGIN_DATA}/.venv` (persistent across plugin updates, outside the version-pinned cache dir) so a running server doesn't lock the cache during `/plugin update`.

The SessionStart hook (`hooks/session-start.sh`) syncs deps and injects context. It does **not** write `.mcp.json`; for users upgrading from an older version it surgically removes the stale `vibe-cognition` entry from any project `.mcp.json` (via `vibe_cognition.migrate_mcp`), leaving all other servers and keys untouched. When a removal happens the hook surfaces a one-line note (what was removed, what was preserved) via `prime`, alongside the usual context injection. `python -m vibe_cognition.migrate_mcp <path> --dry-run` previews the change (removed/preserved) without writing — handy for confirming the removal is surgical on a real file.

## Plugin Release Procedure

**The canonical procedure lives in the cognition graph, not here** — fetch it with
`cognition_get_workflow("plugin release procedure")`, which always resolves to the
current HEAD version (v6 at the time of writing; versioned by supersession, so this
file can never go stale the way an inlined copy did twice). Follow that workflow
step-by-step for every release; do not reconstruct the procedure from memory or from
this file.

Orientation only (not the procedure): the marketplace lives in a separate repo —
`Haagndaazer/colton-claude-plugins` (marketplace name `coltondyck`), maintained by
Loki. This repo ships **code only** and does NOT carry its own `marketplace.json`
(a second file named `coltondyck` would collide, since Claude Code keys marketplaces
by name). The marketplace `sha` always points to a code commit on this repo's `main`.
