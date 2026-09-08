---
description: Launch the background curator that links new memories — semantic edges (led_to, resolved_by, supersedes, contradicts, relates_to) and cluster summary nodes. Use after recording new nodes, or when the graph has uncurated nodes. Curation itself runs in a background curate-orchestrator agent; this skill's only job is to launch it and report back.
---

# Curate — Launch the Background Curator

## What This Does

{{harness:claude-code}}Launches `vibe-cognition:curate-orchestrator` in the background to process every uncurated node:{{/harness}}{{harness:codex}}Launches the curate-orchestrator (its instructions are `references/curate-orchestrator.md` in this skill's directory) as a `spawn_agent` subagent to process every uncurated node:{{/harness}} it proposes and commits semantic edges, then identifies clusters and creates summary nodes. This skill itself does none of that work — it's a thin launcher. The orchestrator finalizes everything itself and reports back only success/failure + bare counts; detailed narration of what connected to what lives in its own transcript and the graph/dashboard, not in this skill's output.

## When to Use

- After recording new nodes this turn (the standing `{{invoke:vibe-cognition}}` rule: never finish a recording turn without triggering this)
- When `get_status` shows `uncurated` > 0
- When the user asks about graph health or curation

## Workflow

1. Call `get_status`. If `uncurated` is 0, tell the user "graph is fully curated" and stop — do not launch anything.
2. **Don't-double-launch guard:** if a curation run was already launched this session and hasn't yet reported completion, don't launch another. Background mode widens the concurrency window — nodes recorded mid-run just stay uncurated until the next launch, which is fine; launching a second orchestrator on top of a still-running one just wastes tokens re-analyzing an overlapping worklist.
{{harness:claude-code}}3. Launch `vibe-cognition:curate-orchestrator` via the {{spawn_tool}} with `run_in_background: true` and an explicit `model: "{{model:mid}}"` override — always pass it, regardless of the orchestrator's own frontmatter pin. **Hard rule:** the orchestrator MUST run on {{model:mid}} via this explicit param, never pin-reliance alone (fail f09e770da046) — this cuts both ways, since without the param a pin failure means inheriting the *launching* session's model, which on an Opus/Fable main makes curation drastically more expensive, while a silent downgrade guts review quality. **Second hard rule:** pass ONLY `subagent_type`, `model`, `run_in_background`, `description`, and `prompt` — NEVER a `name` param. A named launch registers a persistent teammate-mailbox agent instead of a background subagent: it can stall in the mailbox queue before ever starting, and it leaves a stale roster entry behind after the run. `run_in_background: true` is correct for THIS launch only — the launching session is the one that should receive the completion notification (verified for top-level sessions; nested subordinate-launches-subordinate topologies are unverified) — while the orchestrator's own analyzer spawns are foreground-only per its SPAWN DISCIPLINE rule.
{{/harness}}{{harness:codex}}3. Read `references/curate-orchestrator.md` from this skill's directory. Launch the orchestrator with `spawn_agent`: `task_name: "vibe_curate"`, `fork_turns: "none"` (required with a model override), `model: "{{model:mid}}"` — always pass the model explicitly, never let it inherit this session's model (fail f09e770da046 precedent) — and `message` = the full reference text followed by one line: `Uncurated nodes at launch: {N}.` Do NOT pass `agent_type`. Do NOT `wait_agent` on it: the orchestrator's final counts arrive later as a FINAL_ANSWER message from `/root/vibe_curate`; carry on with other work meanwhile. If `spawn_agent` rejects the model name, relay Codex's "Unknown model … Available models" error verbatim and tell the user to set `VIBE_MODEL_MID` on the `vibe-cognition` MCP entry (`codex mcp get vibe-cognition --json` shows the current env); never retry with a guessed name.
{{/harness}}4. Tell the user: "Curation launched in background — {N} uncurated nodes. Completion will be reported when it finishes; ground truth in the meantime is `get_status`'s uncurated count."

## Concurrency

If two agents/sessions launch curation on the same graph around the same time, both orchestrators will see overlapping uncurated worklists and do duplicate subagent analysis (wasted tokens) — the `(from, to, edge_type)` idempotency key on edge creation prevents the waste from becoming duplicate edges, but not the redundant analysis itself. If you know a teammate is also curating, check their status via teammate-comms (if available) before launching, or accept the waste knowingly rather than being surprised by it.

## What NOT to do

Do not attempt any of the orchestrator's work yourself. `cognition_add_edge`, `cognition_add_edges_batch`, and `cognition_mark_curated` are reserved for the curate-orchestrator agent this skill launches — if you need edges created, launch this skill; don't hand-author them.
