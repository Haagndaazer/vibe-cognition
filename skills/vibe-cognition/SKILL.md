---
description: You MUST use this skill any time you need to retrieve information about the project or write project history to persistent memory, retrieving project information without using this skill will affect the clarity of the research. You must also use this skill when storing memories about the project. Curation is YOUR job to TRIGGER — after recording any nodes you MUST run the /vibe-curate skill (launches the background curator); never author semantic edges yourself.
---

# Vibe Cognition — Project Knowledge Graph

## Tools

| Tool | Purpose |
|------|---------|
| `cognition_record` | Record a knowledge node or episode |
| `cognition_add_task` | File a trackable task (server-attributed to the git user) |
| `cognition_list_tasks` | List the backlog — open tasks, priority-sorted, grouped by parent; optional `exclude_people` filter |
| `cognition_update_task` | Update a task's status/owner/priority/parent/assignment in place (transition- and assignment-logged) |
| `cognition_register_person` | Register a HUMAN identity (never an agent) as a first-class person node |
| `cognition_update_person` | Edit a person's profile fields in place (audit-trailed via profile_history) |
| `cognition_get_person` | Get a person's full profile, including the profile_history audit trail |
| `cognition_list_people` | List every registered person — the team roster |
| `cognition_search` | Semantic search across all cognition nodes; reports `total_found`/`exhaustive`, optional `exclude_people` filter |
| `cognition_get_node` | Read a single node's full narrative (incl. `detail`) by id |
| `cognition_update_node` | Edit a node's narrative (summary/detail/context/severity) in place; re-embeds on text change |
| `cognition_get_chain` | Traverse reasoning chains (LED_TO edges) from a node |
| `cognition_get_superseded_chain` | Walk a node's version history via SUPERSEDES (newest first) |
| `cognition_get_workflow` | Find a workflow procedure by name/topic and return the current HEAD version + chain |
| `cognition_get_incident_resolution` | Get an incident + its resolutions, follow-ons, and contradictions |
| `cognition_get_history` | Browse nodes by context area, type, or recency |
| `cognition_add_edge` | Create an edge between two nodes — ONLY the curate-orchestrator agent (launched via `/vibe-curate`) may use this; never call it yourself |
| `cognition_add_edges_batch` | Create multiple edges in one call (max 500) — same ONLY-the-curate-orchestrator restriction |
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
so it is NOT "semantic only." `supersedes` is THE reconciliation edge for duplicates (e.g.
two clones each recording an episode for the same commit) — `cognition_add_edge` enforces
same-node-type-to-same-node-type and no cycles when creating one.

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
  it with `cognition_update_task(status=...)` — the ONLY path to status/owner/parent/
  assignment edits (each status change is appended to an audit log). `cognition_update_node`
  can still fix a task's summary/detail/priority, but not its status/owner/parent/assignment.
- **Assign with `assigned_to_email`** (on `cognition_add_task` or `cognition_update_task`):
  directs a task AT someone — email-keyed and identity-matched (unlike the free-text
  `owner`), so it surfaces under the assignee's "Your Open Tasks" at their next session
  start even if they neither created nor claimed it. Assigning is NOT claiming — the
  assignee still claims it via `status="in_progress"`. Every effective (different-email)
  assignment/reassignment/unassignment appends one entry to `metadata.assignments`; a
  same-email resubmission is a no-op. Anyone may assign anyone (no ACL; the audit trail
  is the control).
- **Claiming never blocks, with one exception.** Retaking someone else's LIVE claim
  (`in_progress`/`blocked`, `claimed_by` set) via `blocked → in_progress` requires
  `note=` — a retryable error names the current claimant otherwise. Every other
  collision (poking an in-progress task without taking it over, reopening someone
  else's closed task) succeeds and returns a `claim_warning` (`kind`, `claimant`,
  `claimed_at`, `message`) instead of blocking — present only when a real foreign
  claim was detected; self-actions and unverifiable identities never trigger it.
- **Role-aware session-start prime.** A person node's `reports_to_email` (a
  reporting relationship — not the free-text `person.role` job title) drives two
  personalized sections: managers get `## Your Team` (direct reports' in-progress
  claims with claimant + age, stale ones first, blocked claims, capped) right after
  `## Team Critical`; subordinates get `## Your Manager's Recent Decisions` right
  after that. Your OWN claimed tasks stay under `## Your Open Tasks` — these are
  new sections about your reports/manager, not a replacement. No new section for
  own-claims; a role-less user (no person node, no reports either direction) sees
  no change at all.
- **"Since You Were Gone" digest.** A machine-local, per-email marker
  (`.cognition/last-seen.json`, git-ignored) tracks your last session-start here.
  Personalized prime shows `## Since You Were Gone` (right after `## Your
  Manager's Recent Decisions`): teammates' decisions/constraints/incidents newer
  than that marker, newest first, capped, excluding your own writes but including
  unstamped ones. No marker yet → a capped lookback window, never a full-history
  dump. Stamped only by the real SessionStart hook — `generate_prime()` itself
  stays pure read-only.
- **Curate tasks** like any node: `/vibe-curate` links a task `relates_to` the
  decision/pattern it implements, or a done task `resolved_by`/`led_to` the closing episode.
- **Filter out an author with `exclude_people`** (comma-separated emails, on
  `cognition_list_tasks`) — matched on `created_by`, user-invoked only (see Querying).

## People — the team roster

A `person` node models a **HUMAN identity only** — agent identity lives in
teammate-comms, never here. Fields: name, role, seniority
(`owner | senior | mid | junior`), and an optional `reports_to_email` (direct
manager; dangling — no backing node yet — is legal).

- **Create with `cognition_register_person`** (NOT `cognition_record` — that path is
  rejected for `person`). Omit `email` to self-register using your server-resolved
  git identity (impersonation-resistant); pass an explicit `email` to register
  someone else. One node per (casefolded) email — re-registering an existing email
  returns the existing node with `already_registered: true`, never a duplicate.
- **Update in place** with `cognition_update_person` — anyone may update anyone
  (local trust domain; the append-only `profile_history` audit trail is the
  control, not an ACL). `summary` ("Name — role") regenerates automatically.
- **Look up** with `cognition_get_person(email_or_id)` (full profile + history) or
  browse the whole roster with `cognition_list_people()`.
- Person nodes are graph-inert (no automatic `part_of` edges) and searchable via
  `cognition_search(node_type="person")`.

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
(`task` and `person` are also node types, but each has its own dedicated creation tool —
`cognition_add_task` / `cognition_register_person` — not `cognition_record`; see the
Tasks and People sections above)

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

`cognition_search`/`cognition_get_history` responses always carry `total_found`
(distinct matches discovered) + `exhaustive` (whether that's the exact count or a
floor) — `count` (what you got back) can be less than `total_found`; check
`exhaustive` before treating a search as complete. `cognition_search` and
`cognition_list_tasks` both take an optional `exclude_people` (comma-separated
emails) to drop hits/tasks by those authors — **user-invoked only**, never add it
on your own initiative; only when a human explicitly asks to filter someone out
for that call. Matched on the server-resolved identity stamp, never free-text
`author`/`owner`; an unstamped node is never excluded; `cognition_search`'s filter
exempts constraint/incident hits. A filtered call discloses `excluded_count`/
`excluded_for` whenever something was actually dropped.

`cognition_search` also ranks by `weighted_score` (`score * weight.multiplier`), a
penalty-only adjustment (`multiplier` always `(0, 1.0]` — never a boost): a hit is
never hidden or wiped by this, only ever pushed lower relative to peers. Every hit
carries `weight` (`{multiplier, seniority, from_agent, basis}`) even when neutral —
never silent. Agent-authored hits (`basis: "agent"`) are always weighted below every
human seniority tier; constraint/incident hits (`basis: "exempt:<node_type>"`) are
always pinned at 1.0. `cognition_get_workflow`'s internal match search inherits this
too, so it can change which workflow a lookup resolves to.

## Workflow Integration

- **During planning:** Record `decision` and `assumption` nodes
- **During implementation:** Record `discovery`, `pattern`, and `constraint` nodes
- **During debugging:** Record `fail` nodes
- **During incidents:** Record `incident` nodes
- **When work is complete:** Record an `episode` summarizing the full lifecycle
- **Always include** `references` (issue/PR numbers) so nodes link to their episode and `/vibe-curate` can relate them
- **After recording:** run `/vibe-curate` to link the new nodes — don't wait to be asked (see Final Step)

## Final Step: Trigger Curation — MANDATORY, do it yourself

**Triggering curation is your responsibility — never author semantic edges yourself.**
`/vibe-curate` launches a background curate-orchestrator agent that does the actual
linking; you do not create `led_to`/`resolved_by`/`supersedes`/`contradicts`/`relates_to`
edges by hand. If you recorded **any** nodes during this turn / unit of work, you **MUST**
run the `/vibe-curate` skill before you finish responding — **without being asked**. This
is the step users most often have to remind you about; own it yourself, every time.

- This is a hard rule, not a suggestion: recording without curating leaves the new
  nodes semantically disconnected (only their deterministic `part_of` edges exist).
- `/vibe-curate` only processes **uncurated** nodes, so it just links what you added — cheap to run after recording.
- **Always** curate after creating an `episode`.
- Skip it **only** if you recorded nothing this turn, or `get_status` shows 0 uncurated nodes.
- This is for **recording** sessions only — if you only queried/retrieved (no new nodes), there is nothing to curate.

Deterministic edges (`part_of`, and `relates_to` for document→episode) are the *only*
edges created automatically (on record). This step adds the **semantic** relationships
(`led_to`, `resolved_by`, `supersedes`, `contradicts`, `relates_to`) that make the graph
navigable — only the `/vibe-curate` background curate-orchestrator creates these, never
the main instance directly.

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
