# Codex Full-Parity Plan — everything after WP-C1b, built for maintainability

Status: PLAN rev 1 (scoping — awaiting sonnet peer review, then Colton's rulings)
Author: Colton Dyck (solo session)
Date: 2026-09-08
Baseline: v0.35.0 shipped and field-verified (Gate C1, episode `6c3c15e3a0a8`);
`docs/codex-parity-findings.md` §7a–7b; `docs/wp-codex-plugin-plan.md`;
Hermes plan rev 2 Phase 0 (`docs/hermes-parity-plan.md`) whose portability
ideas this plan adopts with Hermes shelved.

## Where parity stands

| Surface | Claude Code | Codex today | Gap |
|---|---|---|---|
| MCP server, all tools, project binding | native | hook-registered, one restart after install/update | none functionally |
| Session-start digest, compact reinjection, standing practices | yes | yes (Codex wording for the curation line) | none |
| Dependency bootstrap, venv probe, what's-new | yes | yes | none |
| Skills: cognition, document, workflow, dashboard | yes | yes, but bodies say `/vibe-…` and "Agent tool" | wording |
| **Curation** (orchestrator + 3 analyzers, tiered, contained) | yes | **absent** | the big one |
| Backfill | yes | absent | same mechanism as curation |
| Plan agent | yes | absent | agent definition |
| Update nudge | yes | disabled (Claude CTA) | harness-keyed source + CTA |
| Model tiering (haiku/sonnet) | hardcoded in skill/agent text | n/a | should not be hardcoded anywhere |
| Containment of edge writes | subagent tool whitelist | none | server-side token (both harnesses) |

The functional gap is curation and the two other subagent-driven features;
the *maintainability* gap is that every harness-specific fact (invocation
syntax, spawn tool, model names, update CTA, plugin root variable) is
scattered through 1,262 lines of prose across `skills/` and `agents/`, with
no test that notices when a fact changes on one harness and not the other.
This plan fixes the second problem first, because it makes the first one
cheap and keeps harness #3 cheaper still.

## Architecture principles (carried from the Hermes plan, now concrete)

1. **Policy lives in the server; adapters stay thin.** Containment,
   model-tier resolution, the harness name, and every user-facing CTA string
   are computed in one Python module. Skills and hooks ask; they don't know.
2. **One source, generated outputs, committed.** Both harnesses install
   straight from the git tree with no build step, so generated files must be
   committed. A drift test regenerates and diffs, so a stale render fails CI.
   No harness-specific fact may appear in a source template as a literal —
   only as a variable or a harness block.
3. **Parity is a registry, not a checklist.** A `parity.yaml` row for every
   tool, skill, hook event, and agent × every harness, with
   `full | partial | waived | n/a` and a mandatory note for anything not
   `full`. CI fails when a registry item has no row. `PARITY.md` is
   generated from it.
4. **Conformance over installation.** CI never installs a harness. Each
   adapter has a conformance suite that launches our code exactly the way
   that harness does (env, cwd, transport, hook JSON), like
   `tests/test_codex_hooks_shell.py` already does. The human field gate stays
   for what only a real harness can show.
5. **Adding a harness is a checklist, not a project.** `docs/HARNESSES.md`
   lists the duties (D1–D9 from the findings) and the files an adapter owns.

## The harness module (`src/vibe_cognition/harness.py`)

A small table, one entry per harness, keyed by `VIBE_HARNESS`
(default `claude-code`; the Codex hook already sets `codex`):

| Field | claude-code | codex |
|---|---|---|
| `skill_invoke(name)` | `/name` | `$name` |
| `spawn_tool` | "the Agent tool" | "`spawn_agent`" |
| `spawn_background_hint` | `run_in_background: true` | spawn returns immediately; do not `wait_agent` |
| `plugin_root_var` | `CLAUDE_PLUGIN_ROOT` | `PLUGIN_ROOT` (Codex also sets `CLAUDE_*`) |
| `update_cta(market)` | `/plugin update vibe-cognition@{market}` | `codex plugin marketplace upgrade {market}` + re-add (exact form decided by WP-P3's spike) |
| `update_source` | Claude marketplace.json pin | `.agents/plugins/marketplace.json` sha |
| `model_tier(small)` | `haiku` | `VIBE_MODEL_SMALL` if set, else omit (inherit) |
| `model_tier(mid)` | `sonnet` | `VIBE_MODEL_MID` if set, else omit |
| `containment` | tool whitelist + token | token only (stated in PARITY.md) |

Consumers: `prime.py`, `instructions.py`, `update_check.py`, `whats_new.py`,
and a new `get_status.harness` block so *agents* can ask at runtime
("which model do I pass for analyzers?") instead of reading it from prose.
The hardcoded `haiku`/`sonnet` in skills and agents goes away on Claude too.

## Templates and rendering

- `skills-src/<skill>/SKILL.md` and `agents-src/<agent>.md` become the only
  hand-edited copies. Template syntax is deliberately tiny:
  `{{invoke:vibe-curate}}`, `{{spawn_tool}}`, `{{spawn_background_hint}}`,
  `{{model:small}}`, and `{{harness:codex}} … {{/harness}}` blocks. No logic.
- `tools/render_harness.py` renders:
  - `skills/*/SKILL.md` (Claude Code; must be byte-identical to today's files
    on the first run — the refactor's acceptance test),
  - `adapters/codex/skills/*/SKILL.md` (Codex; the manifest's `skills` list
    moves to these paths),
  - `agents/*.md` (Claude Code, frontmatter intact),
  - `adapters/codex/skills/vibe-curate/references/*.md` (the orchestrator and
    analyzer bodies as skill references — Codex skills may carry
    `references/`; a Codex plugin cannot ship agent roles),
  - `adapters/codex/agents/*.toml` (optional role files for the Plan agent,
    per Colton's ruling that roles live in `~/.codex/agents/`).
- `tests/test_harness_render.py`: renders into a temp dir and diffs against
  the committed outputs; lints `skills-src`/`agents-src` for forbidden
  literals (`/vibe-`, `$vibe-`, `Agent tool`, `spawn_agent`, `haiku`,
  `sonnet`, `/plugin update`, `CLAUDE_PLUGIN_ROOT`).
- Codex skill descriptions get a length budget check (Codex trims the skill
  list at 8,000 chars).

## Curation on Codex (the functional core)

Shape, mirroring Claude Code one-to-one:

1. `$vibe-curate` (rendered skill) tells the main agent to call
   `get_status`, read `harness.models`, then `spawn_agent` the orchestrator
   with `message` = the orchestrator reference + the `uncurated` count, and
   `model` = the mid tier if set. Spawn is asynchronous; the skill says not to
   wait and points at `get_status.uncurated` as ground truth, exactly like
   today's "launched in background" contract.
2. The orchestrator spawns analyzers the same way (`model` = small tier),
   reviews proposals, writes edges with the curation token, and reports bare
   counts. Nesting exists in Codex's spawn model (depth counter; a
   configurable limit on the V1 path, none on V2) — the spike confirms the
   default allows depth 2.
3. Containment is the server-side token (WP-P1): `cognition_begin_curation`
   issues a session token; `cognition_add_edge(s)` and `cognition_mark_curated`
   refuse without it and stamp every write with the session id;
   `edges_outside_curation` stays. Claude Code keeps its whitelists on top.
   PARITY.md states plainly that Codex has friction-and-audit containment,
   not a hard boundary.
4. Backfill follows the identical pattern with its worker prompt as a
   reference file.

## Work packages

**WP-P0 — Harness module, templates, parity registry (pure refactor).**
`harness.py` + `get_status.harness`; `skills-src`/`agents-src` + renderer;
first render byte-identical for Claude Code (assert in CI, then relax to the
drift test); `parity.yaml` + `PARITY.md` generator + completeness test over
registered tools, skill dirs, hook events, agents; `docs/HARNESSES.md`.
Codex manifest `skills` list switches to `adapters/codex/skills/*`.
Acceptance: Claude Code artifacts unchanged byte-for-byte; every registry
item has a parity row; forbidden-literal lint passes; full suite green.
≈ 3–4 days.

**WP-P1 — Server-side curation containment (was WP-H0a).** Token flow,
per-write provenance, actionable refusal text pointing at
`{{invoke:vibe-curate}}`; orchestrator template updated; Claude pipeline
green with the token; a test that a token-less edge write is refused for the
right reason (ledger 20). ≈ 2–3 days. Independent of P0's templates except
for the orchestrator text, so it can run in parallel once P0's renderer
exists.

**WP-P2 — Curation + backfill on Codex.** Codex renders of `vibe-curate` and
`vibe-backfill`; orchestrator/analyzer/backfill references; spike first:
(a) default spawn depth allows orchestrator → analyzers, (b) how a finished
subagent surfaces to the main thread, (c) whether `model` overrides are
honored with the user's plan. Then extend `docs/codex-test-brief.md` with a
Phase 5 (record three nodes, run `$vibe-curate`, observe `uncurated` fall to
0, inspect `edge_sources`). Needs P0 (templates) and P1 (token).
≈ 4–5 days including the live gate.

**WP-P3 — Update nudge, what's-new, Plan on Codex.** `update_check` gains a
harness-keyed source (the Codex marketplace sha vs the installed plugin's
sha, read from Codex's install metadata in the cache) and CTA; spike: does
`codex plugin marketplace upgrade coltondyck` alone move the installed
plugin, or is a re-add needed? The hook's re-registration must **preserve
user-added env keys** on the `vibe-cognition` MCP entry (read the existing
entry before `codex mcp add`), so a user's `VIBE_MODEL_SMALL` survives a
plugin update. Plan agent ships as a Codex skill (`$vibe-plan`) rendered from
`agents-src/plan.md`; a role file is optional. Needs P0 only. ≈ 2 days.

**WP-P4 — Institutionalize.** Release workflow node superseded: version bump
in three manifests (enforced), `render --check`, parity gate green, both
marketplace pins in one Loki commit (as done for 0.35.0). Linux CI job for
the hook shell tests (today Windows-only). Recurring tool-surface audit
extended with "parity row present". ≈ 1–2 days.

Sequencing by real edges: P0 → (P1 ∥ P3) → P2 → P4. Solo ≈ 2.5–3 weeks;
with a second implementer on P1/P3 while P0 finishes, ≈ 2 weeks. The
critical path is P0 → P2 → P4.

## KNOWN-INTENTIONAL

- Containment on Codex is weaker than on Claude Code until Codex grows
  per-agent tool scoping; recorded in PARITY.md, not hidden.
- Generated files are committed; hand-editing a rendered file is a CI
  failure by design.
- Codex default model tier is "inherit" unless the user sets
  `VIBE_MODEL_SMALL`/`VIBE_MODEL_MID`; we do not hardcode OpenAI model names.
- One restart after install/update on Codex stays (B-hook design).
- Hermes remains shelved; nothing here blocks it — the same renderer gains a
  third column when wanted.

## Risks

1. Codex multi-agent churn (V1/V2 spawn semantics, depth limits, model
   override behavior) — spike before P2 commits; pin a tested Codex range.
2. Template discipline — the forbidden-literal lint is the guard; without it
   harness facts creep back into prose within a release.
3. Re-registration clobbering user env — P3's preserve-existing-env step;
   until then, document that manual edits to the entry are overwritten on
   plugin updates.
4. Codex skill-list budget (8,000 chars) — Codex renders must keep
   descriptions short; the render test measures.
5. Prompt-only containment on Codex — the token makes bypass visible, not
   impossible; the audit field is the control.

## Decisions needed from Colton

1. Approve the template-and-render approach with generated files committed
   (versus maintaining per-harness copies by hand).
2. Codex model tiers: default to inherit, with `VIBE_MODEL_SMALL`/`_MID` as
   the override knob — or name a default small model now?
3. Plan agent on Codex as a skill (`$vibe-plan`) rather than an installed role
   file — agreed?
4. Backfill in scope for Codex parity (it rides P2 for little extra cost) —
   yes or defer?
