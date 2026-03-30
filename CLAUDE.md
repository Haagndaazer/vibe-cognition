# Vibe Cognition

MCP server plugin for Claude Code that maintains a project knowledge graph.

## Plugin Release Procedure

1. Make your code changes and commit them.
2. Bump the version in `pyproject.toml`.
3. Commit the code changes (this is the "code commit").
4. Update `.claude-plugin/marketplace.json` — set `sha` to the code commit's SHA.
5. Commit the marketplace SHA update as a separate commit.

The marketplace SHA always points to the code commit (the one before the SHA-update commit).
