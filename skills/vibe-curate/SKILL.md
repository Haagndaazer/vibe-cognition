---
description: Launch the background curator that links new memories — semantic edges (led_to, resolved_by, supersedes, contradicts, relates_to) and cluster summary nodes. Use after recording new nodes, or when the graph has uncurated nodes. Curation itself runs in a background curate-orchestrator agent; this skill's only job is to launch it and report back.
---

# Curate — Launch the Background Curator

## What This Does

Launches `vibe-cognition:curate-orchestrator` in the background to process every uncurated node: it proposes and commits semantic edges, then identifies clusters and creates summary nodes. This skill itself does none of that work — it's a thin launcher. The orchestrator finalizes everything itself and reports back only success/failure + bare counts; detailed narration of what connected to what lives in its own transcript and the graph/dashboard, not in this skill's output.

## When to Use

- After recording new nodes this turn (the standing `/vibe-cognition` rule: never finish a recording turn without triggering this)
- When `get_status` shows `uncurated` > 0
- When the user asks about graph health or curation

## Workflow

1. Call `get_status`. If `uncurated` is 0, tell the user "graph is fully curated" and stop — do not launch anything.
2. **Don't-double-launch guard:** if a curation run was already launched this session and hasn't yet reported completion, don't launch another. Background mode widens the concurrency window — nodes recorded mid-run just stay uncurated until the next launch, which is fine; launching a second orchestrator on top of a still-running one just wastes tokens re-analyzing an overlapping worklist.
3. Launch `vibe-cognition:curate-orchestrator` via the Agent tool with `run_in_background: true` and an explicit `model: "sonnet"` override — always pass it, regardless of the orchestrator's own frontmatter pin. **Hard rule:** the orchestrator MUST run on sonnet via this explicit param, never pin-reliance alone (fail f09e770da046) — this cuts both ways, since without the param a pin failure means inheriting the *launching* session's model, which on an Opus/Fable main makes curation drastically more expensive, while a silent downgrade guts review quality.
4. Tell the user: "Curation launched in background — {N} uncurated nodes. Completion will be reported when it finishes; ground truth in the meantime is `get_status`'s uncurated count."

## Concurrency

If two agents/sessions launch curation on the same graph around the same time, both orchestrators will see overlapping uncurated worklists and do duplicate subagent analysis (wasted tokens) — the `(from, to, edge_type)` idempotency key on edge creation prevents the waste from becoming duplicate edges, but not the redundant analysis itself. If you know a teammate is also curating, check their status via teammate-comms (if available) before launching, or accept the waste knowingly rather than being surprised by it.

## What NOT to do

Do not attempt any of the orchestrator's work yourself. `cognition_add_edge`, `cognition_add_edges_batch`, and `cognition_mark_curated` are reserved for the curate-orchestrator agent this skill launches — if you need edges created, launch this skill; don't hand-author them.
