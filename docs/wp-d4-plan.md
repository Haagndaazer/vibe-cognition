# WP-D4 Execution Plan — dashboard document list + download + dashboard hardening (v0.8.0)

Brief = `docs/DESIGN-document-storage.md` §7 (WP-D4 row) + the dashboard audit cluster (`docs/AUDIT-2026-06-10.md` D-1..D-5) + the D2-deferred dashboard-NAV item (D-6). **The LAST functional WP; v0.8.0 cuts after it.** Builds on merged D3 (`9afc538`). The dashboard is Starlette + uvicorn with a TestClient harness (`tests/test_dashboard.py`) — endpoints are genuinely testable.

## Grounded in the LIVE code (audit re-verified, ledger 23 — some items are already fixed)
A fresh read of the current dashboard found two audit items ALREADY FIXED — do NOT re-fix:
- **D-2 (`_find_free_port` returns 0):** already correct — the preferred-port loop returns the bound `port` (`server.py:79`) and the fallback returns `getsockname()[1]` (`:84`). Note as done.
- **D-3d (deleted episode stays in sidebar):** already fixed — `deleteNode` calls `removeEpisodeFromList` (`app.js:236, 277-283`).
- **D-5j (unused `Response` imports):** already gone.

## Scope tiers (deliberate — keep D4 from ballooning, per the directive)
- **CORE (in D4):** the feature (document list + token-gated download), D-6 nav (dedupe+hydrate dashboard search), D-1 (start liveness), D-4 (vendor cytoscape/fcose — SRI + offline).
- **BUNDLED (cheap + justified — the download endpoint adds a network-facing file-serving surface, so harden it in the same WP):** D-5 security subset — `secrets.compare_digest` token check, malformed-body → JSON 400, search `limit` clamp; plus D-5h (export `stop_dashboard`) and D-3a (a Refresh button — the dashboard's primary use is a live session and staleness is the headline gap; a button is cheap).
- **TRACKED ONWARD (NOT D4 — stated so the asymmetry isn't read as intent):** the rest of D-3 (auto-poll, `--no-embeddings` "disabled" banner, search-wiring-attaches-only-on-init-success) — a coherent UI-state cluster larger than D4's document focus; remaining D-5 cosmetics (IPv6 `[::1]` host check, duplicate `type`/`edge_type` keys, unused `context`/`severity` in the graph payload, hardcoded-port dedup, ExitStack-close-on-join-timeout). All LOW. File to BACKLOG.

## Binding rules (carried)
Rule 20, 12 (fails-before RUN), 11, 21, 18. Journal protocol. pyright ≤ 29. SHA-pinned merge gate.

---

## Commit 1 — D-6: dashboard search dedupe-to-node + hydrate (the navigation fix)
The D2 SAFETY filter (drop graph-absent ghosts via `search_hit_is_live`) shipped; this is the navigation half. Today `dashboard/api.py search()` returns un-deduped `<node>#chunk-N` rows that don't navigate (`cy.$id("<node>#chunk-N")` misses) and lack node metadata.
- Dedupe hits to the best (first, score-desc) per stripped node id; rewrite `_id` to the NODE id so the frontend's `cy.$id(id)` navigates; hydrate `summary` from `cognition_storage.get_node(node_id)` (the graph, authoritative — chunk metadata has no summary); carry `matched_excerpt` from the chunk text. Keep the existing `search_hit_is_live` drop (reuse the shared predicate). **Field-name correctness (peer-review A1):** the frontend `renderSearchResults` reads **`entity_type`** (`app.js:374`, `r.entity_type`), NOT `type`, and does not read `timestamp` — so PRESERVE the hit's existing `entity_type` (the chunk's Chroma metadata already carries it; do NOT overwrite it with a graph `type` key) and set `summary` from the node. Done right, a node-keyed hydrated row navigates with NO JS change; the Commit-1 test MUST assert the returned row has a non-empty `entity_type` (locks A1).
- The MCP `_format_search_results` already does dedupe+hydrate (different output shape); do NOT force-share the body (the dashboard hydrates from the graph + keeps its raw-ish JSON contract), but DO reuse `search_hit_is_live` for the live check (the one shared identity question, ledger 11).

**Tests (rule 20, TestClient):** seed a document with N chunks + a live node; POST /api/search → the document appears ONCE keyed on the node id (not a chunk id), hydrated with its summary, plus the other node; a deleted document's chunk rows still drop (D2 safety intact). Fails-before RUN (pre-dedupe → N chunk rows with `#chunk-` ids).

## Commit 2 — document LIST endpoint + UI
- `GET /api/documents` → list `document` nodes via `cognition_storage.get_nodes_by_type(CognitionNodeType.DOCUMENT)` (peer-review A2: there is NO `get_history(node_type=...)` — `get_nodes_by_type` at storage.py:377 is the only correct API). Per-doc shape `{node_id, doc_ref, summary, mode, size, mime, filename, indexed_text_chars, timestamp, has_blob}` from node metadata — note for documents the **title IS `node["summary"]`** (B1), and `has_blob` = `metadata.get("mode") == "copy"` (B2; `blob_path`/`path` are mode-conditional, treat as optional). Define `has_blob`/blob-resolution ONCE and share it with the download endpoint (ledger 11). Token-gated by the existing middleware. Small payload (metadata only, never the text/blob).
- Frontend: a "Documents" panel/tab listing stored documents (title, mode, size); clicking one selects its graph node (reuse the existing node-detail path) — so documents are browsable, not only search-reachable.

**Tests:** store 2 documents (1 reference, 1 copy) → GET /api/documents returns both with correct mode/`has_blob`; empty graph → `[]`; token required (401 without).

## Commit 3 — token-gated, path-SAFE document download endpoint + UI
The security-critical new surface — serves file bytes over HTTP. Path-safety is the whole game.
- `GET /api/document/{node_id}/download` → resolve the node via the graph; serve bytes based on mode:
  - **copy mode:** stream the content-addressed blob from `documents/<sha[:2]>/<sha><ext>` — the path is RECONSTRUCTED from the node's stored `sha256`/`blob_path` metadata, validated to live UNDER the documents dir (reject anything that resolves outside it — defense even though the path is server-derived), never from the request.
  - **reference mode (no blob):** the bytes aren't stored. Serve the **text sidecar** (always present, the searchable extract) as the download, OR 409/410 with a clear "reference mode — original not stored; bytes live at <path> on the storing machine" message. DECISION: serve the sidecar text as `<title>.txt` (always available, including to a teammate who pulled the journal) and report `mode: reference` so the UI labels it "extracted text". Do NOT read the absolute referenced original path over HTTP (it's machine-specific and an arbitrary-local-file-read vector even if token-gated).
  - **missing artifact:** 404, never a traceback.
- `node_id` is the ONLY client input and it's a graph key (not a path); the filename in the `Content-Disposition` is sanitized. Token-gated. Privacy caveat unchanged (committed blobs persist in git history).
- Frontend: a "Download" action on each document (list row + node-detail sidebar) hitting the token'd URL.

**Tests (rule 20):** copy-mode download returns the exact blob bytes; reference-mode download returns the sidecar text labeled `reference`; a `node_id` that isn't a document → 404; **path-safety: a node whose metadata blob_path is tampered to escape the documents dir → rejected** (fails-before RUN against an un-validated join); token required.

## Commit 4 — dashboard hardening (D-1, D-4, D-5 security, D-5h, D-3a)
- **D-1 (start liveness):** after `thread.start()`, briefly poll `server.started` (uvicorn sets it True at `server.py:195`; verified real) up to a bounded timeout (≤2s, no hang) before storing state + returning the URL; if it never comes up, return an error status (not a dead URL). **Fails-before that isn't theatrical (B3):** the port is pre-bound by `_find_free_port` before the thread starts, so a real bind failure is rare — test the failure path by monkeypatching `server.run` to raise / never set `started`, and assert `start_dashboard` returns an error status, not a live URL; assert the happy path sets `started`.
- **D-4 (vendor cytoscape/fcose):** download the pinned `cytoscape@3.30.2`, `cose-base@2.2.0`, `cytoscape-fcose@2.2.0` into `static/vendor/` and point `index.html` at the local copies (drop the jsdelivr `<script>` tags). Fixes the no-SRI CDN-compromise-runs-JS-that-can-delete-nodes risk AND makes the dashboard work offline. (The one step needing a network fetch — vendoring IS the fix. Verify the dashboard still renders.)
- **D-5 security (the download surface justifies it):** `secrets.compare_digest` for the token compare (`middleware.py:34,38`); wrap `request.json()` → JSON 400 on malformed body (`api.py:126`); clamp search `limit` to a sane range (`api.py:128`).
- **D-5h:** export `stop_dashboard` from `dashboard/__init__.py` (pure tidiness — it's already defined at `server.py:187` and imported where used; no behavior change).
- **D-5 security needs an import:** `secrets` is NOT imported in `middleware.py` today (only in `server.py`) — the `compare_digest` fix must add `import secrets`.
- **D-3a (Refresh):** a Refresh button that re-fetches `/api/graph` and re-renders (the dashboard's main use is a live session; one button, no polling).

**Tests:** token compare rejects a wrong token / accepts the right one (compare_digest path); malformed JSON body → 400 not 500 (fails-before RUN: bare `request.json()` → 500); `limit` clamp; `stop_dashboard` importable from the package root; D-1 liveness returns an error status when bind fails (simulate). D-4/D-3a are frontend — verify by rendering + a smoke assertion the vendored files exist and index.html references no `cdn.jsdelivr.net`.

---

## Out of scope (tracked → BACKLOG)
- The deferred D-3 cluster (auto-poll, `--no-embeddings` disabled banner, search-wiring robustness) + the D-5 cosmetics (IPv6 host, duplicate keys, unused payload fields, hardcoded-port dedup, ExitStack-on-timeout). All LOW.
- README standalone-dashboard fix (S-3 MED) — **fold into Commit 4's docs** since D4 is the dashboard WP (the README's "run `uv run vibe-cognition-dashboard`" doesn't work for plugin users; the vibe-dashboard skill gets it right — align the README to the skill's caveat).
- v0.8.0 version bump (`pyproject.toml` + `plugin.json`) + CHANGELOG = the RELEASE step AFTER D4 merges (Vince/Loki drive it). Not in D4.
- Vince backlog #1/#2; audit E-7.

## Build order rationale
D-6 nav (1) first — smallest, fixes a shipped regression, no new surface. List (2) then download (3) — the feature, download last since it's the security-sensitive surface and builds on the list. Hardening (4) last — independent, and the security items harden the download surface that now exists. Each commit independently green (suite + ruff + pyright ≤ 29), every fails-before RUN.

## Verification gate (per push)
Full pytest + ruff + pyright ≤ 29; fails-before RUN; push → CI green 3 legs → ping Vince the tip SHA → SHA-pinned merge gate. Then the v0.8.0 cut.
