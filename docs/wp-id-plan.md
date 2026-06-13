# WP-ID Execution Plan — global node-id collision (data-loss minter fix)

A CORE change — treat it like the journal-atomicity work, not a quick fix. Off the post-WP-T main (`cc9cd73`). Queued after WP-T per Vince's sequencing.

## The bug
`generate_node_id` hashes `type:summary:timestamp`. Under a coarse clock (Windows ~15 ms) two same-type + same-summary nodes minted in one tick produce the SAME id, and the public `add_node` then `self._graph.add_node(id, ...)` — which OVERWRITES the first node (silent data loss). D1a fixed it **document-scoped** (a salt-retry loop in `_store_document`); `_record_node` (every decision/discovery/episode the agent records) and the post-commit hook's hand-rolled id STILL collide. This actually surfaced in D1a CI on Windows.

## Fix direction (Vince) + the resolving insight
Hoist the uniqueness loop into `add_node`'s locked block so (a) all server minters benefit and (b) check-and-mint is atomic — shrinking the cross-process `has_node`→`add_node` TOCTOU (backlog #2) in the same change. Then UNIFY: remove the document-scoped salt-retry so there's ONE mechanism (ledger 11).

**THE REPLAY SEAM (the one to name explicitly — Vince's chief concern):** a uniqueness loop must fire only when MINTING a fresh id, NEVER when REPLAYING an already-journaled `add_node` (a replayed id that already exists is legitimate cross-process convergence, not a collision to salt around — salting it would fork the graph and break multi-process convergence). **This is already structurally safe in the codebase and the fix must preserve it:** `_replay_entry` (storage.py:899-914) writes DIRECTLY to `self._graph.add_node(...)` — it does NOT call the public `add_node`. So the public `add_node` is exclusively the mint+journal path; replay bypasses it. Putting the mint loop in `add_node` therefore cannot touch replay **by construction** — and the plan keeps it that way (no mint logic added to `_replay_entry`/`_catch_up`). The fix lives at the GENERATION/journaling boundary, exactly as required.

Why this also shrinks the TOCTOU: `add_node` runs under `_synced`, which `_catch_up`s the journal FIRST — so the in-memory graph already reflects other processes' journaled nodes when the mint loop checks `id in self._graph`. An in-process same-tick collision is fully closed; a recently-journaled cross-process one is caught too. Residual: a truly concurrent cross-process mint landing between this process's catch-up and its journal append (the existing non-transactional window) — SHRUNK, not eliminated. State that honestly; do not over-claim.

## Cross-writer minter inventory (ledger 11 — load-bearing)
Every id minter, and how each is closed:
1. **`_record_node`** (cognition_tools.py) — mints via `generate_node_id`, calls `add_node`. → routes through the new minting `add_node`. ✓
2. **`_store_document`** (cognition_tools.py) — has its OWN salt-retry today. → REMOVE it; route through the minting `add_node` (one mechanism). ✓
3. **post-commit HOOK** (`hooks/post-commit.py:108`, `_generate_id`) — a SEPARATE process; hand-rolls the id and writes the journal line DIRECTLY (journal-as-IPC; it cannot call the running server's `add_node`, and replay of its line doesn't mint). It canNOT share the server lock. BUT it has a naturally-unique discriminator the server lacks — the **commit hash** (`commit["hash"]`, globally unique). → mint the hook's id from `type:summary:timestamp:commit_hash` so two distinct commits with an identical message in one tick get distinct ids, with NO graph check needed. Different minting context, different right tool (Commit 2).
4. **REPLAY** (`_replay_entry`) — re-applies journaled `add_node`s directly to the graph, idempotent convergence. → NOT a minter; must NOT salt. Preserved by construction (see the seam above). ✓
5. Inventory the remaining `storage.add_node(` callers (grep) — confirm none rely on add_node OVERWRITING on a repeated id (tests with hand-chosen ids must be unaffected → the mint is opt-in, default off).

## Binding rules
Rule 20, 12 (fails-before RUN), 11, 21, 18. Journal protocol. pyright ≤ 8 (the WP-T floor). **N-consecutive-green (≥3)** on the timing-sensitive tests (the frozen-clock collision + replay-convergence), since this touches the minting path. SHA-pinned merge gate.

---

## Commit 1 — server-side global mint in `add_node` (+ unify out the document salt)
- `add_node(node, *, mint_unique_id: bool = False) -> str`. Default `False` = current behavior (add with `node.id` as-is; existing callers / hand-chosen-id tests unaffected — non-breaking). With `mint_unique_id=True`, UNDER the existing `_synced` lock: salt-retry while `node.id in self._graph` — recompute the id via `generate_node_id(node.type.value, f"{node.summary}#{salt}", node.timestamp)` (the salt perturbs only the id hash; the stored `summary` stays the original, exactly as the D1a doc fix did) and `model_copy(update={"id": ...})`. Then add + journal. RETURN the final id.
- `generate_node_id` stays a pure hash (no change).
- `_record_node`: `node_id = add_node(node, mint_unique_id=True)` and **rebind `node_id` to the return BEFORE the embedding block** — then EVERY downstream use reads the minted id: the ChromaDB `upsert_embedding(node_id, ...)` (peer-review A1 — the gap), `create_deterministic_edges(node_id)`, and the result dict. If a salt fires and the embedding is upserted under the STALE id, the node lands in the graph under the minted id while its vector lands under the old one → the node is silently UNSEARCHABLE (the N1 `search_hit_is_live` filter drops a hit whose stripped id isn't in the graph). All three uses must see the rebound local.
- `_store_document`: REMOVE its salt-retry loop; `node_id = add_node(node, mint_unique_id=True)`; **rebind BEFORE `_embed_document` and `create_deterministic_edges`** (peer-review A2 — else the document's node vector AND every `<id>#chunk-N` vector land under the stale id → unsearchable document + orphaned chunk vectors). One mechanism now (ledger 11). Doc salt input is equivalent (the doc node's `summary == title`, verified).

**Tests (rule 20, fails-before RUN):**
- **Generalize the frozen-clock collision test from documents to `_record_node`** (the D1a discovery `e434566c8440`, now global): with the module clock frozen (`monkeypatch.setattr(ct, "datetime", _FrozenClock)` — `_record_node` takes its timestamp from the same `ct.datetime` the doc test froze), record two nodes with the SAME `node_type` + `summary` but DIFFERENT `detail`, embeddings DISABLED (a bare-storage ctx with no `embedding_ready` → the embed block is skipped, no ChromaDB needed) → distinct ids, BOTH survive in the graph. Fails-before RUN (without the mint: identical ids, the second silently overwrites the first → one node).
- **Embedding-id rebind (guards A1/A2 — the unification's actual crux):** on a salted collision, the embedding is upserted under the MINTED id, not the stale one — assert the stored node is searchable (or that the upserted vector id == the returned node id, with a fake embedding store recording the id). Without the rebind this regresses SILENTLY (the graph-count test still passes while the vector is orphaned). Add for both `_record_node` and `_store_document`/`_embed_document`.
- **Replay-convergence seam (the explicit one):** write a journal via one storage (two minted nodes, possibly salted), hydrate a SECOND storage from the same journal → it converges to the SAME ids with the SAME node count — replay does NOT re-salt or duplicate. (Guards that the mint lives at generation, not replay.)
- **Opt-out default:** `add_node(node)` (no flag) with an id already present overwrites as before (a hand-chosen-id test still works) — confirms non-breaking.
- **Keep `test_distinct_docs_get_distinct_ids_under_a_frozen_clock` green** — it's the unified path's doc-side guard; the salt loop physically moves out of `cognition_tools` into `storage.add_node`, but the frozen timestamp still drives the collision through it.
- N≥3 consecutive green on the frozen-clock + replay + embedding-id tests.

## Commit 2 — post-commit hook: commit-hash discriminator
- In `_generate_id`'s call site (`_append_episode`, post-commit.py:108), fold the commit hash into the id input: `_generate_id("episode", commit["message"], timestamp, commit["hash"])` → `raw = f"{node_type}:{summary}:{timestamp}:{commit_hash}"`. The commit hash is unique per commit, so two distinct commits can't collide regardless of message/clock — without the hook (a stdlib-only separate process) needing to load/replay the graph to check ids. Keep it stdlib-only.
- The id stays an opaque key (nothing depends on it equaling the server's `generate_node_id`); the journal-line `_commit_already_tracked` idempotency keys on the commit hash, not the id — unaffected.

**Tests (rule 20):** the hook's id-gen yields DISTINCT ids for two distinct commit hashes with an identical message + identical timestamp (fails-before RUN: without the hash in the input, identical ids). Verify no existing hook test asserts the old id scheme.

## Commit 3 — composition review + inventory (rule 11)
- Confirm the full `storage.add_node(` caller inventory (grep): only `_record_node`/`_store_document` opt into minting; all others (tests, any internal) use the default and are unaffected; replay never calls the public `add_node`.
- A code comment at the `add_node` mint loop naming the TOCTOU-shrink-not-eliminate framing and the replay-seam (mint at generation, never replay) so a future reader doesn't "hoist" it into replay.
- Add any test the review surfaces; full suite + ruff + pyright ≤ 8.

---

## Out of scope (tracked → BACKLOG)
- Full elimination of the cross-process mint TOCTOU (would need a transactional/cross-process-locked id reservation — the journal-IPC model doesn't give that cheaply; this WP SHRINKS it, backlog #2 stays open with the residual documented).
- The cosmetic items from prior WPs already on BACKLOG.

## Build order rationale
Server mint first (Commit 1) — the frequent, real-collision path (the one that surfaced in CI) + the unification; the replay seam is proven there. The hook (2) is the separate-process minter with its own right fix. Composition/inventory (3) closes the cross-writer claim. Each commit independently green; the timing tests N≥3 green.

## Verification gate (per push)
Full pytest (frozen-clock + replay tests N≥3 green) + ruff + pyright ≤ 8 → push → CI green 3 legs → ping Vince the tip SHA → SHA-pinned merge gate.
