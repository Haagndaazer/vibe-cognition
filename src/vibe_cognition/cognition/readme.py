"""Canonical orientation content for vibe-cognition — ASCII-only, stdlib-only.

Imported by prime.py (the JSON-to-stdout SessionStart hook path) and by the
cognition_readme MCP tool. No third-party deps; no runtime file reads.
"""

COGNITION_GUIDE = """\
# Vibe Cognition

Vibe Cognition is the project knowledge graph for this repo. It is already active
(the plugin is installed and the server is running). Every insight, decision, failure,
and pattern you capture here persists across sessions and is searchable via embeddings.

## The core loop

1. Record as you work -- call cognition_record whenever you make a decision, hit a
   failure, discover something non-obvious, or notice a reusable pattern.
2. Curate after recording -- run /vibe-curate to add semantic edges (led_to,
   resolved_by, supersedes, contradicts, relates_to) that connect new nodes to the
   existing graph. Deterministic part_of edges are created automatically.

## Tool groups

| Group | Tools |
|-------|-------|
| Record | cognition_record, cognition_update_node, cognition_remove_node |
| Search | cognition_search |
| History | cognition_get_history, cognition_get_node, cognition_get_chain, |
|         | cognition_get_superseded_chain, cognition_get_incident_resolution, |
|         | cognition_get_neighbors |
| Curate | cognition_add_edge, cognition_add_edges_batch, cognition_remove_edge, |
|        | cognition_get_edgeless_nodes, cognition_get_uncurated_nodes, |
|        | cognition_mark_curated |
| Document | cognition_store_document, cognition_get_document |
| Cross-project | cognition_load_project, cognition_unload_project, |
|               | cognition_list_projects (use the project= arg on read/search tools) |
| Service | get_status, cognition_dashboard, cognition_readme, cognition_reload |

## Node types

Entities (concise searchable facts -- summary max 250 chars):
  decision, fail, discovery, assumption, constraint, incident, pattern

Episodes (full narrative of a completed body of work):
  episode

Documents (stored files with text sidecar for search):
  document -- use the /vibe-document skill

## Edge types

  part_of (auto), led_to, resolved_by, supersedes, contradicts, relates_to, duplicate_of (reserved)

## When to record

- You make an architectural or implementation decision (with rejected alternatives).
- You hit a failure or bug that took non-trivial time to understand.
- You discover something non-obvious that will matter again.
- You identify a reusable pattern or anti-pattern.
- You complete a body of work (record an episode to anchor the entities).
- You observe a constraint that others must respect.

## Cross-project reads

Load a foreign project with cognition_load_project, then pass project="<tag>" (or
project="*" for fan-and-merge on aggregates) to cognition_search, cognition_get_history,
cognition_get_edgeless_nodes, and cognition_get_uncurated_nodes. Single-node tools
(get_node, get_chain, etc.) reject "*" -- node ids are not project-namespaced.
"""

COGNITION_GETTING_STARTED = """\
## Getting started on this project

The graph for this project is empty. Here is the act-now procedure:

1. Run /vibe-cognition to load the skill and read the full orientation guide, OR call
   cognition_readme for the guide and getting-started text directly.

2. Record the first decision or constraint you are currently aware of for this project:
     cognition_record(type="decision", summary="<what was decided>",
                      detail="<why, and what was rejected>")

3. Run /vibe-curate to add semantic edges to anything you record. Curation is your
   job -- no background curator runs automatically.

4. Use cognition_search to verify what is already captured before recording duplicates.

Start small: one decision or discovery node is enough to begin. The graph grows
incrementally as you work -- you do not need to backfill everything upfront.
"""

# Short injection block for prime.py: orient + instruct (not the full guide).
ONBOARDING_BLOCK = """\
## Vibe Cognition -- Empty Graph

This project has no cognition history recorded yet. Vibe Cognition is active and ready.

INSTRUCTION: Alert the user that this project has no cognition history and that
vibe-cognition is installed and ready to use. Briefly explain: (a) what vibe-cognition
does (persistent project knowledge graph -- decisions, failures, patterns, discoveries),
and (b) that they can call cognition_readme for the full orientation guide and
getting-started procedure. Encourage them to record the first node when they make a
decision or discovery.
"""
