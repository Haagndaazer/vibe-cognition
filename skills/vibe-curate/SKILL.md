---
description: Use this skill after adding new cognitive episodes and entities to the graph to curate and organize the new memories. Curate the cognition graph — create semantic edges between related nodes and identify clusters of connected knowledge. Use when the graph has edgeless nodes that need linking or after significant new nodes have been added.
---

# Curate Edges — Cognition Graph Curation

## What This Does

Analyzes uncurated nodes, proposes and creates semantic relationships (led_to, resolved_by, supersedes), then identifies clusters of densely-connected nodes and creates summary nodes for them.

Deterministic `part_of` edges are the only edges created automatically (on record). This skill is the agent's **semantic** curation pass — the way semantic edges get created — reviewing nodes that haven't been processed by this skill yet, even if they already have their automatic `part_of` edges.

## When to Use

- After running `/vibe-backfill` (many new episode nodes without semantic edges)
- When `get_status` shows **10 or more** uncurated nodes
- After any session that recorded **3 or more** related nodes
- When the user asks about graph health or curation

## Before you start: cost, scope, and concurrency

The edge-analyzer and cluster-analyzer subagents both call MCP tools themselves
(`cognition_search`, which needs the embedding model). If `get_status`'s
`embedding_status` is `loading` or `syncing`, subagent search calls may return the
`loading_embeddings` error for a few seconds — wait for `ready`, or proceed with
structural-only context (`cognition_get_neighbors`, `cognition_get_history`) if the
work can't wait.

Before fanning out, tell the user (a one-line note, not a prompt for approval — curation
proceeds autonomously either way): **"Curating N uncurated nodes -> about M
edge-analyzer subagent batches."** (M = ceil(N / 10), since batches are 5-10 nodes.)

**Concurrency:** `cognition_get_uncurated_nodes` is a stateless read — it doesn't claim
or lock nodes. If two agents run `/vibe-curate` at the same time on the same graph, both
will see the same uncurated nodes and do duplicate subagent analysis (wasted LLM work);
the `(from, to, edge_type)` idempotency key on edge creation prevents the WASTE from
becoming duplicate edges, but not the redundant analysis itself. If you know a teammate
is also curating, check their status via teammate-comms (if available) before starting,
or accept the waste knowingly rather than being surprised by it.

## Workflow

### Step 1: Assess

```
1. Call get_status — note total nodes, edges, edge type breakdown, uncurated count, and embedding_status
2. Call cognition_get_uncurated_nodes(limit=500) — get nodes not yet reviewed by this skill
3. If 0 uncurated nodes → report "graph is fully curated" and stop
4. Log: "{N} uncurated nodes found, starting curation" + the preflight cost note above
```

### Step 2: Edge Curation

Process uncurated nodes in batches of 5-10 (timestamp order, oldest first).

For each batch:
1. Launch the **edge-analyzer** subagent (its prompt is in `edge-analyzer.md`, alongside this SKILL.md in the skill's own directory)
   - **ALWAYS spawn this subagent with the Haiku model** (e.g. `model: "haiku"` on the Agent call). Do NOT let it inherit the main instance's model — edge analysis is a high-volume, mechanical fan-out and running it on Opus/Sonnet is extremely wasteful. Every edge-analyzer invocation MUST be Haiku.
   - Pass the node IDs as a list in the prompt
   - The subagent calls MCP tools itself to gather context
   - It returns proposed edges as a JSON list, each with a `source` field (see edge-analyzer.md's output schema)
2. Review the proposals:
   - Remove any self-references (from_id == to_id)
   - Remove any `duplicate_of` proposals (retired — not a valid edge type; propose
     `supersedes` for genuine duplicates instead)
   - Remove any `part_of` proposals for TASK nodes specifically (collides with the
     authoritative task-parent edge — see Task nodes below); other `part_of` proposals
     are simply redundant with the deterministic matcher, not harmful, but still prefer
     discarding them since this skill's job is semantic edges
   - Discard proposals with vague reasons ("related" without specifics)
3. Commit approved edges via `cognition_add_edges_batch` — each edge object in the JSON
   array carries its OWN `"source": "curate-skill"` key (source is a per-edge field
   inside each array element, not a separate argument to the tool call):
   ```json
   [{"from_id": "...", "to_id": "...", "edge_type": "led_to", "source": "curate-skill"}]
   ```
4. Mark ALL nodes in the batch as curated via `cognition_mark_curated` with their IDs
   — including nodes where no edges were created (they were still reviewed)

Repeat for all batches until all uncurated nodes are processed.

Log: "{N} edges created across {M} batches, {K} proposals discarded"

#### Task nodes

`task` nodes appear in the uncurated worklist like any other node — no special fetch
needed. When you encounter one, prefer these semantic links:

- A task `relates_to` the `decision`, `discovery`, or `pattern` it implements or acts on.
- A **done** task is `resolved_by` (or `led_to`) the `episode` that closed it.
- **Never propose `part_of` for a task.** A task's parent hierarchy is an EXPLICIT
  `part_of` edge set at creation/re-parent time (`cognition_add_task` /
  `cognition_update_task`). This is a SKILL-LEVEL rule, not a tool-level block — the
  tools don't reject a task `part_of` edge, so nothing stops you from proposing one by
  mistake; it's on you not to. Doing so anyway would collide with the authoritative
  `task-parent` edge (two `part_of` edges from the same task, one right and one wrong).

### Step 3: Cluster Identification

After all edges are committed:

1. Launch the **cluster-analyzer** subagent (its prompt is in `cluster-analyzer.md`, alongside this SKILL.md in the skill's own directory)
   - **ALWAYS spawn this subagent with the Haiku model** (`model: "haiku"` on the Agent call). Do NOT let it inherit the main instance's model — running cluster analysis on Opus/Sonnet is wasteful.
   - It analyzes the graph for densely-connected groups
   - Returns proposed summary nodes (pattern or episode type)
2. Review each proposed summary node:
   - Check it doesn't duplicate an existing pattern/episode with similar summary
   - Verify the member nodes actually exist and are connected
3. Create approved summary nodes via `cognition_record`
4. For each summary node, create `part_of` edges from member nodes using `cognition_add_edges_batch`

Log: "{N} clusters identified, {M} summary nodes created"

### Step 4: Report

Summarize the full run — this is the ONLY in-chat surface for what curation did; a user
who never opens the dashboard should still be able to see what got connected and why:

- Uncurated nodes: before → after
- **The actual edges created, narrated by content** — not just a count. For each
  committed edge (or a representative sample if there were many): "`<id-a>`
  `<edge_type>` `<id-b>`: `<reason>`" using the reason text the edge-analyzer proposed.
  A stats-only report ("12 edges created") is not sufficient — name what connected.
- Nodes reviewed with no edges created
- Clusters identified, and for each: its title and member count (from cluster-analyzer's
  proposal)
- Summary nodes created, with their ids

## Key Rules

- **Both the edge-analyzer AND cluster-analyzer subagents MUST run on Haiku** — pass `model: "haiku"` on every Agent call. Never let them inherit the main instance's (Opus/Sonnet) model; doing so is needless token waste on these fan-out tasks.
- Process ALL uncurated nodes, not just the first batch
- Review and commit autonomously — no user approval needed
- If a subagent returns poor-quality proposals, discard them rather than committing noise
- Bad edges are worse than missing edges — when in doubt, skip
