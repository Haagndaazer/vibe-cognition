# WP-Emb (non-E-3) — Implementation Plan

Base: `main` @ 2c203a4. Branch: `fix/wp-emb` (once green-lit).
Scope: E-4 (concurrent-Chroma retry), E-6 (dead-code prune), E-7 (revision-pin +
`datetime.utcnow`). **E-3 PARKED** (full re-embed; needs Colton's go). **E-8 deferred**
(`generate_batch` / startup sync — not in Vince's scope call).

Binding rules: journal-not-committed on WP branch; pyright ≤ 8; fails-before on every
behavioral delta; CI green 3 legs before gate.

---

## E-4. Concurrent ChromaDB write retry

**Problem:** two MCP server processes (two open Claude Code sessions on the same project)
both open the same `.cognition/chromadb/` dir via `PersistentClient`. ChromaDB 1.5.5 uses
SQLite for segment metadata; SQLite in WAL mode allows concurrent readers but enforces a
single-writer lock. Concurrent writes (embed-on-record from both sessions) raise
`sqlite3.OperationalError: database is locked` or an equivalent chromadb-wrapped form.
Currently `upsert_embedding` and the delete paths raise immediately on contention — the
write is lost to callers that catch broadly. `vector_search` already swallows to `[]`; that
path is out of scope (degraded search on contention is acceptable).

**Fix:** add `import time` at the top of `storage.py` (alongside `import logging`), then add
`_chroma_call_with_retry` private helper on `ChromaDBStorage`:

```python
def _chroma_call_with_retry(self, fn, *, max_retries: int = 3, base_delay: float = 0.05):
    """Retry fn on transient Chroma/SQLite write-lock contention."""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            msg = str(e).lower()
            if ("locked" in msg or "busy" in msg) and attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))  # 50ms, 100ms, 200ms
                continue
            raise
```

Wrap with `_chroma_call_with_retry`:
- `upsert_embedding`: the `_collection.upsert(**kwargs)` call
- `delete_embedding`: the `_collection.delete(ids=[entity_id])` call
- `delete_by_node_id`: the `_collection.delete(where={"node_id": node_id})` call

**`delete_embedding` swallow behavior:** `delete_embedding` already wraps its body in
`except Exception: return False`. After the retry helper is applied to the inner
`_collection.delete()` call, any lock error that exhausts all retries re-raises from the
helper — and is then caught by `delete_embedding`'s outer `except`, returning `False`.
This is acceptable (callers of `delete_embedding` check the return value; a logged-and-swallowed
final-retry failure is still better than a silent immediate miss). No behavior change is
needed here; document it explicitly.

**Does NOT address:** stale per-process read-cache (segment caches are per-process; reads
from process B may not see writes from process A until B reopens the client). Resolving
read-staleness requires ChromaDB's HttpClient — out of scope. Document the residual in the
class docstring: "Multiple PersistentClient instances on the same directory are write-safe
(retried) but read-stale across processes. For full multi-process safety use HttpClient."

**Fails-before test (behavioral delta):** mock `_collection.upsert` to raise
`Exception("database is locked")` on the first call, succeed on the second. Before fix:
`upsert_embedding` raises on the first call. After fix: retries and succeeds. Also assert
that a non-lock exception (e.g. `ValueError("bad input")`) is NOT retried and re-raises
immediately (so we don't mask real errors). Two focused cases.

---

## E-6. Dead code-search heritage prune

**Zero callers confirmed:** `bulk_upsert`, `delete_by_file`, `delete_by_repo`, `get_by_id`,
`get_content_hashes` — grep over all of `src/` and `tests/` shows no callers outside
`storage.py` itself. `vector_search`'s `repo`/`file_path_prefix` params — `adaptive_vector_search`
only passes `entity_type`; nothing in the codebase ever passes `repo` or `file_path_prefix`.

**Remove from `embeddings/storage.py`:**
- `bulk_upsert(items)` method (lines ~72-102)
- `delete_by_file(repo, file_path)` method (lines ~162-181)
- `delete_by_repo(repo)` method (lines ~183-198)
- `get_by_id(entity_id)` method (lines ~200-221)
- `get_content_hashes(repo)` method (lines ~223-243)
- `vector_search`'s `repo: str | None = None` and `file_path_prefix: str | None = None` params
- The `where_conditions = []` / `if repo:` / `where_filter` build-up — replace with direct
  `where_filter = {"entity_type": entity_type} if entity_type else None`
- The `query_limit = limit * 3 if file_path_prefix else limit` → replace with plain `limit`
- The `file_path_prefix` post-filter loop body
- Update the cross-ref comment in `delete_by_node_id` that mentions `delete_by_file` shape

**NOT pruned (live callers):** `delete_embedding` (called by `cognition/operations.py` and
`tools/cognition_tools.py`) and `delete_by_node_id` (called by operations.py document
deletion). These are active write paths, NOT dead code. E-4 wraps both.

**Default `collection_name`:** change `collection_name: str = "code_embeddings"` to
`collection_name: str = "cognition_embeddings"` in `__init__`. The old default is dead
(every real call passes `"cognition_embeddings"` explicitly); the new default reflects the
only actual use. Tests use `_FakeEmbeddingStorage` (no-op constructor). CLI + server both
pass it explicitly — no behavior change.

**E-8 boundary (scope note for PR):** `EmbeddingGenerator.generate_batch` in `generator.py`
is also dead (zero callers; the startup sync loop uses `.generate()` one-at-a-time). NOT
pruned here — E-8 plans to put it to work. If E-8 is dropped, prune it then.

**Fails-before:** dead-code removal doesn't require behavioral fails-before (callers don't
exist to go red). Confirm existing 262-test suite stays green post-prune.

---

## E-7. `revision=` pin + `datetime.utcnow`

### 7a. `datetime.utcnow()` fix (ruff UP017)

`embeddings/storage.py:_flatten_metadata` uses `datetime.utcnow().isoformat()` — deprecated
in Python 3.12+, naive. Fix: import `UTC` and use `datetime.now(UTC).isoformat()`.

The file already does `from datetime import datetime` — add `UTC` to the import:
```python
from datetime import UTC, datetime
```
And change `now = datetime.utcnow().isoformat()` → `now = datetime.now(UTC).isoformat()`.

This removes the ruff UP017 warning and fixes the remaining pyright `datetime.utcnow`
deprecation notice.

**Fails-before:** ruff `check` catches UP017 before the fix, is clean after.

### 7b. `revision=` pin for `SentenceTransformer`

**Problem:** `SentenceTransformer(model_name, trust_remote_code=True)` loads Python code from
the HuggingFace hub with no `revision=` pin. If the model author pushes a new commit to
`nomic-ai/nomic-embed-text-v1.5`, the plugin silently runs that code on the next model load.
`trust_remote_code` is non-negotiable (nomic's model requires it; see the `einops` comment in
`pyproject.toml` for why). A pinned `revision=` makes the remote code auditable and stable.

**Changes:**

`config.py` — add field after `embedding_dimensions`:
```python
embedding_revision: str | None = Field(
    default=None,
    description=(
        "HuggingFace Hub revision (branch, tag, or full commit SHA) for the "
        "sentence-transformers model. When set, pins the remote code loaded via "
        "trust_remote_code=True to a specific commit — recommended for production. "
        "Set via EMBEDDING_REVISION env var. Default None = use the model hub HEAD."
    ),
)
```

`embeddings/generator.py:SentenceTransformersBackend.__init__`:
- Add `revision: str | None = None` parameter AFTER `dimensions: int | None = None` (full
  signature: `def __init__(self, model_name: str, dimensions: int | None = None, revision: str | None = None)`)
- Pass `revision=revision` to `SentenceTransformer(model_name, trust_remote_code=True, revision=revision)`

`embeddings/generator.py:EmbeddingGenerator.from_config`:
- Extract `revision=config.embedding_revision` and pass to `SentenceTransformersBackend(..., revision=...)`

`pyproject.toml` — expand the `einops` comment to also document `trust_remote_code`:
```toml
# Required at runtime by nomic-embed-text-v1.5's trust_remote_code model code, which
# runs from the HF Hub (embeddings/generator.py: SentenceTransformer(...,
# trust_remote_code=True)). Pin the remote code to a known-good revision via the
# EMBEDDING_REVISION env var to harden against upstream hub changes.
"einops>=0.7.0",
```

**Fails-before test:** monkeypatch `sentence_transformers.SentenceTransformer` to capture
kwargs; instantiate `SentenceTransformersBackend("m", revision="abc123")`; assert
`revision="abc123"` was passed to the constructor. Before fix: `SentenceTransformer` is
called without `revision` kwarg → assertion fails. After fix: passes.

Also confirm that `revision=None` does NOT pass the kwarg at all (let ST pick its own
default), OR passes it as `None` (ST treats `None` the same as absent — verify at
implementation time; if ST raises on `revision=None`, use conditional kwarg injection).

---

## Open question for align

**E-8 boundary on `bulk_upsert`:** The backlog says E-6 may choose "prune OR put to work
in E-8". Vince scoped this WP as "dead-code prune" — this plan prunes `bulk_upsert` from
`storage.py`. If E-8 is later scoped to use it, it would need to be re-added. Confirm: is
`bulk_upsert` safe to prune now, or should it be preserved for E-8?

**E-8 scoping:** `generate_batch` in `generator.py` is intentionally NOT pruned here (E-8
natural home). If E-8 is permanently deferred/dropped, it should be pruned in a hygiene WP.

---

## Sequencing

1. Branch `fix/wp-emb` off `main` @ 2c203a4.
2. E-7a: `datetime.utcnow` fix (smallest, cleans the ruff warning).
3. E-7b: `revision=` config + generator threading.
4. E-6: dead-code prune + `vector_search` simplification.
5. E-4: retry wrapper + tests.
6. Full suite (≥ 262) + pyright ≤ 8 locally → push → CI green 3 legs → ping Vince SHA.

## Files touched

| File | Changes |
|---|---|
| `src/vibe_cognition/config.py` | E-7b: `embedding_revision` field |
| `src/vibe_cognition/embeddings/storage.py` | E-4: retry helper + wrap 3 write ops; E-6: prune 5 methods + simplify `vector_search`; E-7a: `datetime.now(UTC)` |
| `src/vibe_cognition/embeddings/generator.py` | E-7b: `revision` param + config threading |
| `pyproject.toml` | E-7b: expand einops/trust_remote_code comment |
| `tests/test_embeddings_storage.py` | E-4: retry fails-before tests; E-7a: ruff clean |
| `tests/test_config.py` or `tests/test_generator.py` | E-7b: revision pin test |

## Risks / watch-items

- **E-4 exception message match:** chromadb 1.5.5 wraps SQLite errors — verify the exact
  exception text in the fails-before test. If chromadb uses a custom exception class
  (not a `sqlite3.OperationalError` subclass), the `"locked"` string check may not trigger.
  Add a test that verifies the retry fires on the actual exception type, not just any Exception.
- **E-6 `vector_search` simplification:** after removing `where_conditions`, the
  `adaptive_vector_search` + `_FakeEmbeddingStorage.vector_search` surface in tests must stay
  identical — no behavior change, just fewer params.
- **E-7b `revision=None` behavior:** `SentenceTransformer(revision=None)` may differ from
  `SentenceTransformer()` (no kwarg) depending on the ST version. Test both forms; use
  conditional kwarg injection if ST treats `None` differently from absent.
- **Pyright baseline improves after E-6:** all 7 pre-existing errors live in the
  `file_path_prefix` filter block at `storage.py:307` (1× `reportArgumentType` + 6×
  `reportAttributeAccessIssue` / `reportOptionalMemberAccess`). E-6 removes that block
  entirely → post-E-6 pyright should reach 0 errors. Confirm the final count is ≤ 8
  (binding constraint), but expect 0. (`UTC` import for E-7a is Python 3.11+ stdlib —
  safe since the project requires `>=3.11`.)
