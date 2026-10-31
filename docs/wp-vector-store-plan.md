# WP-VectorStore: replace ChromaDB with a self-healing SQLite vector store

Status: rev 2 (2026-10-31). The sonnet plan review is folded in (§10); where §10 and an earlier section disagree, §10 wins. It revives `docs/wp-vector-store-sqlite-shelved.md`, whose reviewed findings are kept unless this document says otherwise.
Ruling: decision `b309ac5504c2` (Colton, 2026-10-31): replace chromadb, peer-reviewed plan, then implement. It partly supersedes `c175bdb49a6c` (2026-10-05). Task `0857de583304`.
Evidence: discovery `66fd09a732c9` (2026-10-30); the earlier incident is discovery `d603acc95c54` (2026-09-30).

## 1. Why

On 2026-09-30 and again on 2026-10-30 every new Ling Lang session failed to connect. The server process died with `0xC0000005` about 0.1 s after opening the store, so there was no traceback and Claude Code reported only `CONNECTION_CLOSED`.

- **Mechanism (2026-10-30, reproduced on a scratch copy with no other process attached):**
  - The saved HNSW index (`index_metadata.pickle`) records 5459 labels added but only 5160 live ids, meaning 299 tombstones.
  - `data_level0.bin` has room for 5462 elements, so only 3 slots are free.
  - On load, chromadb replays the 54 unsaved `embeddings_queue` entries on top of that index.
  - Any `get()` then crashes natively, while `count()` works. With the queue removed, `get()` works 100% of the time. At about 21 queued entries the crash is flaky, and at 54 it is near-certain.
  - Healthy stores (2025_Enhanced_Nav, vibe-memory, vibe-cognition) all show added == ids == capacity.
- **It is a single-process chromadb bug.** That matters because the 2026-10-05 ruling looked to a hosted service for multi-process safety, and a hosted service that still ran chromadb would crash the same way.
- **Its trigger is ordinary use:** churn from re-chunked documents and re-embedded nodes creates tombstones. Ling Lang hit it twice in a month.
- **A side benefit is start-up speed:** `import chromadb` alone costs 1.6 to 2.2 s cold, against 0.26 s for numpy plus sqlite3. Measured after implementation, the server module imports in 2.7 s instead of 3.5 s (alternating warm runs on Colton's machine). Today's server needs about 3.4 s before it answers, past Claude Code 2.1.314's 3 s `server/discover` probe bound.

## 2. Goals and non-goals

Goals
- G1. No state of the vector store can crash the server process natively.
- G2. Any number of same-project processes read and write the store concurrently without corruption, through SQLite's own multi-process locking.
- G3. The store is a regenerable cache of the journal. Damage never fails start-up and never fails a graph write. A damaged store is set aside and rebuilt, the user is told once, and graph tools keep working throughout.
- G4. Search is unchanged for callers: the same hit shape (`_id`, flattened metadata, `score`, optional `matched_text`), the same score (`score = 1 - cosine_distance`, which is cosine similarity), the same filters, and the same error behaviour (`[]` plus a logged error).
- G5. The server process never imports chromadb. The package itself is uninstalled in the following release (§4.12).

Non-goals: no change to the journal, the graph, the embedding model or the sidecar. No approximate-nearest-neighbour (ANN) index, because exact search is enough at our scale (§4.6). No salvaging vectors from old chroma stores (§9). No hosted service.

## 3. Rulings and defaults taken (no open questions block this plan)

- Shelved Q1 (an interim chromadb-guard release): **no.** Ling Lang was unblocked by a manual quarantine (`.corrupt-261031`), and this WP replaces chromadb outright.
- Shelved Q2 (old `chromadb/` dirs): **leave them untouched for one release** so a downgrade to 0.46.x still finds them. A later release removes them.
- The 246 empty `repo-*` stores that tests leaked into the real data dir (task `46822ab83433`): the leak is fixed here (§7, test 16), and deleting the strays is left to Colton.

## 4. Design

### 4.1 Changes from the shelved design, and why

| Shelved design | This plan | Why |
| --- | --- | --- |
| Remove the `chromadb` dependency now | Keep it installed for one release, but never import it; remove it in the next release | On Windows, `uv sync` cannot delete `chromadb_rust_bindings.pyd` while an older 0.46.x server in another open session has it loaded. That is the half-swapped-venv failure the SessionStart probe already warns about (WP-5). Keeping the package makes this release's sync a no-op for third-party packages. |
| No-data-root fallback at `<repo>/.cognition/vectors/`, plus a hygiene v10→v11 bump | Fallback at `<repo>/.cognition/local/vectors/` | `local/` is already ignored by git (`_GITIGNORE_ENTRIES`) and by SVN (derived globs). No hygiene bump, so no modified `.cognition/.gitignore` appears in every user's repo, and no SVN change. |
| Quarantine by renaming the whole store directory, under a lock, retrying every 5 min if Windows blocks the rename, and comparing POSIX inode identity | **Generation files with corrupt markers** (§4.8): mark generation g as corrupt with an empty marker file and move to generation g+1 | Nothing that is open ever has to be renamed or deleted. It needs no lock and no retry loop, behaves the same on Windows and POSIX, and makes concurrent quarantines idempotent. |
| A rebuild lease (a heartbeat lock file) so only one process bulk-embeds | Each process **shuffles** its missing list and re-checks each node just before embedding it | Same-project processes split the work instead of duplicating it, with no lease, no staleness rules and no extra lock. |
| One connection per thread | One connection per store object, guarded by an `RLock` | Fewer moving parts. The dispatch pool is fixed and small, and every operation is short. |
| Own writes patch the matrix cache in place | Own writes mark the cache dirty, and the next search reloads | A reload costs tens of ms at our sizes, and there is no partial-patch logic to get wrong. |
| Matrix load pre-yield as the health check | Pre-yield: open, schema check, count, plus a tiny numpy matmul to warm BLAS. Matrix load and `quick_check` run **post-yield** in the background thread | Handshake time stays flat however large the store grows. All corruption here is catchable, so detection does not need to happen pre-yield. |
| A mid-session quarantine leaves the index empty until restart | The background thread stays alive as a maintenance loop and re-runs the sync when the store's generation changes | No new thread is started post-yield (WP-Wedge INV-1). |
| `upsert` rejects a wrong dimension (as chroma did, by raising) | `upsert` **never raises**. It logs and skips, and the next start-up sync fills the gap | A cache must never fail a graph write. Today a chroma error at that point raises out of `cognition_record` after the node is already in the graph. |
| No signal that results are partial during a rebuild | `cognition_search` and the dashboard search carry `index_syncing: true` while the start-up (re)build runs | After the upgrade every project rebuilds once, which takes minutes, and agents should know their results are partial. |
| `VIBE_CHROMADB_DIR` read as a fallback for one release | `VIBE_VECTORS_DIR` replaces it. `VIBE_CHROMADB_DIR` is ignored, with a one-line warning | Pointing the new store into an old chroma dir would mix two formats in one folder. Only tests use the old variable. |
| (not covered) | A store whose schema is newer than this code is opened **degraded and never quarantined** | Downgrade safety: an older plugin must never destroy a newer plugin's healthy store. |

### 4.2 Location and files

- Home store directory: `VIBE_VECTORS_DIR` if set (exact directory), else `<data root>/vectors/<project key>/`, else `<repo>/.cognition/local/vectors/`.
  - The data root resolves exactly as today (`VIBE_DATA_DIR`, then `CLAUDE_PLUGIN_DATA`, then a single glob match).
  - The project key is today's `chroma_project_key`, renamed `project_store_key`.
- Foreign store directory: `<data root>/vectors/<key>/` if it exists, else `<repo>/.cognition/local/vectors/` if it exists, else none (structural-only, §4.10). It never honours `VIBE_VECTORS_DIR`, for the same reason today's code ignores `VIBE_CHROMADB_DIR`.
- Inside the directory:
  - `vectors-<g>.sqlite3` with its `-wal` and `-shm` files, for g = 1, 2, …
  - `vectors-<g>.corrupt`, an empty marker meaning generation g is abandoned.
  - The **active generation** is the highest g that has any file (database, `-wal`, `-shm` or marker), plus one if that g is marked; 1 when the directory is empty. Every process computes the same answer from a directory listing, and deleting an old generation's files never moves it backwards (§10, finding 2).
- New names: `config.resolve_vectors_dir`, `config.resolve_foreign_vectors_dir`, `Settings.cognition_vectors_path`, `config.resolve_store_data_root`. The old names are removed, and every caller and test is updated (no aliases inside the codebase).

### 4.3 Schema (`PRAGMA user_version = 1`)

```sql
CREATE TABLE vectors (
  id          TEXT PRIMARY KEY,
  embedding   BLOB NOT NULL,      -- float32 little-endian, L2-normalised at write
  metadata    TEXT NOT NULL,      -- JSON of the flattened metadata, including updated_at
  document    TEXT,               -- chunk text (matched_text); NULL for node vectors
  entity_type TEXT,
  node_id     TEXT,
  is_chunk    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX vectors_entity_type ON vectors(entity_type);
CREATE INDEX vectors_node_id     ON vectors(node_id);
CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

- `store_meta` keys:
  - `embedding_model` and `embedding_dimensions` are stamped **only at creation**, never refreshed on open, because refreshing would defeat the drift guard.
  - `embed_scheme` = `doc-prefix-v1`.
  - `dim` is set by the first write. A write or query with any other dimension is rejected: the write is logged and skipped, and a query returns `[]` with an error log.
- Connection settings:
  - `journal_mode=WAL` (persistent, set at creation), `synchronous=NORMAL`, `busy_timeout=5000`.
  - Autocommit (`isolation_level=None`) with an explicit `BEGIN IMMEDIATE … COMMIT` around each write.
  - `check_same_thread=False`, with every call under the store's `RLock`.
- `IN (…)` id lists are chunked at 500 per statement.
- The schema is created in one `BEGIN IMMEDIATE` transaction with `IF NOT EXISTS`, so concurrent creators converge on one schema.
- No dependency on JSON1: filters on keys that have no column are applied in Python.

### 4.4 API: `VectorStore` (module `embeddings/vector_store.py`)

It keeps the full public surface that callers use today, with the same signatures and return shapes:
- `upsert_embedding(entity_id, embedding, metadata, document=None)`
- `delete_embedding(id) -> bool`
- `delete_by_node_id(node_id)`
- `vector_search(query_embedding, limit=10, entity_type=None)`
- `count_documents(filter=None)`
- `close()`

It adds the methods that replace today's private `_collection` access:

| Call site today | New method |
| --- | --- |
| `server.py` sync, `get(ids, include=["metadatas"])` | `get_existing(ids) -> dict[id, metadata]` |
| `server.py` sync chunk probe, `cognition_tools.py` pending check | `existing_ids(ids) -> set[str]` |
| `server.py` orphan sweep, `get()["ids"]` / `delete(ids)` | `all_ids()` / `delete_ids(ids)` |
| `project_registry.py` `_collection.metadata` | `store_metadata() -> dict` |
| `project_registry.py` pre-stamp dimension probe | `sample_dim() -> int | None` |

Open modes (class methods):
- `VectorStore.open_home(directory, embedding_model, embedding_dimensions)` is used by the server lifespan.
  - It creates the directory and the active generation if missing, and stamps the store at creation.
  - It never raises. Corruption leads to quarantine and a fresh generation; a transient error returns a store in the `unavailable` state, which retries lazily.
- `VectorStore.open_existing(directory)` is used for foreign attach.
  - It opens read-only (`mode=ro` URI built with `Path.as_uri()`, so spaces and non-ASCII characters are safe).
  - It returns `None` when there is no active generation or the store is unreadable. It never creates and never quarantines.
- `VectorStore.open_viewer(directory)` is used by the dashboard CLI. It opens read-write so dashboard deletes keep working, but it never creates, stamps or quarantines, and returns `None` when absent.

Removed: `ChromaDBStorage`, `_retry_chromadb_open`, `recreate_collection`, `live_embed_scheme`, and the E-3 migration block in `server.py`. A new store is created stamped `doc-prefix-v1`, so there is nothing to migrate. The package exports `VectorStore` and `adaptive_vector_search`, which is unchanged.

### 4.5 Filters

- Equality on `entity_type`, `node_id` and `is_chunk` uses the columns.
- Any other key is matched in Python against the parsed metadata.
- A non-equality filter shape (a dict value, or an operator key such as `$and`) raises `ValueError`.
- Documented difference: `is_chunk=False` matches rows that never set the key, whereas chroma matched only an explicit False. No caller uses a False filter.

### 4.6 Search and the matrix cache

- Exact cosine search with numpy. Stored vectors are normalised at write, and the query is normalised at search.
  - A zero, wrong-dimension or non-finite query returns `[]` with a logged error.
  - `k = min(limit, N)`. `argpartition` takes the top k, and a stable sort on −score orders them, with ties broken by row order.
  - `adaptive_vector_search`'s rule that fewer results than requested means exhaustive holds by construction.
- The cache is held per store object: ids, an N×dim float32 matrix, parsed metadata dicts, documents and entity types, plus `_dirty` and `_data_version`.
- Before each search, under the lock: read `PRAGMA data_version`. If it changed, or `_dirty` is set, reload everything with one `SELECT`.
  - The version is read before the `SELECT` and recorded as the baseline, so a peer commit that lands mid-reload triggers another reload rather than being lost.
  - Own writes set `_dirty`, because a connection's own commits do not change its `data_version`.
- **The reload is also a health check:** every blob must satisfy `len == dim*4` with finite values, and every metadata field must parse as JSON. A failure is corruption (§4.7).
- Scale: Ling Lang, the largest store, holds about 5.3k vectors (≈16 MB). A reload takes tens of ms and a search under 10 ms. `get_status` reports the count, and a warning is logged above 50k vectors, the comfortable exact-search ceiling.
- Exact search recalls at least as well as HNSW, so some rankings can improve slightly. Score values use the same formula as today.

### 4.7 Error classification

- **Corruption** (leads to quarantine):
  - an `sqlite3.DatabaseError` whose `sqlite_errorname` starts with `SQLITE_CORRUPT` or equals `SQLITE_NOTADB` (Python ≥3.11, which is our floor);
  - a failed row validation during a reload;
  - `PRAGMA quick_check` returning anything other than `ok`;
  - a missing `vectors` table in a database whose `user_version` is 1.
- **Not corruption** (never quarantined): everything else.
  - This includes `SQLITE_BUSY` / `SQLITE_LOCKED` after the busy timeout, `SQLITE_FULL`, `SQLITE_IOERR`, `SQLITE_CANTOPEN`, `SQLITE_READONLY` and permission errors.
  - The call is retried once. After that the operation degrades: a write is logged and skipped, a search returns `[]`, and a count returns 0 with the error recorded for `get_status`.
  - Quarantining on lock contention would destroy healthy stores, and with many peer servers contention is the common case.
- A `user_version` higher than 1 is a **newer schema**. The store opens in the `unavailable: newer schema` state (search `[]`, writes skipped, `get_status` explains) and is never quarantined.

### 4.8 Quarantine: generation markers

1. **Confirm the damage.** Open a fresh connection to generation g and run the failing check again (`quick_check`, or a full reload with validation). If it now passes, treat the first failure as transient: degrade that one call and do not quarantine. Two independent verdicts are required, as in the ChromaGuard plan.
2. **Mark it.** Create `vectors-<g>.corrupt` with exclusive create. If the file already exists, another process got there first, which is fine.
3. **Switch.** Close this object's connection, recompute the active generation (now g+1), and open or create it. The creation is the idempotent transaction from §4.3, so two processes quarantining g at once end up sharing g+1.
4. **Tell the user.** Append a `vector_store_rebuilt` notice to the existing locked notices file in `.cognition/local/` (the `REHYDRATE_FLAG_FILENAME` mechanism). The next SessionStart prime renders it once: "vibe-cognition: this project's search index was damaged and has been rebuilt from the journal; nothing was lost." `get_status` reports the event for this process's lifetime. Only `open_home` and the home store write the notice; foreign and viewer stores never do.
5. **Rebuild.** Set the store's `generation_changed` flag. The background maintenance loop (§4.9) re-runs the sync.

- **Peers notice** before every operation, with one `os.path.exists` on their own generation's marker. If the marker exists, they close, reopen the active generation and set `generation_changed`.
- **Nothing is ever renamed or deleted:**
  - Corrupt generations stay on disk for diagnosis. `get_status` lists them, and the README tells users they may delete the `vectors-<g>.sqlite3`, `-wal` and `-shm` files of a marked generation once no session is open, but never the `.corrupt` marker.
  - Shipped code never deletes them, following the precedent that shipped code never deletes vector stores.
- **A false positive costs CPU, never data:** the journal is the source of truth, and the confirm step in 1 makes a false positive unlikely.

### 4.9 Start-up sequence and the maintenance loop

Pre-yield (in the lifespan):
- `VectorStore.open_home(...)`: open or create the store, check the schema, check the generation.
- `count_documents()`.
- `_warm_numpy()`: one 8×768 by 768 product plus an `argpartition` on a dummy array, so BLAS threads and numpy code paths exist before the background import thread opens its wedge window (WP-Wedge-2 INV-2, the role the chroma pre-exercise plays today).
- New breadcrumbs `vectors_open_start` and `vectors_open_done` replace `chroma_open_*`, and the 200 ms budget warning stays.

Post-yield, in the existing background thread `_load_embeddings_and_sync`:
1. Run `quick_check` and a full cache load (health check). On corruption, quarantine (§4.8).
2. Run the existing steps unchanged (model drift guard, model load, deterministic edges, the document-orphan sweep).
3. Run the sync, `_sync_cognition_embeddings`, with two changes:
   - it uses `get_existing` and `existing_ids`;
   - plain nodes are shuffled with `random.Random(os.getpid())`, and each is re-checked with `existing_ids([id])` just before it is embedded, so peers syncing the same project split the work.
   Documents and workflows keep their current completeness logic, since they are few.
4. Set `embedding_sync_done`.
5. **Maintenance loop:** wait on `context["vector_resync"]`, an Event set when the store's generation changes, and re-run step 3 each time it fires. The thread is a daemon and is never joined. The loop exits at shutdown when the lifespan sets the event with a stop flag.

### 4.10 Tool and status surface (tool-surface audit applies)

- `get_status`:
  - adds `vectors_path` (the active generation's database file);
  - keeps `chromadb_path` as a **deprecated alias with the same value**, documented as removed in a later release;
  - adds `vector_store`: `{state: "ok" | "unavailable" | "newer-schema", generation: int, reason: str | None, quarantined: [{generation, at, reason}]}`.
  - `cognition_embeddings` is unchanged.
- `cognition_search`: `index_syncing: true` is present only while the start-up sync (or a maintenance re-sync) is running, so results may be partial. Absent otherwise.
- `cognition_load_project`:
  - when a foreign project has no vectors store yet, `model_guard` stays `no-index` and the result gains `index_reason`: "search index not built yet for this project; open a session in it once on vibe-cognition ≥ 0.47.0 to build it";
  - when the foreign store is unreadable, `index_reason` names the error.
- Dashboard: `/api/search` adds the same `index_syncing` flag. A missing store shows "search index unavailable" while the graph view keeps working, through the existing `embeddings_disabled` path.

### 4.11 Other code that changes

- `_venv_guard.py`: `REAL_IMPORT_MODULES = ("numpy", "sqlite3")`.
- `hooks/session-start.sh`: the probe becomes `import torch, numpy, sqlite3`. The Codex adapter hook gets the same audit (it has no probe today).
- `pyproject.toml`: add `numpy>=1.26` as a direct dependency. It is already installed transitively at 2.4.3, so the lock gains only the direct edge. `chromadb` stays this release, with a comment saying why.
- `service_tools.py`, `project_registry.py`, `cognition_tools.py`, `dashboard/cli.py`, `dashboard/api.py`, `server.py`, `operations.py`, `prime.py` (the new notice kind), `readme.py` (`chromadb_path` → `vectors_path`, store location text), `instructions.py`, README (location, model-change recovery, the deletable corrupt generations), and `_startup_timing` docs.
- `git_hygiene._GITIGNORE_ENTRIES` keeps `chromadb/` for teammates on old versions. Nothing is added.
- Comments that say "Chroma" in code we touch are reworded. Untouched historical comments elsewhere are left alone.

### 4.12 Next release (recorded as a task, not done here)

- Remove `chromadb` from `pyproject.toml`.
- Document that the `chromadb/` dirs under the data root are no longer used and may be deleted, or remove them with an explicit ruling.

## 5. Migration and upgrade

- The first start on 0.47.0 finds no `vectors/` store, creates it, and the start-up sync re-embeds everything.
  - Measured after implementation on a copy of Ling Lang's graph: 4.5 vectors/s for one session, 6.4 for two sharing the work, so about 20 minutes for Ling Lang and a few minutes for most projects.
  - Search is partial meanwhile, with `index_syncing` set. Recording and every graph tool work at once.
- Several sessions on one project split the rebuild (§4.9), so it finishes sooner rather than costing N times the CPU.
- Old `chromadb/` dirs are left untouched. A downgrade finds them, stale only by what was recorded since, and the old sync catches up.
- Mixed versions are safe: 0.46.x servers write chroma, 0.47.0 servers write vectors, and both write the journal. Each server's start-up sync fills in what the other recorded.

## 6. User-facing hiccups and how each is handled

| Situation | What the user sees | Handling |
| --- | --- | --- |
| First session per project after the update | Search partial for minutes | whats-new bullet; `index_syncing` on results; `get_status` `syncing` |
| Several projects opened at once after the update | High CPU for a few minutes | whats-new bullet; per-project rebuild runs once |
| Plugin updated while 0.46 sessions are open | Nothing new | No third-party package changes (§4.1); old sessions keep their chroma store |
| Foreign project not yet opened on 0.47 | Structural-only search with `index_reason` | CHANGELOG plus the reason string |
| Dashboard for a project not yet opened on 0.47 | "search index unavailable" | Existing degrade path |
| Antivirus or indexer holding the file | One degraded call, no quarantine | §4.7 classification |
| Store corrupted | One prime line next session; rebuild in the background | §4.8 |
| Downgrade to 0.46.x | Old chroma stores used | Left untouched (§3) |
| Embedding model changed in config | Same drift guard as today | Stamp at creation only |
| Disk full | Writes skipped and logged; the sync fills them later | §4.7 |

## 7. Tests

Fix and proof land in the same commit (ledger 20), and every new test states what it fails on against the reverted code (ledger 12).

1. **Golden parity.** While chromadb is still installed, a one-off script records `ChromaDBStorage` outputs on a fixed fixture set into `tests/fixtures/vector_store_golden.json`. The recorded outputs cover ids, order, scores to 1e-5, metadata round-trip types including `updated_at` presence, `matched_text`, filter results, `count_documents` with and without a filter, and `n > count`. `VectorStore` must match, allowing the documented `is_chunk=False` difference and exact-vs-HNSW tie order.
2. **Search edge cases:** `limit > N`; zero, NaN or wrong-dimension queries give `[]` plus an error; `adaptive_vector_search`'s exhaustive flag.
3. **Upsert never raises:** a wrong dimension, a locked database, or a store in the newer-schema state each lead to a skipped write, a logged error and no exception.
4. **Cross-process convergence with a barrier:**
   - N real subprocesses each open the store and run one search to warm their cache, then all pass a file-based barrier.
   - Each then upserts and deletes its own ids and asserts that its search sees the others' commits.
   - The final state is the union of writes, and `integrity_check` is ok.
   - A no-invalidation implementation fails this test. It must pass 5 times in a row (ledger 6).
5. **Error classification:**
   - A peer holding a write lock makes this process's write hit `SQLITE_BUSY`, and the test asserts **no marker** and a degraded call.
   - NOTADB garbage in the header leads to quarantine, and so does a truncated file.
6. **Payload corruption:** flip bytes so a blob holds NaN or Inf, and separately truncate a blob. The test asserts that the reload validation catches it, names that check, and that quarantine follows.
7. **Page corruption:** corrupt a b-tree page, assert which check catches it (reload or `quick_check`), and confirm quarantine.
8. **Quarantine mechanics:**
   - A marker is created, the next generation is opened, and the notice is appended.
   - Two processes quarantining at once produce one marker and one shared next generation.
   - A peer holding the old generation switches on its next operation and sets `generation_changed`.
   - Nothing is renamed or deleted.
9. **Confirm step:** a transient failure that passes on re-check does not mark.
10. **Newer schema:** `user_version = 2` gives the newer-schema state, with no marker and no writes.
11. **The server starts on a broken store:** the real server entrypoint as a subprocess, given a NOTADB active generation, reaches `handshake_yield`, answers `initialize`, and lists its tools.
12. **The real crash cannot recur:** the server module is imported in a subprocess, and the test asserts that `chromadb` is not in `sys.modules` after the lifespan's pre-yield path.
13. **Open modes:** `open_existing` never creates and reads a WAL store with and without `-shm`; `open_viewer` never creates, stamps or quarantines; home stamps at creation only.
14. **Sync split:** two in-process syncs against one store with a counting embed stub. The total embed calls are under 1.5× the node count, and the stores converge.
15. **Maintenance loop:** setting `vector_resync` re-runs the sync. This is the test that `generation_changed` actually reaches it.
16. **Test isolation (task `46822ab83433`):**
    - `conftest` sets `VIBE_DATA_DIR` to a per-test temp root instead of deleting it, so subprocesses inherit an isolated root. Tests that need it absent remove it themselves.
    - A guard test runs one subprocess helper and asserts that nothing new appeared under the real `~/.claude/plugins/data/vibe-cognition-*`.
17. **Venv guard and hook probe** pass, and fail with a clear message when numpy is missing.
18. Existing suites are migrated from `ChromaDBStorage` to `VectorStore`. E-3 migration tests (`recreate_collection`, `live_embed_scheme`) are deleted along with the code, and their doc-prefix embedding assertions are kept. `test_chromadb_cross_process.py` is replaced by test 4.

Gates: `ruff`, `render_harness --check`, `render_parity --check`, the full pytest suite, `pytest -m svn` (prime and the notices file are shared with the journal machinery), the sonnet code review, and the tool-surface audit.

## 8. Release hooks

- 0.47.0, a minor bump: the three manifests, CHANGELOG, whats-new (each bullet 200 characters or fewer), and `uv lock`.
- The notes cover:
  - the one-time rebuild per project and its duration;
  - `index_syncing`;
  - the foreign-attach gap;
  - faster start-up;
  - `VIBE_VECTORS_DIR` replacing `VIBE_CHROMADB_DIR`;
  - old `chromadb/` dirs left untouched.
- Tool-surface audit for `get_status`, `cognition_search` and `cognition_load_project`.
- Pin request to Loki with the SHA and the audit statement.
- Human gates: `/plugin update`, then a Ling Lang session (the rebuild, `index_syncing`, search after the rebuild), then a second concurrent session on the same project, then the Codex search phases of `docs/codex-test-brief.md`.

## 9. Rejected alternatives

- **Keep chromadb behind WP-ChromaGuard.** The native-crash class and silent rebuilds would continue.
- **ChromaDB behind a single-writer lock.** The crash shown here happens inside a single process.
- **A hosted service** (the 2026-10-05 direction). It is a separate project, and it would still need a store that cannot crash natively.
- **sqlite-vec or other ANN extensions.** A native extension brings back the crash class, for no benefit at our scale.
- **Salvaging vectors from old chroma stores.** Reading them is exactly what crashes, and re-embedding is proven and bounded.
- **Directory-rename quarantine.** Windows blocks the rename while any peer holds the files (§4.1).

## 10. Plan review findings (rev 2) and how each is resolved

The sonnet review of rev 1 reported 1 blocker, 5 high, 9 medium and 8 low findings. All of them are accepted.

| # | Finding | Resolution |
| --- | --- | --- |
| 1 | BLOCKER: quarantine can loop on a deterministic fault (sync folder, network share, SMB WAL) | At most 3 quarantines per process lifetime and 3 per 24 h, counted from marker mtimes. At most 3 generation hops per open. Past that, the state is `unavailable` with the reason "repeated corruption: this location may not be safe for SQLite (network or sync folder?)". An unconfirmed corruption, or a marker that cannot be written, starts a 5-minute cooldown during which the confirm step is not re-run. Tests cover both. |
| 2 | Deleting `vectors-<g>.*` including the marker resets the active generation, causing a split brain | The active generation is the highest g with any file, plus one if it is marked (§4.2). The README says to delete only the database files and never the marker. Test: deleting old files and markers never changes the active generation. |
| 3 | A never-returning maintenance loop breaks `_load_embeddings_and_sync` tests and the shutdown | The loop runs only when `context["vector_resync"]` exists, and hand-built test contexts lack it. The thread target stays `_load_embeddings_and_sync`. The health check runs before the `embedding_error` early return. On shutdown the lifespan sets the stop flag and the event before `bg_thread.join` and before `close()`, and every store call on a closed store returns its default. |
| 4 | Empty results from an unusable store look like "no history" | `cognition_search` (home and every fan-out entry) and the dashboard search check the store first. A state other than `ok` returns `semantic_unavailable: true` with `reason` set to `vector-store-unavailable` or `newer-schema`. A failure during the search itself (busy, I/O) is recorded per thread (`last_search_error()`), and the result is reported the same way. Tests cover both paths. |
| 5 | A transaction left open on the shared connection blocks every later write; an open cursor freezes `data_version` | Every write goes through one `_txn()` context manager: BEGIN IMMEDIATE, then COMMIT, with ROLLBACK on any exception, and a dropped and reopened connection if the rollback itself fails. `_run` also rolls back any transaction left open by a failed call. Only `fetchall()` is used. Tests: an injected failure mid-transaction followed by a successful write, and a peer commit seen after a failed call. |
| 6 | A pre-yield open can stall the handshake behind a peer's write lock | Pre-yield, `open_home` uses a 200 ms busy timeout. It reads `user_version` and `journal_mode` without a write lock, takes BEGIN IMMEDIATE only to create a new store, and sets WAL only when the mode is not already wal. If it is busy, the state is `unavailable`, and the background thread finishes the open with the normal timeout through `ensure_open()`. Test: a peer holding the write lock on a new store, and `open_home` returns in under 1 s. |
| 7 | Marker `reason` cannot be read if the marker is empty | The marker is written as complete JSON to a temporary file and moved into place with `os.replace`, so a reader never sees a half-written marker. |
| 8 | A future format inside `user_version` 1 could look like corruption and cause ping-pong rebuilds | `store_meta.format = "f32-l2-json-v1"`. An absent or unknown format is treated as newer-schema: degraded and never quarantined. |
| 9 | Retrying a BUSY call doubles a 5 s stall under the lock | BUSY and LOCKED are not retried, because the busy timeout already waited. After one, a 5 s circuit breaker fast-fails that store's calls, which degrade and are recorded for finding 4. Other operational errors are retried once. |
| 10 | Read-only opens of a WAL store on Windows after a crash (`-wal` present, no `-shm`), and UNC paths | Tested empirically on Windows: a `mode=ro` connection opens a crashed WAL store with or without `-shm` (it recreates `-shm`) and reads the rows that exist only in the WAL. `immutable=1` was rejected: it ignores the WAL and returned an empty schema. Test 13 covers both copies. A CANTOPEN error (including UNC URIs) gives `None` with an `index_reason`. |
| 11 | Test 14 passes trivially with an instant stub | A slow stub plus a barrier forces the two syncs to interleave. A second variant with the recheck disabled must exceed 1.5 N embeds, which proves the test is sensitive. |
| 12 | Test 16 passes vacuously on CI | `HOME` and `USERPROFILE` point at a temporary home seeded with a sentinel `vibe-cognition-x` data dir, and the test asserts that nothing is written there. |
| 13 | Inventory misses | Added to the work list: `dashboard/cli.py` (constructor and `--data-dir` help), `scripts/bench_wave1.py`, `scripts/bench_wave3.py`, the root `.gitignore`, and every test file named by the review (102 `ChromaDBStorage(` call sites). Also added: `test_hooks_shell.py`'s fake uv match on the probe text, `test_venv_guard.py`, `test_server_lifespan.py`'s log and monkeypatch, and `test_wp_sidecar.py`. `prime._render_notices` gets an explicit `vector_store_rebuilt` branch, with a test that the notice renders once. |
| 14 | Chunk delete-then-write is not atomic between peers | A new `replace_chunks(node_id, rows)` deletes and writes a node's chunk set in one transaction. `_embed_workflow` and `_embed_document` compute every chunk embedding first and then call it. |
| 15 | With model or dimension drift, the sync re-embeds everything on every start and every write is rejected | The start-up sync is skipped when the home guard is `dim-mismatch` or `model-mismatch`, with a test. |
| 16 | Zero or non-finite vectors, and `argpartition` at `k >= N` | Both are already handled (zero or non-finite writes are skipped, and `k == N` takes the full-pool branch). Test 2 adds `limit == N` and `N == 0`. |
| 17 | Lone surrogates make the bind fail forever | Document text is written with `errors="replace"`. Metadata is ASCII JSON, which escapes surrogates. |
| 18 | The numpy warm-up is too small | It uses a 2048×768 matrix and calls every numpy function used later: `frombuffer`, `isfinite`, `linalg.norm`, matmul, `argpartition` and a stable `argsort`. |
| 19 | `chromadb_path` changes meaning | `chromadb_path` keeps directory semantics: it is the store directory. `vectors_path` is the active database file. |
| 20 | The WAL can grow without bound | `PRAGMA journal_size_limit = 67108864` on writable connections, with the default autocheckpoint. |
| 21 | The in-repo fallback relies on `local/` being ignored | When the store resolves inside the repo and `.cognition/.gitignore` lacks `local/`, the lifespan logs a warning. |
| 22 | The dashboard's search and stats need `index_syncing` | Added to both. |
| 23 | `delete_embedding` must return False on error | Kept. |
