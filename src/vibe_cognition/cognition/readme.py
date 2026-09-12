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
Deterministic part_of edges are created automatically. This exclusivity is
enforced server-side: cognition_add_edge, cognition_add_edges_batch, and
cognition_mark_curated require a curation_token minted by cognition_begin_curation
(the curate-orchestrator calls it at the start of a run) and refuse without one;
every accepted edge records its curation_session. get_status's cognition_graph.
edges_outside_curation (WP-TC15) still counts semantic-edge writes whose source
isn't one of the curator's own values (legacy or hand-tagged sources); edge_sources
is the full per-source histogram alongside it. On Codex the same loop runs with
$vibe-curate; get_status.harness tells you which harness you are on.

## Codex CLI

This plugin also runs under OpenAI Codex CLI (get_status.harness tells you which
harness you are on; skills are invoked as $name there instead of /name). Codex never
reads .claude folders: it discovers skills in ~/.agents/skills, in .agents/skills
inside the repo (project root down to the current directory), the deprecated
~/.codex/skills, and installed plugins. To share a project's Claude Code skills with
Codex without duplicating them, junction (Windows: cmd /c mklink /J) or symlink the
repo's .agents/skills to its .claude/skills, or link individual skill folders under
~/.agents/skills. Git on Windows walks into junctions, so either ignore .agents/ or
commit .agents/skills as the real copy and link .claude/skills to it. Skills whose
text names Claude-only mechanics (/skill invocations, the Agent tool, subagent_type,
model names) read wrong on Codex until their wording is harness-neutral; .claude/
commands and .claude/agents have no Codex equivalent.

## Tool groups

| Group | Tools |
|-------|-------|
| Record | cognition_record, cognition_update_node, cognition_remove_node |
| Tasks | cognition_add_task, cognition_list_tasks, cognition_update_task |
| Identity | cognition_set_identity (confirm who is driving; unblocks refused writes) |
| People | cognition_register_person, cognition_update_person, cognition_get_person, |
|        | cognition_list_people |
| Env facts | cognition_set_env_fact, cognition_delete_env_fact, |
|           | cognition_clear_env_facts, cognition_list_env_facts |
| Search | cognition_search |
| History | cognition_get_history, cognition_get_node, cognition_get_chain, |
|         | cognition_get_superseded_chain, cognition_get_incident_resolution, |
|         | cognition_get_neighbors |
| Curate | cognition_begin_curation, cognition_add_edge, cognition_add_edges_batch, |
|        | cognition_remove_edge, cognition_get_edgeless_nodes, |
|        | cognition_get_uncurated_nodes, cognition_mark_curated |
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

Environment facts (durable per-machine setup truths -- project root, OS, tool
choices -- so teammates' sessions detect divergence instead of tripping over it):
  NOT graph nodes -- they live in committed per-person delta files
  (.cognition/people/<slug>.jsonl) folded into a registry, joined into
  cognition_get_person's `environment` field. Writes are SELF-ONLY by
  construction: cognition_set_env_fact / cognition_delete_env_fact /
  cognition_clear_env_facts take NO email parameter and always target the
  server-resolved git identity. Reads are open (cognition_list_env_facts takes
  any email). Every successful write returns a `disclosure` string you MUST
  relay to the human -- they can have any fact removed at any time
  (cognition_clear_env_facts with no args = "forget everything about me").

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

## Search ranking: seniority & agent-origin weighting

cognition_search ranks by weighted_score (score * weight.multiplier), penalty-only
(multiplier always (0, 1.0] -- never a boost): a hit is never hidden by this, only
ever pushed lower relative to peers. Every hit carries weight (multiplier, seniority,
from_agent, basis), even when neutral -- never silent. basis: exempt:<node_type>
(constraint/incident, always pinned 1.0), agent (from_agent true -- always weighted
below every human seniority tier), human:<seniority> (stamped + registered person),
human:unregistered (stamped, no matching person node), unverified (no stamp at all).
cognition_get_workflow's internal match search shares this path and inherits it too.

## Session-start prime: personalized vs. global

prime_personalize (auto default | on | off) picks whether session-start prime
shows the global digest or one keyed to your git identity. auto personalizes
when the graph has more than one distinct stamped writer email OR more than
one registered person -- the second condition catches a team's
first-onboarded member, where every node so far was written by one person but
several people are now registered (a solo user who registers only themselves
stays global either way). When personalized and your identity resolves to a
registered person node, the block opens with a one-line identity header (You
are registered as {name} -- {role} ({seniority}), reporting to {manager}.,
degrading field-by-field when role/seniority/manager are blank) -- mutually
exclusive with the New Here notice below, by construction. Full pinned order
when personalized: identity header -> Your Open Tasks -> Team Critical ->
Your Team -> Your Manager's Recent Decisions -> Since You Were Gone -> Your
Recent Activity.

## Session-start prime: role-aware sections

A person node's reports_to_email (a REPORTING relationship, distinct from the
free-text person.role job title) drives two personalized prime sections: managers
get "Your Team" (direct reports' in-progress claims -- claimant + age, stale ones
first, blocked claims; a claim is stale once its age is strictly greater than
prime_stale_claim_days, default 7 -- exactly 7 days old is not stale, and a null/
legacy claimed_at is never stale) right after Team Critical; subordinates get
"Your Manager's Recent Decisions" (no HEAD-filter, same as the global Recent
Decisions model) right after that. A middle manager gets both. No new section for
your OWN claims -- those already surface under Your Open Tasks. A role-less user
(no person node, no reports either direction) or personalization off sees no
change at all.

## Session-start prime: "Since You Were Gone" digest

A machine-local, per-email marker (.cognition/last-seen.json, git-ignored, never
synced) tracks when you last started a session here. Right after Your Manager's
Recent Decisions, personalized prime shows "## Since You Were Gone": teammates'
decisions, constraints, and incidents recorded since that marker -- newest first,
capped, with your own writes excluded (not news to you) but unstamped nodes
included (an awareness view reports content, not people). Constraints are
HEAD-filtered (mirroring the global Active Constraints section); decisions and
incidents are not (mirroring their own global sections) -- a node can
legitimately also appear in Your Manager's Recent Decisions or the global
Recent Decisions, deliberately not deduplicated. No marker yet (first
run, or an ephemeral sandbox) falls back to a capped lookback window -- never a
full-history dump, never a silently-skipped section. The marker is stamped only
by the real SessionStart hook, never by a bare generate_prime call (dashboard,
library use, tests) -- starting a session is what marks things "seen."

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

## Graph identity (required before you can record)

Every memory is attributed to a person. The server resolves who is driving, first
hit wins: the confirmed `.cognition/identity.json`, then git config `[user] email`,
then the OS user (name only, no address). SVN credentials are read but NEVER
attribute a write -- the auth cache is machine-wide and realm-keyed, so trusting it
could attribute this repo via another project's credential; SVN usernames appear
only as `suggestions` in the refusal payload.

**If no email resolves, every write is REFUSED** with `identity_required: true`.
Reads still work. The fix: ASK THE HUMAN for their name and work email, then call
`cognition_set_identity(name=..., email=...)`. The refusal payload carries
`suggestions` drawn from git config and cached SVN credentials -- offer them for
confirmation, never assume one, and NEVER use your own agent name.

`identity.json` is machine-local and never committed: it says who drives THIS
checkout. Registering a person node is a separate, shared step -- still do it.

`cognition_set_identity` only affects future writes. To correct attribution already
recorded (a personal address used before a work one), the graph owner runs the
`vibe-cognition-remap-identity` CLI, which is dry-run by default and infers nothing.

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
`.cognition/.gitignore` (one-time-ever, idempotent). The vector store itself lives
OUTSIDE the repo since v0.32.0 (under the plugin data dir -- see `chromadb_path` in
`get_status`); the ignore line still protects repos where teammates run older
plugin versions. Opt out with
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

## Team setup (svn)

**Subversion has no equivalent of `merge=union`, and cannot be given one.** There is
no per-path merge configuration in SVN at any version. Two people appending to
`.cognition/journal.jsonl` between syncs WILL conflict on `svn update`. This is
verified behaviour, not a caution.

**The conflict rule: keep BOTH sides.** The journal is append-only and replay order
does not matter, so "keep both" is always the correct resolution -- there is no case
where one side should win. `svn update` leaves `journal.jsonl.mine` and
`journal.jsonl.rNNN` beside the conflicted file (the number is the incoming
revision, so find the highest one rather than assuming a name). Concatenate both
sides, drop duplicate lines by their node `id`, write the result back, then:

    svn resolve --accept working .cognition/journal.jsonl

Reload the graph afterwards: union resolves the TEXT, not the replay order.

**Sync discipline keeps the conflict window short.** `svn update` before starting a
session and commit the journal at the end of one. Most conflicts come from long
uncommitted stretches, not from genuine simultaneous work.

**Never set `svn:eol-style` on anything under `.cognition/`.** The server replays the
journal by byte offset, so any line-ending rewrite forces a full re-read, and the
document store under `.cognition/documents/` is content-addressed -- rewriting those
bytes breaks the sha-named-content invariant. SVN performs no EOL translation unless
the property is set, so the safe state is the default state: leave it unset. Beware a
repository-root `svn:auto-props` rule (e.g. `*.md = svn:eol-style=native`), which
stamps the property on new files automatically. An inherited rule like that CANNOT be
neutralized from `.cognition/` -- properties from different ancestors combine rather
than override, and there is no property value meaning "do not translate". If your
repo has such a rule, exclude `.cognition/` from it at the root.

**Machine-local files must stay unversioned.** A versioned `last-seen.json`
conflicts on every teammate's session, and a versioned `identity.json` publishes
whoever happens to drive that checkout. The server writes `.cognition/.gitignore`
with the full list; SVN does not read it, so mirror it once.

TAKE THE GLOBS FROM `.cognition/.gitignore`, NOT FROM THIS PAGE -- that file is
generated and gains entries across releases (`identity.json*` arrived in 0.37.0),
so any list transcribed into prose goes stale and silently stops covering a new
machine-local file. ONE EDIT IS REQUIRED WHILE COPYING: strip the trailing slash
from `chromadb/`. SVN ignore globs are fnmatch patterns with no directory-only
meaning, so `chromadb/` matches NOTHING while bare `chromadb` works -- verified in
the validation lab, and it fails silently, which is why it is called out here.

If `.cognition` is not yet under version control, propset alone FAILS with
`E155010: not under version control`. The full sequence, in this order:

    svn add --depth empty .cognition
    svn propset svn:global-ignores -F <file> .cognition
    svn add --force .cognition

Step 1 registers the directory so a property can be set on it. Step 2 installs the
ignores. Step 3 needs `--force` because plain `svn add` refuses an already-versioned
directory (`E200009`), and it is filtered by step 2's property -- which is why the
order matters: reverse it and the machine-local files are swept in. Write that file WITHOUT a
UTF-8 BOM: `svn propset -F` mis-decodes a BOM and silently kills the FIRST rule in
the list, while `svn propget` still displays it as though it were fine. If such a
file is already committed, `svn rm --keep-local <path>` removes it from version
control without deleting your copy.

**Ordering matters when adding `.cognition/` to SVN.** Set the ignore property
BEFORE adding the directory's contents, or the machine-local files above are swept in
by the same `svn add`.
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
