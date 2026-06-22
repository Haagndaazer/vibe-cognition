# WP-Plan-XP-Discovery — Plan agent learns cross-project memory + sibling discovery

**Status:** spec FINAL — sonnet-reviewed (3 blockers) + claude-code-guide consult; all resolved
(B1 prefix fix/S-2, B2 label→path bridge, B3 silent-drop safe). Ready for Vorpid.
**Owner (impl):** Vorpid. **Gate:** full WP protocol. Vince does not write code.
**Also closes:** BACKLOG S-2 (broken `mcp__vibe-cognition__*` prefix in agents/plan.md).
**Release:** folds into the held v0.10.0 (independent of the git-hygiene WP — low file overlap;
touches `agents/plan.md`, not `src/`).

## Goal

The `vibe-cognition:Plan` agent (defined in `agents/plan.md`) currently only searches the
HOME project's graph — its `tools:` list has just `cognition_search/get_chain/get_history/
get_neighbors/get_status`. Teach it the cross-project memory capability that shipped in
v0.9.0 (XP1/XP2): it can attach OTHER project directories' cognition graphs read-only and
search them, and — when teammate-comms is installed — discover which sibling projects on
this machine might align and hold useful memory, before loading them.

## Changes (content only — `agents/plan.md`, an agent-definition markdown)

**A. Frontmatter `tools:` — FIX the broken prefix (closes S-2) AND add the cross-project tools.**
*(B1, confirmed by claude-code-guide vs Claude Code docs):* a PLUGIN-provided MCP server's tools
resolve as `mcp__plugin_<plugin>_<server>__<tool>`. This repo's MCP server is plugin-declared, so
the correct prefix is `mcp__plugin_vibe-cognition_vibe-cognition__`. The agent file's current
`mcp__vibe-cognition__*` names (all 5 cognition tools) are the OLD `.mcp.json` style and DO NOT
RESOLVE — this is open BACKLOG item S-2. So this WP:
  - rewrites the 5 existing cognition tool names to the `mcp__plugin_vibe-cognition_vibe-cognition__`
    prefix (closing S-2), and
  - ADDS, with the same correct prefix: `..._cognition_load_project`, `..._cognition_unload_project`,
    `..._cognition_list_projects`.
  These attach a foreign graph read-only and never modify the home codebase — compatible with the
  agent's READ-ONLY-files contract (note explicitly: load/unload mutate the server's in-memory
  registry, NOT files). **Verify the exact resolvable names in a live session before writing** (the
  prefix is authoritative per docs, but confirm the server key spelling).

**B. Frontmatter `tools:` — add teammate-comms project discovery (safe even if absent).**
Add `mcp__plugin_teammate-comms_teammate-comms__list_projects` (and/or `..._teammate_list`).
*(B3, RESOLVED by claude-code-guide):* an unavailable tool in `tools:` is **silently dropped at
load time** — the agent still loads and works for users WITHOUT teammate-comms. So listing it is
safe; no frontmatter regression risk. The body still instructs graceful fallback (no teammate-comms
→ use `cognition_list_projects` or plan home-only).

**C. Update frontmatter `description`** (exact one-line replacement — keep it tight so the picker
doesn't wrap badly):
  `Enhanced Plan agent with Vibe Cognition. Software architect for implementation plans using semantic search over this project's decisions, discoveries, patterns, and failures -- and, when a sibling project on this machine is relevant, its graph too. Use for planning features, analyzing architecture, and designing solutions.`

**D. New body section "Cross-project memory (when it helps)"** after the existing tool docs.
Spell out the discipline:
  1. **When to reach for it:** only when the plan's domain plausibly overlaps a sibling project
     (shared stack, shared subsystem, a pattern likely solved elsewhere). Not by default — most
     plans are home-only. **Hard cap: load at most 2 foreign graphs per plan** (cost + noise).
  2. **Discover candidates:** if teammate-comms is installed, `list_projects` /
     `teammate_list(all=True)` enumerates sibling projects on this machine (entries carry a
     "project" LABEL like "Projects/foo", not a path); judge alignment by name/domain. If absent,
     use `cognition_list_projects` (already-loaded graphs) or skip.
  3. **Resolve label → absolute path, then attach** *(B2, RESOLVED):* `cognition_load_project`
     takes a filesystem PATH whose dir contains `.cognition/journal.jsonl`; teammate-comms gives a
     LABEL, not a path. Bridge via the filesystem: the home repo's own dir gives the sibling parent
     (e.g. cwd `.../Documents/Projects/vibe-cognition` → siblings under `.../Documents/Projects/`);
     resolve a chosen label to `<parent>/<leaf>` with Glob/Bash and CONFIRM `.cognition/journal.jsonl`
     exists there before calling `cognition_load_project(<abs_path>)`. teammate-comms is the
     relevance signal; the path comes from disk. Skip any candidate you can't resolve to a real
     `.cognition/journal.jsonl`.
  4. **Search with `project=`:** pass `project="<tag>"` to `cognition_search`/`get_history`
     (or `project="*"` to fan across all loaded) to pull the sibling's decisions/patterns/failures.
  5. **Relevance-gate + cite:** only fold in genuinely transferable knowledge; **port the category,
     not the command** — e.g. "sibling used Redis as a rate-limit store" is reusable as a pattern;
     their specific config/version is NOT. Attribute cross-project insights to their source project.
  6. **Unload UNCONDITIONALLY before returning:** `cognition_unload_project(<tag>)` for every graph
     you loaded — even if the plan is incomplete or you hit an error. Leave the registry as you
     found it.
  7. **Degraded semantic search:** a foreign graph may be guarded off (model/dim mismatch) →
     `project_notes` says so; structural reads still work and "no hits" != "no history."
  Keep the agent's READ-ONLY contract intact — this section adds graph attaches, NOT file writes.

**D2. Wire it into "=== YOUR PROCESS ===" (Nit 1):** add a sub-step under step 2 ("Explore
Thoroughly"), e.g. "If the task's domain plausibly overlaps a sibling project, consult the
Cross-project memory section before designing." A standalone section the process doesn't reference
gets skipped — the process is the agent's checklist.

**E. Out of scope:** no `src/` changes (the XP tools already exist + are tested since v0.9.0);
no new MCP tools; the analogous `/vibe-cognition` SKILL.md already documents the XP tools (from
WP-XP-Docs) — only touch it if the doc-drift GUARD requires it (it should not, since this WP
registers no new tools).

## Acceptance

- `agents/plan.md` frontmatter is valid (Claude Code parses the agent; tool names resolve to
  real registered tools — verify the teammate-comms tool name string is exactly right).
- The agent, given a planning task with a likely sibling overlap, discovers + loads + searches a
  foreign graph and unloads it; given no teammate-comms, it plans home-only without erroring.
- `uv run ruff/pyright/pytest` unaffected (no code change); doc-drift GUARD still green.
- Manual sanity (human): run the Plan agent on a task and confirm it can surface sibling memory.

## Known-intentional (do NOT "fix")

- The agent is deliberately READ-ONLY on files; load/unload are allowed because they don't write
  files. Do not add Write/Edit.
- Cross-project search is OPT-IN per plan, not automatic — do not make the agent load every
  sibling graph on every run (cost + noise). The "when it helps" gate is intentional.
