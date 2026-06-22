# WP-XP0 — Cross-Project Cognition: de-risking SPIKE

**Type:** investigation spike (throwaway). Output is a findings doc + go/no-go + concrete
inputs for XP1/XP2 — **not** production code.
**Base:** `main` @ 4c562e4. Branch: `fix/xp0-spike` (scratch).
**Gates:** XP1 and XP2 do not start until this spike's findings are in and Vince has signed
off on the go/no-go.

## Why a spike before any code

The cross-project feature (Colton-approved design) hinges on opening a **second** ChromaDB
client — project B's `.cognition/chromadb` — inside project A's already-running server
process, querying it (semantic search) while B's own server may be writing, and releasing it
cleanly on unload. The design peer-review found multi-client-at-different-paths *empirically*
works on chromadb 1.5.5, but flagged residual risks; **Vorpid's E-4 finding (1.5.5 ships a
Rust backend, `chromadb_rust_bindings`, that handles SQLite locking internally) moved the
`close()`/handle-release question from a Python-sqlite3 question to a Rust-layer one.** Prove
the four unknowns on the **real installed stack** before committing XP1/XP2.

This is a "live endpoint is the primary artifact" (ledger rule 23) task: reproduce real
behavior on chromadb 1.5.5 / the project venv / Windows. Do **not** reason from docs.

## The four questions — each with an experiment + pass/fail

### Q1. Multi-client coexistence (the make-or-break)
Open `ChromaDBStorage(A_dir)` AND `ChromaDBStorage(B_dir)` — different persist dirs, both
`collection_name="cognition_embeddings"` (now the default post-Emb E-6) — in ONE process.
Seed each with a couple of distinct vectors. Query both.
- **PASS:** both clients construct (no "instance already exists" / `System` singleton error),
  results are isolated (A's query returns only IDs seeded into A's dir — none from B's, and
  vice versa), and one shared `EmbeddingGenerator` embeds the query once and drives both
  queries.
- **NOTE in findings:** `SentenceTransformersBackend` serializes all encodes under a single
  `threading.Lock` (`generator.py:54,74`). A shared generator is SAFE across A's + B's queries
  but they queue at the encoder — record this so XP2 knows concurrent foreign searches
  serialize there (not a blocker; a documented constraint).
- **FAIL → STOP + ESCALATE.** If construction errors or cross-contaminates, Option A (shared
  read of B's chroma) is dead. Do NOT proceed to Q2–Q4; report to Vince → Colton. The only
  fallback is re-embed-B-into-an-A-namespaced-collection, which **sacrifices always-live
  vectors** — that's a Colton-level design change, not an implementer call.

### Q2. Clean unload / handle release on Windows (the Rust-layer question)
After querying B, run the unload path, then **attempt to delete B's chromadb dir** (and/or
re-open it fresh) in the same process.
- `ChromaDBStorage.close()` is currently a **no-op** (`embeddings/storage.py:200-202`). The
  spike must find the EXACT incantation that releases the Rust/SQLite handle: try
  `client.close()`, `client._system.stop()` / `SharedSystemClient._release_system`,
  `del client; gc.collect()`, in combination — and record which one actually frees the handle.
- **PASS:** after the unload incantation, B's chromadb dir can be **renamed** (the strict test
  — on Windows a file can sometimes be deleted while a handle is still open, but a rename fails
  if any handle holds the dir; rename success proves release). Also confirm re-open works.
  Record the exact recipe → XP1 implements it in `close()`.
- **PARTIAL:** if nothing fully releases it in-process, document that (unload leaves a handle
  until process exit) so XP1 ships an honest limitation rather than a false "unloaded".

### Q3. Always-live concurrent read while B writes
Open B read-only in process A; from a **separate OS process** (simulating B's own live
server), upsert to B's chromadb concurrently; run A's `vector_search` against B during the
writes.
- Vorpid's E-4 finding covers the WRITE side (Rust swallows lock contention). This verifies
  the **READ** side under concurrent foreign writes.
- **PASS:** A's reads return without exception during B's writes (possibly slightly stale —
  that's the accepted freshness ceiling, see Known-Intentional). Record it.
- **FAIL:** if lock/contention errors reach Python on the read path, XP2 needs a
  catch/retry wrapper on the foreign read (note the exact exception text for the matcher).

### Q4. Embedding-compatibility behavior — dimension AND model identity (for the load-time guard)
Two sub-parts, because dimension is necessary but NOT sufficient.

**Q4a — dimension mismatch.** Build B's collection with a DIFFERENT dimension than A's
`embedding_dimensions` (e.g. 384-dim in B; A queries with 768). Call `collection.query()` with
the mismatched embedding.
- **OUTPUT (pure info):** record whether `query()` raises, returns garbage, or returns empty.
  Confirm the probe works: `collection.get(limit=1, include=["embeddings"])` returns a sample
  vector whose `len()` is B's stored dimension.

**Q4b — model identity (the silent-garbage gap).** `embedding_model` and `embedding_dimensions`
are SEPARATE settings (`config.py:51-67`); two projects can share dim=768 but use different
models (e.g. nomic-embed-text-v1.5 vs all-MiniLM at 768) → dimension probe PASSES, query
returns plausible-but-garbage rankings. ChromaDB stores NO model provenance: the collection is
created with only `{"hnsw:space": "cosine"}` (`storage.py:38-41`), and `upsert_embedding`
(`storage.py:62-70`) stores no model field.
- **OUTPUT + DECISION:** confirm `collection.metadata` carries no model identity today, then
  recommend the XP1 guard strategy: either (a) stamp `embedding_model` + `embedding_dimensions`
  into the collection metadata at creation — **this is a PREREQUISITE src change to
  `ChromaDBStorage.__init__` that must land BEFORE XP1** (flag it explicitly; the no-src
  constraint does NOT silently defer this), or (b) a `.cognition/embedding_meta.json` sidecar
  convention written by each project's server.
- Net: XP1's load-time guard must check BOTH dimension and model identity, refuse/warn semantic
  search on either mismatch. The spike must hand XP1 a concrete mechanism for the model check.

### Q5. Journal-side always-live cost (the networkx/JSONL side, distinct from Q3)
Q1–Q4 cover ChromaDB (the vector side). But the STRUCTURAL cognition tools (get_node,
get_neighbors, get_chain, get_history) read B's `CognitionStorage` — the networkx graph rebuilt
from B's `journal.jsonl`. "Always-live" means every such call runs `_synced()` → `_catch_up()`
(`storage.py` catch-up path), which `stat()`s and conditionally re-reads B's journal. Concurrent
catch-up reads are SAFE by design (torn-tail parking + idempotent replay; the append lock is a
sentinel byte far past EOF, `journal_io.py`), so this is a COST question, not a safety one.
- **EXPERIMENT:** open `CognitionStorage(B_dir)` in A's process with a realistically-sized B
  journal (MB-scale / thousands of records); call structural reads while B's own process appends;
  measure per-call catch-up latency.
- **OUTPUT:** is replay-per-call latency acceptable for always-live foreign structural reads, or
  does XP2 need a throttle (e.g. catch-up at most every N seconds / on-demand refresh)? Record
  the measured numbers and a recommendation. Read-only invariant holds: catch-up only READS B.

## Deliverables (the spike OUTPUT)

1. **`docs/wp-xp0-spike-findings.md`** — answers Q1–Q4 with **observed** behavior (not
   speculation), including:
   - Q2: the verified `close()` recipe (proven by an actual delete-after-close on Windows).
   - Q3: whether the foreign read path needs a lock-retry wrapper (+ exception text if so).
   - Q4a/b: the observed dimension-mismatch behavior + the **model-identity guard strategy**
     (and whether it requires a PREREQUISITE collection-metadata-stamp src change before XP1).
   - Q5: measured journal catch-up latency + whether always-live structural reads need a throttle.
2. **An explicit go/no-go on Option A** (shared read of B's chroma).
3. The concrete **inputs XP1/XP2 consume**, precise enough to spec the next WPs without
   re-investigating: (a) the `close()` incantation, (b) foreign-read lock-retry need,
   (c) dimension-probe behavior, (d) **the model-identity probe result + recommended guard
   strategy** (incl. any prerequisite src change), (e) journal catch-up cost + throttle decision.
4. Findings recorded as cognition **discovery node(s)** (journal stays uncommitted on-branch;
   Vince flushes).

## Constraints / known-intentional (do NOT "fix" these in the spike)

- **Throwaway only.** Scratch scripts are fine; do NOT add anything to `src/`'s import graph.
  If a harness is worth keeping, park it under a scratch path or a `@pytest.mark.skip` probe —
  the PROOF is the findings doc + recipes, not shipped code.
- **Time-box it (~1 day total).** If Q1 FAILS, stop immediately and escalate — don't burn time
  on the rest. Q2 (Windows handle release) is the one most likely to absorb unbounded time
  chasing incantations — cap it; if no incantation releases the handle in a few attempts,
  record the PARTIAL outcome and move on.
- The fixed collection name `"cognition_embeddings"` is **correct** (default post-Emb E-6) —
  don't change it.
- `close()` being a no-op TODAY is known — the spike DECIDES the fix, **XP1 implements it**.
  Don't patch `close()` here.
- The always-live **vector-freshness ceiling** (foreign vectors only as fresh as B's own
  embedding sync) is an ACCEPTED Colton design decision, not a bug to solve.
- Read-only invariant: the spike opens B for READS only. Never call `add_*`/`upsert`/
  `delete`/sync/backfill against B (those would write to another project's store).

## Acceptance criteria

- Findings doc answers all four Q's with observed behavior + the Q2 close recipe **verified by
  an actual delete-after-close on Windows**.
- Explicit go/no-go on Option A.
- The three XP1/XP2 inputs are concrete enough to spec the next WPs with no re-investigation.
- If Q1 failed: a clear escalation note (no XP1/XP2 until Colton rules on the fallback).
