# WP-T Execution Plan — tool-layer correctness + the pyright ratchet (P1)

Audit/BACKLOG items clustered in `tools/` (they compose, so review against each other — rule 11): **T-9** (lifespan accessor → drop the pyright baseline), **T-2** (uncurated count caps at 500), **T-3** (batch partial-commit crash), **T-6** (node_type/direction error-contract split), **C-5** (add_edge TOCTOU result swallowed). Off the post-v0.8.0 unblock (main `6c2ce12`). No feature work — correctness + the strict-pyright ratchet.

## Binding rules
Rule 20 (assertions name the failure mode), 12 (every guard's fails-before RUN), 11 (compose — same tool layer), 21, 18. Journal protocol. **pyright must DROP, not just hold** (the T-9 win) — lower `.github/pyright-baseline.txt` by the amount actually OBSERVED (measure, don't pre-commit a number — the D2 B5 discipline). SHA-pinned merge gate.

## Testing strategy (the tools are @mcp.tool closures, not directly callable)
Following the established `_store_document`/`_search_cognition` pattern: where a fix needs end-to-end tool testing, extract a module-level core that the thin closure calls; test the core. Pure helpers (`_parse_node_type`, direction validation) are module-level and tested directly. Storage-level fixes (T-2 count) are tested on the storage method directly.

---

## Commit 1 — T-9: route every raw `lifespan_context` access through `get_lifespan` (the baseline mover)
`get_lifespan(ctx)` already exists (utils.py, seeded in WP-D1a; narrows fastmcp's `request_context: ... | None`). **20** raw `ctx.request_context.lifespan_context[...]` sites remain (peer-review A1 — verified by pyright count): `cognition_tools.py` 55, 56, 59, 82, 83, 742, 766, 820, 883, 972, 1010, 1041, 1074, 1132, **1173, 1174** (the `embed_storage` subscript in `cognition_remove_node` — easy to miss), 1198 (17); plus `dashboard_tool.py:40`, `service_tools.py:27`, `utils.py:21` (`require_embeddings` itself). Route each through `get_lifespan(ctx)`:
- Simple subscripts → `storage = get_lifespan(ctx)["cognition_storage"]` (already the D1a-tool form; `.get(...)` on the returned `dict[str, Any]` typechecks too — verified).
- `require_embeddings` (utils.py) and `_record_node` → `lc = get_lifespan(ctx)`.
- Each removed direct access drops a `reportOptionalMemberAccess` error.

**Expected floor: 29 → 9** (peer-review A2 — measured, but NAME the target so a deviation is investigated, not silently committed): 20 errors are this T-9 class; the **9 residual won't move** — `dashboard/server.py:165` (`install_signal_handlers`), 7 in `embeddings/storage.py` (~285 chromadb `where` arg-type + ~307 the `startswith` block), and 1 in `tests/test_cognition.py:863` (pyright scans tests). LOWER `.github/pyright-baseline.txt` to the OBSERVED count; if it isn't 9, investigate before editing the file. (Commit 2 fixes the test_cognition.py:863 one → 8; see B2.)

**Tests (rule 20):** `get_lifespan` raises `RuntimeError` when `ctx.request_context` is None (wiring guard — the accessor's whole reason to exist); the existing tool tests stay green (behavior unchanged — pure narrowing). pyright drop is the proof.

## Commit 2 — T-2: honest uncurated count
`storage.get_uncurated_nodes(limit)` hard-caps at `min(limit, 500)` (storage.py:445), and `cognition_get_uncurated_nodes` computes `total_uncurated = len(all_uncurated)` from a `limit=999999` call (cognition_tools.py:1015-1021) — so the cap silently swallows the count: `total_uncurated` can never exceed 500.
- Add `storage.count_uncurated_nodes(node_type=None) -> int` — counts uncurated nodes with NO cap. **Mirror the get filter EXACTLY** (storage.py:438-441): skip `data.get("curated_by_skill_at") is not None` AND, when `node_type` given, skip `data.get("type") != node_type.value` (the tool passes a `CognitionNodeType`, so compare `.value`). The tool uses it for `total_uncurated`; the returned `nodes` list keeps its 500 cap (Vince: the list cap can stay; it's the COUNT that lies).
- **B2 (free adjacent win):** while in the uncurated test cluster, fix the one residual pyright error in `tests/test_cognition.py:863` (`marked = storage.get_node(...)` subscripted without a None-guard) → add `assert marked is not None`. Drops the baseline floor 9 → **8**; lower the baseline file to 8 in this commit.

**Tests (rule 20, real boundary):** create 501 uncurated nodes → `count_uncurated_nodes()` returns 501 and the tool's `total_uncurated` is 501 while `nodes` is capped at ≤500. Fails-before RUN (old path → total_uncurated caps at 500). 501 in-memory `add_node`s is fast. Also a type-filtered count (some other-type nodes present) returns only the matching count.

## Commit 3 — T-6: one node_type parser + direction validation; consistent error contract
Three node_type-parsing behaviors today: `get_history` handles it correctly (try/except → error dict, 766-774); `get_uncurated` does the BARE `CognitionNodeType(node_type)` (1010 — raises an uncaught ValueError); `get_edgeless` does a raw string compare (978 — a bad type silently returns nothing). And `direction` is unvalidated: `get_chain` passes it to `get_reasoning_chain` (unknown → silently treated as incoming), `get_neighbors` (1089/1101) silently returns neither list for an unknown direction.
- Extract `_parse_node_type(node_type: str | None) -> tuple[CognitionNodeType | None, dict | None]` (returns `(nt, None)` or `(None, error_dict)` with the canonical `{"error": "Invalid node type '...'. Valid: [...]"}` shape). Route `get_uncurated`, `get_edgeless`, `get_history` through it (one shape, no raise, no silent-empty).
- Extract `_validate_direction(direction, allowed)` → error dict on an unknown direction; apply in `get_chain` (allowed: outgoing/incoming) and `get_neighbors` (incoming/outgoing/both) instead of silently doing the wrong thing. **This is an intentional contract change** (peer-review B4): `get_neighbors(direction="sideways")` today returns a silent empty success; after, an error dict. No existing test covers it, so nothing breaks — but it's deliberate.
- **Canonical error shape:** pick ONE (e.g. `{"error": "Invalid node type '<x>'. Valid: [...]"}`) and use it everywhere; note `get_history`'s current string says "node type" (space) — converging is fine.
- Scope discipline (Vince): unify the parser + the clearly-wrong silent-wrong-answers; LEAVE cosmetic items. **Already-fixed / out-of-scope (do NOT touch):** `get_neighbors`'s missing-node fields already return `None` (not the audit's old `{type: unknown}` stub) — only its direction is the bug; `get_reasoning_chain`'s `"type": "unknown"` for a missing traversed node (queries.py) is a separate cosmetic — leave it (BACKLOG); the duplicate `type`/`edge_type` keys in neighbor payloads (D-5-class cosmetic) — BACKLOG.

**Tests (rule 20):** `_parse_node_type` — valid → enum; bad → error dict (no raise); None → (None, None). `_validate_direction` — bad → error dict. Tool paths: `get_uncurated`/`get_edgeless` with a bad node_type → error dict (fails-before RUN: uncurated raised ValueError / edgeless returned empty-success); `get_chain`/`get_neighbors` with a bad direction → error dict (fails-before: silent wrong answer).

## Commit 4 — T-3 + C-5: add_edge correctness (batch partial-commit + swallowed TOCTOU)
Extract `_add_edge_core(storage, from_id, to_id, edge_type, reason, source)` and `_add_edges_batch_core(storage, edges_json)`; the closures become thin (`get_lifespan` + call core).
- **T-3 (batch partial-commit crash):** in the batch loop (901-906), a non-dict array element makes `e.get(...)` raise `AttributeError` AFTER earlier edges were already journaled — a partial commit + crash, while every other malformed input is skip-and-reported. Add `if not isinstance(e, dict): errors.append(f"[{i}] not an object"); skipped += 1; continue` at the top of the loop.
- **C-5 (swallowed TOCTOU):** `storage.add_edge(edge)` returns `False` if a node is missing (a delete racing the post-`has_node` check), but the single tool returns `{"created": True}` regardless (852-858) and the batch does `created += 1` regardless (948-949). Surface the real result: single → `{"error": ...}` (or `created: False`) when `add_edge` returns False; batch → `errors.append` + `skipped += 1`, don't count it created.

**Tests (rule 20):** batch with a STRING element mid-array → earlier valid edges ARE committed AND the bad one is reported, no crash (fails-before RUN: AttributeError + partial commit); `_add_edge_core` against a fake storage whose `add_edge` returns False (has_node True) → returns an error, not `created: True` (fails-before RUN: old returned created:True); batch likewise counts a False add as skipped, not created.

## Commit 5 — composition review (rule 11)
The four fixes share the tool layer: confirm T-9's `get_lifespan` routing didn't miss a site the others touch; the extracted cores (`_add_edge_core`, `_add_edges_batch_core`) + helpers (`_parse_node_type`, `_validate_direction`) are each single-sourced (no re-encoded copies — ledger 11); the error-dict shape is consistent across every tool touched. Add any test the review surfaces. Full suite + ruff + pyright at the new lowered baseline.

---

## Out of scope (tracked → BACKLOG)
- WP-ID (the GLOBAL node-id collision — hoist salt-retry into `add_node`'s locked block + cross-writer review incl the post-commit hook) — queued RIGHT AFTER WP-T, separately.
- D-5 dashboard cosmetics, the dashboard fixed-over-query consistency item, the deferred D-3 cluster — all BACKLOG.
- Audit E-7 (`datetime.utcnow()`), the cosmetic neighbor-payload duplicate keys.

## Build order rationale
T-9 first (Commit 1): biggest diff + the baseline mover, and it routes the same sites the later commits touch, so the cores extracted in Commits 3/4 are born already using `get_lifespan`. Then the storage count (2), the parsing/direction contract (3), the add_edge correctness (4), composition (5). Each commit independently green (suite + ruff + pyright ≤ the new baseline), every fails-before RUN.

## Verification gate (per push)
Full pytest + ruff + pyright (≤ the LOWERED baseline) + the baseline file updated to the observed count → push → CI green 3 legs → ping Vince the tip SHA → SHA-pinned merge gate.
