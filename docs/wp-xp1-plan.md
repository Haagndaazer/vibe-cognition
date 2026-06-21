# WP-XP1 — Cross-Project Cognition: registry + load/unload/list + guards

**Base:** `main` @ 928e80f. Branch: `fix/xp1-registry`.
**Depends on:** WP-XP0 spike (GO on Option A; findings in `docs/wp-xp0-spike-findings.md`).
**Followed by:** WP-XP2 (read-tool `project` routing + provenance + semantic search over B).

XP1 is the **plumbing**: it makes a foreign project *loadable, listable, and unloadable*, with
all the safety guards, but does NOT yet route read tools to foreign projects (that's XP2). After
XP1 you can `load`/`list`/`unload` project B and confirm its bindings + guard state; querying B's
content is XP2. This keeps XP1 independently testable.

Design is Colton-approved + peer-reviewed: attach-as-separate-graph, read-only, semantic-search
(XP2), always-live, multiple projects loadable, home always pinned/un-unloadable, writes never
touch any dir but home.

---

## Commit 1 (PREREQUISITE) — stamp embedding model identity into NEW collection metadata

From XP0 Q4b: ChromaDB stores no model provenance (`collection.metadata` is just
`{'hnsw:space': 'cosine'}`), so two projects at the same dimension but different models would
return silent-garbage rankings. Stamp the model identity at collection CREATION so new
collections self-describe.

- **`ChromaDBStorage.__init__`** (`embeddings/storage.py:17-41`): pass model/dims to
  `get_or_create_collection`'s metadata: `{"hnsw:space": "cosine", "embedding_model": <model>,
  "embedding_dimensions": <dims>}`. Thread the config values into the **two** construction sites
  that build `ChromaDBStorage` — `server.py:253` and `dashboard/cli.py:31` (accept them as
  `ChromaDBStorage` params with safe defaults). NOTE: `backfill.py`/`prime.py` do NOT construct
  `ChromaDBStorage` — no change there.
- **NO re-stamp of existing collections.** chromadb 1.5.5 makes this UNSAFE (peer-review,
  empirically verified): `get_or_create_collection` with expanded metadata on an EXISTING
  collection **silently ignores** the new keys, and `collection.modify(metadata=...)` **replaces**
  (drops `hnsw:space`) and in fact **raises `ValueError`** if `hnsw:space` is present at all. So
  there is no safe in-place stamp. Existing collections (incl. the home project's own) stay
  **unstamped → `model_guard="unknown"` → warn-and-allow**. They self-correct only when their
  chroma is rebuilt (a fresh `get_or_create` then stamps it) — which the **E-3 re-embed** does for
  the home collection. (This supersedes the earlier "one-time metadata re-stamp" plan — see the
  Colton note in §Decisions below; the goal still lands, via E-3, just not as a separate pass.)
- **Known-intentional:** NOT E-3, no vectors recomputed. The stamp affects new collections only.
- **Fails-before tests:** (a) a freshly-created collection's metadata carries `embedding_model` +
  `embedding_dimensions` (old code: absent); (b) opening an EXISTING (pre-stamp) collection returns
  it unchanged — assert NO `collection.modify` is called and metadata still holds `hnsw:space`
  (proves we didn't introduce the drop-hnsw hazard); (c) `hnsw:space` survives on both paths.

## Commit 2 — `ChromaDBStorage.close()` actually closes (from XP0 Q2)

- `close()` (`embeddings/storage.py:200-202`, currently `pass`) → `self._client.close()`.
  Proven by XP0: this releases the Windows handle (rename succeeds after close; re-open works).
- **Fails-before test:** spy/mock the client; assert `close()` calls `self._client.close()`
  (old no-op: not called). Plus an integration check (may be Windows-gated): after `close()`, a
  fresh `PersistentClient` on the same path opens and reads prior data.

## Commit 3 — the `LoadedProjects` registry (lifespan)

- New object in the FastMCP lifespan context (e.g. `lc["loaded_projects"]`). Holds:
  - **Home entry**, built from the existing config at startup, `pinned=True`, keyed by the
    **resolved** home repo path. Its `storage`/`embeddings` ARE the existing
    `lc["cognition_storage"]` / `lc["cognition_embedding_storage"]` (no duplication).
  - A dict of **foreign entries** keyed by resolved canonical path.
  - Entry = `{path (resolved), tag, storage: CognitionStorage, embeddings: ChromaDBStorage | None,
    pinned: bool, model_guard: "match"|"unknown"|"no-index"|"dim-mismatch"|"model-mismatch"}`.
    `embeddings` is `None` when B has no usable vector index (absent, dim- or model-mismatch) →
    structural-only attach.
- **Blast-radius control:** leave `lc["cognition_storage"]` / `lc["cognition_embedding_storage"]`
  pointing at HOME, unchanged — every existing tool keeps working untouched. The registry is
  ADDITIVE. Writes continue to use `lc["cognition_storage"]` (home) → the write-isolation
  invariant holds for free in XP1 (no write tool gains a `project` arg here or in XP2).
- A resolver helper `resolve_project(lc, project_arg) -> entry | list[entry]` is STUBBED here
  (default → home) for XP2 to extend; XP1 doesn't wire it into read tools yet.

## Commit 4 — the three tools

- **`cognition_load_project(path)`**:
  1. Resolve + canonicalize `path` (`Path(path).resolve()`); normalize trailing slash / relative.
  2. Refuse if resolved == home path ("already loaded as home").
  3. Validate `<path>/.cognition/journal.jsonl` exists → friendly error if not ("no cognition
     graph at <path>").
  4. Build `CognitionStorage(<path>/.cognition)` — its `__init__` does `mkdir(exist_ok=True)` on
     an already-existing dir (no-op, validated in step 3) + `_catch_up()` (READ-ONLY). Open B's
     chroma via a **READ-ONLY path** — NOT the default `ChromaDBStorage.__init__`, which calls
     `get_or_create_collection` and would CREATE `chroma.sqlite3` if B has no vector index (a WRITE
     to B — violates read-only). Add **`ChromaDBStorage.open_existing(path, collection_name)`** (or
     an `init` flag `create=False`) that uses `client.get_collection(...)` and, if B's chroma dir
     or collection is **absent, returns `None` (NEVER creates)** — the load DEGRADES to
     structural-only rather than failing or writing. `ChromaDBStorage.__init__` (home init) is
     UNCHANGED. **Never** run embedding-sync or deterministic-edge backfill against B (those WRITE).
     Read-only invariant.
  5. **Load-time guard** (XP0 Q4) — runs only when B's chroma binding is present. A guard failure
     disables SEMANTIC search for B but never blocks the structural attach, and never writes to B:
     - chroma binding `None` (no index) → attach structural-only; `model_guard="no-index"`;
       warning "semantic search unavailable for <B> (no vector index)".
     - dim present and ≠ A's `embedding_dimensions` → drop the chroma binding (query would raise);
       structural-only; `model_guard="dim-mismatch"` + clear warning.
     - model present and ≠ A's `embedding_model` → drop the chroma binding (silent-garbage risk);
       structural-only; `model_guard="model-mismatch"` + warning.
     - model present and == A → `model_guard="match"` (semantic search enabled in XP2).
     - model absent (pre-stamp collection) → **warn-and-allow**, `model_guard="unknown"`.
     - empty collection (no vectors to probe dim) → allow, `model_guard="unknown"`.
  6. Assign a `tag` (repo dir name); auto-suffix `-2`, `-3` on collision; exact-path re-load
     returns a friendly "already loaded".
  7. Register; return `{tag, path, node_count, vector_count, model_guard, warning?}`.
- **`cognition_unload_project(project)`**: resolve by tag or path; **refuse if pinned/home**
  (resolved-path comparison); call the foreign chroma `close()` **only if the binding is not
  `None`** (`if entry.embeddings: entry.embeddings.close()` — structural-only entries have no
  binding; a bare `.close()` would `AttributeError`); drop entry. Test both: a chroma-bound unload
  closes the client; a structural-only (no-index) unload drops cleanly without error.
- **`cognition_list_projects()`**: home (pinned) + each foreign — `tag`, `path`, `node_count`,
  `vector_count` (0 / "n/a" when the binding is `None`), `pinned`, `model_guard`. Also extend
  `get_status` to mention the count of loaded foreign projects (so an agent on the home tools sees
  them).

## Guards (cross-cutting)

- **Home-pin:** unload refuses when resolved target == home resolved path OR `pinned`. Test against
  resolved-path variants (trailing slash, `.`-relative, mixed separators) — all must map to home.
- **Write-isolation:** add a guard/assert (or a single `home_storage(lc)` accessor that the write
  tools use) so a future refactor can't route a write to a foreign entry. Test: after loading B,
  `cognition_record` leaves B's `journal.jsonl` mtime + B's chroma vector count unchanged.
- **Read-only on load:** test that `load_project` + `list_projects` + `unload_project` leave B's
  `journal.jsonl` mtime and chroma unchanged (no sync/backfill fired against B).

## Out of scope (→ XP2)

- The `project` arg on read tools (search/get_node/get_chain/get_neighbors/get_history/...),
  result provenance tags, semantic search OVER B, the `"*"` fan-across-all. XP1 loads/lists/unloads
  only; reads still go to home.

## Decisions (corrections folded from the XP1 peer-review)

- **No in-place re-stamp of existing collections.** chromadb 1.5.5 has no safe path
  (`get_or_create` ignores metadata on an existing collection; `collection.modify` drops/raises on
  `hnsw:space`). Commit 1 stamps only NEW collections. Existing collections (incl. home's own) ride
  as `model_guard="unknown"` (warn-and-allow) and get a real stamp only when their chroma is
  rebuilt — which the **E-3 re-embed** does for the home collection. **COLTON NOTE:** this
  supersedes the "one-time re-stamp on existing embeddings" you approved — that exact mechanism
  isn't possible in chromadb 1.5.5, but the same end-state (home collection stamped) is delivered by
  the E-3 re-embed you also greenlit. Practically harmless meanwhile: all projects use the same
  model, so "unknown" never causes a wrong refuse.
- **A guard failure degrades to structural-only, never a hard load failure and never a write to B.**
  Foreign chroma opens read-only via `get_collection` (returns `None` if absent).

## Known-intentional (do NOT "fix")

- Collection name stays `"cognition_embeddings"`.
- The model stamp is metadata-only and applies to NEW collections only — NOT a re-embed (E-3 is
  the re-embed).
- The always-live vector-freshness ceiling (foreign vectors only as fresh as B's own sync) is an
  accepted design decision.
- `model_guard="unknown"` / `"no-index"` (warn-and-allow, structural-only) is intended — refusing
  would block every pre-stamp or un-indexed project.

## Acceptance criteria

- Fails-before on every behavioral delta (Commit-1 stamp on NEW collections, Commit-2 close, the
  guards). Commit-1 test PROVES no `collection.modify` is called on an existing collection and
  `hnsw:space` survives (the drop-hnsw hazard the rework avoids).
- Read-only invariant PROVEN: B's `journal.jsonl` mtime + B's chroma (vector count AND the chroma
  dir's existence — a load of a no-index B must NOT create `chroma.sqlite3`) unchanged after
  load/list/unload.
- Structural-only degrade PROVEN: loading a B with a journal but no chroma dir attaches
  (`model_guard="no-index"`), writes nothing to B, and `list_projects` shows it.
- Home-pin guard proven against resolved-path variants.
- Write-isolation proven: a home write after loading B touches only home.
- Tag collision auto-suffix + exact-path re-load handled.
- pyright at baseline (server.py:167 pre-existing only; no new errors); CI green 3 legs.
- Journal not committed on branch; manager flushes.
