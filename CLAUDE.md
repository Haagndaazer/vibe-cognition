# Vibe Cognition

MCP server plugin for Claude Code and OpenAI Codex CLI that maintains a project knowledge graph. Harness-specific facts live in `src/vibe_cognition/harness.py` (selected by `VIBE_HARNESS`); skills and agents are rendered per harness from `skills-src/` and `agents-src/` by `tools/render_harness.py` — never edit a rendered file under `skills/`, `agents/`, or `adapters/codex/`. `PARITY.md` (generated from `parity.json`) is the per-harness feature registry; `docs/HARNESSES.md` is the add-a-harness checklist.

## MCP Setup

The MCP server is **declared by the plugin** in `.claude-plugin/plugin.json` (`mcpServers`), launched via `uv run --directory ${CLAUDE_PLUGIN_ROOT}`. There is **no per-project `.mcp.json`** — the server learns the project root from `${CLAUDE_PROJECT_DIR}` (Claude Code injects it into the server env; `REPO_PATH` is also set to it explicitly, and `config.py` falls back to it). The uv virtualenv lives in `${CLAUDE_PLUGIN_DATA}/.venv` (persistent across plugin updates, outside the version-pinned cache dir) so a running server doesn't lock the cache during `/plugin update`.

On Codex the plugin ships skills + hooks only; `adapters/codex/hooks/session-start.sh` registers the server at user level with `codex mcp add` (so it inherits the project cwd), installs the Plan role into `~/.codex/agents/`, and preserves user-added env keys across re-registration. Codex fires SessionStart hooks on the first turn, not at launch.

The SessionStart hook (`hooks/session-start.sh`) syncs deps and injects context. It does **not** write `.mcp.json`; for users upgrading from an older version it surgically removes the stale `vibe-cognition` entry from any project `.mcp.json` (via `vibe_cognition.migrate_mcp`), leaving all other servers and keys untouched. When a removal happens the hook surfaces a one-line note (what was removed, what was preserved) via `prime`, alongside the usual context injection. `python -m vibe_cognition.migrate_mcp <path> --dry-run` previews the change (removed/preserved) without writing — handy for confirming the removal is surgical on a real file.

## Plugin Release Procedure

The marketplace lives in a separate repo — `Haagndaazer/colton-claude-plugins` (marketplace name `coltondyck`), maintained by Loki. This repo ships **code only** and does NOT carry its own Claude Code `marketplace.json` (a second file named `coltondyck` would collide, since Claude Code keys marketplaces by name). It DOES carry a **Codex dev marketplace** at `.agents/plugins/marketplace.json` (name `vibe-cognition-dev`, source = this repo on GitHub, `ref: main`) for installing current `main`; the release path for Codex is the `coltondyck` Codex marketplace in `colton-claude-plugins` (`.agents/plugins/marketplace.json`, source `url`, sha-pinned), which Loki maintains alongside the Claude one.

1. Make your code changes. Prose changes go in `skills-src/` / `agents-src/`; run `uv run python tools/render_harness.py` and commit sources and renders together. New tools/skills/hooks/agents need `parity.json` rows (`uv run python tools/render_parity.py`).
2. If the change is user-facing, bump the version in `pyproject.toml`, `.claude-plugin/plugin.json`, and `.codex-plugin/plugin.json` (each plugin system reads version from its own manifest; `tests/test_codex_plugin.py` fails if the three disagree), then `uv lock`. Add the `CHANGELOG.md` section and the `.claude-plugin/whats-new.json` bullets.
3. Gates: `uv run ruff check .`, `uv run python tools/render_harness.py --check`, `uv run python tools/render_parity.py --check`, full pytest, sonnet review, and the tool-surface audit below when a tool changed.
4. Commit and push to `main` (this is the "code commit"; Codex installs pull from git, so it must be pushed).
5. Ping Loki with the code-commit SHA and the version, saying the audit was run. Loki re-pins that SHA in `colton-claude-plugins`'s `.claude-plugin/marketplace.json` (Claude Code) AND `.agents/plugins/marketplace.json` (Codex) in one commit, and pushes, so installs/updates pick it up.
6. Human gates on Colton's machine: Claude Code `/plugin update`; Codex `codex plugin marketplace upgrade coltondyck` + `codex plugin add vibe-cognition@coltondyck`, then start Codex in a project, send one message, restart, and run the relevant phases of `docs/codex-test-brief.md`. The full procedure is the cognition workflow "vibe-cognition plugin release procedure" (v6).

The marketplace `sha` always points to the code commit on this repo's `main`.

## HARD RULE — tool-surface audit before every release

If a release adds or changes ANY MCP tool (new tool, new/renamed parameter, changed return shape, changed error semantics), the release is NOT ready to pin until the recurring tool-surface self-sufficiency audit (cognition workflow `67751ebc39bd`) has been run and its findings fixed in the same release:

1. Each affected tool's docstring, read in isolation, documents every parameter in its `Args:` block (including any new gating parameter such as `curation_token`), the full `Returns:` shape (every key an agent will read), and the error semantics.
2. `get_status`'s documented return shape lists every key the tool actually returns.
3. `skills-src/vibe-cognition/SKILL.md`'s tool table and `README.md`'s MCP tools table carry a row for every registered tool (`tests/test_doc_drift.py` enforces the skill table; check the README by hand).
4. `parity.json` has a row for the tool on every harness (`tools/render_parity.py --check`).
5. The `cognition_readme` tool's guide text still describes the current record → curate loop.

Say in the pin request to Loki that the audit was run. This rule exists because v0.36.0 shipped `curation_token` on three tools with the parameter missing from their `Args:` blocks and `get_status` returning two undocumented keys; the audit caught it only because Colton asked.
