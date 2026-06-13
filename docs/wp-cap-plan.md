# WP-Cap Execution Plan — capability gaps (P2)

Audit/BACKLOG capability gaps that compose as tool/query additions, off post-WP-ID main (`d585a22`). Four items: **T-5** (`cognition_get_node` + `cognition_update_node`), **T-4** (persist edge `reason`), **T-11** (expose `get_superseded_chain` / `get_incident_resolution`).

## The gate-hard crux (Vince): update_node MUST re-embed
`storage.update_node` is implemented/journaled/replayed/tested but UNEXPOSED. Exposing it lets an agent fix a typo without delete+re-record (which loses the id, edges, and curation marker). BUT **nothing re-embeds after `update_node`** — the embedding sync only ADDS missing nodes, never refreshes an existing vector — so a summary/detail edit would leave `cognition_search` serving the STALE vector (silent search-staleness, same class as the WP-ID orphan-vector). **The tool MUST re-embed on update.** That re-embed is the acceptance crux: fails-before RUN (update a node's summary → assert search finds the NEW text / the vector was refreshed).

## A cross-cutting constraint (the WP-D3 doc-drift guard, working as designed)
`tests/test_doc_drift.py` asserts EVERY registered MCP tool appears in `skills/vibe-cognition/SKILL.md`. So **every commit that adds a tool MUST add it to the SKILL table (+ README) in the SAME commit**, or the guard goes red. This keeps each commit independently green and the surface docs honest.

## Binding rules
Rule 20, 12 (fails-before RUN — esp. the re-embed), 11 (one embed path; one get-by-id discipline), 21, 18. Journal protocol. pyright ≤ 8. SHA-pinned merge gate.

---

## Commit 1 — `cognition_get_node` (read a node's full narrative)
Search results omit `detail` and `get_neighbors` returns summaries only — after a hit there's no way to read the full node. Expose `storage.get_node`.
- `cognition_get_node(node_id) -> {id, type, summary, detail, context, references, severity, timestamp, author, metadata}` or `{"error": ...}` if absent.
- Ledger-11 note: `cognition_get_document` is the DOCUMENT-specialized get-by-id (adds sidecar text + freshness); `cognition_get_node` is the generic node read. Distinct surfaces, both valid — cross-reference in the docstrings; don't force one shape.
- SKILL + README tables updated (doc-drift guard).

**Tests:** get an existing node → full dict incl `detail`; a missing id → error. (Storage.get_node is already tested; this is the thin tool surface + the docs-guard satisfaction.)

## Commit 2 — persist the edge `reason` (T-4)
The edge-analyzer produces a `reason` per edge; `cognition_add_edge` only logs it, the batch drops it, `CognitionEdge` has no field — curation rationale is lost.
- Add `reason: str | None = None` to `CognitionEdge` (models.py).
- `storage.add_edge`: write `reason=edge.reason` onto the graph edge; `model_dump` already carries it into the journal (verified). `_replay_entry` add_edge branch: read `reason=data.get("reason")` (graceful — old journals lack it, like the D1a metadata round-trip).
- `_add_edge_core` / `_add_edges_batch_core`: pass the agent `reason` into the `CognitionEdge` (single: the existing `reason` param; batch: `e.get("reason")`). `get_neighbors` can surface it. `create_deterministic_edges` constructs edges with no reason (None default — fine; a deterministic edge has no agent rationale).
- **Residual (peer-review B2, note it — don't claim full round-trip everywhere):** `storage.redirect_edges` rebuilds add_edge journal payloads BY HAND (only `timestamp`/`source`), not via `model_dump`, so a redirected edge drops `reason` in its journal line. Pre-existing partial-fidelity (it already drops everything but timestamp/source), low-traffic (node-supersession redirect only). Note as a known residual; widening redirect's fidelity is out of scope.

**Tests (rule 20):** an edge created with a `reason` round-trips through a journal REPLAY in a second storage instance (assert the reason on the replayed edge — the D1a-metadata-class round-trip test, fails-before RUN against the no-field version); batch edge reason persists.

## Commit 3 — `cognition_update_node` + re-embed (the gate-hard one)
- Extract a shared `_embed_entity_node(embedding_storage, generator, node)` from `_record_node`'s inline embed block (builds `embed_text = f"{type}: {summary}\n{detail}"` + metadata + `upsert_embedding(node_id, ...)`) — ONE embed path (ledger 11); `_record_node` calls it too (no behavior change — re-green its tests).
- `cognition_update_node(node_id, summary?, detail?, context?, severity?) -> {...}`: a WHITELIST of editable narrative fields ONLY (NOT id/type/references/metadata/timestamp — editing those would corrupt invariants like a document's sha/mode/doc: ref). Apply via `storage.update_node`. **If ANY whitelisted field changed AND embeddings are ready, RE-EMBED** the node-level vector via `_embed_entity_node` (re-read the post-update node) so search reflects the edit. If embeddings aren't ready, skip + report a `reembed: "deferred"`-style flag (the residual: the vector/metadata stays stale until re-embedded — note it; rare, since updates need a loaded model anyway).
  - **Gate correction (Vince, post-C3):** re-embed on ANY change, not just summary/detail. `_embed_entity_node` writes `context` + `severity` into the Chroma metadata that `_format_search_results` SURFACES in every hit, so a context/severity-only edit must refresh that metadata too — otherwise search results display the stale value (the WP's own search-staleness, on the metadata rather than the match vector). For such an edit the regenerated vector is identical (embed text unchanged); only the metadata refreshes via the same upsert — negligible on a rare path. Shipped as Commit 6.
- **DOCUMENT-node nuance (peer-review B1 — acknowledge, don't gloss):** re-embedding a document's node vector via `_embed_entity_node` is SAFE (the embed text `document: {summary}\n{detail}` matches what `_embed_document` used, and `_format_search_results` reads metadata defensively) but CHANGES the node-vector metadata shape from the doc-shape `{entity_type, summary}` to the entity-shape `{+author, timestamp, context, ...}`. Not corrupting (chunk vectors untouched — no `delete_by_node_id`; `is_chunk` count unaffected), just an edited-vs-never-edited asymmetry. ACCEPTED + noted in the tool docstring. Chunk vectors are sidecar-derived and unchanged by a summary edit.
- SKILL + README tables updated.

**Tests (rule 20, the crux, fails-before RUN):** record a node + embed it (real ChromaDB + a **text-KEYED fake generator** — NOT the existing constant-vector `_FixedGen`, which can't tell old from new; map distinct query/summary strings to distinct orthogonal vectors, or hash→vector), search finds the OLD summary; `update_node` the summary to new text; search now finds the NEW text and NOT the old (the vector was refreshed — both live under the same node_id, upsert overwrites, so no ghost/dedup ambiguity). Fails-before RUN (without the re-embed: search still serves the old vector — silent staleness). Also: the field whitelist rejects/ignores a structural field (id/type/metadata unchanged); update of a non-existent node → error.

## Commit 4 — expose `get_superseded_chain` (+ `get_incident_resolution`) (T-11)
Both are exported + tested but called by nothing; `cognition_remove_node` even recommends a supersedes chain no tool can traverse.
- `cognition_get_superseded_chain(node_id) -> {chain: [...]}` (newest→oldest version history via SUPERSEDES).
- `cognition_get_incident_resolution(node_id) -> {incident, resolutions, discoveries, contradictions}`. The identical if/else at queries.py ~178-182 IS a real dead branch (both arms `discoveries.append`, the DISCOVERY check inert) — FIX by COLLAPSING to a single append (pure simplification, NO behavior change; filtering to DISCOVERY-only would be a behavior change with nowhere for non-discovery `led_to` targets to go). Add a test exercising that branch (the existing test only asserts `resolutions`).
- **Imports (peer-review B3):** `cognition_tools.py` imports only `get_history_for_context`/`get_reasoning_chain` today — add `get_superseded_chain`, `get_incident_resolution` to the `..cognition` import (they're exported from `cognition/__init__.py`).
- SKILL + README tables updated.

**Tests:** a supersedes chain (A supersedes B supersedes C) → chain returns [A,B,C] newest-first via the tool; incident-resolution returns the resolutions. (Queries are already tested; this is the tool surface.)

## Commit 5 — composition review + docs guard
- Confirm the doc-drift guard is green (all new tools in the SKILL table); the shared `_embed_entity_node` is single-sourced (used by `_record_node` + `update_node`, no re-encoded copy); the edge `reason` round-trips replay.
- Run the full suite + ruff + pyright ≤ 8.

---

## Out of scope (tracked → BACKLOG)
- WP-Emb (incl. the E-3 query-prefix re-embed — needs coordination), WP-Core-tail (C-4/C-6/C-7), dashboard cosmetics, the over-query consistency — the P3 tail.
- `update_node` while embeddings are LOADING leaves a stale vector until re-embedded (documented residual; the embedding sync doesn't refresh existing vectors — a general re-embed-on-change pass is WP-Emb territory).
- Document chunk re-embedding on a node-summary edit (chunks are sidecar-derived; a content change goes through re-store, not update_node).

## Build order rationale
get_node (1, smallest, read-only) → edge reason (2, model+replay round-trip) → update_node+re-embed (3, the careful one, builds on the shared embed helper) → expose queries (4) → composition (5). Each commit independently green (suite + ruff + pyright ≤ 8; the doc-drift guard green because each tool-adding commit updates the SKILL table), every fails-before RUN.

## Verification gate (per push)
Full pytest (the re-embed fails-before RUN) + ruff + pyright ≤ 8 + the doc-drift guard green → push → CI green 3 legs → ping Vince the tip SHA → SHA-pinned merge gate.
