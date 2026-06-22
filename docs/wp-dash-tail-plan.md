# WP-Dash-tail — Implementation Plan

Base: `main` @ 3961f32. Branch: `fix/wp-dash-tail`.
Scope: the dashboard P3 cleanup (audit items D-3 rest, D-5 cosmetics, + the dashboard/MCP
over-query divergence). LOW/cosmetic — tiered, bundled, not over-ceremonied.

Binding rules: journal-not-committed on the WP branch; pyright ≤ 8; fails-before where there's
a behavioral delta; CI green 3 legs before gate. The dashboard is TestClient-testable, so every
**backend** delta gets a real assertion. Frontend (app.js) changes are JS-only and not
TestClient-reachable — called out explicitly as such.

---

## TIER 1 — actually matters (fails-before required)

### T1. Over-query unify (dashboard ↔ MCP search divergence)
**Problem:** `dashboard/api.py:search._do_search` uses a FIXED `limit * 5` over-query.
`tools/cognition_tools.py:_search_cognition` uses an ADAPTIVE doubling widen (the D2 B3 fix:
a fixed `limit×k` can't guarantee `limit` distinct nodes when one document yields more than
`limit×k` chunks). The two recall behaviors are silently diverged.

**Factoring (chosen):** extract the widen LOOP — not the dedupe — into a shared helper. The
two surfaces legitimately have different result shapes (MCP `_format_search_results` →
`{id, node_type, ...}`; dashboard → raw `{_id, **metadata, score}` preserved + summary
hydrated). The loop is identical; the dedupe is not. So the helper takes the dedupe as a
callback:

```python
# embeddings/storage.py  (natural home: it wraps vector_search; leaf module both surfaces
# already depend on — avoids a dashboard→tools layering edge)
_SEARCH_OVERQUERY_K = 5
_SEARCH_OVERQUERY_CAP = 500

def adaptive_vector_search(embedding_storage, query_embedding, *, entity_type, limit, dedupe):
    """Widen n_results (doubling) until `limit` distinct deduped results, Chroma exhausted,
    or the cap. `dedupe(results, limit) -> list` owns N1-drop + chunk-dedupe per surface."""
    n = max(limit * _SEARCH_OVERQUERY_K, limit, 1)
    while True:
        results = embedding_storage.vector_search(
            query_embedding=query_embedding, limit=n, entity_type=entity_type)
        formatted = dedupe(results, limit)
        if len(formatted) >= limit or len(results) < n or n >= _SEARCH_OVERQUERY_CAP:
            return formatted
        n = min(n * 2, _SEARCH_OVERQUERY_CAP)
```

- `embeddings/__init__.py`: export `adaptive_vector_search` (+ `_SEARCH_OVERQUERY_K/_CAP` if
  imported by name) — **or** callers do `from ..embeddings.storage import adaptive_vector_search`.
  Pick one; don't leave the import dangling (review #1).
- `cognition_tools.py`: delete its local `_SEARCH_OVERQUERY_K/_CAP` + the while-loop body in
  `_search_cognition`; import the helper + constants from `embeddings`; pass a
  `lambda results, lim: _format_search_results(results, storage, lim)` dedupe.
- `dashboard/api.py`: replace `_do_search`'s fixed `limit * 5` call with
  `adaptive_vector_search(...)`, moving its inline dedupe into a local `dedupe(results, lim)`
  closure (same logic, now driven by the loop). `_MATCHED_EXCERPT_LEN` (500) — dashboard
  currently hardcodes `[:500]`; reuse the constant or keep its literal (note in PR).

**Fails-before test (`tests/test_dashboard.py`):** seed `_search_results` = **≥ 11** chunks of
ONE live document (`docA#chunk-0..10+`) followed by a second live node `B`, request `limit=2`.
The ≥11 count is load-bearing (review #2): `FakeEmbeddingStorage.vector_search` returns
`_search_results[:n]`, so with the OLD fixed `n = limit*5 = 10` the slice stops INSIDE doc A's
chunks → never reaches `B` → 1 distinct node (FAILS). The adaptive helper widens `n` (10→20) and
the second slice reaches `B` → 2 distinct nodes (PASSES). Assert `len(results) == 2` and `B`
present. (Confirm the fake returns all rows when `n ≥ len(_search_results)` — it does, via `[:n]`
— so the widen loop terminates by exhaustion.)
Also keep `test_search_limit_is_clamped` green (n starts at cap 500; empty results → exhaust →
`last_limit == 500 ≤ 500`); update its comment to note the over-query/clamp now lives in
`adaptive_vector_search` (review #3, nit).

### T2. `--no-embeddings` → "embeddings off" state (not "Loading…" forever)
**Problem:** CLI `--no-embeddings` never sets `ready_event` and leaves `embedding_error=None`,
so `_embedding_status` reports `"loading"` and the frontend `pollEmbeddingReady()` spins
forever showing "Loading embedding model…".

- `cli.py`: when `--no-embeddings`, set `ctx["embeddings_disabled"] = True`.
- `api.py:_embedding_status`: first check `if lc.get("embeddings_disabled"): return False, "disabled"`.
  (Flows into `/api/stats.embedding_status` and the search 503 body unchanged otherwise.)
- `app.js` (JS-only): in `init()` and `pollEmbeddingReady()`, treat `status === "disabled"` as a
  terminal state — `setSearchEnabled(false, "Search disabled (embeddings off)")`, set the banner
  to a neutral class, and DO NOT poll.

**Fails-before test (backend):** set `lc["embeddings_disabled"] = True`, GET `/api/stats`,
assert `embedding_status == "disabled"` (currently `"loading"` → FAILS); ALSO assert the search
503 body carries `embedding_status == "disabled"` (review #4). Existing
`test_stats_shape`/`test_search_503_when_not_ready` stay green (their fixture sets no
`embeddings_disabled` key). Frontend terminal-state rendering is JS-only (not TestClient-tested).

### T3. IPv6 loopback host-check
**Problem:** `middleware.py` accepts only `host.startswith("127.0.0.1:" | "localhost:")`,
rejecting `[::1]:<port>` (IPv6 loopback) with 403.

- Accept `[::1]:` prefix alongside the two existing forms. (Server binds AF_INET 127.0.0.1, so
  this is correctness/consistency, not a new exposure; the DNS-rebinding mitigation intent is
  preserved — still loopback-only.)

**Fails-before test:** GET `/api/graph` with `Host: [::1]:7842` + valid token — currently 403,
after the fix 200. (TestClient lets us set the Host header directly.)

> **Scope note (review #5):** this is a **middleware-layer** fix only. uvicorn binds AF_INET
> `127.0.0.1` (`server._find_free_port` + the `host="127.0.0.1"` configs), so a real client
> hitting `[::1]` is refused at TCP and never reaches the middleware. Actual IPv6 loopback support
> would additionally require binding `::1`/`::` — **out of scope** for this WP (Vince scoped only
> "accept it" in the host-check). Call this out in the PR so no one assumes IPv6 "just works."

---

## TIER 2 — tidy-while-you're-in-there

### D-3a. Auto-refresh `/api/graph` on an interval (JS-only)
Add a `setInterval` (e.g. 30s) that re-runs `refreshStats()` + `loadGraph()` — the manual
Refresh button (D4) stays. Guard against overlap (skip if a fetch is in flight) and preserve the
current selection where possible. JS-only — not TestClient-tested; called out in PR.

### D-3b. Search-wiring robustness (JS-only)
`init()` does `await loadGraph()` BEFORE wiring the search input + refresh button. If the initial
graph fetch throws, the catch fires and the listeners never attach. Reorder: wire all event
listeners FIRST (they don't depend on graph data), then `loadGraph()` in its own try/catch so a
graph-fetch failure degrades to "empty canvas, search still works" rather than a dead UI. JS-only.

### D-5b. Drop duplicate `type` key in neighbor payloads (backend)
`api.py:get_node` emits both `type` and `edge_type` (same value) per successor/predecessor.
`app.js` reads only `edge_type` (line 208). Drop `type` **from the `get_node` neighbor dicts
only** — NOT from `get_graph`'s `edges_out`, whose `type` key drives the Cytoscape edge label
`data(type)` (app.js line 104) and must stay (review #7). **Fails-before:** assert a successor
dict has `edge_type` and NOT `type`.

### D-5c. Drop unused `context`/`severity` from the graph payload (backend)
`get_graph` ships `context` + `severity` per node, contradicting its own "keep payloads small"
docstring (it already excludes `detail`). The frontend graph/episode rendering uses neither (full
node detail is fetched separately on click). Drop both. **Confirmed safe (review #6):**
`renderDetail` (app.js ~196-200) DOES read `context`/`severity`, but from the `/api/node/{id}`
response, NOT the graph payload — so dropping them from `get_graph` only doesn't touch the detail
pane. **Fails-before:** assert a graph node `data` has no `context`/`severity` (mirrors the
existing `"detail" not in node` assertion).

### D-5d. Dedup the default port `7842` (3 sites → 1 constant)
Hardcoded in `server.run_dashboard_blocking`, `server.start_dashboard`, and `cli.py --port`.
Add `DEFAULT_PORT = 7842` in `server.py`; cli imports it for the argparse default. Light test:
assert the constant is the argparse default (or just rely on existing lifecycle tests).

### D-5h. `stop_dashboard`: don't close the ExitStack on join-timeout (backend)
`stop_dashboard` calls `state["stack"].close()` unconditionally after `thread.join(timeout)`.
If the join TIMES OUT (thread still alive), closing the stack tears down the static-files
resource context the still-running server thread may serve from. Fix: only `stack.close()` when
`not thread.is_alive()` after the join; if still alive, log a warning and leave it (daemon thread;
process exit reclaims it). **Fails-before (two focused cases, review #8):**
(a) fake thread `is_alive()→True` + a stack recording `close()` → assert `close()` NOT called;
(b) fake thread `is_alive()→False` → assert `close()` IS called. Existing
`test_stop_dashboard_joins_thread` (real clean join) stays green.

---

## Sequencing
1. Branch off 3961f32.
2. T1 (touches embeddings + tools + dashboard — biggest blast radius first).
3. T2, T3 (small, independent).
4. Tier-2 backend items (D-5b/c/d/h), then JS items (D-3a/b, T2 frontend).
5. Full suite + pyright (≤ 8) locally → push → CI green 3 legs → ping Vince SHA → SHA-pinned gate.

## In scope vs deferred
- **In:** all of the above (Vince's full WP-Dash-tail scope).
- **Deferred (not this WP):** E-3 (PARKED, needs Colton — invalidates vectors); WP-Emb non-E-3
  (E-4 concurrent-Chroma, E-6 dead-code prune, E-7 revision-pin).

## Risks / watch-items
- T1 layering: helper home is `embeddings/storage.py` to avoid a dashboard→tools import edge.
  Confirm no circular import (storage is a leaf — imports nothing from tools/cognition).
- T1 regression surface: the MCP `_search_cognition` path is the higher-value one; its existing
  cross-process ghost + dedupe tests must stay green (they exercise the real path).
- D-5c: double-check (grep) no app.js / no other API consumer reads node `context`/`severity`
  from the graph payload before dropping.
