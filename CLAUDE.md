# Vibe Cognition

MCP server plugin for Claude Code that maintains a project knowledge graph.

## Plugin Release Procedure

The marketplace lives in a separate repo — `Haagndaazer/colton-claude-plugins` (marketplace name `coltondyck`), maintained by Silvie. This repo ships **code only** and does NOT carry its own `marketplace.json` (a second file named `coltondyck` would collide, since Claude Code keys marketplaces by name).

1. Make your code changes.
2. If the change is user-facing, bump the version in both `pyproject.toml` and `.claude-plugin/plugin.json` (the plugin system reads version from `plugin.json`).
3. Commit and push to `main` (this is the "code commit").
4. Ping Silvie with the code-commit SHA and the version.
5. Silvie re-pins that SHA in `colton-claude-plugins`'s `marketplace.json` and pushes, so installs/updates pick it up.

The marketplace `sha` always points to the code commit on this repo's `main`.
