# WP-Task-Node — Execution Plan

**Status:** spec FINAL — sonnet adversarial peer-review done (verdict REVISE; 2 blockers + 8 should-fix/nits all
folded below). Pending Colton approval, then Vorpid. Vince does not write code.
**Owner (impl):** Vorpid (on Colton's go). **Gate:** full WP protocol (sonnet spec review, SHA-pinned merge,
fix+proof same commit, manager worktree verify, CI green 3 legs).
**Origin:** decision `d1192f7e7bf8` (Colton, 2026-06-23) + clarifiers (Colton, 2026-06-24, this session).
**Sibling/precedent:** `workflow` node type — WP shipped v0.11.0 (`docs/wp-workflow-node-plan.md`, episode `34f8d342f38f`).
**Release:** user-facing — version bump in `pyproject.toml` + `.claude-plugin/plugin.json` + `uv lock`.

## Goal

Add a first-class `task` `CognitionNodeType`: trackable, open, actionable work — server-attributed to the
git user, carrying a **lifecycle** (open → in_progress → blocked → done/cancelled), a **priority**, optional
**owner**, and a **parent/group hierarchy** — surfaced **at session start** and via a **list tool**, **editable
in place**, and **curated into the graph**. The intent (Colton): retire the hand-maintained `docs/BACKLOG.md` —
the graph itself becomes the backlog.

## Clarifiers resolved (Colton, 2026-06-24) — the design contract

1. **Lifecycle = mutable status + transition log.** `status`/`priority` are live fields edited in place as the
   task moves; each status change stamps `{status, at, by}` into metadata for an audit trail (no node spawning).
2. **Editable in place** — the OPPOSITE of `workflow` (which blocked `update_node`). Tasks are concise → they
   embed as ONE entity vector (no chunks), so editing has no chunk-orphan hazard. **CAVEAT (peer-review F1 — this
   is load-bearing): `cognition_update_node` CANNOT change `status`.** Its whitelist is `summary/detail/context/
   severity` only; `metadata` is explicitly non-editable there. `status` lives in `metadata`, so status changes go
   through a dedicated `cognition_update_task` that writes metadata directly AND re-embeds explicitly. See
   "Embedding" + Tools §3 for the exact wiring — the naive "update_node handles it" path does NOT work.
3. **Curated into the graph** — also opposite of `workflow` (matcher-inert). Tasks connect to what they're
   relevant to via TWO layers: (a) an EXPLICIT parent `part_of` edge set at creation; (b) AGENT curation via
   `/vibe-curate` (task `relates_to` the decision/pattern it implements; a done task `resolved_by`/`led_to` the
   episode that closed it).
4. **Ownership = git-resolved creator + free-form owner.** `created_by` resolved SERVER-SIDE from
   `git config user.name`/`user.email` at record time — NEVER trusting a client value (unlike today's `author`
   arg). `owner` is an optional free-text "who's on it." Git-unset → OS user → `"unknown"`; never hard-fail.
5. **Surfacing = session-start injection + `cognition_list_tasks`.** Open tasks appear in context every session
   (like constraints do via `prime.py`) AND are queryable via a dedicated tool. The injection is what actually
   retires the markdown.

**Clarifiers round 2 (Colton, 2026-06-24) — locked:**
6. **Status vocabulary:** `open / in_progress / blocked / done / cancelled` (reopen allowed). No team-specific
   review/handoff state in v1 — a finished-but-awaiting-merge task uses `done` + teammate-comms for the gate.
7. **Hierarchy depth: ARBITRARY** (any task parents any task, cycle-guarded). No depth cap. The session-start
   injection stays a FLAT priority-sorted top-N regardless of depth; depth only manifests in `cognition_list_tasks`
   tree rendering.
8. **`cognition_list_tasks` scope: HOME PROJECT ONLY** — no `project=` arg in v1 (matches write-isolation; can be
   added later without breaking the contract).
9. **Write trigger: FOLD into `skills/vibe-cognition/SKILL.md`** (prominent, early-placed `### Tasks` section) — NOT
   a dedicated `/vibe-task` skill. Three named tools + the section are the trigger.

## Prior art from the graph (respect, don't re-litigate)

- **Node-type survival pattern `8c5619b691f7`** (n=110 audit): a type lives only with BOTH a retrieving question
  AND a write trigger. `assumption` had neither → recorded 0× ever. **Mandate: ship `task` with both.**
- **`author` is client-supplied today.** `cognition_record`'s docstring says "author should be the current git
  user name" — it's whatever the LLM types, untrusted. The post-commit HOOK resolves the real author server-side
  (`git log %an`), but the MCP server PROCESS never resolves git identity at record time. `task.created_by` is
  net-new server-side plumbing — decision `d1192f7e7bf8` explicitly forbids trusting the client value.
- **`update_node` re-embed contract `986687c1ed27`:** `_update_node` re-embeds on any whitelisted change (summary,
  detail, context, severity) via `_embed_entity_node`. BUT its whitelist EXCLUDES `metadata` (verified
  `cognition_tools.py` `_update_node` ~line 759: params are `summary/detail/context/severity` only; docstring:
  "metadata ... NOT editable here"). And `_embed_entity_node` (~line 67) embeds `f"{type}: {summary}\n{detail}"`
  with a Chroma metadata dict that has NO `status`/`owner`. **So status edits canNOT ride `update_node`, and
  re-embedding alone won't make status searchable — both need explicit work (see Embedding + Tools §3).**
- **Node-id collision saga (`e434566c8440`/`0bd725b83bd0`, fixed in WP-ID):** `add_node(..., mint_unique_id=True)`
  (used by `_record_node`) mints collision-free ids. Same-summary tasks recorded in one coarse tick MUST be
  regression-tested (two "fix the thing" tasks created back-to-back).
- **Matcher uses LITERAL type checks** (`_deterministic_edge_for_pair`): a new type SILENTLY falls into the
  "entity" bucket and auto-`part_of`-links to any episode sharing a reference unless explicitly gated. The
  `workflow` WP introduced `_INERT_TYPES` for exactly this.
- **Curate skill forbids agent `part_of`** (`skills/vibe-curate/SKILL.md` step 2: "Remove any `part_of` or
  `duplicate_of` proposals"). So task hierarchy MUST be an explicit edge at creation — curate will never add it.
- **Pyright whole-repo gate (MEMORY.md):** `uv run pyright` with NO path (else test files silently skipped).
- **Doc-drift GUARD** (`tests/test_doc_drift.py`): every tool name must appear in SKILL.md; the workflow WP added
  a **node-type coverage guard** that pins every `CognitionNodeType` member to SKILL+README presence — this will
  FAIL until `task` is documented, which is the intended forcing function.

## Data model

- `models.py`: add `TASK = "task"` to `CognitionNodeType` (one line).
- **Reuse existing schema fields** (no `CognitionNode` schema change):
  - `summary` = task title; `detail` = description; `context` = topical terms / files; `references` = PR/issue/commit.
  - **`severity` = priority** (`critical`/`high`/`normal`/`low`). Reusing it gets the `prime.py` `SEVERITY_ORDER`
    sort and search-surfacing for FREE. (Document this repurposing in the tool docstring so the LLM sets it.)
  - `author` stays LLM-supplied as today (kept for continuity); the TRUSTED identity is `metadata.created_by`.
- **`metadata` carries task-specific structured fields** (the `document` precedent — journaled + replayed):
  - `status`: `open` | `in_progress` | `blocked` | `done` | `cancelled` (default `open`). Server-validated.
  - `created_by`: `{name, email}` resolved server-side at creation. Never client-trusted.
  - `owner`: optional free-text.
  - `parent_id`: optional id of a parent task/epic — the AUTHORITATIVE parent pointer (what `list_tasks` groups
    by). Mirrored as a `part_of` edge child→parent (`source="task-parent"`). The two are kept in sync ONLY through
    `cognition_add_task` / `cognition_update_task` (single owner — see "Re-parenting"). Re-parenting is a move, not
    a create — it is explicitly supported (Colton, 2026-06-24).
  - `transitions`: append-only list of `{status, at, by}`; seeded with the initial `open` transition at creation.

## Identity resolution (server-side, net-new)

- New helper `resolve_git_identity(repo_path) -> dict` — **net-new code.** (Peer-review F6: `git_hygiene.py` is
  pure-filesystem with ZERO subprocess — do NOT claim to "reuse" it. The real git-shelling precedent is
  `hooks/post-commit.py`, which does `subprocess.run(["git", "-C", repo, ...], capture_output=True, text=True,
  encoding="utf-8", timeout=5)`.) Model on that: `git -C <repo> config user.name` / `user.email`. Fallback chain:
  git config → OS user (`getpass.getuser()`) → `"unknown"`. 5s timeout; NEVER raises (returns the fallback).
  `repo_path` from `REPO_PATH`/`CLAUDE_PROJECT_DIR` (already resolved in `config.py`). `git -C <repo> config` reads
  the repo-local identity, which is what we want.
- Used at task creation (`created_by`) and at each transition (`by`). The CLIENT cannot override it — there is
  no `created_by` parameter on the tool.

## Matcher behavior (the tension — peer-review please pressure-test)

Tasks must be CURATABLE (Colton) but NOT noisily auto-glued. Resolution:
- **Deterministic shared-reference matcher:** add `task` to `_INERT_TYPES` so a task sharing a commit ref with an
  episode does NOT auto-`part_of`-link. (Peer-review F2: `_INERT_TYPES` currently contains ONLY `workflow` —
  `document` is NOT in it; documents are handled by separate `doc_gated` pair-rules reached AFTER the inert gate.
  So add `task` next to `workflow`, and while there, fix `storage.py`'s misleading comment that claims document
  is in the set.) The gate is the first check in `_deterministic_edge_for_pair` (`return None` if either type is
  inert).
- **Explicit hierarchy edge:** when `parent_id` is set, the add-task tool creates the `part_of` child→parent edge
  via `_add_edge_core`/`storage.add_edge` (source `"task-parent"`). **This is NOT gated by `_INERT_TYPES`** —
  that gate only short-circuits `create_deterministic_edges` (the reference-matching path), not direct edge
  creation. So "inert in the matcher" and "explicit part_of at creation" do not contradict (peer-review F3).
- **Agent curation:** tasks remain fully eligible for `/vibe-curate` (they appear in the uncurated worklist like
  any node). Semantic links (`relates_to`, `resolved_by`, `led_to`) come from there.
- **Open question for the reviewer:** is suppressing the deterministic auto-`part_of` correct, or is
  done-task↔closing-episode auto-linking by shared commit ref actually wanted? Vince's rec: SUPPRESS — keep
  semantic links agent-curated (cleaner, matches "curated into the graph" = agent-driven intent). The explicit
  parent edge + curation cover the real cases.

## Embedding (peer-review F1 — the wiring must be explicit, not implied)

- Tasks are concise → embed as ONE vector via the existing `_embed_entity_node` path (NO chunking, unlike
  workflow/document). `task` falls through to the entity embed in `_record_node` (no new branch needed there).
- **Extend `_embed_entity_node` to surface task lifecycle:** it currently builds metadata from
  `entity_type/summary/author/timestamp/context` (+severity/references). Add `status` and `owner` from
  `node.metadata` to that Chroma metadata dict (only when present), AND append them to the embed TEXT
  (e.g. `\nstatus: {status} owner: {owner}`) so both metadata filters and semantic search reflect lifecycle state.
  This is the SINGLE shared embed path, so the change is once-and-done for create + re-embed.
- **Re-embed on edit is NOT free via `update_node`** (it can't touch metadata). `cognition_update_task` must,
  after mutating `metadata` in storage, fetch the fresh `CognitionNode` and call `_embed_entity_node` DIRECTLY
  (guarded by `embeddings_ready`, mirroring `_update_node`'s deferred-on-not-ready behavior). Spell this out in
  Tools §3 — do not route status changes through `_update_node`.

## Tools (the surface)

1. **`cognition_add_task(summary, detail, context, priority="normal", owner=None, parent_id=None, references=None)`**
   — resolves git identity server-side, seeds `metadata` (`status="open"`, `created_by`, `owner`, `parent_id`,
   initial transition), records via the `_record_node` path (`mint_unique_id=True`), creates the explicit
   `part_of`→parent edge if `parent_id` given (validate parent exists + is a task), embeds. Returns the node.
   Home-project only (writes aren't cross-project). **This is the survival WRITE trigger.**
   - *Rationale for a dedicated tool over `cognition_record(node_type="task")`:* server-side identity, status
     seeding, and the parent edge are task-specific; a dedicated tool keeps the LLM contract clean and unambiguous.
2. **`cognition_list_tasks(status=None, priority=None, owner=None, parent_id=None, include_done=False)`** — the
   backlog view + survival RETRIEVAL trigger. Returns tasks filtered + sorted (priority then recency); default
   EXCLUDES `done`/`cancelled`. Group/annotate by parent so the hierarchy reads as a tree (ARBITRARY depth — render
   the full tree; tolerate a missing parent per F10). **HOME PROJECT ONLY** — no `project=` arg in v1 (locked
   clarifier 8; matches write-isolation, additive later).
3. **`cognition_update_task(node_id, status=None, priority=None, owner=None, summary=None, detail=None, parent_id=None, note=None)`**
   — the ONLY path to `status`/`owner`/`parent_id`/transition edits (peer-review F1/F4: `update_node` literally cannot write
   `metadata`, so there is no competing path — the risk is an agent TRYING `update_node` for status and getting a
   "no updatable fields" error). Implementation contract:
   - Read the current node; reject non-task ids.
   - Validate the status transition (see "Status transition legality" below).
   - **Read-modify-write metadata** (peer-review F8): `storage.update_node` replaces fields wholesale, so fetch
     current `metadata`, set `status`, APPEND `{status, at: server-time, by: git-resolved}` to `transitions`, then
     write the WHOLE metadata dict back. (`priority` maps to the top-level `severity` field, not metadata.)
   - **Re-embed explicitly**: fetch the fresh node, call `_embed_entity_node` directly when `embeddings_ready`
     (deferred otherwise). NOT via `_update_node`.
   - Non-status edits (`priority`→severity, `summary`, `detail`) may delegate to the `_update_node` core for the
     narrative-field re-embed, but `status`/`owner`/transitions stay in this tool.
   - **Document on `cognition_update_node`**: "for `task` nodes, use `cognition_update_task` for status/owner."

## Re-parenting / re-grouping (Colton, 2026-06-24 — moves are first-class)

A task's parent must be MOVABLE, not just set-at-creation (promote out of a WP, move under a different epic).
`cognition_update_task` handles it via the `parent_id` argument, performed atomically under the storage lock so
`metadata.parent_id` and the `part_of` edge never diverge:

1. Validate the new parent exists AND is a `task` (reject otherwise). 
2. **Cycle guard:** walk the new parent's ancestor chain (via `metadata.parent_id`); if this task appears, reject
   — a task can't become a child of its own descendant. Reuse the path-based cycle detection from the C-7 fix.
3. Remove the OLD `part_of` edge (child → old `metadata.parent_id`), if any.
4. Add the NEW `part_of` edge (child → new parent, `source="task-parent"`).
5. Write the new `metadata.parent_id`.

**Three semantics on the one arg** (since `None` already means "no change" everywhere else in this tool):
- `parent_id=None` → leave parent unchanged.
- `parent_id="<task-id>"` → move under that task.
- `parent_id=""` (empty sentinel) → DETACH to top-level (remove the edge, clear the pointer).

**Subtrees move for free:** children attach to THIS task, not the grandparent — moving a task carries its whole
subtree; only one edge changes (no recursive rewrite).

**Target only the parent edge:** a task may ALSO carry a `part_of` edge to a `/vibe-curate` Step-3 summary/cluster
node. The move logic must touch ONLY the parent edge — identified by `source="task-parent"` AND a target that is
itself a `task` — and never disturb a cluster-membership edge. Tests must cover a task that has both.

## Status transition legality (peer-review F8 follow-up)

Define the legal transitions so `cognition_update_task` validates them (else any string is a "status"):
`open → in_progress | blocked | cancelled | done`; `in_progress → blocked | done | cancelled | open`;
`blocked → in_progress | open | cancelled | done`; `done → open` (reopen) ; `cancelled → open` (reopen).
Reject unknown status strings with a clear error listing the valid set. Reopen (done/cancelled → open) is allowed
(real backlogs reopen work). Keep the table small and in one constant so the tool + tests share it.

## Surfacing — `prime.py` session-start injection

- Add `_format_tasks(storage)` mirroring `_format_constraints`: fetch `task` nodes, filter to open
  (`status not in {done, cancelled}`), sort by `severity` (=priority) via the EXISTING `SEVERITY_ORDER` dict
  (peer-review F5: reuse it, don't redefine), render `## Open Tasks` with status + owner per line. Wire into
  `generate_prime`'s `sections` list (after constraints, before/after patterns — pick a stable slot).
- **Bound the injection with a precise cap** (peer-review F5 — "critical/high in full" is ambiguous when there
  are 20 criticals): show the top `N=10` open tasks by `SEVERITY_ORDER` then recency; if more remain, append a
  single `+{K} more open tasks — use cognition_list_tasks` line. A fixed top-N is unambiguous and keeps the
  session-start payload bounded regardless of priority distribution.

## Curate-skill pass (Colton's 2nd message — REQUIRED, in-scope)

- `skills/vibe-curate/SKILL.md`: add a **"Task nodes"** guidance block — a task `relates_to` the
  decision/discovery/pattern it implements; a `done` task `resolved_by`/`led_to` the closing episode; do NOT
  propose `part_of` for tasks (parent links are explicit, and agent `part_of` is already forbidden). Tasks appear
  in the uncurated worklist automatically — no worklist change needed.
- `skills/vibe-curate/edge-analyzer.md`: teach the analyzer task semantics (so its proposals are good — it's the
  subagent that actually generates edge proposals).
- `cluster-analyzer.md`: review — tasks under a shared parent already cluster via the explicit `part_of`; likely
  no change, confirm it doesn't mis-summarize open tasks.

## Doc-drift + survival surfaces (enumerate ALL — the workflow WP's B2/B4 lesson)

- `cognition_record` docstring type-list + the OTHER hardcoded lists (`cognition_get_history`, `cognition_search`,
  `cognition_get_edgeless_nodes`) — grep `"decision, fail, discovery"` and add `task` to every occurrence.
- `skills/vibe-cognition/SKILL.md`: node-types list + a **prominent, early-placed** `### Tasks` section (peer-review
  F9: folding into the main skill is accepted over a dedicated `/vibe-task` skill — three named tools ARE a real
  write trigger, unlike the dead passive-only `assumption` — but place the section early so it isn't buried under
  9 other types) + the THREE new tools in the tool table (doc-drift guard fails otherwise) + a RETRIEVAL question
  ("Before picking up work, check open tasks via cognition_list_tasks").
- `README.md`: `task` row in the Node Types table.
- `src/vibe_cognition/cognition/readme.py` `COGNITION_GUIDE`: `task` in `## Node types` + the new tools in the
  tool-groups table (this is `cognition_readme`'s content + the prime onboarding — keep in sync).
- Dashboard `app.js` `TYPE_COLORS`: add a `task` color.
- **Survival WRITE trigger (locked clarifier 9):** `cognition_add_task` + its docstring + the prominent, early
  `### Tasks` section in `skills/vibe-cognition/SKILL.md`. NO dedicated `/vibe-task` skill — three named tools + the
  section are the trigger; avoids skill sprawl. (Place the section early so it isn't buried under 9 other types.)
- **Survival RETRIEVAL trigger:** `cognition_list_tasks` + session-start injection + the SKILL retrieval question.

## Tests (`tests/test_task.py` new, plus existing guards)

- create → list → update status → done: transition log grows; done drops from the default `list_tasks` view.
- git-identity resolution: mock subprocess — name set; name unset → OS-user fallback; total failure → `"unknown"`.
- `created_by` is NOT client-overridable (no parameter; server value wins).
- parent edge: `part_of` child→parent created; `parent_id` validated (non-existent / non-task parent rejected).
- matcher inert: no auto-`part_of` for any task-involving pair (`tests/test_deterministic_edges.py`).
- re-embed on status change: search/metadata reflect the new status (proves the `update_node` re-embed path).
- collision regression: two same-summary tasks one tick apart get distinct ids.
- transition legality: reject an unknown status string; allow reopen (done→open); reject a disallowed jump.
- parent deletion: delete a parent → children survive, `list_tasks` parent-grouping tolerates the missing parent.
- re-parenting: move a task → old `part_of` edge gone, new one present, `metadata.parent_id` updated, both in sync;
  `parent_id=""` detaches to top-level; cycle (make a task child of its own descendant) is rejected; moving a task
  with children carries the subtree; a task carrying BOTH a parent edge and a cluster-membership `part_of` edge
  keeps the cluster edge untouched on move.
- re-embed surfaces status: after `cognition_update_task(status=...)`, the Chroma metadata + embed text carry the
  new status (proves `_embed_entity_node` was extended AND called directly — guards against the F1 regression).
- `prime`: open tasks injected + sorted by priority; done/cancelled excluded; overflow line past the top-N cap.
- doc-drift: the node-type coverage guard (added by the workflow WP) now passes with `task` documented.
- `get_status` statistics include the new type for free (enum-iterating, like the workflow WP verified) — assert.

## Sequence

1. `models.py` enum + `resolve_git_identity` helper + matcher `_INERT_TYPES` gate + `test_deterministic_edges.py`.
2. `cognition_add_task` (record + identity + metadata seed + explicit parent edge + entity embed w/ status/owner).
3. status/transition + `cognition_update_task` + re-embed test.
4. `cognition_list_tasks` (filter / sort / parent-group).
5. `prime.py` `_format_tasks` injection + cap/overflow.
6. docstrings (all 4 lists) + SKILL/README/readme.py + dashboard color.
7. curate-skill pass (SKILL.md + edge-analyzer.md; confirm cluster-analyzer).
8. `tests/test_task.py`.
9. version bump (pyproject + plugin.json + `uv lock`) + refresh `docs/BACKLOG.md` (mark shipped, retire the
   feature-backlog entry).

**One PR, do not merge mid-sequence (peer-review F7):** adding `TASK` to the enum (step 1) immediately turns
`tests/test_doc_drift.py::test_node_types_documented_in_skill_and_readme` red, and it STAYS red until step 6
documents the type. So steps 1–5 are intentionally CI-red on the branch; land the whole WP as one PR and gate the
merge on the full green run, not on intermediate commits.

## Acceptance

- `task` is a valid node type; `cognition_add_task` stamps a server-resolved `created_by` the client cannot set.
- `cognition_list_tasks` returns open tasks sorted by priority, grouped by parent, excluding done by default.
- A status change via `cognition_update_task` appends a transition record AND re-embeds (search reflects it).
- Open tasks inject at session start (capped); done/cancelled never injected.
- Parent `part_of` edge formed; NO deterministic auto-edge on task-involving pairs (matcher inert).
- `/vibe-curate` proposes sensible task edges (relates_to / resolved_by) and never `part_of`.
- Same-summary tasks one tick apart → distinct minted ids (collision regression green).
- Doc-drift GUARD green (3 tools in SKILL.md; node-type guard passes). Standing gate green whole-repo:
  `uv run ruff check .`, `uv run pyright` (NO path), `uv run pytest`.
- BOTH survival triggers present (write: add_task + docstring + SKILL section; read: list_tasks + injection + question).

## Risks

- **Injection bloat** if many open tasks → fixed top-N cap + overflow line.
- **created_by spoofing** → resolved server-side, no client parameter; explicit test.
- **NOT two competing status-write paths** (peer-review F4) → `update_node` physically cannot write `metadata`,
  so the only path to `status` is `cognition_update_task`. The real failure mode is an agent trying `update_node`
  for status and getting a "no updatable fields" error — mitigated by docstrings on BOTH tools.
- **Transition-append TOCTOU** (peer-review F8) → two concurrent sessions read the same `transitions` list, both
  append, one write wins. Same cross-process residual as the rest of the journal (in-process `_synced` lock;
  cross-process TOCTOU documented, not eliminated). Acceptable; note it, don't build new locking.
- **`parent_id` is not a foreign key** (peer-review F10) → deleting a parent leaves children with a stale
  `metadata.parent_id` (NetworkX drops the `part_of` edge but not the metadata string). `cognition_list_tasks`
  parent-grouping must tolerate a missing parent (show such children ungrouped, don't crash).
- **git config unset on CI/headless** → fallback chain, never hard-fail; tested.
- **Existing BACKLOG.md is NOT auto-migrated** → out of scope; optionally a one-time agent-driven seeding of the
  live items into task nodes after ship. Note in the WP, don't build a migrator.
- **`severity`-as-priority overload** → one field, two names; the docstring must state it plainly so the LLM sets it.

## Synergy / scaffolding (revisit per the workflow WP's deferral)

Two real new types now exist (workflow + task) — the workflow WP deferred "new-node-type scaffolding" until task
shipped. Recommendation: STILL keep this WP self-contained. Workflow and task remain divergent (verbose/chunked/
inert/superseded vs concise/curated/mutable/git-attributed); the only shared pieces are already shared (matcher
`_INERT_TYPES`, the doc-drift node-type guard). Extract a generic abstraction only if a THIRD type appears.
