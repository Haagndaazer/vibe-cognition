# WP-P3 — Update nudge on Codex, env-preserving re-registration, Plan role

Status: BRIEF (solo build; sonnet code review before "done")
Parent: `docs/codex-parity-plan.md` rev 3 (rulings: update nudge in scope;
Plan agent as an installed role file in `~/.codex/agents/`)
Date: 2026-09-08

## Scope

1. **Harness-keyed update check.** `harness.py` gains `manifest_path`
   (`.claude-plugin/plugin.json` / `.codex-plugin/plugin.json`) and
   `marketplace_manifest_path` (`.claude-plugin/marketplace.json` /
   `.agents/plugins/marketplace.json`). `update_check.py` reads the installed
   version from the harness manifest, fetches the colton marketplace file for
   the harness, reads `plugins[].source.sha` (same shape in both files),
   fetches that commit's harness manifest, compares version strings, and emits
   the harness CTA (already in `harness.py`). The Codex wrapper stops forcing
   `VIBE_UPDATE_NUDGE=off`.
2. **Env-preserving re-registration.** Before `codex mcp add`, the Codex
   wrapper reads the current entry with `codex mcp get vibe-cognition --json`
   (shape confirmed live: `transport.env` map) and re-passes every env key
   that is not one of the three managed ones (`UV_PROJECT_ENVIRONMENT`,
   `VIBE_DATA_DIR`, `VIBE_HARNESS`). JSON is parsed with the plugin venv's
   Python when it exists; on a first install (no venv, no prior entry) there
   is nothing to preserve and the step is skipped. The desired-entry stamp
   does not include user keys, so user edits never trigger re-registration.
3. **Plan role.** `tools/render_harness.py` renders
   `adapters/codex/agents/vibe-plan.toml` from `agents-src/plan.md`
   (`name = "vibe-plan"`, `description`, `developer_instructions` = body with
   Codex tokens; `model` line only when the Codex mid tier has a default).
   The Codex wrapper installs it into `${CODEX_HOME:-$HOME/.codex}/agents/
   vibe-plan.toml` when missing or different (byte compare), so
   `spawn_agent(agent_type="vibe-plan")` works. The Claude `agents/plan.md`
   render is unchanged.
4. Registry/docs: `agent:plan`/codex → `full` with the render test as
   evidence; README Codex section mentions the Plan role and update nudge.

## Acceptance criteria

- AC1 Under `VIBE_HARNESS=codex`, `update_check.check()` fetches the Codex
  marketplace path and the `.codex-plugin/plugin.json` at the pinned sha,
  reads the installed version from `.codex-plugin/plugin.json`, and the nudge
  text contains `codex plugin marketplace upgrade <market>` and
  `codex plugin add vibe-cognition@<market>`. Claude behavior unchanged
  (existing exact-text test still passes).
- AC2 Shell test: with a fake `codex` whose `mcp get --json` returns an env
  containing `VIBE_MODEL_MID=x`, a re-registration's `mcp add` call carries
  `--env VIBE_MODEL_MID=x` plus the three managed keys; without a prior
  entry, only the three.
- AC3 Shell test: the wrapper writes `vibe-plan.toml` into a fake
  `CODEX_HOME/agents/` on first run, rewrites it when the rendered content
  changes, and leaves it alone when identical (breadcrumb shows skip).
- AC4 `render_harness --check` covers the TOML; forbidden-literal lint still
  passes; `parity` gate green.
- AC5 Full suite green (modulo the two known lifecycle failures), ruff clean.

## KNOWN-INTENTIONAL

- Codex plugin updates require `codex plugin add` again after the marketplace
  refresh; the CTA says so. No automatic update.
- The role file is written into the user's Codex config dir by the hook
  (ruling `004232949d8c`); uninstall docs tell the user to delete it.
- The Plan role carries no tool whitelist (Codex roles cannot restrict tools);
  its read-only discipline is prompt-only there, as on Codex generally.
