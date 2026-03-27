---
description: Use this skill after adding new cognitive episodes and entities to the graph to curate and organize the new memories. Curate the cognition graph — create semantic edges between related nodes and identify clusters of connected knowledge. Use when the graph has edgeless nodes that need linking or after significant new nodes have been added.
---

# Curate Edges — Cognition Graph Curation

## What This Does

Analyzes uncurated nodes, proposes and creates semantic relationships (led_to, resolved_by, supersedes), then identifies clusters of densely-connected nodes and creates summary nodes for them.

Deterministic `part_of` edges and background-curator edges are created automatically. This skill handles the **quality semantic** curation pass — reviewing nodes that haven't been processed by this skill yet, even if they already have automatic edges.

## When to Use

- After running `/vibe-backfill` (many new episode nodes without semantic edges)
- When `get_status` shows a high number of uncurated nodes
- After recording several related nodes in a session
- When the user asks about graph health or curation

## Workflow

### Step 1: Assess

```
1. Call get_status — note total nodes, edges, edge type breakdown, and uncurated count
2. Call cognition_get_uncurated_nodes(limit=500) — get nodes not yet reviewed by this skill
3. If 0 uncurated nodes → report "graph is fully curated" and stop
4. Log: "{N} uncurated nodes found, starting curation"
```

### Step 2: Edge Curation

Process uncurated nodes in batches of 5-10 (timestamp order, oldest first).

For each batch:
1. Launch the **edge-analyzer** subagent (see `skills/vibe-curate/edge-analyzer.md`)
   - Pass the node IDs as a list in the prompt
   - The subagent calls MCP tools itself to gather context
   - It returns proposed edges as a JSON list
2. Review the proposals:
   - Remove any self-references (from_id == to_id)
   - Remove any `part_of` or `duplicate_of` proposals (not allowed)
   - Discard proposals with vague reasons ("related" without specifics)
3. Commit approved edges via `cognition_add_edges_batch` with `source: "curate-skill"`
4. Mark ALL nodes in the batch as curated via `cognition_mark_curated` with their IDs
   — including nodes where no edges were created (they were still reviewed)

Repeat for all batches until all uncurated nodes are processed.

Log: "{N} edges created across {M} batches, {K} proposals discarded"

### Step 3: Cluster Identification

After all edges are committed:

1. Launch the **cluster-analyzer** subagent (see `skills/vibe-curate/cluster-analyzer.md`)
   - It analyzes the graph for densely-connected groups
   - Returns proposed summary nodes (pattern or episode type)
2. Review each proposed summary node:
   - Check it doesn't duplicate an existing pattern/episode with similar summary
   - Verify the member nodes actually exist and are connected
3. Create approved summary nodes via `cognition_record`
4. For each summary node, create `part_of` edges from member nodes using `cognition_add_edges_batch`

Log: "{N} clusters identified, {M} summary nodes created"

### Step 4: Report

Summarize the full run:
- Uncurated nodes: before → after
- Edges created (by type)
- Nodes reviewed with no edges created
- Clusters identified
- Summary nodes created

## Key Rules

- Process ALL uncurated nodes, not just the first batch
- Review and commit autonomously — no user approval needed
- If a subagent returns poor-quality proposals, discard them rather than committing noise
- Bad edges are worse than missing edges — when in doubt, skip
