# WP-Scope-Review — curation flags constraints that may have the wrong scope

Status: rev 2, peer-reviewed (Sonnet, 2026-09-17; findings folded in below). Rulings: decision 1a73f21bd98e (Colton, 2026-09-17).
Builds on WP-Personal-Constraints (docs/wp-personal-constraints-plan.md, v0.42.0).
Target: v0.43.0.

## 1. Problem

Since 0.42.0 a new constraint is personal unless the recording agent passes
`scope="project"`. The only safeguard is the agent's judgment at record time. A
project rule recorded as personal is hidden from teammates who could break it; a
preference recorded as project makes every teammate's agent obey it. Neither is
flagged anywhere.

## 2. Rulings

1. Scan the constraints in the curation run's uncurated worklist (everything
   recorded since the last run, committed or not, git or SVN alike). Not the VCS
   diff: the curator has no shell, and that would need a new subprocess tool.
2. Flags are durable: stored on the node and listed to the constraint's OWNER at
   session start until they rule. Also reported in the curator's hand-back.
3. A dedicated `curate-scope-analyzer` agent, explicitly pinned to the mid model
   (Sonnet on Claude Code).
4. Both directions: personal that looks like project, and project that looks
   personal.
5. Never blocks. The agent keeps working; a ruling can come any time.

## 3. Design

### 3.1 Where the flag lives — a top-level node attribute, not metadata

`scope_review: {suggested, reason, flagged_at, curation_session}` is stored as its
own top-level node attribute (like `curated_by_skill_at`), NOT inside `metadata`.

Why: replay is last-writer-wins per attribute, and `metadata` is one attribute. A
curator on Bob's machine that read Alice's constraint before Alice's scope change
replayed would write the whole old `metadata` back with a newer stamp and silently
revert her scope. A separate attribute cannot collide with `metadata.scope`.

Clearing: any `cognition_update_node(node_id, scope=...)` by the owner also writes
`scope_review=None`. Passing the current scope is how the owner rules "keep it".
The partially built draft stored the flag inside metadata and reproduces that exact
revert (peer review HIGH); it is rewritten in the same change, and the LWW test in §5
must fail against the draft before it passes against the fix.

Checked: `_apply_update_node` sets each attribute under its own stamp and accepts
`None`, so clearing needs no engine change. A cleared flag reads back as
`scope_review: null`; every consumer treats only a dict as a live flag.

### 3.2 New curation-only tool: `cognition_flag_constraint_scope`

`cognition_flag_constraint_scope(node_id, suggested_scope, reason, curation_token)`

- Curation token checked FIRST (same gate and order as `cognition_mark_curated`),
  then the identity gate.
- Refuses (error, nothing written): node absent or not visible (a teammate's
  personal constraint), not a constraint, suggested scope invalid or equal to the
  current scope, no recorded owner (it can never change scope), blank reason.
- Also refuses a constraint that is not HEAD (has an incoming `supersedes` edge):
  reviewing history is noise.
- Writes `scope_review`; reason trimmed to 300 chars; counts as a curation write.
- Returns `{flagged, node_id, current_scope, suggested_scope, owner_is_you}`.

Rejected: letting the curator call `cognition_update_node` — it is owner-only for
scope, has no flag concept, and would widen the curator's write surface.

### 3.3 Analyzer: `agents-src/curate-scope-analyzer.md`

- Tools: `cognition_get_node`, `cognition_search`, `cognition_get_neighbors`,
  `cognition_get_history`. Propose-only.
- Input: constraint ids. For each: read the full node, current scope, recorded_by.
- Lens (with a one-line "mirrors cognition_record's CONSTRAINT SCOPE guidance — keep
  in sync" pointer, peer review LOW): the same guidance `cognition_record` gives at record time (personal = how
  this person works / wants agents to behave for them; project = true for anyone
  in the repo: platform/API limits, build/ship rules, client requirements, security).
  Signals for project: names a system limit, a file/asset that breaks, a client or
  contract, a security exposure, "the project", "we". Signals for personal: names a
  person's own authorship or habit, "I", "me", "Colton authors", "ask me".
- Precision bar: flag only when the text clearly reads as the other scope. Ambiguous
  stays unflagged (the recorder already judged it, and a noisy review trains people
  to ignore it). Never flag a constraint with no recorded owner.
- Output JSON: `[{node_id, current_scope, suggested_scope, reason, quote}]` where
  `quote` is the verbatim phrase that drove the call. Missing quote = drop.

### 3.4 Orchestrator changes (`agents-src/curate-orchestrator.md`)

- Step 1.5 also captures the constraint ids from the same worklist (both scopes).
- New Step 3b "Scope review", after the conflict pass, before clustering. Skipped
  when the captured list is empty.
  - Batches of 5-10, same convention as the conflict pass. Spawn `curate-scope-analyzer` with explicit
    `model: "{{model:mid}}"` (this analyzer is the one mid-model exception to the
    "analyzers run small" rule — state it explicitly in MODEL PIN ENFORCEMENT).
    Foreground, no `name`, same spawn discipline as the other analyzers. The step is
    written with the same side-by-side harness blocks as Steps 2-4 (peer review
    MEDIUM): Claude Code `subagent_type: "vibe-cognition:curate-scope-analyzer"`;
    Codex `task_name: "scope_batch_N"`, `fork_turns: "none"`, `model:
    "{{model:mid}}"`, `message` = `references/curate-scope-analyzer.md` + ids, then
    `wait_agent`.
  - Review: drop proposals missing quote/reason, equal to the current scope, or on
    a constraint with no owner / not HEAD (the tool would refuse anyway).
  - Suspect cap: once 10+ constraints examined, if more than 40% come back flagged,
    discard all flags from this run and note it in the transcript (a systematic
    misread, not a mis-scoped graph). Looser than the conflict pass (15 / 20%) on
    purpose: scope is more subjective than a contradiction, and a wrong flag costs
    one question, never data. Revisit with real-run numbers.
  - Commit each surviving flag with `cognition_flag_constraint_scope`.
  - Degraded / no-nesting: skip the pass (no inline fallback, same as the conflict
    pass) — the flags are advisory, missing a run costs nothing.
- Final report carve-out (amends constraint f6ab87cb77a8 "bare counts only"): add
  `scope review: N examined / M flagged`, and when any flag has
  `owner_is_you: true`, append ONE clause listing only those node ids with their
  suggested scope, e.g. `flagged for your ruling: a1b2c3 -> project, d4e5f6 ->
  personal`. No summaries, no reasons (the session-start section and get_node carry
  those). Flags on teammates' constraints are a count only. Kept because curation
  usually finishes mid-session: without it the owner would not hear until their next
  session start. The CONTAINMENT section itself names this as its sole exception so a
  later edit does not strip it.

### 3.5 Main instance behaviour (`skills-src/vibe-curate/SKILL.md`)

New "When curation reports scope flags" section:
- Do NOT stop the current task. Mention the flags in one line and continue.
- At the next natural pause (task done, waiting on the human anyway), ask the human
  per flagged constraint, showing its summary (`cognition_get_node`) and the
  suggested scope, then call `cognition_update_node(node_id, scope=<their answer>)`.
- If you are not talking to the human directly (a subordinate), pass the flags to
  whoever is, instead of asking.
- If the session ends first, nothing is lost: the owner's next session start lists them.

### 3.6 Session start (`prime.py`)

New `## Constraints to Review` section, directly after `## Your Personal
Constraints`, shown only to the owner (`recorded_by` email == current email), HEAD
constraints only, oldest flag first, capped at `prime_constraint_limit` with the
overflow line. Each line: `- <id> [<current> -> <suggested>] <summary> — <reason>`.
One lead line tells the agent not to stop work and how to rule. Appears whether or
not prime personalization is on (it is keyed on the email, not the personalized
block).

### 3.6b The flag is visible only to the owner (peer review HIGH)

`scope_review` is stripped from every read by anyone other than the constraint's
owner, at the same storage choke point that hides personal constraints (the node
dicts returned by `get_node`, `get_all_nodes`, by-type, recent, uncurated, snapshot).
So a teammate browsing a flagged project constraint through search, get_node,
neighbors or the dashboard sees the constraint but not the pending flag or its
reason. `show_all_scopes` maintenance storage keeps it. The replay engine and the
flag tool's own writes are unaffected (they use the raw graph).

### 3.7 Unchanged

- Visibility rules from 0.42.0. A flag never changes who can see a constraint.
- A constraint is reviewed once (only while uncurated). A ruling is final: nothing
  re-flags it.
- Older plugins ignore the unknown attribute.

## 4. Surfaces to update

- Tool-surface audit (new tool): Args/Returns/errors docstring; README + SKILL tool
  tables; `parity.json` rows `tool:cognition_flag_constraint_scope` and
  `agent:curate-scope-analyzer`; `cognition_readme` guide (Curate group + curation
  loop); `get_status` unaffected (writes already counted in curation_sessions).
- `cognition_update_node` docstring: scope clears the review flag.
- `tools/render_harness.py` CODEX_REFERENCES gains `curate-scope-analyzer`; Codex
  orchestrator branch names the new reference file.
- Gated-tool registries in tests: `CURATION_WRITE_TOOLS` (test_identity),
  `_TOOL_ARGS` (test_wp_wedge), write-tools list (test_xp2_routing), registered-tool
  set (test_tool_wrappers).
- CHANGELOG 0.43.0, whats-new, three manifests + uv.lock.

## 5. Tests

- Tool: refused without token; refused before identity gate order; each refusal case
  in 3.2 writes nothing; success writes `scope_review` as a top-level attribute and
  leaves `metadata` untouched; `owner_is_you` true/false.
- LWW safety: a flag written from a stale read does not revert a concurrent scope
  change made in another process (two storages, interleaved).
- Clearing: owner `cognition_update_node(scope=current)` clears the flag and keeps
  scope; `scope=other` changes scope and clears; non-owner cannot clear.
- Cross-checkout replay: two independent storages over the same journal files, one
  as the flagging curator's identity (Bob) and one as the owner (Alice): Alice's
  storage sees the flag, `owner_is_you` was false for Bob, Alice's prime lists it,
  Bob's reads no longer show the flag, and Alice's ruling clears it for both.
- Owner-only visibility: a teammate's get_node / search / snapshot on a flagged
  project constraint carries no `scope_review`.
- Prime: owner sees the section; a teammate does not see it for a flagged project
  constraint; superseded constraints excluded; cap + overflow.
- Doc drift: analyzer output schema carries `quote`; orchestrator pins mid for the
  scope analyzer; rendered outputs match sources.

## 6. Release gates

ruff, render/parity checks, full pytest, `pytest -m svn` if `_apply_update_node`
changes, tool-surface audit, sonnet review, then pin via Loki.

## 7. Review answers (adopted)

- Top-level attribute, not a metadata sub-key merge: widening `_apply_update_node`
  touches every update caller and needs its own tombstone design; out of scope here.
- 40%-of-10 cap kept (see §3.4 rationale).
- Hand-back ids kept (see §3.4 rationale).
