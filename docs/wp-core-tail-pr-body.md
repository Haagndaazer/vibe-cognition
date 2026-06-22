Three P3 core-robustness fixes to the cognition graph storage, off `043dadd`. They compose with each other and with WP-ID's `add_node` mint. Four commits, each independently green.

## Commits
1. **C-4 (the meaty one): journal-FIRST, then mutate.** Every write op mutated the in-memory NetworkX graph BEFORE appending to the journal. If `_append_journal` raised (disk full, Windows AV transiently locking the file), the graph kept a **phantom write the journal never recorded** — invisible to other processes, lost on this process's next re-hydrate. Reordered, under `_synced`, to: read-only validation → (`add_node`) mint → `_append_journal` → mutate → return, at all 8 `_append_journal` sites (`add_node`, `add_edge`, `update_node`, `remove_node`, `remove_edge` single + remove-all loop, `redirect_edges` ×2). A failed append now leaves nothing mutated; a crash/exception between append and mutate self-heals on replay.
2. **C-6: document why appends don't advance the offset; reword the log.** `_append_journal` deliberately doesn't advance `_offset`/`_journal_hasher`, so a process re-reads its own appends on the next `_catch_up` and logged a misleading "caught up: +N entries". Documented the deliberate design + reworded the log. Advancing the offset would be **unsafe** (see below).
3. **C-7: path-based cycle detection in `get_reasoning_chain`.** A traversal-global `visited` set (never popped) wrongly flagged a re-convergent DAG node — a diamond A→B→D, A→C→D — as a cycle on the second arm. Now tracks the current root→node path; a node is a cycle only if it's its own ancestor.
4. **Composition** — the whole reordered write surface round-trips through replay.

## Why C-4 is correct (composition)
- **WP-ID mint stays first** — it needs the caught-up in-memory graph to detect collisions, and it never runs on replay (`_replay_entry` writes `self._graph` directly), so journaling the minted node before mutating is sound.
- **Self-replay stays idempotent** — re-reading our own appended line on the next `_catch_up` re-applies `add_node` (overwrite by id; `_index_node_refs` dedupes refs) and `remove_node` (guarded no-op) without duplication or resurrection. Verified by a peer reviewer against the actual code.
- **`redirect_edges` reorder-only** — the self-loop guard is validation and stays before the append; no existence/dup guards added; the `out_edges`/`in_edges` snapshot kept. (Its pre-existing `reason`-drop in the hand-built journal payload is the noted WP-Cap residual, out of scope.)

## Why C-6 does NOT advance the offset (the trap avoided)
The in-process `RLock` and the `journal_io` append lock are **different locks**, so another process can append between this op's `_catch_up` and its own `append_journal_line`. Our bytes therefore need not land at `_offset` (un-replayed remote bytes may sit in front), so advancing by `len(our_blob)` would corrupt the C-3 prefix-hash invariant → a spurious full re-hydrate. Re-reading from disk with idempotent replay is what keeps the offset correct without assuming where our bytes landed. The naive C-6 "fix" would have broken C-3.

## The load-bearing test detail (C-4 fails-before)
The append-failure guards assert against the **RAW** in-memory `storage.graph` / `storage._reference_index`, NOT a `_synced` accessor: a synced read runs `_catch_up`, and since the failed append wrote nothing to the journal, catch-up can't see the in-memory phantom — reading through it would hide the phantom and tautologize the guard. Fails-before RUN: against the mutate-first code the 4 phantom guards are red. The C-7 diamond test is fails-before the same way (red against the global-`visited` impl).

## Accepted, named trade-off (C-7)
A re-convergent node is now fully re-expanded once per path — worst case O(b^max_depth) tree nodes (b = avg LED_TO out-degree), bounded by `max_depth` (default 5; `cognition_get_chain` exposes it to callers).

## Verified
Full suite 252 green, ruff clean, pyright == baseline (8). N≥3 consecutive green on the C-4 append-failure + replay tests. Journal protocol held — no commit touches `.cognition/journal.jsonl`. Plan was decorrelated-peer-reviewed before build (GO-with-changes; both blocking items folded).

## Out of scope (BACKLOG)
WP-Emb / E-3 query-prefix re-embed (parked, needs Colton's separate go), `redirect_edges` reason-drop, cross-process has_node→add_node TOCTOU (WP-ID backlog #2 — C-4 shrinks the phantom window but the documented mint residual remains), dashboard cosmetics.
