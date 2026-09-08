# Codex Full-Parity Plan — everything after WP-C1b, built for maintainability

Status: PLAN rev 3 — RULED by Colton 2026-09-08 (see § Rulings); rev 2 was the
sonnet adversarial review 2026-09-08 APPROVE-WITH-CHANGES;
all findings applied (spawn depth default = 1 → flat curation shape on Codex
with adaptive fan-out; orchestrator model pin may never be omitted; Codex
plugin update requires a re-add and the install metadata carries no sha;
`spawn_agent` rejects unknown model names; doc-drift and completeness tests
extended to the Codex render with semantic assertions; backfill cost risk).
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

## Verified Codex constraints that shape the design (source, 2026-09-08)

- **Spawn depth: V1 defaults to 1, V2 has no limit — and Colton's Codex runs
  V2.** Source: `DEFAULT_AGENT_MAX_DEPTH = 1` on the V1 path refuses a
  subagent's own spawn ("Agent depth limit reached. Solve the task
  yourself."). **Live spike 2026-09-08 (`docs/codex-spike-report-p2.md`):**
  the session exposed the V2 `collaboration.spawn_agent` surface
  (`task_name`, `fork_turns`, `model`), a nested spawn SUCCEEDED, and
  finished subagents PUSHED `FINAL_ANSWER` messages to the parent without
  polling. Model overrides require `fork_turns: "none"`. The design keeps the
  inline fallback only as a safety net for V1 installs.
- **`spawn_agent` `model` must name an available model.** Unknown names
  hard-error ("Unknown model … Available models: …"); there is no degrade.
- **Installed plugins do not auto-update.** `codex plugin marketplace
  upgrade` refreshes the marketplace snapshot only; moving the installed
  plugin requires `codex plugin add <name>@<market>` again. The install
  metadata file records only a plugin id — no sha, no version.
- **`codex mcp add` replaces the whole entry** (env included);
  `codex mcp get <name> --json` returns the current entry with its env, so
  a hook can read-merge-write.
- **Completion of a spawned agent:** on the V2 surface a `FINAL_ANSWER`
  message is pushed to the parent at its next tool boundary (spike-verified);
  `wait_agent` remains the deterministic way to block for a specific child.
  Design for both: prefer `wait_agent`, accept the pushed message.
- Skills may carry `references/`; the legacy manifest's `skills` accepts a
  list of directories (so Codex can point at `adapters/codex/skills/*`).

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
   `full | partial | waived | n/a`; a `full` row must cite the conformance
   test that proves it, anything else carries a note. CI fails when a
   registry item has no row. `PARITY.md` is generated from it.
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
| `spawn_background_hint` | `run_in_background: true` | spawn returns immediately; poll `get_status.uncurated`, never `wait_agent` in the launcher |
| `spawn_depth_note` | nested fan-out allowed | subagents cannot spawn unless `agents.max_depth ≥ 2`; orchestrator adapts (below) |
| `plugin_root_var` | `CLAUDE_PLUGIN_ROOT` | `PLUGIN_ROOT` (Codex also sets `CLAUDE_*`) |
| `update_cta(market)` | `/plugin update vibe-cognition@{market}` | `codex plugin marketplace upgrade {market}` then `codex plugin add vibe-cognition@{market}`, then restart |
| `update_source` | Claude marketplace.json pin | `.agents/plugins/marketplace.json` sha → that commit's `plugin.json` version |
| `model_tier(small)` | `haiku` | `VIBE_MODEL_SMALL` if set, else the Codex default small model from `harness.py` |
| `model_tier(mid)` | `sonnet` | `VIBE_MODEL_MID` if set, else the Codex default mid model from `harness.py` |
| `containment` | tool whitelist + token | token only (stated in PARITY.md) |

Consumers: `prime.py`, `instructions.py`, `update_check.py`, `whats_new.py`,
and a new `get_status.harness` block so *agents* can ask at runtime instead
of reading model names from prose. The hardcoded `haiku`/`sonnet` in skills
and agents goes away on Claude too.

**Model-pin rule, unchanged by harness.** The orchestrator's own spawn must
carry an explicit `model` on every harness (fail `f09e770da046`: an omitted
pin inherits the launching session's model). **Ruling (Colton):** first-time
install must be low-friction, so `harness.py` ships **default model names for
both Codex tiers**, exactly as it does for Claude Code (`haiku`/`sonnet`);
`VIBE_MODEL_SMALL`/`VIBE_MODEL_MID` are per-user/per-project **overrides
only**. The defaults are chosen at the WP-P2 spike from the live available
model list (Codex's own "Available models: …" error enumerates it) and kept
current in one place; they are a maintained fact like any other harness
literal. If a default is not available on a user's plan, `spawn_agent` fails
with Codex's "Unknown model … Available models" error; the skill instructs
the agent to relay that verbatim and name the override variable, never to
retry or silently inherit. `VIBE_MODEL_*=inherit` is accepted as an explicit
opt-in to the main session's model.

## Templates and rendering

- `skills-src/<skill>/SKILL.md` and `agents-src/<agent>.md` become the only
  hand-edited copies. Template syntax is deliberately tiny:
  `{{invoke:vibe-curate}}`, `{{spawn_tool}}`, `{{spawn_background_hint}}`,
  `{{model:small}}`, `{{model:mid}}`, and `{{harness:codex}} … {{/harness}}`
  blocks. No logic.
- `tools/render_harness.py` renders:
  - `skills/*/SKILL.md` (Claude Code; byte-identical to today's files on the
    first run — the refactor's acceptance test),
  - `adapters/codex/skills/*/SKILL.md` (Codex; the manifest's `skills` list
    moves to these directories),
  - `agents/*.md` (Claude Code, frontmatter intact),
  - `adapters/codex/skills/vibe-curate/references/*.md` and
    `adapters/codex/skills/vibe-backfill/references/*.md` (orchestrator,
    analyzer, and worker bodies as skill references — a Codex plugin cannot
    ship agent roles),
  - `adapters/codex/agents/plan.toml` (the Plan agent as a Codex custom role,
    rendered from `agents-src/plan.md`; the session-start hook installs and
    version-stamps it into `~/.codex/agents/` per Colton's rulings — role
    files live there, and Plan ships as a role, not a skill).
- Tests, each with a semantic assertion so a no-op renderer cannot pass:
  - `test_harness_render.py`: renders into a temp dir and diffs against the
    committed outputs; **and** asserts that for every `{{harness:codex}}`
    block the Codex output differs from the Claude output at that block, and
    that every `{{…}}` token in a source was consumed (no literal `{{` in
    any output).
  - Forbidden-literal lint over `skills-src`/`agents-src`: `/vibe-`,
    `$vibe-`, `Agent tool`, `spawn_agent`, `haiku`, `sonnet`,
    `/plugin update`, `codex plugin`, `CLAUDE_PLUGIN_ROOT`, `PLUGIN_ROOT`.
    The list is a maintained allowlist; adding a harness fact type means
    adding its literal here (plan Risk 2).
  - Orphan lint: every top-level directory under `skills/` and
    `adapters/codex/skills/` traces back to a `skills-src` entry; the parity
    completeness gate enumerates from `skills-src`/`agents-src`/registered
    tools/hook events, never from output trees, so a hand-authored
    Codex-only skill cannot bypass the registry.
  - `tests/test_doc_drift.py` parametrized over both rendered
    `vibe-cognition/SKILL.md` files, with an explicit per-harness set of
    tools a render may omit (Codex: none once P2 lands).
  - Codex skill descriptions get a length budget check (Codex trims the
    skill list at 8,000 chars).

## Curation on Codex (the functional core)

Shape, adapted to depth 1:

1. `$vibe-curate` (rendered skill) tells the main agent to call `get_status`,
   read `harness.models`, refuse if the mid tier is unresolved, then
   `spawn_agent` the orchestrator with `message` = the orchestrator reference
   + the `uncurated` count and `model` = mid tier. Spawn returns immediately;
   the skill says not to wait and names `get_status.uncurated` as ground
   truth — the same contract as today's "launched in background".
2. **The orchestrator runs flat by default.** Its Codex reference contains the
   edge, conflict, and cluster analyzer procedures as sections it executes
   itself, in the same order and with the same review rules, writing edges
   with the curation token and reporting bare counts. **Adaptive fan-out:**
   it first attempts one analyzer spawn (small tier); if Codex answers
   "Agent depth limit reached" it continues inline and notes
   `fan_out: unavailable` in its final counts; if the spawn succeeds (the
   user raised `agents.max_depth`), it fans out exactly as on Claude Code.
   PARITY.md records tiering on Codex as *partial* (flat by default).
3. Containment is the server-side token (WP-P1): `cognition_begin_curation`
   issues a session token; `cognition_add_edge(s)` and `cognition_mark_curated`
   refuse without it and stamp every write with the session id;
   `edges_outside_curation` stays. Claude Code keeps its whitelists on top.
   PARITY.md states plainly that Codex has friction-and-audit containment,
   not a hard boundary.
4. Backfill follows the identical pattern: flat by default, adaptive fan-out,
   worker prompt as a reference. Before spawning, the Codex skill prints the
   commit count it is about to process and, when `VIBE_MODEL_SMALL` is
   unset, a one-line note that workers will run at the orchestrator's model
   price.

## Work packages

**WP-P0 — Harness module, templates, parity registry (pure refactor).**
`harness.py` + `get_status.harness`; `skills-src`/`agents-src` + renderer;
first render byte-identical for Claude Code (assert in CI, then relax to the
drift test); `parity.yaml` + `PARITY.md` generator + completeness test over
registered tools, `skills-src`, hook events (enumerated from both hooks
files), `agents-src`; orphan lint; `docs/HARNESSES.md`. Codex manifest
`skills` list switches to `adapters/codex/skills/*`.
Acceptance: Claude Code artifacts unchanged byte-for-byte; every registry
item has a parity row and every `full` row names a test; forbidden-literal
and orphan lints pass; render tokens all consumed; full suite green.
≈ 3–4 days.

**WP-P1 — Server-side curation containment (was WP-H0a).** Token flow,
per-write provenance, actionable refusal text pointing at
`{{invoke:vibe-curate}}`; orchestrator template updated; Claude pipeline
green with the token; a test that a token-less edge write is refused for the
right reason (ledger 20). ≈ 2–3 days. Runs in parallel with P0 once the
renderer exists.

**WP-P2 — Curation + backfill on Codex.** Codex renders of `vibe-curate` and
`vibe-backfill`; flat orchestrator/worker references with adaptive fan-out;
mid-tier refusal path; spike first on a real session: (a) confirm the depth
refusal text and that one inline run completes within Codex's tool timeout
(300 s in source) per tool call, (b) confirm `model` overrides with the
user's plan, (c) observe how the main thread learns of completion (expect
polling only). Then extend `docs/codex-test-brief.md` with a Phase 5 (record
three nodes, run `$vibe-curate`, watch `uncurated` fall to 0, inspect
`edge_sources`; repeat once with `agents.max_depth = 2` to exercise
fan-out). Needs P0 (templates) and P1 (token). ≈ 4–5 days including the
live gate.

**WP-P3 — Update nudge, what's-new, Plan on Codex.** `update_check` gains a
harness-keyed source: read the Codex marketplace sha, fetch that commit's
`.codex-plugin/plugin.json`, compare **version strings** against the locally
installed manifest (the existing policy; the install metadata has no sha to
read), and emit the Codex CTA (marketplace upgrade + re-add + restart). The
hook's re-registration reads the current entry with `codex mcp get --json`
and merges user-added env keys before `codex mcp add`, so `VIBE_MODEL_*`
survive a plugin update. Plan agent ships as an installed Codex role:
`adapters/codex/agents/plan.toml` rendered from `agents-src/plan.md`
(`developer_instructions` = body, `model` = mid tier), copied by the
session-start hook into `~/.codex/agents/vibe-plan.toml` when missing or
stale (version stamp in a comment line), so `spawn_agent(agent_type=
"vibe-plan")` works. Needs P0 only. ≈ 2–3 days.

**WP-P4 — Institutionalize.** Release workflow node superseded: version bump
in three manifests (enforced), `render --check`, parity gate green, both
marketplace pins in one Loki commit (as done for 0.35.0). Linux CI job for
the hook shell tests (today Windows-only). Recurring tool-surface audit
extended with "parity row present and cites a test". ≈ 1–2 days.

Sequencing by real edges: P0 → (P1 ∥ P3) → P2 → P4. Solo ≈ 2.5–3 weeks;
with a second implementer on P1/P3 while P0 finishes, ≈ 2 weeks. The
critical path is P0 → P2 → P4.

## KNOWN-INTENTIONAL

- Containment on Codex is weaker than on Claude Code until Codex grows
  per-agent tool scoping; recorded in PARITY.md, not hidden.
- Curation on Codex runs flat (no analyzer fan-out) unless the user raises
  `agents.max_depth`; tiering is *partial* there by design, not an oversight.
- The orchestrator is always spawned with an explicit model; on Codex that
  is the `harness.py` default unless overridden — we never silently inherit
  the main session's model for the orchestrator.
- Generated files are committed; hand-editing a rendered file without its
  source is a CI failure by design.
- Default OpenAI model names live in exactly one place (`harness.py`) and are
  a maintained fact; env vars override them per user/project.
- One restart after install/update on Codex stays (B-hook design).
- Hermes remains shelved; the same renderer gains a third column when wanted.

## Risks

1. Codex multi-agent churn (V1/V2 spawn semantics, depth default, model
   validation) — spike before P2 commits; pin a tested Codex range; the
   adaptive fan-out keeps the design correct under either depth setting.
2. Template discipline — the forbidden-literal allowlist is the guard and
   must grow with each new harness fact type; the orphan lint closes the
   hand-authored-skill bypass.
3. Re-registration clobbering user env — closed by P3's read-merge-write;
   until then, manual edits to the entry are overwritten on plugin updates.
4. Codex skill-list budget (8,000 chars) — renders keep descriptions short;
   the render test measures.
5. Prompt-only containment on Codex — the token makes bypass visible, not
   impossible; the audit field is the control.
6. **Backfill cost on Codex** — worker fan-out (when available) or a long
   inline run with `VIBE_MODEL_SMALL` unset bills at the orchestrator's
   model; the skill prints commit count and a cost note before starting.
7. Test tautology — every structural test carries a semantic assertion
   (render differs per harness at every block; tokens consumed; `full` rows
   cite a test); reviewers check new tests against ledger 12.

## Rulings (Colton, 2026-09-08)

1. **Approach approved:** source templates rendered to committed per-harness
   files, one harness policy module, parity registry with the CI completeness
   gate.
2. **Model tiers:** explicit pin always; Codex ships **default model names**
   for both tiers so first install is low-friction; `VIBE_MODEL_*` are
   overrides only.
3. **Curation shape:** flat by default with adaptive fan-out when the user
   has raised `agents.max_depth`.
4. **Scope:** backfill on Codex — yes; update nudge on Codex — yes; Plan
   agent as an **installed role file** in `~/.codex/agents/` (not a skill).

Progress: **WP-P0 shipped** (8fc5230) and **WP-P1 shipped** (d80f16e), both 2026-09-08 and sonnet-reviewed (briefs `docs/wp-p0-harness-brief.md`, `docs/wp-p1-containment-brief.md`). **WP-P3 shipped** (same day; brief `docs/wp-p3-update-plan-brief.md`). **WP-P2 built** (v0.36.0; live Phase 4 (curation) gate pending on Colton's machine). Remaining: WP-P4 institutionalization.
