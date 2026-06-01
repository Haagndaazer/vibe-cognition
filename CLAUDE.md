# Vibe Cognition

MCP server plugin for Claude Code that maintains a project knowledge graph.

## MCP Setup

The MCP server is **declared by the plugin** in `.claude-plugin/plugin.json` (`mcpServers`), launched via `uv run --directory ${CLAUDE_PLUGIN_ROOT}`. There is **no per-project `.mcp.json`** — the server learns the project root from `${CLAUDE_PROJECT_DIR}` (Claude Code injects it into the server env; `REPO_PATH` is also set to it explicitly, and `config.py` falls back to it). The uv virtualenv lives in `${CLAUDE_PLUGIN_DATA}/.venv` (persistent across plugin updates, outside the version-pinned cache dir) so a running server doesn't lock the cache during `/plugin update`.

The SessionStart hook (`hooks/session-start.sh`) syncs deps and injects context. It does **not** write `.mcp.json`; for users upgrading from an older version it surgically removes the stale `vibe-cognition` entry from any project `.mcp.json` (via `vibe_cognition.migrate_mcp`), leaving all other servers and keys untouched. When a removal happens the hook surfaces a one-line note (what was removed, what was preserved) via `prime`, alongside the usual context injection. `python -m vibe_cognition.migrate_mcp <path> --dry-run` previews the change (removed/preserved) without writing — handy for confirming the removal is surgical on a real file.

## Plugin Release Procedure

The marketplace lives in a separate repo — `Haagndaazer/colton-claude-plugins` (marketplace name `coltondyck`), maintained by Silvie. This repo ships **code only** and does NOT carry its own `marketplace.json` (a second file named `coltondyck` would collide, since Claude Code keys marketplaces by name).

1. Make your code changes.
2. If the change is user-facing, bump the version in both `pyproject.toml` and `.claude-plugin/plugin.json` (the plugin system reads version from `plugin.json`).
3. Commit and push to `main` (this is the "code commit").
4. Ping Silvie with the code-commit SHA and the version.
5. Silvie re-pins that SHA in `colton-claude-plugins`'s `marketplace.json` and pushes, so installs/updates pick it up.

The marketplace `sha` always points to the code commit on this repo's `main`.
