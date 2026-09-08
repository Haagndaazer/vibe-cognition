# Adding or maintaining a harness

vibe-cognition runs under more than one agent harness (Claude Code, Codex).
Every harness-specific fact lives in exactly one place, and CI proves the two
renders stay in step. This is the checklist for touching that machinery or
adding harness #3.

## Where harness facts live

| Concern | Location |
|---|---|
| Policy table (invocation prefix, spawn tool wording, model tiers, update CTA, containment) | `src/vibe_cognition/harness.py`; runtime selection by `VIBE_HARNESS` |
| Runtime exposure to agents | `get_status()["harness"]` |
| Skill and agent prose | `skills-src/*/SKILL.md`, `agents-src/*.md` (sources); `skills/`, `agents/`, `adapters/codex/skills/` (generated, committed) |
| Renderer | `tools/render_harness.py` (`--check` in CI via `tests/test_harness_render.py`) |
| Parity registry | `parity.json` → `PARITY.md` via `tools/render_parity.py` (`--check` in CI via `tests/test_parity_registry.py`) |
| Claude Code adapter | `.claude-plugin/plugin.json`, `hooks/hooks.json`, `hooks/*.sh`, `agents/` |
| Codex adapter | `.codex-plugin/plugin.json`, `adapters/codex/hooks.json`, `adapters/codex/hooks/*`, `adapters/codex/skills/`, `.agents/plugins/marketplace.json` |

## Template tokens

`{{invoke:<skill>}}` `{{spawn_tool}}` `{{model:small}}` `{{model:mid}}`
`{{harness_name}}` and `{{harness:<name>}} … {{/harness}}` blocks. No other
logic. Never write a harness literal (`/vibe-…`, `$vibe-…`, `Agent tool`,
`spawn_agent`, `haiku`, `sonnet`, `/plugin update`, `codex plugin`,
`CLAUDE_PLUGIN_ROOT`, `PLUGIN_ROOT`, `Claude Code`) into a source outside a
harness block — the lint fails.

## Editing prose

1. Edit the source under `skills-src/` or `agents-src/`.
2. Run `uv run python tools/render_harness.py`.
3. Commit sources and rendered outputs together.

## Adding a tool, skill, hook event, or agent

1. Add it. `tools/render_parity.py --check` now fails with "missing parity
   row".
2. Add its row to `parity.json` for every harness. `full` needs `evidence`:
   `tests/<file>.py::<test_function>` or `gate:<episode id>` (a human field
   gate recorded in the cognition graph). Anything else needs a `note`.
3. Run `uv run python tools/render_parity.py` to regenerate `PARITY.md`.

## Adding harness #3

1. Add a `Harness` entry in `harness.py` (all fields; model defaults may be
   `None` until chosen — the renderer refuses `{{model:*}}` without one).
2. Add the name to `HARNESS_NAMES` in `tools/render_parity.py` and a column
   to every `parity.json` row (status `waived` with a note is acceptable to
   start).
3. Decide which skills render for it (`CODEX_SKILLS`-style list in the
   renderer) and where the manifest points.
4. Write the adapter: manifest, hooks file, wrapper scripts (mind Windows
   shell quoting — one unquoted path per command), marketplace entry.
5. Add a conformance test that launches the hooks the way the harness does
   (see `tests/test_codex_hooks_shell.py`) — CI never installs the harness.
6. Field-verify on a real install with a self-test brief
   (see `docs/codex-test-brief.md`) before flipping rows to `full`.
7. Release: version bump in every manifest, both renders checked, parity gate
   green, marketplace pins in one Loki commit.
