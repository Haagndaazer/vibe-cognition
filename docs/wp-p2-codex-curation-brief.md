# WP-P2 — Curation and backfill on Codex (flat by default, adaptive fan-out)

Status: BRIEF rev 2 — spike DONE 2026-09-08 (`docs/codex-spike-report-p2.md`): nested spawning WORKS on Colton's Codex (V2 surface), completion is PUSHED as FINAL_ANSWER, model override needs `fork_turns: "none"`; Codex tier defaults set (luna/sol). BUILT 2026-09-08 — sonnet code review APPROVE-WITH-CHANGES, all five items applied; shipped in v0.36.0 pending the live Phase 4 (curation) gate
Parent: `docs/codex-parity-plan.md` rev 3 (rulings: flat/adaptive shape;
default model names shipped in `harness.py`; backfill in scope)
Depends on: WP-P0 (templates), WP-P1 (curation token), WP-P3 (env-preserving
re-register so `VIBE_MODEL_*` overrides survive)
Date: 2026-09-08

## Spike results (2026-09-08) and what they change

- Nested spawn succeeded (`/root/depth_probe/say_hi`): the Claude fan-out
  shape transfers to Codex as-is. The inline "embedded analyzer protocol"
  stays as the fallback for a V1 install that returns the depth error.
- Subagent completion is pushed to the parent as a `FINAL_ANSWER` message
  between tool calls; no `wait_agent`/`list_agents` was needed. The Codex
  orchestrator collects analyzer output by `wait_agent(task_name)` when
  available, else by making a cheap call (`get_status`) and reading the
  pushed message — it never assumes the JSON is in the spawn's own result.
- Every Codex spawn passes `task_name`, `fork_turns: "none"` (required with a
  model override), `model`, and `message`. No `subagent_type`,
  `run_in_background`, or `name`.
- Warm read-only tool calls cost 2.3–3.5 s each on Codex; a 10-node batch
  inline would be minutes, another reason fan-out is preferred when allowed.

## What already exists that P2 reuses

- `agents-src/curate-orchestrator.md` already contains a **degraded /
  no-nesting fallback** and an **Embedded analyzer protocol** section: when
  the spawn tool is unavailable the orchestrator performs edge, conflict, and
  cluster analysis inline. On Codex the trigger is different (a stock install
  refuses a subagent's spawn with "Agent depth limit reached") but the
  behavior is the same. P2 does not write a second pipeline; it renders the
  existing one for Codex with harness blocks around the spawn mechanics.
- The curation token (P1) is the containment on Codex; the orchestrator
  template already begins a session and passes the token at every write.
- `get_status.harness.models` (P0) tells the launcher which model to pin.

## Scope

1. **Model defaults.** DONE (5b66103): small `gpt-5.6-luna`, mid
   `gpt-5.6-sol`, from the live list.
2. **Codex render of `vibe-curate`.** Harness blocks replace the Claude launch
   step: call `get_status`, read `harness.models.mid`, `spawn_agent` with
   `message` = the orchestrator reference (see 4) + the uncurated count,
   `model` = mid tier, no `wait_agent`; refuse with a clear message if the mid
   tier resolves to nothing (cannot happen once defaults ship, but the text
   stays for the `inherit` opt-in). Keep the don't-double-launch and
   concurrency sections; swap the Claude-only spawn parameter names.
3. **Codex render of `vibe-backfill`.** Same shape: the launcher spawns one
   backfill orchestrator (mid tier) that walks chunks itself and spawns
   per-chunk workers (small tier) only when the depth limit allows; prints
   the commit count and a one-line cost note before starting.
4. **Reference files.** New renderer target: `agents-src/*.md` bodies →
   `adapters/codex/skills/vibe-curate/references/<agent>.md` (orchestrator,
   edge/conflict/cluster analyzers) and
   `adapters/codex/skills/vibe-backfill/references/worker.md` (new source
   `agents-src/backfill-worker.md`, extracted from the skill's per-commit
   workflow so Claude's skill text and the Codex worker share one source).
   Frontmatter is dropped in references; `{{harness:codex}}` blocks give the
   orchestrator its Codex spawn syntax (`spawn_agent(message=<analyzer
   reference + batch ids>, model=<small>)`), and the analyzer-spawn steps
   gain an adaptive clause: on "Agent depth limit reached", switch to the
   embedded protocol for the rest of the run and note `fan_out: unavailable`
   in the final counts.
5. **Manifest.** `.codex-plugin/plugin.json` skills list adds
   `./adapters/codex/skills/vibe-curate` and `./adapters/codex/skills/vibe-backfill`;
   `CODEX_SKILLS` in the renderer grows accordingly; the Codex
   `vibe-cognition` skill's "curation is not available on Codex yet" blocks
   flip to "run `$vibe-curate`" (the harness table's `curation_available`
   becomes true for Codex, which also switches `instructions.py` and the
   token refusal text).
6. **Test brief Phase 5.** Record three nodes, run `$vibe-curate`, watch
   `uncurated` fall to 0 and `edge_sources` gain `curate-skill`; repeat once
   with `agents.max_depth = 2` in `~/.codex/config.toml` to exercise fan-out
   and confirm `fan_out` reporting; run `$vibe-backfill` on a repo with a
   few untracked commits.

## Spike (before build; live on Colton's machine)

1. In a Codex session, list the available models (the `/model` picker or
   the "Unknown model … Available models" error) and choose small/mid.
2. Confirm a root-session `spawn_agent` with an explicit `model` succeeds and
   a nested spawn from that agent returns the depth error verbatim.
3. Confirm one inline curation pass over ~10 nodes completes within Codex's
   per-tool timeout (300 s in source) — the inline mode issues many tool
   calls, each short; no single call should approach the limit.
4. Confirm how the root thread observes the orchestrator finishing (polling
   `get_status.uncurated` is the assumed contract).

## Acceptance criteria

- AC1 `harness.py` Codex defaults set; `render_harness --check` and the
  forbidden-literal lint pass; Codex renders of `vibe-curate`/`vibe-backfill`
  contain no `subagent_type`/`run_in_background`/`Agent tool` text.
- AC2 Reference files render from the same sources as `agents/*.md`; a drift
  test covers them; the orchestrator reference names the depth error text and
  the adaptive fallback.
- AC3 `curation_available` true for Codex flips the cognition skill blocks,
  `instructions.py`, and the token refusal text; tests updated (the Codex
  refusal test now expects the `$vibe-curate` invocation).
- AC4 Phase 5 of the test brief passes live: `uncurated` reaches 0 with
  `edge_sources.curate-skill` incremented and `curation_sessions.writes > 0`;
  with `agents.max_depth = 2`, fan-out is exercised.
- AC5 Full suite green (modulo the two known lifecycle failures); ruff clean;
  `parity.json` rows for `skill:vibe-curate`, `skill:vibe-backfill`, and the
  four agents flip to `full` (Codex) citing the Phase 5 gate episode, with
  tiering recorded as `partial` in the note (flat by default).

## KNOWN-INTENTIONAL

- Codex tiering is flat by default; fan-out only when the user raises
  `agents.max_depth`. Recorded in PARITY.md.
- Containment on Codex is the token (friction + audit), not a tool boundary.
- No completion notification is assumed; `get_status.uncurated` is ground
  truth on both harnesses.
- Backfill cost note prints whenever `VIBE_MODEL_SMALL` is unset and the
  default small tier is used, so the user sees the model that will run.
