# WP-D2 Execution Plan — chunked document search (v0.8.0)

Brief = `docs/DESIGN-document-storage.md` §3 + §7 (WP-D2 row) + the §3 "integration debts the peer review surfaced (all in WP-D2 acceptance)". Builds on the merged D1 core (`0faf302`): documents have a node, metadata, `doc:<sha[:12]>` ref, an agent-extracted **text sidecar**, reference/copy modes, and are currently **NOT embedded** at all (D1a/D1b deliberately skipped them). D2 makes documents **searchable**.

## What D2 delivers (DESIGN §3)
1. **Chunk the sidecar text into ChromaDB** as `<node_id>#chunk-N` (~1000-token windows, ~100 overlap). Chunk text stored as Chroma **documents** (today's `upsert_embedding` stores none — must change).
2. **`cognition_search` over-query + dedupe-to-best-hit-per-node**, returning the node with a `matched_excerpt`.
3. **Teammate re-sync chunking** (`_sync_cognition_embeddings`): detect document nodes lacking chunks and re-chunk from the sidecar — AND **backfill node-level embeddings** for any document created in the D1a/D1b interim (deliberately never embedded).
4. **`get_status` count split**: node vectors vs chunk vectors reported separately.
5. **Optional multi-call text append** (only if needed; DESIGN §2 — keep minimal).

## Binding rules (carried)
Rule 20 (assertions name the failure mode; fix+proof same commit), 12 (fails-before RUN), 11 (composition review), 21 (re-search constraints bound to touched files), 18 (seam-check). Journal protocol (no journal commits; flush via worktree). pyright baseline **29** (lower in-PR if reduced, never exceed). SHA-pinned merge gate + voiding clause.

## Carry-over context (do NOT re-litigate)
- D1 chunk-purge wiring already exists: `ChromaDBStorage.delete_by_node_id(node_id)` → `delete(where={"node_id": node_id})`, called in `delete_cognition_node`. D2 makes it load-bearing (chunks now exist). The artifact-class delete test must extend to real chunks.
- N1 fix: `cognition_search` routes through `_search_cognition` → `_format_search_results`, which strips `#chunk-` and drops graph-absent hits. **D2 must keep search routed through that core** — chunk hits strip to their node id and are dropped if the node is gone.
- `documents_with_sha` is THE shared identity predicate. Re-chunk/backfill must not duplicate it.
- `vector_search` currently `include=["metadatas","distances"]` — must add `"documents"` to return chunk text.
- `upsert_embedding`/`bulk_upsert` flatten metadata; `_flatten_metadata` uses deprecated `datetime.utcnow()` (audit E-7 / backlog) — do NOT expand its use; leave it (rule 21).

---

## Commit 1 — embedding store carries chunk TEXT (enabling change, no behavior change to existing nodes)
`ChromaDBStorage`:
- `upsert_embedding(entity_id, embedding, metadata, document: str | None = None)` gains an optional chunk-text arg, passed as Chroma `documents=[...]` (None → omit, so existing node-vector upserts are byte-identical). Verified (peer-review C1): a Chroma collection can MIX text-bearing and text-less entries — no "all-or-none documents" requirement. (`bulk_upsert` has zero callers in src — leave it untouched, NOT forward-threaded; per-review B1.)
- `vector_search` adds `"documents"` to `include` and surfaces the hit text as `matched_text` in each result dict — pull `results["documents"][0][i]` with the same `if i < len(...)` guard the loop already uses for metadatas/distances; it is `None` for text-less node vectors (verified C2), so `matched_text` is cleanly absent — backward compatible.

**Tests (rule 20):** upsert WITH document → vector_search returns its `matched_text`; upsert WITHOUT document (existing node path) → `matched_text` None/absent, result shape otherwise unchanged (no regression). All existing embedding tests stay green.

## Commit 2 — chunking helper (pure, deterministic)
New `cognition/chunking.py` (stdlib-only): `chunk_text(text, *, window, overlap) -> list[str]`.
- **Token approximation:** the agent already bears the real token cost; the server chunks by a deterministic, dependency-free measure. Default to a **word/whitespace-based window** (~1000 words ≈ DESIGN's ~1000 tokens, ~100 overlap) — NOT the model tokenizer (avoids coupling chunking to the embedding backend and a second tokenizer dependency; the ~window is approximate by design). State the approximation explicitly.
- Deterministic + total-coverage (every character of the sidecar lands in ≥1 chunk; overlap is additive); empty/short text → 0 or 1 chunk; stable chunk count for a given text.
- **Chunk-count SHRINK hazard (peer-review A5):** same-text re-chunk is idempotent by id, but a re-store/version-bump/constant-change can yield FEWER chunks for a node_id — upsert touches `#chunk-0..M` and leaves stale `#chunk-(M+1)..` orphaned under the LIVE node_id, so `has_node` passes and they surface in search as ghost excerpts of deleted text (N1-class, from the re-chunk side). **The chunker stays pure; the fix lives in the write paths (Commits 3 & 4): `delete_by_node_id(node_id)` BEFORE writing the fresh chunk set** (delete-then-write), so chunk count can shrink without orphans.

**Tests (rule 20):** exact chunk count + boundaries for a known multi-window text; overlap present between adjacent chunks; empty → []; single-window text → 1 chunk; idempotent (same text → same chunks). No embedding/IO here — pure. (The shrink-orphan fails-before lives with the write path: re-chunk with fewer chunks → assert no orphan high-N chunk survives → red without delete-then-write.)

## Commit 3 — write document chunks (+ node vector) at store time
`_store_document` (D1 core) gains embedding, since D2 makes documents searchable:
- After writing the node + sidecar: embed the document **node** (so it appears in node-level search) AND chunk the sidecar text → **delete any existing chunks for this node_id first** (`delete_by_node_id`, no-op-safe — see A5) then upsert `<node_id>#chunk-N` each with its chunk text + metadata `{node_id, entity_type: "document", is_chunk: True}`. **`is_chunk: True` is a REQUIRED positive discriminator (peer-review A1):** ChromaDB 1.5.5 has no `$exists`, and `$ne` matches entries that LACK a field, so "entries with node_id" is NOT expressible — the node-vs-chunk count (Commit 6) can only separate them via a positive `is_chunk` marker the chunk-write path sets here. The document NODE vector carries `entity_type: "document"` and NO `is_chunk`.
- **Signature (peer-review A2/A3):** `_store_document` gains `embedding_storage=None, generator=None` — **optional, skip embedding when either is None** (mirrors how `_record_node` conditionalizes on `embedding_ready`). This keeps the ~30 existing storage-only `_store_document(s, ...)` test calls green. The tool wrapper threads the lifespan embedding store + generator in. The test fakes' `upsert_embedding(self, entity_id, embedding, metadata)` must also gain `document=None` (the fake at the chunk-write path would otherwise `TypeError`).
- **Guard:** if embeddings aren't ready (model still loading — `require_embeddings`) or the deps are None, store the node/sidecar/blob now and DEFER embedding to the next `_sync` — never block or fail the store on embedding readiness.
- This **removes D1a's document-skip** from the store path. The `_sync` document-skip (D1a) is replaced by the re-sync logic in Commit 4.

**Tests (rule 20):** store a document → its node vector (no `is_chunk`) + N chunk vectors (`is_chunk: True`, `node_id` set) land in Chroma; a search hits a chunk and returns the node; storage-only `_store_document` (no embedding deps) still works (no regression); deferred path — store with embeddings "not ready" writes node+sidecar and zero vectors, next sync backfills.

## Commit 4 — re-sync chunking + interim backfill (`_sync_cognition_embeddings`)
Replace D1a's blanket document-skip with document-aware sync. **Retire/invert the D1a test `test_sync_skips_document_nodes_never_embeds_them`** (test_documents.py) — its guarantee (documents never embedded) is intentionally reversed by D2; note that in the commit.
- **Precise "chunked?" idempotency key (peer-review A4):** a document is fully synced iff its node vector exists AND (the sidecar is empty/absent OR `<id>#chunk-0` exists). Probe `<id>#chunk-0` (a single cheap id lookup), NOT a per-document `get(where=)` for N documents every start. Without the "sidecar empty" branch, a zero-chunk empty-text document would be "missing" forever and re-embed every boot — the exact re-embed loop the D1a skip was added to prevent.
- For each under-synced document: node-level embed (backfilling the D1a/D1b-interim documents never embedded) + **delete-then-write** its chunks (`delete_by_node_id` then upsert fresh — see A5) from the sidecar (`read_text_sidecar`). If the sidecar is missing (teammate pulled the journal but not the sidecar — reference mode, machine-specific), embed the node from `detail` and log chunks unavailable (not an error).
- Keep the orphan-reconciliation sweep (D1b) — it already reclaims orphan `#chunk-*` whose node is gone (chunk-strip in the sweep). Ordering verified safe (peer-review C7): the sweep runs after the add pass and keys on GRAPH presence, so a just-added chunk whose node is in the graph is never swept.

**Tests (rule 20):** seed a document node with a sidecar but NO chunks (the interim case) → sync → node vector + chunks appear; sidecar-missing document → node embedded, no chunks, no raise; re-sync is idempotent (no duplicate chunks, and an already-chunked document is NOT re-embedded every run — assert no churn); empty-sidecar document → not re-embedded every run (the A4 loop guard).

## Commit 5 — `cognition_search` over-query + dedupe-to-best-hit-per-node + matched_excerpt
In `_search_cognition`:
- **ADAPTIVE over-query** (revised — a FIXED `limit*k` cannot satisfy B3: one document can yield more than `limit*k` chunks and starve other live nodes). Start at `n = limit*k` (k=5) and DOUBLE `n_results` until we have `limit` distinct live nodes, OR Chroma is exhausted (returned < n), OR a cap (`_SEARCH_OVERQUERY_CAP`) is hit. Doubling keeps round-trips logarithmic. Residual: a single document with > cap chunks degrades recall only (never a wrong/deleted node — dedupe + N1 are exact); ACCEPTED + capped. The `entity_type` filter applies Chroma-side before dedupe.
- **Dedupe to best hit per node:** strip `#chunk-` to the node id, keep the highest-score hit per node, carry its `matched_text` as `matched_excerpt` (truncated to a sane length). The existing N1 graph-presence filter still applies (drop graph-absent nodes).
- Return at most `limit` deduped nodes, each with `matched_excerpt` when the best hit was a chunk.

**Tests (rule 20):** two chunks of one document + one other node → search returns the document ONCE (best chunk) with `matched_excerpt`, plus the other node; the deduped node id is the stripped node id, not the chunk id; **a document with k+ chunks all out-ranking other nodes, `limit=2` → still returns 2 DISTINCT nodes** (proves over-query k is sufficient, B3); N1 filter still drops a deleted document's chunk hits; `node_type="document"` filter returns chunk hits deduped to their node.

## Commit 6 — `get_status` node-vs-chunk count split + composition review + dashboard N1 decision
- **Count split:** `get_status` reports `cognition_embeddings` as `{nodes, chunks, total}`. Count chunks via `count_documents(filter={"is_chunk": True})` — the PUBLIC param is `filter=`, not `where=` (peer-review N1; `where` is the internal `_collection.get` kwarg). The positive `is_chunk` marker is set in Commit 3 (A1: chromadb 1.5.5 has no field-presence predicate, and `entity_type=="document"` conflates document node vectors with document chunks); `nodes = total − chunks`. Don't let chunks silently inflate the old single count.
- **Test-fake updates (peer-review N2/N3):** the dashboard fake `count_documents(self) -> int` (test_dashboard.py) takes no arg — give it `filter=None` so the split call doesn't `TypeError`. Align the pre-existing chunk seeds (test_documents.py `test_reconcile_orphan_sweep...` and `test_all_artifact_classes...`) to the new chunk-metadata contract (`{node_id, entity_type:"document", is_chunk:True}`) so the contract isn't split across two shapes.
- **Composition review (rule 11):** chunking × dedupe × deletion × N1 — e.g. delete a document with chunks → `delete_by_node_id` purges all its chunks (extend the D1b artifact-class test to REAL chunks, not a single seeded one); a re-synced document doesn't double-embed; search dedupe + N1 filter compose (a deleted document's multiple chunk hits all drop).
- **Optional multi-call append (DESIGN §2):** include ONLY if a concrete need surfaces; otherwise explicitly defer (note it).

### Dashboard document-search NAVIGATION — DEFERRED to WP-D4 (scope note, not a silent regression)
The dashboard N1 SAFETY filter (below) lands in D2, but document hits there surface as un-deduped chunk rows (`_id == "<node>#chunk-N"`) that don't navigate (no graph node by that id) and lack node metadata. Safety is intact (no deleted text served); only dashboard document-search navigation is incomplete. Dedupe-to-node + node hydration is WP-D4's cluster (the dashboard raw-shape contract is deliberate, and an un-testable JS change is higher-risk than a documented deferral). The MCP `cognition_search` surface IS deduped to nodes today. Stated in the dashboard code so it's not silent.

### Dashboard N1 — DECISION: pull the one-line filter INTO D2 (recommended), not WP-D4
Vince's scoping call. **Analysis:** the dashboard search (`dashboard/api.py` `search()` → `vector_search` raw, no `has_node` filter) is harmless TODAY only because documents aren't embedded. **D2 is precisely what makes documents searchable**, so D2 escalates the dashboard ghost from "stale decision summary" to **verbatim deleted client-document chunk text** served cross-process — the exact harm the N1 fix exists to prevent. The principle already applied in D1b (documents escalated N1 → N1 fixed in the PR that raised the stakes) says the mitigation belongs in the PR that introduces the risk. The fix is a SMALL INLINE filter — NOT a one-liner and NOT a `_format_search_results` reuse (peer-review B2): `dashboard/api.py search()` returns RAW `vector_search` dicts (`{_id, **metadata, score}`) while `_format_search_results` REMAPS keys (`id/node_type/summary/...`), so reusing it would silently break the dashboard's response contract + its JS consumer. The graph handle IS in scope (`lc["cognition_storage"]`), so add a ~3–4-line inline filter that strips `#chunk-` and drops `not has_node`, PRESERVING the raw dict shape. **Recommend: pull it into this WP** (Commit 6) with a fails-before test (dashboard search returns a cross-process document-chunk ghost → red → filtered). If Vince prefers it stay WP-D4, the residual must be stated loudly: D2 ships a surface that serves deleted client text via the dashboard. Surfacing the decision per his instruction; my recommendation is to fix it in D2.

---

## Out of scope (tracked)
- WP-D3 (`/vibe-document` skill), WP-D4 (dashboard document list + token-gated download).
- Vince backlog #1 (global node-id collision), #2 (has_node→add_node TOCTOU), audit E-7 (`datetime.utcnow()` in `_flatten_metadata`).

## Build order rationale
Storage text-carry (1) and the pure chunker (2) are the dependency-free enablers. Store-time chunking (3) + re-sync/backfill (4) are the write paths (paired so a stored doc is always re-syncable). Search dedupe (5) is the read path. Status + composition + the dashboard decision (6) close it. Each commit independently green (suite + ruff + pyright ≤ 29), every fails-before RUN.

## Verification gate (per push)
Full pytest + ruff + pyright ≤ 29; every fails-before RUN; push → CI green 3 legs → ping Vince the tip SHA → SHA-pinned merge gate.
