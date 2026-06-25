---
description: You MUST use this skill any time you need to retrieve information about the project or write project history to persistent memory, retrieving project information without using this skill will affect the clarity of the research. You must also use this skill when storing memories about the project. Curation is YOUR job — after recording any nodes you MUST run the /vibe-curate skill to link them; there is no automated background curator.
---

# Vibe Cognition — Project Knowledge Graph

## Tools

| Tool | Purpose |
|------|---------|
| `cognition_record` | Record a knowledge node or episode |
| `cognition_add_task` | File a trackable task (server-attributed to the git user) |
| `cognition_list_tasks` | List the backlog — open tasks, priority-sorted, grouped by parent |
| `cognition_update_task` | Update a task's status/owner/priority/parent in place (transition-logged) |
| `cognition_search` | Semantic search across all cognition nodes |
| `cognition_get_node` | Read a single node's full narrative (incl. `detail`) by id |
| `cognition_update_node` | Edit a node's narrative (summary/detail/context/severity) in place; re-embeds on text change |
| `cognition_get_chain` | Traverse reasoning chains (LED_TO edges) from a node |
| `cognition_get_superseded_chain` | Walk a node's version history via SUPERSEDES (newest first) |
| `cognition_get_workflow` | Find a workflow procedure by name/topic and return the current HEAD version + chain |
| `cognition_get_incident_resolution` | Get an incident + its resolutions, follow-ons, and contradictions |
| `cognition_get_history` | Browse nodes by context area, type, or recency |
| `cognition_add_edge` | Manually create an edge between two nodes |
| `cognition_add_edges_batch` | Create multiple edges in one call (max 500) |
| `cognition_get_edgeless_nodes` | Find nodes with no edges (need curation) |
| `cognition_get_neighbors` | Get all connections to a node (all edge types) |
| `cognition_remove_edge` | Remove a specific edge between two nodes |
| `cognition_remove_node` | Delete a node and all its attached edges (destructive — for junk/test/duplicate nodes) |
| `cognition_get_uncurated_nodes` | List nodes not yet processed by `/vibe-curate` |
| `cognition_mark_curated` | Mark nodes as curated (used by `/vibe-curate`) |
| `cognition_reload` | Force a full re-hydrate of the graph from the journal |
| `cognition_store_document` | Store a document as a first-class node (see `/vibe-document`) |
| `cognition_get_document` | Retrieve a stored document: metadata + text + freshness |
| `cognition_load_project` | Attach a foreign project for cross-project structural reads |
| `cognition_unload_project` | Detach a foreign project and release its file handles |
| `cognition_list_projects` | List all loaded projects (home + foreign) with guard status |

| Service / dashboard tool | Purpose |
|------|---------|
| `get_status` | Server status: graph stats + embedding readiness + foreign project count |
| `cognition_dashboard` | Start/stop the local graph dashboard |
| `cognition_readme` | Orientation guide + getting-started procedure (call on an empty graph or to explain vibe-cognition) |

**Documents:** to store a document (client doc, PDF, spec) as project memory, use the
**`/vibe-document`** skill — it makes the load-bearing workflow the default (store the
document, then record its facts as descriptor nodes citing the returned `doc:<hash>` in
THEIR `references` so they auto-link, then curate).

### Edges

Deterministic edges are created automatically on record when nodes share references:
`part_of` (entity↔episode on any shared ref; entity→document on a shared `doc:` ref) and
`relates_to` (document→episode on a shared `doc:` ref). For the semantic edges
(`led_to`, `resolved_by`, `supersedes`, `contradicts`, `relates_to`), use the
`/vibe-curate` skill or create them manually with `cognition_add_edge`. Note `relates_to`
has three provenances — deterministic (document→episode), curator-proposed, and manual —
so it is NOT "semantic only." `duplicate_of` is reserved and not supported by
`cognition_add_edge`.

Deletion is destructive and not undoable: `cognition_remove_node` cascades to every edge attached to the node. Use it to prune junk, test, or duplicate nodes. For a node that is outdated but historically real, prefer recording the correction and adding a `supersedes` edge rather than deleting the history.

## Tasks — the project backlog

A `task` is **trackable open work** — actionable, owned, with a lifecycle. Open tasks
are injected at session start and listed via `cognition_list_tasks`, so the graph itself
**is** the backlog (no hand-maintained TODO file).

**Before picking up work, check the open tasks first: `cognition_list_tasks`.**

- **Create with `cognition_add_task`** (NOT `cognition_record` — that path is rejected for
  tasks). The creator is resolved **server-side** from your git config — you can't set it,
  so multiple people sharing one graph see who filed each task. `priority` is
  `critical | high | normal | low`; an optional `owner` is free-text "who's on it".
- **Hierarchy:** pass `parent_id` to file a task under a parent task/epic (any depth).
  Re-parent later with `cognition_update_task(parent_id="<id>")`, or `parent_id=""` to
  detach to top-level. Moving a task carries its whole subtree.
- **Lifecycle:** `open → in_progress → blocked → done | cancelled` (reopen allowed). Change
  it with `cognition_update_task(status=...)` — the ONLY path to status/owner/parent edits
  (each status change is appended to an audit log). `cognition_update_node` can still fix a
  task's summary/detail/priority, but not its status/owner/parent.
- **Curate tasks** like any node: `/vibe-curate` links a task `relates_to` the
  decision/pattern it implements, or a done task `resolved_by`/`led_to` the closing episode.

## Two Kinds of Nodes

### Entities (concise facts)

Types: `decision`, `fail`, `discovery`, `assumption`, `constraint`, `incident`, `pattern`, `workflow`

Entities are **concise, searchable facts** — like index cards, not essays.

- **summary**: MAX 250 chars. Write like a commit message.
- **detail**: 1-3 sentences of rationale. NOT the full story.

### Workflows (step-by-step procedures)

Type: `workflow`

Workflows store **prescriptive, ordered procedures** as ONE cohesive unit — so a how-to is fetched whole, not reconstructed from scattered nodes.

- **summary**: Brief title of the procedure ("deploy to production", "onboard a new engineer").
- **detail**: The FULL procedure. Verbose is correct here.
- **Versioned by supersession**: to update a workflow, record a NEW workflow node with the complete revised procedure and add a `supersedes` edge to the old version. Never edit in place (`cognition_update_node` is blocked on `workflow` nodes).
- **Retrieve with `cognition_get_workflow(name_or_topic)`** — resolves any matched version to the current HEAD automatically. Use the `/vibe-workflow` skill for the full write+retrieve workflow.

**Before starting any multi-step task**, search for an existing workflow first: `cognition_get_workflow("topic")`.

### Episodes (full narratives)

Type: `episode`

Episodes capture the **complete narrative** of a body of work — a Linear task lifecycle, git push, a debugging session, a feature implementation. Create one when work is complete.

- **summary**: Brief title ("LL-298: Data wipe investigation and 3-phase fix")
- **detail**: The full story — everything that happened, all context. Verbose is fine here.

Entities are automatically linked to episodes via `PART_OF` edges when they share references (commit hashes, issue numbers, PR numbers). This happens instantly via deterministic matching — no LLM needed.

## When to Record

**If in doubt, record it.** A node that turns out to be low-value costs nothing. A missing node when you need context later is expensive.

### Record entities when:
- You make or recommend a decision (and why)
- Something fails — a build, test, approach, or assumption
- You discover something non-obvious about the codebase
- You identify a reusable pattern or anti-pattern
- You hit a constraint (technical, API, platform)
- A production incident occurs
- An assumption is made that could later prove wrong

### Create episodes when:
- A Git push is done
- a Linear issue is completed.
- A significant debugging session concludes
- A feature implementation is done
- An incident is fully resolved

## Field Guide

### `node_type` (required)
One of: `decision`, `fail`, `discovery`, `assumption`, `constraint`, `incident`, `pattern`, `episode`, `workflow`
(`task` is also a node type, but it is created with `cognition_add_task`, not `cognition_record` — see the Tasks section above)

### `summary` (required)
For entities: MAX 250 chars. Someone scanning 50 nodes should understand what happened.
- Good: "Double-filter bug: query filters by language after already opening language-scoped box"
- Bad: "Found a bug in the flashcard data source that was causing data to be invisible after migration"

For episodes: Brief title of the work.

### `detail` (required)
For entities: 1-3 sentences of rationale or context.
- Good: "FlashcardLocalDataSourceImpl opens language-scoped box then redundantly filters by flashcard.language. Migrated cards have old format, making them invisible."
- Bad: [500-word root cause analysis]

For episodes: Full narrative. Be thorough — this is where verbose context belongs.

### `context` (required)
Comma-separated list of **both** specific file paths **and** topical terms. Used for filtering and discovery.
- Example: "flashcard_local_datasource.dart, HiveService, data migration, LL-298"

### `author` (required)
Use the current git user name.

### `severity` (optional)
`critical` / `high` / `normal` / `low`

### `references` (optional)
Comma-separated references to external resources. Shared references are how entities link to their episode — instantly, via deterministic `part_of` matching (no LLM).
- Examples: "issue:LL-298, pr:97" or "commit:ba64aeb"

## Querying

Use these tools to query the cognition graph:

1. `cognition_search` — Find decisions, failures, patterns by meaning
2. `cognition_get_history` — Browse by context area, type, or recency
3. `cognition_get_chain` — Follow causal chains from a specific node

## Workflow Integration

- **During planning:** Record `decision` and `assumption` nodes
- **During implementation:** Record `discovery`, `pattern`, and `constraint` nodes
- **During debugging:** Record `fail` nodes
- **During incidents:** Record `incident` nodes
- **When work is complete:** Record an `episode` summarizing the full lifecycle
- **Always include** `references` (issue/PR numbers) so nodes link to their episode and `/vibe-curate` can relate them
- **After recording:** run `/vibe-curate` to link the new nodes — don't wait to be asked (see Final Step)

## Final Step: Curate the New Nodes — MANDATORY, do it yourself

**Curation is your responsibility. There is no automated background curator.** If you
recorded **any** nodes during this turn / unit of work, you **MUST** run the
`/vibe-curate` skill before you finish responding — **without being asked**. This is the
step users most often have to remind you about; own it yourself, every time.

- This is a hard rule, not a suggestion: recording without curating leaves the new
  nodes semantically disconnected (only their deterministic `part_of` edges exist).
- `/vibe-curate` only processes **uncurated** nodes, so it just links what you added — cheap to run after recording.
- **Always** curate after creating an `episode`.
- Skip it **only** if you recorded nothing this turn, or `get_status` shows 0 uncurated nodes.
- This is for **recording** sessions only — if you only queried/retrieved (no new nodes), there is nothing to curate.

Deterministic edges (`part_of`, and `relates_to` for document→episode) are the *only*
edges created automatically (on record). This step adds the **semantic** relationships
(`led_to`, `resolved_by`, `supersedes`, `contradicts`, `relates_to`) that make the graph
navigable — and only the agent can do it.

## Examples

### Concise entity during a task
```
cognition_record(
  node_type: "decision",
  summary: "Word Placement uses true drag-and-drop, placed in medium mastery tier",
  detail: "Draggable/DragTarget for positional knowledge testing. Harder than recognition, easier than full reconstruction.",
  context: "word_placement_review.dart, app_settings.dart mastery tiers",
  author: "Colton Dyck",
  references: "issue:LL-282, pr:100"
)
```

### Episode when task is complete
```
cognition_record(
  node_type: "episode",
  summary: "LL-282: Replace Sentence Reconstruction with Word Placement review type",
  detail: "Sentence Reconstruction required reordering ALL words — too broad for flashcard-specific review. Replaced with Word Placement: sentence displayed with target word removed as drop zone, user drags word to correct position. Key decisions: true drag-and-drop interaction, multi-word targets as single unit, first occurrence only blanked, medium mastery tier. SR kept for reinforcement phases. Reused fill_in_blank_utils.dart. Touched 7 files following the modular review type system.",
  context: "review_type_factory.dart, word_placement_review.dart, ReviewType enum, LL-282",
  author: "Colton Dyck",
  references: "issue:LL-282, pr:100"
)
```

### Recording a failure
```
cognition_record(
  node_type: "fail",
  summary: "Mocking Hive boxes masked type adapter registration issue — tests passed, prod crashed",
  detail: "Mock bypassed serialization path. ReviewSession adapter (ID 22) not registered in test setup.",
  context: "test/, Hive, mocking, serialization, ReviewSession",
  author: "Colton Dyck",
  severity: "high",
  references: "issue:LL-260"
)
```
