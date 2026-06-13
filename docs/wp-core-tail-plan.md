# WP-Core-tail Execution Plan — core robustness (P3)

Three core-robustness items off current origin/main (`2a2be41`). They compose with each other and with WP-ID's `add_node` mint. **C-4** (mutate-then-journal → journal-first), **C-6** (self-replay offset/log noise), **C-7** (`get_reasoning_chain` flags diamonds as cycles).

## Binding rules
Rule 20 (assertions name the failure mode; fix+proof same commit), 12 (every guard fails-before: revert→red→restore), 11 (one shared discipline per concern), 18 (seam-check), 21 (constraint-drift), 23. Journal protocol (NOBODY commits `.cognition/journal.jsonl`; Vince flushes). pyright ≤ 8. SHA-pinned merge gate. **N≥3 consecutive green** on the C-4 append-failure + replay/convergence tests (replay-sensitive).

---

## The core machinery (what C-4 must NOT disturb)
- Every write runs under `_synced()`, which takes the in-process RLock and runs `_catch_up()` **once** at the outermost depth (before any mutation), so the op sees other processes' journaled writes.
- `_append_journal` writes one line via `append_journal_line` (CRLF on Windows, LF on POSIX) and **does NOT advance `_offset` or `_journal_hasher`**. So on the next `_catch_up`, this process **re-reads its own appended bytes from disk** and replays them idempotently. That re-read is the source of the C-6 log noise — and it is ALSO what makes the byte-offset/prefix-hash invariant (C-3) correct without the process having to know where its bytes landed.
- `_catch_up` advances `_offset` and updates `_journal_hasher` ONLY for bytes it actually read from disk past complete newlines; the C-3 invariant is `_journal_hasher == sha256(file_bytes[0:_offset])`.

**Critical composition fact:** C-4 only swaps the order of two already-adjacent statements *inside the lock* (append vs mutate). It does NOT touch `_offset`/`_journal_hasher`/`_catch_up`. The self-replay dynamics are identical before and after C-4. Replay stays idempotent (overwrite-by-id / has-node guards), so re-reading our own just-applied write is a no-op convergence either way.

---

## Commit 1 — C-4: journal-FIRST, then mutate (MED, the core reorder) — gate-hard
**Bug:** every write mutates the in-memory graph BEFORE `_append_journal`. If the append RAISES (disk full, Windows AV transiently locking the file), the graph keeps a **phantom write the journal never recorded** — invisible to other processes, lost on this process's next re-hydrate. The journal is the source of truth; in-memory is its cache.

**Fix (one discipline, every write path):** under `_synced`, the order becomes
1. read-only **validation / existence checks** (return `False`/early on invalid — we must NOT journal a no-op or invalid op),
2. for `add_node` with `mint_unique_id`: run the **mint loop** (read-only collision check against the caught-up in-memory graph; `model_copy` is pure) to finalize the id,
3. `_append_journal(...)` the validated, minted op,
4. **mutate** in-memory (`_graph.*`, `_index_node_refs`/`_unindex_node_refs`),
5. return.

A failed append then leaves NOTHING mutated (clean failure). A crash between append and mutate self-heals on replay (the op WAS journaled). A non-crash mutate exception self-heals on the next `_catch_up` (re-reads the journaled line). Either way: no phantom, eventual convergence — strictly better than today.

**WP-ID composition (confirm, don't break):** the mint's collision check needs the in-memory graph, so it MUST stay first; `_synced` already caught up before it, so it still sees cross-process journaled nodes. Order: mint → journal(minted) → `_graph.add_node` + `_index_node_refs`. The mint never fires on replay (`_replay_entry` writes the graph directly), so this is unchanged.

**Sites to reorder (all the same bug class):**
- `add_node` (storage.py ~187–200): move `_graph.add_node` + `_index_node_refs` AFTER `_append_journal`; mint stays before.
- `add_edge` (~224–232): both-nodes-exist check (read-only) stays first; journal then `_graph.add_edge`.
- `update_node` (~249–253): existence check first; journal then the field mutation loop.
- `remove_node` (~269–271): existence check first; journal then `_unindex_node_refs` + `_graph.remove_node`.
- `remove_edge` single (~298–308) AND the **remove-all loop** (~311–320): per-edge journal-then-`remove_edge` (a mid-loop append failure leaves a clean journaled+mutated prefix).
- `redirect_edges` (~338–370, two loops): per-edge journal-then-`add_edge`. Lower-traffic (supersession only); included for consistency (same bug). NB: it already hand-builds the journal payload and drops `reason` — that's the noted WP-Cap residual, **out of scope here**; reorder only.
  - **[review #5, BLOCKING] Reorder ONLY — do not add or move guards.** The per-edge self-loop guard `if target_id != new_node_id` / `if source_id != new_node_id` is VALIDATION and MUST stay BEFORE the journal append — journaling before it would write a self-edge the current code never journals, changing on-disk bytes + replay. Do NOT add an existence/duplicate guard while reordering: today it journals + `add_edge` + increments `redirected` unconditionally per non-self edge, and `add_edge` silently overwrites a pre-existing key (a pre-existing over-count, out of scope). Keep the `list(self._graph.out_edges(...))`/`in_edges(...)` **snapshot** — iterate the snapshot, never the live view.
- `create_deterministic_edges`: routes through `self.add_edge` (verified) — covered by fixing `add_edge`; no separate change.
- **Primary caller [review #18]:** `_record_node` calls `add_node` then embeds under the returned id; journal-first keeps the post-mint id contract AND is strictly safer (can no longer embed a node that wasn't durably journaled). No change to that caller.

**Tests (rule 20, fails-before — gate-hard):** monkeypatch the **instance** `storage._append_journal` to raise; for `add_node`, `add_edge`, `update_node`, `remove_node` assert (a) the op raises, and (b) state is **UNCHANGED**. **[review #14, BLOCKING] The unchanged-state assertion MUST read the RAW in-memory graph — `storage.graph`/`storage._graph` (`.has_node`/`.number_of_nodes`/`.number_of_edges`) and `storage._reference_index` — NOT a `_synced` public accessor.** A `_synced` accessor runs `_catch_up`; since the failed append wrote nothing to the journal, catch-up can't see the in-memory phantom, so reading through it would HIDE the phantom and make the guard tautological. The phantom lives only in the unsynced `_graph`; assert there (no phantom node/edge, no orphan ref-index entry; for the removes, target/edge still present). Fails-before RUN: against mutate-first code the phantom IS in `_graph` → red. Plus convergence/idempotency: a successful journal-first write is visible to a second `CognitionStorage` replaying the same journal, and re-applying it on this process's own next `_catch_up` is idempotent — count stable for add_node AND **[review #6]** `remove_node` (replay guarded by `if node_id in self._graph`, so a tombstone doesn't resurrect). **N≥3 consecutive green** on the append-failure + replay tests.

## Commit 2 — C-6: self-replay offset/log noise (LOW)
**Bug:** because `_append_journal` doesn't advance `_offset`, each process re-reads its own appends on the next `_catch_up` and logs `caught up: +N entries` — benign but misleading (implies a cross-process write when it's our own).

**Decision: DOCUMENT why we don't advance the offset (option b), do NOT advance it.** Advancing the offset on self-write is genuinely **unsafe** under the cross-process append model: the in-process RLock and the journal_io append lock are different locks, so another process can append BETWEEN this process's `_catch_up` and its own `append_journal_line`. Our just-written bytes therefore do NOT necessarily land at our current `_offset` (un-replayed remote bytes may sit in front of them), so we cannot advance `_offset`/`_journal_hasher` by `len(our_blob)` without corrupting the C-3 prefix-hash invariant → false re-hydrate. Re-reading from disk on the next `_catch_up` (idempotent replay) is what makes the offset correct *without* assuming where our bytes landed.

**Change:** add a precise comment at `_append_journal` (and a one-line pointer at the existing C-6 note in `_catch_up` ~832–835) stating the above, so the "noise" is a documented deliberate design, not an oversight. Reword the INFO log (count/nodes/edges line ~869–873) to neutral "replayed +N journal entries (includes this process's own appends)" — keeps ops visibility without falsely implying remote origin. No offset/hash change → no replay-correctness risk. C-4 interaction: none — C-4 doesn't touch the offset path; this commit edits comments + the log string only.
- **[review #8] Rejected alternative (note for the record):** a `_pending_self_appends` counter to subtract our own writes from the logged `+N` was considered — rejected as no-safer, since a concurrent remote append between the increment and the replay would mis-attribute under the same interleave. The neutral reword is the right minimal call.

**Tests:** assert behavioral invariants don't regress — a write followed by `_catch_up` on the same instance leaves node/edge **counts stable** (idempotent self-replay), `_offset == file size` afterward, and the C-3 prefix-hash still matches (no false re-hydrate). **[review #10]** This `_offset == file size` assertion is valid ONLY because the test is single-process (no concurrent appender) — state that in the test so nobody later "strengthens" it into a cross-process test where it would be a flaky false invariant (the whole C-6 point is that offset ≠ our-bytes under concurrency). This is a guard that the documented design holds; the log wording itself isn't asserted (no fails-before — C-6 has no behavioral delta).

## Commit 3 — C-7: `get_reasoning_chain` marks diamonds as cycles (LOW)
**Bug:** `visited` is a single set for the whole traversal and is never popped (queries.py ~29,47). A diamond A→B→D and A→C→D flags the **second** D as `cycle: true`, though it's a re-convergent DAG path, not a cycle.

**Fix:** path-based cycle detection — track the **current root→node path**, not a global visited set. Pass an immutable `path: set[str]` down (`traverse(nid, depth, path | {nid})`); a cycle is `nid in path` (an ancestor on the current path). No explicit pop (each recursion gets its own extended set; siblings don't see each other's nodes). A true cycle (A→B→A) still sets `cycle: true`. **[review #19]** annotate the new `path` param (`set[str]`) to keep pyright ≤ 8 clean.
- **[review #12] Accepted behavior change + bound:** a node reachable by two paths now appears once **per path** (correct for a reasoning *tree*), not stubbed as a cycle. The returned tree is worst-case **O(b^max_depth)** nodes (b = avg LED_TO out-degree) on a dense re-convergent DAG. `cognition_get_chain` exposes `max_depth` to callers (default 5), so a caller passing a large `max_depth` on a dense LED_TO graph could blow up the response — bounded and fine at the default; named here as a decided trade-off, not a blind spot.

**Tests (rule 20, fails-before):** a diamond → the second D is NOT `cycle: true` and IS expanded; a true cycle A→B→A → still `cycle: true`; a deep chain past `max_depth` → still `truncated: true`. Fails-before RUN: against the global-`visited` impl the diamond's second D is `cycle: true` → red.

## Commit 4 — composition
Confirm: every reordered write path is journal-first (one discipline); the C-6 comment + reworded log are in; C-7 diamond/cycle/truncate all correct. Full suite + ruff + pyright ≤ 8. The doc-drift guard is unaffected (no new tools / edge types).

---

## Out of scope (tracked → BACKLOG)
- WP-Emb / **E-3 query-prefix re-embed** — PARKED; needs Colton's explicit separate go (invalidates all existing vectors).
- `redirect_edges` dropping `reason` in its hand-built journal payload (WP-Cap residual) — reorder only here, fidelity widening stays out.
- Cross-process has_node→add_node TOCTOU (WP-ID backlog #2) — unchanged; C-4 shrinks the phantom window but the documented cross-process mint residual remains.
- Dashboard cosmetics, over-query consistency — P3 tail.

## Build order rationale
C-4 first (the core reorder the others sit on; touches every write path + composes with the WP-ID mint) → C-6 (documents the offset decision the reorder must respect) → C-7 (isolated, in queries.py) → composition. Each commit independently green (suite + ruff + pyright ≤ 8), every fails-before RUN; N≥3 consecutive green on the C-4 append-failure + replay tests.

## Verification gate (per push)
Full pytest (C-4 append-failure + replay fails-before; C-7 diamond) + ruff + pyright ≤ 8 → push → CI green 3 legs → ping Vince the tip SHA → SHA-pinned merge gate. Vince gates C-4 hardest (every write path + the data-loss composition).
