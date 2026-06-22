P2 capability gaps from the audit/BACKLOG that compose as tool/query additions: T-5 (read + edit a node), T-4 (persist edge `reason`), T-11 (expose two orphaned queries). Five independently-green commits off the WP-Cap plan.

## Commits
1. **`cognition_get_node`** — search results and `get_neighbors` omit `detail`; after a hit there was no way to read the full node. Thin wrapper over `storage.get_node` (re-attaches the graph key as `id`). The generic node read; `cognition_get_document` stays the document-specialized get-by-id.
2. **Persist the edge `reason` (T-4)** — the edge-analyzer produces a curation rationale per edge, but `add_edge` only logged it, the batch dropped it, and `CognitionEdge` had no field. Add `reason: str | None`; `add_edge` writes it (model_dump → journal) and the replay branch reads `data.get("reason")` (graceful for old journals). Single + batch tool paths carry it; `get_neighbors` surfaces it.
3. **`cognition_update_node` + mandatory re-embed (gate-hard)** — lets an agent fix a typo without delete+re-record (which loses the id, edges, curation marker). The hazard it closes: the embedding sync only ADDS missing nodes, never refreshes an existing vector, so a summary/detail edit would leave `cognition_search` serving the STALE vector (silent search-staleness — same class as the WP-ID orphan-vector). So the tool RE-EMBEDS on a searchable-text change, through the extracted single `_embed_entity_node` path (ledger 11). WHITELIST of narrative fields only (summary/detail/context/severity); structural fields (id/type/references/metadata/timestamp) are not editable — they back invariants (a document's sha/mode/`doc:` ref, the part_of index, the minted id).
4. **Expose `get_superseded_chain` + `get_incident_resolution` (T-11)** — both exported + tested but called by nothing (`cognition_remove_node` even recommends a supersedes chain no tool could traverse). Also collapses a dead if/else in `get_incident_resolution` (both arms appended to `discoveries` identically; the DISCOVERY check was inert) — pure simplification, no behavior change.
5. **Composition** — update preserves id/edges/curation across an edit; record and update share one embed path (identical text → identical vector).

## The re-embed proof (rule 12, fails-before — the load-bearing one)
The re-embed test uses a **text-KEYED fake embedder** (orthogonal vector per marker word), NOT a constant-vector fake — a constant fake literally can't tell "re-embedded" from "stale", so the proof would be tautological. Record on ALPHA → search finds it; `update_node(summary=beta)` + re-embed → a BETA search scores ~1.0 and ALPHA collapses to ~0. **Fails-before:** dropping the re-embed — even while still claiming `reembed="done"` — leaves the BETA search at ~0; caught. The edge-reason round-trip and the incident-branch guard are proven the same way.

## Cross-cutting
- **Doc-drift guard (WP-D3):** every tool-adding commit updates the SKILL table (+ README) in the same commit, so each commit is independently green and the surface docs stay honest.
- **Journal protocol:** no commit touches `.cognition/journal.jsonl`.

## Known residuals (noted, out of scope)
- `storage.redirect_edges` rebuilds add_edge journal payloads by hand (timestamp/source only), so a redirected edge drops `reason` in its journal line — pre-existing partial fidelity, low-traffic (node-supersession only).
- `update_node` while the model is still loading reports `reembed: "deferred"` and the vector stays stale until a future re-embed (rare — an edit needs a loaded model anyway). A general re-embed-on-change pass is WP-Emb territory.

## Verified
Full suite 240 green, ruff clean, pyright == baseline (8). Every guard's fails-before run done (revert → red → restore) for the re-embed, the edge-reason replay, and the incident branch.
