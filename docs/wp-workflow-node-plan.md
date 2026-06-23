# WP-Workflow-Node — Execution Plan

**Status:** spec FINAL — vibe-cognition:Plan design + sonnet adversarial peer-review (verdict REVISE;
all 4 blockers folded in below). Ready for Vorpid on Colton's go.
**Owner (impl):** Vorpid. **Gate:** full WP protocol (sonnet spec review done, SHA-pinned merge,
fix+proof same commit, manager worktree verify, CI green 3 legs). Vince does not write code.
**Origin:** decision `1e3661cb330d` (Colton, 2026-06-22). **Sibling:** `task` node type (decision `acb29c42ec99`).
**Release:** user-facing — version bump in BOTH `pyproject.toml` + `.claude-plugin/plugin.json` + `uv lock`.

## Goal

Add a first-class `workflow` `CognitionNodeType`: a step-by-step PROCEDURE stored and retrieved as
ONE cohesive unit. Verbose body (like `episode`/`document`, not the 250-char entity convention).
Versioned by SUPERSESSION — an update is a NEW workflow node carrying the FULL revised procedure,
linked to the prior version via a `supersedes` edge; the head is authoritative, the chain is history.
Distinct from `pattern` (general reusable approach) and `episode` (narrative of what happened):
`workflow` is prescriptive, ordered, current-version-authoritative.

## Prior art from the graph (respect, don't re-litigate)

- **Node-type survival pattern `8c5619b691f7`** (n=110 audit): a type lives only with BOTH a retrieving
  question AND a write trigger. `assumption` had neither → recorded 0× ever. **Mandate: ship `workflow`
  with both or it dies.**
- **DOCUMENT precedent (`3d06411d0420`, DESIGN `79fb16eb5b42`):** the deterministic matcher uses LITERAL
  type checks, not "episode-like by default." A new type needs an explicit matcher decision. The chunked-
  embedding path (`_embed_document`) is the proven model for full-text search.
- **Node-id collision saga (`e434566c8440` / `bdc17a401bf0` / `0bd725b83bd0`):** `generate_node_id` hashes
  `type:summary:timestamp`; coarse Windows clock made same-type+summary+tick collide and silently overwrite.
  Fixed by `storage.add_node(node, mint_unique_id=True)` (used by `_record_node`). **A same-summary workflow
  update at near-identical timestamp hits exactly this** — inherited via `_record_node`, but MUST be regression-tested.
- **Pyright whole-repo gate (MEMORY.md):** `uv run pyright` with NO path, else test files are silently skipped.

## Verified code facts (peer-review confirmed; re-verify line numbers before coding — they drift)

- `CognitionNode.detail` (`src/vibe_cognition/cognition/models.py`) is an **unbounded `str`** — no cap, no
  validator. "Verbose body like episode" needs NO schema change. `document` additionally slices node detail
  to `detail[:2000]` and stores full text in a sidecar; **workflow does NOT use a sidecar** (see Risk R1).
- `_embed_entity_node` (`src/vibe_cognition/tools/cognition_tools.py`) embeds `f"{type}: {summary}\n{detail}"`
  as ONE vector, no chunking/truncation; nomic silently truncates long input. `_embed_document` chunks full
  text into `<node_id>#chunk-N` via `chunk_text` (1000-word windows) and stamps chunk metadata
  `{node_id, entity_type, is_chunk:True}`.
- Matcher `_deterministic_edge_for_pair` (`src/vibe_cognition/cognition/storage.py`): classifies any
  non-`document`, non-`episode` type as "entity" via `not a_doc and not a_ep`. **A new `workflow` type would
  SILENTLY fall into the entity bucket and auto-`part_of`-link to any episode sharing a reference** — wrong
  (a procedure is not "part of" an episode). Must be gated (Blocker B1).
- `get_superseded_chain` (`src/vibe_cognition/cognition/queries.py`) walks SUPERSEDES **successors**
  (newer→older) from an assumed head. To resolve the HEAD from an arbitrary (possibly old) matched node you
  must walk **incoming/predecessor** supersedes edges — opposite direction (Blocker B6 helper).
- Doc-drift GUARD `tests/test_doc_drift.py`: (a) every registered tool name must appear in `SKILL.md`;
  (b) every non-reserved `CognitionEdgeType` in BOTH SKILL.md + README, pinned `assert len(expected) == 6`.
  **No node-type coverage assertion exists** (we add one — see step 6).
- `mint_unique_id=True` is already used by `_record_node` → workflows inherit collision-safe ids.

## Blockers folded in from peer review (these are MUST-FIX, not optional)

**B1 — Matcher gate (explicit).** In `_deterministic_edge_for_pair`, `workflow` MUST be excluded from the
entity bucket so workflow-involving pairs mint NO deterministic edge (graph-inert, like document↔document).
Concrete approach (do this, don't paraphrase): introduce an inert-type set rather than ad-hoc booleans — e.g.
`_INERT_TYPES = {CognitionNodeType.DOCUMENT.value, CognitionNodeType.WORKFLOW.value}` and early-`return None`
if either node's type is in it; keep `episode` handled as today. Add `tests/test_deterministic_edges.py` cases
asserting no auto-edge forms for any workflow-involving pair (workflow↔episode, workflow↔workflow, workflow↔entity).

**B2/B4/N4 — Hardcoded node-type lists (enumerate ALL, not just one).** `_parse_node_type` is enum-driven
(adding the enum value auto-extends validation), BUT several LLM-facing docstrings hardcode the type list as a
string and each needs `workflow` added. Verified locations (re-confirm before editing): `cognition_record`
(~line 1007), `cognition_get_history` (~1440), `cognition_get_edgeless_nodes` (~1562), `cognition_search`
(~1246). Grep `"decision, fail, discovery"` to find every occurrence; update all.

**B5 — `cognition_update_node` stale-chunk bug (SOLVE, don't just document).** `_update_node` calls
`_embed_entity_node` unconditionally — it refreshes only the node vector, never chunks. If workflows chunk,
editing a workflow's body via `update_node` leaves orphaned old-body chunks (silent search corruption).
**Decision: BLOCK `update_node` on `workflow` type** with a clear error dict ("workflows are versioned by
supersession — record a new workflow and add a `supersedes` edge; do not edit in place") rather than threading
a re-chunk path. This aligns with the supersession model and is the simpler, safer change. Add a test asserting
the block + the error shape.

**B9 — Dashboard color.** `TYPE_COLORS` in the dashboard `app.js` lacks `workflow` (unknown types fall back to
gray `#7a8290` — graceful, not a crash, but reads as an oversight). Add a `workflow` color. Non-gating but in-scope.

## Survival caveat — BOTH triggers (non-negotiable, per `8c5619b691f7`)

**WRITE trigger:** a dedicated **`skills/vibe-workflow/SKILL.md`** (mirrors `/vibe-document`) making "store this
how-to as ONE workflow node; to update, write the full new procedure + `supersedes` the old one" the default.
Register it the way `vibe-document` is registered (check `.claude-plugin/plugin.json` skills wiring; mirror that
entry). Plus the `cognition_record` docstring WORKFLOW block. (Passive docstring-only is how `assumption` died —
the skill is required, not optional.)

**RETRIEVAL trigger:** a thin **`cognition_get_workflow(name_or_topic, project=None)`** tool: type-filtered
semantic search (`node_type="workflow"`) → resolve the match to the HEAD via the new `get_workflow_head` →
return `{head: <full node>, chain: [...], matched: <id>}`. AND a prominent SKILL.md retrieval question
("Before a multi-step task, search for an existing workflow"). The named retrieval question is what builds the
write habit — make it prominent.

> Design note (peer-review N2): an alternative is `resolve_head=true` on the existing
> `cognition_get_superseded_chain` instead of a new tool, avoiding new doc-drift surface. **Rejected** — the
> survival pattern needs a *named, topic-addressed* retrieval handle ("how do I do X"), which `get_node`/
> `get_superseded_chain` (id-addressed) don't provide. The new tool IS the trigger. Worth the SKILL.md row.

## Retrieval head-resolution (Blocker B6)

New helper `get_workflow_head(storage, node_id)` in `queries.py`: walk **incoming** SUPERSEDES edges
(`get_predecessors(nid, SUPERSEDES)`) until a node with no incoming supersedes (the head), cycle-guarded with
the same pattern as `get_superseded_chain`. Export via `src/vibe_cognition/cognition/__init__.py`; import in
`cognition_tools.py`. Test from a MID-chain node (v2 of v1→v2→v3) and assert it returns v3. Document the
branching case: multiple supersedes successors return the first match + a warning (mirror `get_superseded_chain`).

## Embedding strategy (the load-bearing choice)

**Chunk the workflow body like a document** (new `_embed_workflow`, or generalize `_embed_document`): write one
node-level vector (`{type}: {summary}\n{detail[:2000]}`) PLUS `chunk_text(full_body)` chunks under
`<node_id>#chunk-N` with `entity_type="workflow"`. This keeps the FULL procedure searchable when long and reuses
the proven D2 dedupe/`matched_excerpt` collapse in `_format_search_results`. Branch `_record_node`:
`if node_type == WORKFLOW: _embed_workflow(...) else _embed_entity_node(...)`. Chunk purge on delete is already
generic (`delete_cognition_node` → `delete_by_node_id`), so workflows inherit cleanup.

> Peer-review N6: a SUPERSEDED workflow node's chunks persist in ChromaDB until that old node is explicitly
> removed (supersession = new node, old node retained as history). This is correct, but document it and add a
> test asserting the old node's chunks survive supersession (they're history, not orphans) and that
> `search_hit_is_live` / dedupe doesn't double-serve head+superseded for the same topic.

## Touch points (every file)

- `src/vibe_cognition/cognition/models.py` — add `WORKFLOW = "workflow"` to `CognitionNodeType` (one line; no schema change).
- `src/vibe_cognition/cognition/storage.py` — `_deterministic_edge_for_pair` inert-type gate (B1). Confirm
  `get_statistics()` (enum-iterating) picks up the new type for free (it does; just verify).
- `src/vibe_cognition/tools/cognition_tools.py` — `_embed_workflow` + `_record_node` branch; the four hardcoded
  docstring type-lists (B2/B4/N4); the `cognition_record` WORKFLOW block; the new `cognition_get_workflow` tool;
  the `update_node` workflow block (B5).
- `src/vibe_cognition/cognition/queries.py` — new `get_workflow_head`; `__init__.py` export.
- `skills/vibe-cognition/SKILL.md` — node-types list + a `### Workflows` section + **the new tool in the tool table**
  (doc-drift guard fails otherwise).
- `README.md` — `workflow` row in the Node Types table.
- `src/vibe_cognition/cognition/readme.py` `COGNITION_GUIDE` — `workflow` in `## Node types` + the new tool in
  the tool-groups table (this is `cognition_readme`'s content + the prime injection; keep in sync).
- `skills/vibe-workflow/SKILL.md` (NEW, the write trigger) + `.claude-plugin/plugin.json` skill registration.
- Dashboard `app.js` `TYPE_COLORS` (B9).
- Tests: `tests/test_workflow.py` (new), `tests/test_deterministic_edges.py` (workflow inert cases),
  `tests/test_doc_drift.py` (add the node-type coverage guard — see step 6).

## Sequence

1. `models.py` enum + matcher inert gate (B1) + `tests/test_deterministic_edges.py`.
2. `_embed_workflow` + `_record_node` branch; `update_node` workflow block (B5); confirm delete chunk-purge.
3. `get_workflow_head` in `queries.py` + `__init__.py` export.
4. `cognition_get_workflow` tool (search→head→body+chain, `project` arg parity, reject `"*"` like other single-node tools).
5. Docstrings (all 4 hardcoded lists, B2/B4/N4) + `cognition_record` WORKFLOW block + SKILL/README/readme.py +
   new `skills/vibe-workflow/SKILL.md` + plugin.json registration + dashboard color (B9).
6. Tests: `tests/test_workflow.py`; ADD a node-type coverage guard to `tests/test_doc_drift.py` (pin
   `CognitionNodeType` members to SKILL+README presence — mirrors the edge-type `==N` guard; forces `task` to
   self-document later too).
7. Version bump (pyproject + plugin.json + `uv lock`); refresh `docs/BACKLOG.md` (mark shipped).

## Acceptance

- `workflow` is a valid `cognition_record` `node_type`; verbose `detail` round-trips via `cognition_get_node`.
- A long (>1000-word) procedure is semantically searchable via `cognition_search(node_type="workflow")` (proves chunking).
- Supersession: v2 + `supersedes` edge → `cognition_get_superseded_chain` returns the chain;
  `cognition_get_workflow(topic)` resolves to the HEAD regardless of which version matched.
- Same-summary v1/v2 at near-identical timestamps get distinct minted ids (collision regression green).
- `update_node` on a workflow returns the block error (B5); no auto-edge forms on workflow-involving pairs (B1).
- Doc-drift GUARD green (new tool in SKILL.md; new node-type guard passes; edge-type `==N` unaffected — no new edges).
- Standing gate green, whole-repo: `uv run ruff check .`, `uv run pyright` (NO path), `uv run pytest`.
- BOTH triggers present: `/vibe-workflow` skill + record docstring (write); `cognition_get_workflow` + SKILL retrieval question (read).

## Risks

- **R1 verbose body in the journal:** workflows store the full procedure in `detail` (no sidecar) → very long
  procedures bloat the journal. Accept for now (procedures are human-scale, not 20k-word docs); revisit a sidecar
  only if real bodies prove large. Document the trade-off in the WP.
- **Embedding truncation** if treated as a plain entity → mitigated by chunking; test with a long body.
- **Head-resolution direction** (predecessors vs successors) → dedicated cycle-guarded `get_workflow_head`; test from mid-chain.
- **Collision on same-summary updates** → inherited `mint_unique_id=True`; regression test.

## Synergy with `task` node type (`acb29c42ec99`) — recommendation

Keep WP-Workflow-Node SELF-CONTAINED; do NOT build generic "new-node-type scaffolding" now (2-point sample with
divergent needs — workflow is verbose/chunked/supersession-versioned/inert; task is concise/git-attributed/
mutable-status). DO extract two pieces that `task` will genuinely reuse as natural byproducts: (1) the node-type
doc-drift coverage guard (step 6); (2) the matcher `_INERT_TYPES` set (B1). Revisit shared scaffolding AFTER
`task` ships, with two real implementations to generalize from.
