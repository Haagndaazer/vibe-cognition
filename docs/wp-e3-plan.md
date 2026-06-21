# WP-E-3 — asymmetric-retrieval fix: embed stored nodes with the DOCUMENT prefix

**Base:** `main` @ 63905a9. Branch: `fix/e3-doc-prefix`.
**Bundled with the XP feature into one release** (Colton: ~v0.9.0, after E-3 lands).
**Colton greenlit the re-embed; the migration runs on first start post-upgrade — ANNOUNCE before
he deploys/restarts.** Trigger = marker-gated auto-drop (Colton's call).

## The bug (audit E-3)

nomic-embed-text-v1.5 is trained for ASYMMETRIC retrieval — documents/nodes embedded with
`search_document: `, queries with `search_query: `; cosine is calibrated across that pair. Today
**every stored vector (entity nodes AND documents AND chunks) uses the QUERY prefix**
(`generate_query_embedding`), discarding the asymmetry → degraded ranking. The query side is
already correct. The correct path exists but is unused by storage:
`generate(text, input_type="document")` → `DOCUMENT_PREFIX` (`generator.py:159,169`; const `:37`).

**Why this can't be "forward-only":** the whole collection is uniformly query-prefixed today
(self-consistent). If new nodes go document-prefixed while old ones stay query-prefixed, a single
`query()` ranks a MIXED space under one cosine metric → ranking *worse* than today. So the existing
query-prefixed vectors must be replaced. **The simplification (Colton):** the chroma collection is
gitignored and fully regenerable from `journal.jsonl` (the graph is source of truth), so the
"migration" is just **drop the collection once and let the EXISTING startup sync rebuild it** —
no bespoke re-embed loop, no document/sidecar special-casing.

---

## Commit 1 — route all STORAGE embeds through the document prefix (the fix)

There are **FOUR storage embed sites** (all confirmed `tools/cognition_tools.py` / `server.py`).
**ALL FOUR must change in the SAME commit** — missing ANY one produces a mixed-prefix collection on
rebuild that the C2 marker then permanently locks in (no restart fixes it). This is the single
highest implementation risk; the fails-before test below exists to make a missed site go red.
1. `_embed_entity_node` — `cognition_tools.py:67` (entity nodes)
2. `_embed_document` node vector — `cognition_tools.py:457`
3. `_embed_document` chunk loop — `cognition_tools.py:464-470` (separate call in the same function —
   easy to miss; both 2 AND 3 must change)
4. `server.py` startup-sync inline non-doc embed — `server.py:112`

Each switches `generate_query_embedding(text)` → `generate(text, input_type="document")` (→
`DOCUMENT_PREFIX`). **Leave the SEARCH side unchanged** (`cognition_tools.py:386` + `:1281`) —
queries stay query-prefixed; that asymmetry IS the fix.

- **De-dup is OPTIONAL, not required.** Routing the `server.py:112` inline copy through
  `_embed_entity_node` would be cleaner (one path) BUT `_embed_entity_node` takes a `CognitionNode`
  while the sync holds a raw dict from `get_all_nodes()` — reconstructing the node (all of
  `id/type/summary/detail/context/references/severity/timestamp/author` must be present in the
  dict; verify) adds friction. Simpler and acceptable: **fix the inline call in place** and let the
  fails-before test guard against future drift. (One-path consolidation can be a later cleanup.)
- **Fails-before test (covers ALL four sites):** mock the generator; assert that record-time embed
  (`_embed_entity_node`, `_embed_document` node+chunks) AND the startup-sync embed path call
  `generate(..., input_type="document")` (or the backend applies `DOCUMENT_PREFIX`), and that the
  search path still uses the query prefix. Any un-fixed site → red. (This is the regression guard
  that makes "miss a site" impossible to merge green.)

This fix is what makes the rebuild (C2) produce a uniformly document-prefixed collection, AND makes
the normal additive sync self-heal consistently if the rebuild is ever interrupted.

## Commit 2 — marker-gated one-time collection drop → existing sync rebuilds

Reuse the existing rebuild; add only the drop + the marker gate.

- **`ChromaDBStorage.recreate_collection()`**: `self._client.delete_collection(name)` wrapped in
  try/except (chromadb 1.5.5 raises NotFoundError if the collection is absent — this is a DEFENSIVE
  guard against partial state, NOT an expected first-run condition: `__init__` already creates the
  collection in the lifespan before the background thread runs, so the collection exists by then).
  The guard wraps ONLY the delete, never the recreate. Then `get_or_create_collection(name,
  metadata=...)` with
  the SAME stamp `__init__` uses PLUS a schema marker `"embed_scheme": "doc-prefix-v1"`. Reassign
  `self._collection` to the new handle. (delete+recreate is the ONLY way to re-stamp metadata in
  chromadb 1.5.5 — XP1 finding — so this also re-stamps `embedding_model`/`embedding_dimensions`,
  closing home's XP1 `model_guard="unknown"` for free.)
- **Trigger (in the background embedding thread, BEFORE the sync runs — `server.py:197` region):**
  read the collection's metadata; if `embed_scheme` marker is ABSENT (pre-E-3 collection) → call
  `recreate_collection()`. Then the EXISTING `_sync_cognition_embeddings` runs as it already does —
  it backfills MISSING nodes, and after the drop ALL nodes are missing → full rebuild, now
  document-prefixed via C1. **No new re-embed loop.** Marker present → skip the drop (idempotent;
  runs exactly once per collection).
- **Ordering is load-bearing:** drop must happen BEFORE the sync in the same thread, so the sync
  sees an empty collection. Sequence: load model → check marker → (absent) recreate_collection →
  _sync_cognition_embeddings.
- **Crash-recovery:** if the process dies mid-rebuild, the marker is already set (stamped at
  recreate) so next start SKIPS the drop — but the normal additive sync backfills the
  still-missing nodes, document-prefixed (C1), so it self-heals to a consistent collection. (This
  only holds because C1 fixed the sync's embed path — verify no sync embed still uses the query
  prefix.)
- **Fails-before test:** build a collection with NO marker + query-prefixed vectors + some nodes;
  run the startup path; assert (a) collection rebuilt document-prefixed, (b) marker now present,
  (c) node count preserved (graph is source of truth — no data loss), (d) a SECOND startup is a
  no-op (marker present → no drop). Plus: assert the rebuild runs in the background thread (does not
  block lifespan readiness).

## Mid-rebuild query safety

A search arriving AFTER the drop but BEFORE the sync finishes hits an empty/partial collection →
should return few/no results, NOT an error. `vector_search` already swallows to `[]` on any
exception (`embeddings/storage.py`), and empty/partial collections return `[]`/fewer hits — safe.
**Note (review RC-5):** `embedding_ready` is set in the background thread BEFORE the
drop+rebuild (server.py:206, ahead of the sync at :220), so searches DURING the rebuild run and get
silently-degraded results with no in-band "rebuilding" signal to the caller. Acceptable given the
announce-before-execute coordination (the window is brief + one-time), but it is silent — do not
add a half-baked in-band flag in this WP; just document the window.

## Release coordination (bundled with XP)

After E-3 merges: run the **tool-surface self-sufficiency audit** over the XP tools, then bump
version (pyproject.toml + plugin.json, ~v0.9.0) + CHANGELOG (XP feature + E-3), hand Colton the
code-commit SHA for Loki to re-pin. **Human-gated release.**

**Announce-before-execute:** the drop+rebuild auto-runs on the first server start after v0.9.0
installs. Before Colton deploys/restarts, surface: "first start re-embeds the whole collection
document-prefixed (one-time, background, search degraded ~<estimate> for N nodes, no data loss —
graph is source of truth)." His release go IS the deploy gate.

## Known-intentional (do NOT "fix")

- Search side STAYS query-prefixed — the asymmetry is the fix, not an inconsistency.
- The collection drop is whole-collection by necessity (mixed prefixes break cosine), not
  over-scope — but it's CHEAP because the collection is regenerable and the rebuild already exists.
- delete+recreate (not in-place upsert) is required to re-stamp metadata in chromadb 1.5.5; closing
  home's XP1 "unknown" state is an intended side benefit.
- Documents are NOT special-cased — they rebuild via the existing sync's doc path (`_embed_document`
  is the shared store-time + sync path; its chunk delete-then-write is a no-op on a fresh
  collection). Colton barely uses them; no migration logic needed.
- **Ollama backend is OUT of scope (review RC-3):** `OllamaBackend.encode` ignores the query/doc
  distinction (no prefix applied), so C1 is a no-op for Ollama users — the asymmetric prefixes are
  nomic/sentence-transformers-specific. The fix targets the default SentenceTransformers backend.
  Note it in the CHANGELOG; don't try to "fix" Ollama here.
- "Quality improved" is not unit-testable; tests prove the MECHANISM (correct prefix, marker-gated
  rebuild, idempotent, no data loss). The quality link is the audit's premise.

## Acceptance criteria

- ALL FOUR storage embed sites (entity `_embed_entity_node:67`, document node `:457`, document
  chunks `:464-470`, sync inline `server.py:112`) use `DOCUMENT_PREFIX`; search uses `QUERY_PREFIX`
  — a single fails-before test covers all four so a missed site goes red.
- Marker-less collection → dropped + rebuilt document-prefixed on first startup; marker set;
  idempotent on second start; node count preserved (no data loss).
- `recreate_collection` re-stamps `embedding_model`/`embedding_dimensions` + marker; home
  `model_guard` becomes `"match"` post-migration.
- Rebuild runs in the background thread (no lifespan/readiness block); a query mid-rebuild returns
  `[]`/degraded, never an error.
- Whole-repo `uv run pyright` at baseline (server.py:167 only); full suite green; journal not on
  branch (manager flushes).
