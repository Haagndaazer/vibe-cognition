# WP-P0 — Harness module, templates, parity registry (pure refactor)

Status: BRIEF (solo build; sonnet code review before "done")
Parent: `docs/codex-parity-plan.md` rev 3 (Colton's rulings 2026-09-08)
Date: 2026-09-08

## Scope

1. `src/vibe_cognition/harness.py`: one table of harness facts keyed by
   `VIBE_HARNESS` (`claude-code` default, `codex`): display name, skill
   invocation prefix, spawn tool wording, background/depth hints, plugin-root
   variable, model tiers (Claude `haiku`/`sonnet`; Codex defaults live here,
   `VIBE_MODEL_SMALL`/`VIBE_MODEL_MID` override, literal `inherit` → no pin),
   update source and CTA, containment description. `get_status` gains a
   `harness` block from it. `instructions.py`, `prime.py`, `update_check.py`,
   `whats_new.py` read harness facts from it instead of local literals where
   they already branch.
2. Templates: `skills-src/<skill>/SKILL.md` and `agents-src/<agent>.md` become
   the hand-edited sources. Tokens: `{{invoke:<skill>}}`, `{{spawn_tool}}`,
   `{{model:small}}`, `{{model:mid}}`, `{{harness_name}}`,
   `{{harness:<name>}}…{{/harness}}` blocks. No other logic.
3. `tools/render_harness.py` (stdlib only): renders `skills/*/SKILL.md` and
   `agents/*.md` for Claude Code, `adapters/codex/skills/<skill>/SKILL.md` for
   the four Codex-shipping skills. `--check` exits non-zero on drift.
4. `parity.json` (stdlib; the plan said yaml — JSON keeps the dependency set
   unchanged) + `tools/render_parity.py` → `PARITY.md`. Rows for every
   registered tool, every `skills-src` skill, every hook event in either hooks
   file, every `agents-src` agent × {claude-code, codex}; status
   `full | partial | waived | n/a`; `full` rows name a test function that
   exists under `tests/`.
5. Codex manifest `skills` → `./adapters/codex/skills/<skill>` paths.
6. `docs/HARNESSES.md`: add-a-harness checklist (D1–D9 duties, files an
   adapter owns, tests to add).

## Acceptance criteria

- AC1 Claude Code artifacts byte-identical: `git diff --exit-code skills agents`
  after the first render; `.claude-plugin/plugin.json` untouched.
- AC2 `tools/render_harness.py --check` and `tools/render_parity.py --check`
  pass in CI (pytest wrappers), fail when a rendered file or PARITY.md is
  hand-edited.
- AC3 Semantic render assertions: no `{{` survives in any output; every
  `{{harness:codex}}` block produces a Codex output that differs from the
  Claude output at that block; every Codex skill description ≤ 1,024 chars
  and the four together ≤ 8,000.
- AC4 Lints: forbidden literals in `skills-src`/`agents-src` (`/vibe-`,
  `$vibe-`, `Agent tool`, `spawn_agent`, `haiku`, `sonnet`, `/plugin update`,
  `codex plugin`, `CLAUDE_PLUGIN_ROOT`, `PLUGIN_ROOT`, `Claude Code`, `Codex`
  outside harness blocks); orphan dirs under `skills/` and
  `adapters/codex/skills/` without a `skills-src` source fail.
- AC5 Parity completeness: every enumerated item has a row for both
  harnesses; every `full` row's test name resolves to an existing test.
- AC6 `get_status()["harness"]` reports name, models (with override source),
  invocation prefix; unit tests cover env override, `inherit`, unknown
  harness → claude-code.
- AC7 `tests/test_doc_drift.py` parametrized over both rendered
  `vibe-cognition/SKILL.md` files.
- AC8 Full suite green (modulo the two pre-existing Windows-Store
  interpreter failures and the chromadb flake), `ruff check .` clean.

## KNOWN-INTENTIONAL

- Codex renders of `vibe-curate` and `vibe-backfill` are NOT produced in P0
  (WP-P2); the renderer supports them, the manifest keeps them out.
- Codex default model names: chosen in P0 only if the available-model list
  can be enumerated now; otherwise `None` with `default_pending: true` in
  `get_status.harness` and the P2 spike fills them in.
- Registry is JSON, not YAML (no new dependency).
- Generated files are committed; `render --check` is the guard.
