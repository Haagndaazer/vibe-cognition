# WP-XP0 Spike Findings

**Date:** 2026-06-21  
**Branch:** `fix/xp0-spike` (off main @ 4c562e4)  
**Stack:** chromadb 1.5.5 (`chromadb_rust_bindings` Rust backend), Python 3.12.11, Windows 11

---

## Go / No-Go

**Option A (shared read of B's chroma): GO.**  
Q1 passed (isolation confirmed). Q2 yields a working unload recipe. Q3 passed (no lock errors).
Q4 delivers a concrete guard mechanism. Q5 shows acceptable latency without throttling.
All four unknowns resolved. XP1 and XP2 are unblocked.

---

## Q1 — Multi-client coexistence

**Experiment:** two `ChromaDBStorage` instances at different `persist_dir` paths, both with
`collection_name="cognition_embeddings"`, constructed in one process. Seeded distinct vectors
into each, queried both.

**Result: PASS.**
- Both clients construct without error (no "instance already exists" / `System` singleton error).
- Isolation holds: A's query returns only A-seeded IDs, B's query returns only B-seeded IDs.
- One shared `EmbeddingGenerator` (backed by `SentenceTransformersBackend`) can drive both
  queries without error.

**Constraint for XP2:** `SentenceTransformersBackend.encode()` serializes under a single
`threading.Lock` (generator.py:52,72). A shared generator across A+B queries is safe but
serializes at the encoder — concurrent A+B searches queue there, not at the Chroma layer.
Document as an accepted constraint; not a blocker.

---

## Q2 — Clean unload / handle release on Windows

**Experiment:** after querying B's collection, attempt `os.rename(b_chroma_dir, target)`.
Rename success proves handle release (rename fails if any handle holds the directory on Windows).
Tested: baseline (no close), `client.close()`, `client._system.stop()`, `del + gc.collect()`.

**Results:**
| Attempt | Rename succeeds? |
|---|---|
| Baseline (no close) | NO — WinError 5: Access denied |
| `client.close()` | **YES** |
| `client._system.stop()` | **YES** |
| `del client; gc.collect()` | NO — WinError 5: Access denied |

**Exact recipe (proven by rename):** `client.close()` is sufficient and idempotent.

**What `close()` does internally** (`SharedSystemClient`):
```python
def close(self) -> None:
    if self._closed:
        return
    self._closed = True
    if hasattr(self, "_admin_client"):
        SharedSystemClient._release_system(self._admin_client._identifier)
    SharedSystemClient._release_system(self._identifier)
```
Decrements the reference count for the shared `System`; stops the System (and releases the
Rust/SQLite handle) when the last client using it calls `close()`.

**Re-open after close:** verified — a fresh `PersistentClient` on the same path after `close()`
constructs cleanly and returns previously-written data.

**Fix for XP1:** `ChromaDBStorage.close()` (currently a no-op at `storage.py:200-202`) must
call `self._client.close()`. One line change. XP1 implements it.

---

## Q3 — Always-live concurrent read while B writes

**Experiment:** open B's collection for reading in process A; from a separate OS process
(simulating B's own live server), upsert 200 records into B's collection concurrently (10ms
sleep between writes); run `collection.query()` against B from A during the writes.

**Result: PASS.** 100 reads completed, 0 exceptions. The Rust backend handles write-lock
contention internally (consistent with the E-4 finding on upserts); reads from a parallel
`PersistentClient` are unaffected by concurrent foreign writes.

**Known caveat (accepted):** reads may be stale — B's in-flight writes may not be visible to
A's per-process read cache until A's client is re-opened. This is the accepted always-live
freshness ceiling (Colton design decision; not a bug).

**Foreign read lock-retry:** not needed. No exception text to match. No `catch/retry` wrapper
required in XP2.

---

## Q4 — Embedding-compatibility behavior

### Q4a — Dimension mismatch

**Experiment:** seed B's collection with 3-dim vectors; query with a 5-dim vector. Also confirm
the dimension probe (`collection.get(limit=1, include=["embeddings"])`) returns a numpy array
whose `.shape[1]` is the stored dimension.

**Result:** dimension mismatch **RAISES** — not silent garbage:
```
InvalidArgumentError: Collection expecting embedding with dimension of 3, got 5
```

**Probe confirmed:** `col.get(limit=1, include=["embeddings"])` returns a `numpy.ndarray` with
shape `(n, stored_dim)`. `shape[1]` is the reliable dimension probe.

XP1 load-time guard: call the probe on B's collection; if A's `embedding_dimensions` ≠
B's stored dimension → hard refuse foreign semantic search (query would raise anyway; refuse
early with a clear message).

### Q4b — Model identity (silent-garbage gap)

**Observation:** `collection.metadata` today is `{'hnsw:space': 'cosine'}` — **no model
provenance is stored**. Two projects with the same `embedding_dimensions` but different models
would pass the dimension probe and return plausible-but-garbage rankings.

**Guard strategy decision:** stamp `embedding_model` (and `embedding_dimensions`) into
collection metadata at creation — a **PREREQUISITE src change to `ChromaDBStorage.__init__`**
that must land **before XP1**:

```python
self._collection = self._client.get_or_create_collection(
    name=collection_name,
    metadata={
        "hnsw:space": "cosine",
        "embedding_model": config.embedding_model,
        "embedding_dimensions": config.embedding_dimensions,
    },
)
```

**Three-state guard outcome (per Vince steer):**

| B collection state | Guard action |
|---|---|
| `embedding_model` present AND matches A | **Allow** semantic search |
| `embedding_model` present AND mismatches A | **Hard refuse** — silent garbage risk |
| `embedding_model` absent (pre-stamp collections, all existing today) | **Warn-and-allow** (degraded confidence) — do NOT refuse; refusal blocks all pre-stamp projects |

The stamp helps new collections going forward. Existing collections stay `"unknown"` until a
one-time re-stamp or natural re-creation.

**Prerequisite for XP1:** the metadata-stamp src change must land before XP1 so at least
newly-created collections carry the field. XP1 specs the three-state guard against it.

---

## Q5 — Journal-side always-live cost

**Experiment:** seed a `CognitionStorage` at B with 2000 nodes (960 KB journal). Open it in
process A via `CognitionStorage(b_dir)`. Measure per-call `get_node()` latency (which triggers
`_synced()` → `_catch_up()` on every structural read):

| Path | Latency |
|---|---|
| No new data (stat only, journal unchanged) | **~0.02 ms** |
| Replay path — 50 new records appended | **~9.5 ms** (avg across 10 batches) |
| Per-record replay cost | **~0.19 ms / record** |

**Analysis:** the no-new-data path is a single `stat()` — essentially free. The replay path
fires only when B's journal has grown since the last read; at ~0.2ms per new record, a typical
burst (10–50 new nodes from a Claude session) costs 2–10ms per foreign structural call. This is
well within acceptable latency for MCP tool invocations driven by human queries.

**Recommendation: no throttle needed.** The stat-guard makes the common case (no B activity)
free. Only replay when new data exists, and replay is fast enough for interactive use.
If XP2 wants to cap replay cost for pathologically large deltas, a simple "catch-up at most
every N seconds" guard can be added, but it is not required by the measured numbers.

---

## XP1/XP2 Inputs (concrete handoff)

| Input | Value |
|---|---|
| (a) `close()` incantation | `self._client.close()` — proven by rename; one-line fix for `ChromaDBStorage.close()` |
| (b) Foreign read lock-retry | NOT needed — 0 errors in Q3; no exception text to match |
| (c) Dimension-probe mechanism | `col.get(limit=1, include=["embeddings"])` → `.shape[1]`; mismatch RAISES `InvalidArgumentError` |
| (d) Model-identity guard mechanism | Stamp `embedding_model` into collection metadata (PREREQUISITE src change); three-state guard: match=allow, mismatch=hard-refuse, absent=warn-and-allow |
| (e) Journal catch-up cost + throttle | ~0.02ms no-data / ~0.2ms per new record; no throttle required |
