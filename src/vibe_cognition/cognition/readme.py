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

The full record -> curate loop (what to capture, when, and how) is the "Three standing
practices" in your MCP server instructions -- surfaced every session via the MCP
initialize handshake and re-injected after a compact, so it is already in your
context. In brief: record with cognition_record as you work, then run /vibe-curate to
launch the background curate-orchestrator agent, which adds semantic edges (led_to,
resolved_by, supersedes, contradicts, relates_to) -- never author them yourself.
Deterministic part_of edges are created automatically.

## Tool groups

| Group | Tools |
|-------|-------|
| Record | cognition_record, cognition_update_node, cognition_remove_node |
| Tasks | cognition_add_task, cognition_list_tasks, cognition_update_task |
| People | cognition_register_person, cognition_update_person, cognition_get_person, |
|        | cognition_list_people |
| Search | cognition_search |
| History | cognition_get_history, cognition_get_node, cognition_get_chain, |
|         | cognition_get_superseded_chain, cognition_get_incident_resolution, |
|         | cognition_get_neighbors |
| Curate | cognition_add_edge, cognition_add_edges_batch, cognition_remove_edge, |
|        | cognition_get_edgeless_nodes, cognition_get_uncurated_nodes, |
|        | cognition_mark_curated |
| Document | cognition_store_document, cognition_get_document |
| Workflow | cognition_get_workflow (find by topic; resolves to current HEAD) |
| Cross-project | cognition_load_project, cognition_unload_project, |
|               | cognition_list_projects (use the project= arg on read/search tools) |
| Service | get_status, cognition_dashboard, cognition_readme, cognition_reload |

## Node types

Entities (concise searchable facts -- summary max 250 chars):
  decision, fail, discovery, assumption, constraint, incident, pattern

Episodes (full narrative of a completed body of work):
  episode

Workflows (step-by-step procedures stored as ONE cohesive unit):
  workflow -- use the /vibe-workflow skill to store and retrieve procedures.
  Versioned by supersession: update = NEW node + supersedes edge (never edit in place).
  Retrieve: cognition_get_workflow("topic") resolves any matched version to the HEAD.

Tasks (trackable open work, server-attributed to the git user):
  task -- create with cognition_add_task (NOT cognition_record). Mutable lifecycle
  (open/in_progress/blocked/done/cancelled) + priority + arbitrary-depth parent
  hierarchy. Open tasks inject at session start; list/edit via cognition_list_tasks /
  cognition_update_task. Check open tasks before picking up work.
  assigned_to_email (on add/update_task) directs a task AT an email -- distinct
  from the free-text, unmatched owner -- surfacing it under the assignee's Your
  Open Tasks; assigning is not claiming, the assignee still claims it via
  status=in_progress.
  exclude_people (comma-separated emails, on cognition_list_tasks) drops tasks
  CREATED BY those identities -- matched on created_by, never owner. USER-INVOKED
  ONLY -- never add it yourself, only when a human explicitly asks.
  Claiming never blocks except one case: blocked->in_progress over someone else's
  LIVE claim requires note= (retryable error names the claimant otherwise). Every
  other collision (in-progress poke without takeover, reopening someone else's
  closed task) succeeds with a claim_warning (kind/claimant/claimed_at/message)
  instead of blocking; self-actions and unverifiable identities never trigger it.

Documents (stored files with text sidecar for search):
  document -- use the /vibe-document skill

People (a HUMAN identity -- name, role, seniority, reports-to; never an agent):
  person -- create with cognition_register_person (NOT cognition_record). Updated
  IN PLACE (never supersession-versioned) with an append-only profile_history audit
  trail. Omit email to self-register (server-resolved git identity); pass one to
  register someone else. One node per (casefolded) email. List the roster with
  cognition_list_people(); look up one with cognition_get_person(email_or_id).

## Provenance: from_agent

Every write from cognition_record, cognition_add_task, cognition_store_document,
cognition_register_person, and cognition_update_person stamps metadata.from_agent
(default true -- an undeclared write is honestly "via agent"; set false ONLY when a
human explicitly dictated/authored the content themselves). Surfaces in
cognition_search results, cognition_get_node, and cognition_list_tasks rows. A node
written before this existed has no from_agent key -- that reads as unknown, never
coerced to true or false.

## Search filtering & completeness

cognition_search and cognition_get_history always report total_found (distinct
matches discovered) + exhaustive (true = exact count, false = a floor -- more may
exist past the limit/an internal cap); count (what you got back) can be less than
total_found even with no filtering. cognition_search and cognition_list_tasks both
take an optional exclude_people (comma-separated emails) to drop hits/tasks by
those authors -- matched on the server-resolved identity stamp, never free-text
author/owner, never an unstamped node; cognition_search exempts constraint/incident
hits. USER-INVOKED ONLY -- never add it on your own initiative, only when a human
explicitly asks to filter someone out for that call; there is no persistent muting.
A filtered call discloses excluded_count/excluded_for whenever something was
actually dropped.

## Edge types

  part_of (auto), led_to, resolved_by, supersedes, contradicts, relates_to

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

## Team setup (git)

If multiple people or agents share this repo as **separate clones**, add the following
line to your **repo-root** `.gitattributes`:

    .cognition/journal.jsonl merge=union

`union` is a built-in git merge driver (git >= 1.7.x) -- no `[merge "union"]` stanza
or extra git config is needed. This makes the append-only journal union-merge so
concurrent appends from different branches/clones survive a merge instead of conflicting.

**Warning:** Do NOT add this in a single shared checkout (everyone in one clone) -- that
setup uses the worktree-flush protocol and nobody commits the journal on branches. Set
it **early**, before the journal grows; retrofitting it onto a large committed journal
can duplicate entries across the rewrite boundary.

**Auto-configuration:** On first startup in a separate-clones repo, the server
automatically adds the union-merge line to `.gitattributes` and adds `chromadb/` to
`.cognition/.gitignore` (one-time-ever, idempotent). Opt out with
`VIBE_COGNITION_NO_GIT_HYGIENE=1`. To re-arm: delete `.cognition/.git-hygiene-managed`.
For existing projects or non-standard topologies, use the manual line above.

**Residual risk (Windows / autocrlf):** the journal is replayed by byte offset, so
its on-disk bytes must never be rewritten by line-ending normalization. With
`core.autocrlf`-style setups this currently holds only by coincidence of git config,
not by guarantee. If your team sees `.cognition/journal.jsonl` permanently "modified"
in git status, or "re-hydrated from top" replay resets after merges/pulls, add EOL
protection alongside union-merge:

    .cognition/*.jsonl merge=union -text

Set this EARLY in the graph's life. Do NOT retrofit `-text` onto a grown
shared-checkout journal without a planned cut-over: the first commit after adding
`-text` re-normalizes the file bytes once, which live sessions see as a replaced
journal. The server auto-writes only `merge=union`, never `-text` -- adding `-text` is
a deliberate, manual team decision.
"""

COGNITION_GETTING_STARTED = """\
## Getting started on this project

The graph for this project is empty. Here is the act-now procedure:

1. Run /vibe-cognition to load the skill and read the full orientation guide, OR call
   cognition_readme for the guide and getting-started text directly.

   If teammates will share this repo as separate clones, add
   `.cognition/journal.jsonl merge=union` to your repo-root `.gitattributes` now,
   before the journal grows. See the "Team setup (git)" section in the guide
   (cognition_readme) for details.

2. Record the first decision or constraint you are currently aware of for this project:
     cognition_record(node_type="decision", summary="<what was decided>",
                      detail="<why, and what was rejected>", context="<area, e.g. src/auth>",
                      author="<your name>")

3. Run /vibe-curate to launch the background curator on anything you record. Triggering
   curation is your job -- never author semantic edges yourself.

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
decision or discovery. If the user shares this repo with teammates, also mention there
is a one-time `.gitattributes merge=union` setup for the journal -- point them at
cognition_readme for details.
"""
