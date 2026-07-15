"""Tests for the dashboard server, API, and packaging."""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.routing import Route
from starlette.testclient import TestClient

from vibe_cognition.cognition import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    CognitionStorage,
    generate_node_id,
)
from vibe_cognition.cognition.documents import (
    blob_rel_path,
    documents_dir,
    sha256_bytes,
    text_sidecar_path,
)
from vibe_cognition.dashboard.server import (
    build_app,
    start_dashboard,
    stop_dashboard,
)


class _FakeEmbeddingStorage:
    """Minimal stand-in for ChromaDBStorage — avoids spinning up a real DB."""

    def __init__(self):
        self._deleted: list[str] = []
        self._search_results: list[dict] = []
        self._count = 0

    def count_documents(self, filter=None) -> int:
        return self._count

    def delete_embedding(self, entity_id: str) -> bool:
        self._deleted.append(entity_id)
        return True

    def delete_by_node_id(self, node_id: str) -> None:
        pass

    def vector_search(self, query_embedding, limit, entity_type=None):
        self.last_limit = limit
        return self._search_results[:limit]


class _FakeEmbeddingGenerator:
    def generate_query_embedding(self, text: str) -> list[float]:
        return [0.0, 0.1, 0.2]


@pytest.fixture
def storage(tmp_path: Path) -> CognitionStorage:
    s = CognitionStorage(tmp_path / ".cognition")
    now = datetime.now(UTC).isoformat()
    n1 = CognitionNode(
        id=generate_node_id("decision", "test"),
        type=CognitionNodeType.DECISION,
        summary="Use Cytoscape for graph viz",
        detail="Mature library, fcose layout works well.",
        context=["dashboard"],
        references=[],
        timestamp=now,
        author="test",
    )
    n2 = CognitionNode(
        id=generate_node_id("discovery", "test"),
        type=CognitionNodeType.DISCOVERY,
        summary="fcose handles 1000 nodes well",
        detail="Layout completes in <2s.",
        context=["dashboard"],
        references=[],
        timestamp=now,
        author="test",
    )
    s.add_node(n1)
    s.add_node(n2)
    s.add_edge(CognitionEdge(
        from_id=n1.id, to_id=n2.id,
        edge_type=CognitionEdgeType.LED_TO,
        timestamp=now, source="test",
    ))
    return s


@pytest.fixture
def lifespan_ctx(storage):
    return {
        "config": None,
        "cognition_storage": storage,
        "cognition_embedding_storage": _FakeEmbeddingStorage(),
        "embedding_generator": None,
        "embedding_ready": threading.Event(),
        "embedding_error": None,
    }


@pytest.fixture
def client(lifespan_ctx):
    app, stack = build_app(lifespan_ctx, token="testtok")
    with TestClient(app, base_url="http://127.0.0.1:7842") as c:
        yield c, lifespan_ctx
    stack.close()


def _hdr(token="testtok"):
    return {"X-Dashboard-Token": token}


class TestPackaging:
    def test_static_files_ship(self):
        """importlib.resources finds the static files in the package."""
        traversable = resources.files("vibe_cognition.dashboard") / "static"
        with resources.as_file(traversable) as path:
            assert (path / "index.html").exists()
            assert (path / "app.js").exists()
            assert (path / "styles.css").exists()

    def test_dashboard_libs_vendored_not_cdn(self):
        """D-4: the cytoscape/fcose stack is self-hosted (no CDN <script>, no SRI gap,
        works offline). The 4 pinned files ship and index.html references no jsdelivr."""
        traversable = resources.files("vibe_cognition.dashboard") / "static"
        with resources.as_file(traversable) as path:
            for f in ("cytoscape.min.js", "layout-base.js", "cose-base.js", "cytoscape-fcose.js"):
                assert (path / "vendor" / f).exists(), f"vendored lib missing: {f}"
            html = (path / "index.html").read_text(encoding="utf-8")
            assert "cdn.jsdelivr.net" not in html, "index.html still loads a CDN script"
            assert "/static/vendor/cytoscape.min.js" in html, "index.html not pointed at vendored libs"

    def test_stop_dashboard_exported_from_package(self):
        """D-5h: stop_dashboard is importable from the package root."""
        from vibe_cognition.dashboard import stop_dashboard  # noqa: F401


class TestAuth:
    def test_no_token_rejected(self, client):
        c, _ = client
        r = c.get("/api/graph")
        assert r.status_code == 403

    def test_wrong_token_rejected(self, client):
        c, _ = client
        r = c.get("/api/graph", headers=_hdr("nope"))
        assert r.status_code == 403

    def test_index_requires_token_query(self, client):
        c, _ = client
        r = c.get("/")
        assert r.status_code == 403
        r = c.get("/?token=testtok")
        assert r.status_code == 200

    def test_ipv6_loopback_host_accepted(self, client):
        """T3: Host: [::1]:<port> is a valid loopback host; must not be 403.
        Note: this is middleware-layer only — uvicorn binds AF_INET 127.0.0.1, so
        a real [::1] client would be refused at TCP before reaching the middleware."""
        c, _ = client
        r = c.get("/api/graph", headers={**_hdr(), "host": "[::1]:7842"})
        assert r.status_code == 200, \
            "IPv6 loopback host [::1]: should be accepted by middleware (was 403)"


class TestRouteTable:
    """WP-DashV2 acceptance: read-only surface -- no new non-GET route joins
    the two pre-existing ones (DELETE /api/node/{id}, the only mutation; POST
    /api/search, a read-only query pre-dating this WP). The two new endpoints
    (/api/workflows, /api/activity) and the extended /api/documents must not
    add POST/PUT/PATCH/DELETE."""

    _PRE_EXISTING_NON_GET = [
        ("/api/node/{node_id}", ("DELETE",)),
        ("/api/search", ("POST",)),
    ]

    def test_no_new_non_get_routes(self, lifespan_ctx):
        app, stack = build_app(lifespan_ctx, token="testtok")
        try:
            non_get_routes = sorted(
                (r.path, tuple(sorted(r.methods - {"HEAD", "OPTIONS"})))
                for r in app.routes
                if isinstance(r, Route) and r.methods and (r.methods - {"GET", "HEAD", "OPTIONS"})
            )
            assert non_get_routes == sorted(self._PRE_EXISTING_NON_GET), \
                f"unexpected non-GET route(s): {non_get_routes}"
        finally:
            stack.close()


class TestGraphAPI:
    def test_graph_shape(self, client):
        c, _ = client
        r = c.get("/api/graph", headers=_hdr())
        assert r.status_code == 200
        body = r.json()
        assert len(body["nodes"]) == 2
        assert len(body["edges"]) == 1
        node = body["nodes"][0]["data"]
        assert "id" in node and "type" in node and "label" in node
        assert "detail" not in node  # detail excluded for size
        edge = body["edges"][0]["data"]
        assert edge["type"] == "led_to"

    def test_node_detail(self, client, storage):
        c, _ = client
        node_id = next(iter(storage.graph.nodes))
        r = c.get(f"/api/node/{node_id}", headers=_hdr())
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == node_id
        assert "successors" in body and "predecessors" in body

    def test_node_404(self, client):
        c, _ = client
        r = c.get("/api/node/does-not-exist", headers=_hdr())
        assert r.status_code == 404

    def test_node_successor_has_edge_type_not_type(self, client, storage):
        """D-5b: get_node neighbor dicts emit 'edge_type' only — 'type' key was a duplicate
        that app.js (line 208) never reads. Fails-before: previously both keys were present."""
        c, _ = client
        node_id = next(iter(storage.graph.nodes))
        r = c.get(f"/api/node/{node_id}", headers=_hdr())
        assert r.status_code == 200
        body = r.json()
        for neighbor in body.get("successors", []) + body.get("predecessors", []):
            assert "edge_type" in neighbor, "neighbor missing edge_type"
            assert "type" not in neighbor, "neighbor still has redundant 'type' key"

    def test_graph_excludes_context_severity(self, client):
        """D-5c: graph node payloads must not include 'context' or 'severity' —
        these are only needed on /api/node/{id}, not on the bulk graph payload.
        Fails-before: previously both keys were present alongside the guarded 'detail'."""
        c, _ = client
        r = c.get("/api/graph", headers=_hdr())
        assert r.status_code == 200
        for node in r.json()["nodes"]:
            data = node["data"]
            assert "detail" not in data
            assert "context" not in data, "graph node payload should not include 'context'"
            assert "severity" not in data, "graph node payload should not include 'severity'"

    def test_delete_node(self, client, storage):
        c, lc = client
        node_id = next(iter(storage.graph.nodes))
        r = c.delete(f"/api/node/{node_id}", headers=_hdr())
        assert r.status_code == 200
        assert r.json()["deleted"] is True
        assert not storage.has_node(node_id)
        assert node_id in lc["cognition_embedding_storage"]._deleted

        # Second delete returns 404
        r = c.delete(f"/api/node/{node_id}", headers=_hdr())
        assert r.status_code == 404

    def test_delete_node_requires_token(self, client, storage):
        """WP-1 item 2c: missing/wrong token must be rejected by middleware
        BEFORE the handler runs — the node must survive both attempts."""
        c, _ = client
        node_id = next(iter(storage.graph.nodes))

        r = c.delete(f"/api/node/{node_id}")
        assert r.status_code == 403
        assert storage.has_node(node_id)

        r = c.delete(f"/api/node/{node_id}", headers=_hdr("nope"))
        assert r.status_code == 403
        assert storage.has_node(node_id)


class TestSearch:
    def test_search_503_when_not_ready(self, client):
        c, _ = client
        r = c.post(
            "/api/search",
            json={"query": "anything"},
            headers=_hdr(),
        )
        assert r.status_code == 503
        assert r.json()["embedding_status"] == "loading"

    def test_search_503_when_error(self, client):
        c, lc = client
        lc["embedding_error"] = "model crash"
        r = c.post("/api/search", json={"query": "x"}, headers=_hdr())
        assert r.status_code == 503

    def test_search_works_when_ready(self, client):
        c, lc = client
        lc["embedding_generator"] = _FakeEmbeddingGenerator()
        lc["embedding_ready"].set()
        # The hit's node must be live in the graph (WP-D2 N1 filter); seed it.
        lc["cognition_storage"].add_node(CognitionNode(
            id="x1", type=CognitionNodeType.DECISION, summary="result 1", detail="d",
            context=[], references=[], timestamp=datetime.now(UTC).isoformat(), author="t",
        ))
        lc["cognition_embedding_storage"]._search_results = [
            {"_id": "x1", "summary": "result 1", "score": 0.9, "entity_type": "decision"},
        ]
        r = c.post(
            "/api/search",
            json={"query": "graph viz"},
            headers=_hdr(),
        )
        assert r.status_code == 200
        results = r.json()["results"]
        assert len(results) == 1
        assert results[0]["_id"] == "x1"

    def test_search_drains_replayed_nodes_before_querying(self, client):
        """WP-3 (8606d59905a5): the dashboard has its own search pipeline,
        bypassing cognition_search's wrapper — it must run the SAME
        re-embed-on-replay drain, or a teammate's replayed node stays
        invisible in dashboard search until an MCP search happens to run
        first. Verifies the wiring (not the full embed mechanics — those are
        covered against a real ChromaDBStorage in test_reembed_on_replay.py)."""
        c, lc = client
        lc["embedding_generator"] = _FakeEmbeddingGenerator()
        lc["embedding_ready"].set()

        with patch("vibe_cognition.dashboard.api._reembed_replayed_nodes") as mock_drain:
            r = c.post("/api/search", json={"query": "anything"}, headers=_hdr())

        assert r.status_code == 200
        mock_drain.assert_called_once_with(
            lc["cognition_storage"], lc["cognition_embedding_storage"], lc["embedding_generator"]
        )

    def test_search_drops_cross_process_ghost(self, client):
        """WP-D2 N1: a hit whose node was deleted on another machine (absent from the
        graph) must NOT be served by the dashboard — D2 makes documents searchable, so
        an un-filtered dashboard would leak verbatim deleted client-document text."""
        c, lc = client
        lc["embedding_generator"] = _FakeEmbeddingGenerator()
        lc["embedding_ready"].set()
        # A document-chunk hit whose node is NOT in the graph (cross-process delete).
        lc["cognition_embedding_storage"]._search_results = [
            {"_id": "ghostdoc#chunk-0", "summary": "DELETED client doc", "score": 0.99,
             "entity_type": "document"},
        ]
        r = c.post("/api/search", json={"query": "secret"}, headers=_hdr())
        assert r.status_code == 200
        assert r.json()["results"] == [], "dashboard served a cross-process document-chunk ghost"

    def test_search_dedupes_document_chunks_to_navigable_node(self, client):
        """WP-D4 D-6: document chunk hits (<node>#chunk-N) collapse to ONE row keyed on
        the navigable NODE id, hydrated with the node's summary, entity_type preserved
        (so renderSearchResults navigates + labels with no JS change). Fails-before: raw
        chunk rows with #chunk- ids that don't navigate."""
        c, lc = client
        lc["embedding_generator"] = _FakeEmbeddingGenerator()
        lc["embedding_ready"].set()
        lc["cognition_storage"].add_node(CognitionNode(
            id="docx0001", type=CognitionNodeType.DOCUMENT, summary="Acme spec v2",
            detail="d", context=[], references=["doc:abc123abc123"], severity=None,
            timestamp=datetime.now(UTC).isoformat(), author="t",
            metadata={"sha256": "abc123abc123def", "mode": "reference"},
        ))
        lc["cognition_embedding_storage"]._search_results = [
            {"_id": "docx0001#chunk-0", "entity_type": "document", "score": 0.95,
             "matched_text": "the matched chunk body"},
            {"_id": "docx0001#chunk-1", "entity_type": "document", "score": 0.90,
             "matched_text": "another chunk"},
        ]
        r = c.post("/api/search", json={"query": "residency"}, headers=_hdr())
        assert r.status_code == 200
        results = r.json()["results"]
        assert len(results) == 1, f"chunks not deduped to one node: {results}"
        row = results[0]
        assert row["_id"] == "docx0001", "row not keyed on the navigable node id"
        assert row["summary"] == "Acme spec v2", "summary not hydrated from the graph node"
        assert row["entity_type"] == "document", "entity_type not preserved (label would blank)"
        assert row["matched_excerpt"] == "the matched chunk body", "best-chunk excerpt not carried"
        assert "#chunk-" not in row["_id"], "non-navigable chunk id leaked"

    def test_search_missing_query(self, client):
        c, _ = client
        r = c.post("/api/search", json={"query": ""}, headers=_hdr())
        assert r.status_code == 400

    def test_search_malformed_body_is_400_not_500(self, client):
        """D-5a: a malformed JSON body returns a clean 400, not a 500 traceback."""
        c, _ = client
        r = c.post("/api/search", content=b"{not json", headers=_hdr())
        assert r.status_code == 400

    def test_search_limit_is_clamped(self, client):
        """D-5b: an absurd limit is clamped (no unbounded fan-out into ChromaDB)."""
        c, lc = client
        lc["embedding_generator"] = _FakeEmbeddingGenerator()
        lc["embedding_ready"].set()
        r = c.post("/api/search", json={"query": "x", "limit": 999999}, headers=_hdr())
        assert r.status_code == 200
        # adaptive_vector_search starts at limit*5; the clamp caps limit at 100 → n starts at 500 ≤ cap.
        assert lc["cognition_embedding_storage"].last_limit <= 500, "limit not clamped"

    def test_search_adaptive_widens_past_single_doc_chunk_flood(self, client):
        """T1: adaptive widen finds node B even when ≥11 docA chunks flood a fixed-10 slice.

        Fails-before (Vince hunt #1): assert B ABSENT in a raw fixed-10 slice, so a
        revert to limit*5=10 goes red. Fails-before assertion is on the raw result set,
        not a synced accessor — a simulated fixed-10 call directly on the fake storage."""
        c, lc = client
        lc["embedding_generator"] = _FakeEmbeddingGenerator()
        lc["embedding_ready"].set()

        lc["cognition_storage"].add_node(CognitionNode(
            id="docA", type=CognitionNodeType.DOCUMENT, summary="Doc A",
            detail="d", context=[], references=[], severity=None,
            timestamp=datetime.now(UTC).isoformat(), author="t",
            metadata={"sha256": "a" * 64, "mode": "reference"},
        ))
        lc["cognition_storage"].add_node(CognitionNode(
            id="nodeB", type=CognitionNodeType.DECISION, summary="Node B",
            detail="d", context=[], references=[], severity=None,
            timestamp=datetime.now(UTC).isoformat(), author="t",
        ))

        # 11 docA chunks then nodeB — B is position 12, beyond a fixed n=10 slice.
        chunks = [
            {"_id": f"docA#chunk-{i}", "entity_type": "document", "score": 0.9 - i * 0.01,
             "matched_text": f"chunk {i}"}
            for i in range(11)
        ]
        node_b_hit = {"_id": "nodeB", "entity_type": "decision", "score": 0.5}
        lc["cognition_embedding_storage"]._search_results = chunks + [node_b_hit]

        # Fails-before: the old fixed n=limit*5=10 slice returns only docA chunks — B is absent.
        es = lc["cognition_embedding_storage"]
        raw_10 = es.vector_search(query_embedding=[0.0, 0.1, 0.2], limit=10)
        assert not any(h["_id"] == "nodeB" for h in raw_10), \
            "B should be absent in the fixed-10 raw slice (old code would go red on revert)"

        # Adaptive helper widens (10→20), 20-slice reaches nodeB → 2 distinct deduped nodes.
        r = c.post("/api/search", json={"query": "anything", "limit": 2}, headers=_hdr())
        assert r.status_code == 200
        results = r.json()["results"]
        assert len(results) == 2, f"adaptive widen should find both docA and nodeB: {results}"
        ids = {row["_id"] for row in results}
        assert "docA" in ids and "nodeB" in ids, f"expected both nodes; got: {ids}"

    def test_embeddings_disabled_state(self, client):
        """T2: --no-embeddings sets embeddings_disabled=True, status reports 'disabled' (not 'loading'),
        and the search 503 body carries embedding_status='disabled'."""
        c, lc = client
        lc["embeddings_disabled"] = True

        r = c.get("/api/stats", headers=_hdr())
        assert r.status_code == 200
        assert r.json()["embedding_status"] == "disabled", \
            "stats should report 'disabled' when embeddings_disabled=True (was 'loading')"
        assert r.json()["embedding_ready"] is False

        r2 = c.post("/api/search", json={"query": "anything"}, headers=_hdr())
        assert r2.status_code == 503
        assert r2.json()["embedding_status"] == "disabled", \
            "search 503 body should carry embedding_status='disabled'"


def _doc_node(node_id, summary, metadata, doc_ref):
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DOCUMENT, summary=summary, detail="d",
        context=[], references=[doc_ref], severity=None,
        timestamp=datetime.now(UTC).isoformat(), author="t", metadata=metadata,
    )


class TestDocuments:
    def test_list_documents(self, client):
        c, lc = client
        s = lc["cognition_storage"]
        s.add_node(_doc_node("docref01", "Ref doc", {
            "mode": "reference", "size": 10, "mime": "text/plain", "filename": "a.txt",
            "sha256": "a" * 64, "indexed_text_chars": 5}, "doc:aaaaaaaaaaaa"))
        s.add_node(_doc_node("doccopy1", "Copy doc", {
            "mode": "copy", "blob_path": "bb/" + "b" * 64 + ".pdf", "size": 20,
            "mime": "application/pdf", "filename": "b.pdf", "sha256": "b" * 64,
            "indexed_text_chars": 8}, "doc:bbbbbbbbbbbb"))

        r = c.get("/api/documents", headers=_hdr())
        assert r.status_code == 200
        docs = {d["node_id"]: d for d in r.json()["documents"]}
        assert set(docs) == {"docref01", "doccopy1"}, "did not list exactly the document nodes"
        assert docs["docref01"]["has_blob"] is False and docs["docref01"]["mode"] == "reference"
        assert docs["doccopy1"]["has_blob"] is True and docs["doccopy1"]["mode"] == "copy"
        assert docs["docref01"]["doc_ref"] == "doc:aaaaaaaaaaaa"
        assert docs["docref01"]["summary"] == "Ref doc" and docs["docref01"]["filename"] == "a.txt"

    def test_list_documents_empty_when_none(self, client):
        c, _ = client  # fixture graph has only a decision + a discovery, no documents
        r = c.get("/api/documents", headers=_hdr())
        assert r.status_code == 200
        assert r.json()["documents"] == []

    def test_list_documents_requires_token(self, client):
        c, _ = client
        assert c.get("/api/documents").status_code == 403

    def test_download_copy_mode_blob(self, client):
        c, lc = client
        s = lc["cognition_storage"]
        sha = "c" * 64
        rel = blob_rel_path(sha, ".pdf")
        blob = documents_dir(s.cognition_dir) / rel
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(b"PDF-BYTES-HERE")
        s.add_node(_doc_node("dlcopy01", "Copy DL", {
            "mode": "copy", "blob_path": rel, "sha256": sha,
            "mime": "application/pdf", "filename": "c.pdf", "size": 14}, "doc:cccccccccccc"))

        r = c.get("/api/document/dlcopy01/download?token=testtok")
        assert r.status_code == 200
        assert r.content == b"PDF-BYTES-HERE", "blob bytes not served"

    def test_download_reference_mode_serves_sidecar(self, client):
        c, lc = client
        s = lc["cognition_storage"]
        sha = "d" * 64
        sidecar = text_sidecar_path(s.cognition_dir, sha)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("extracted text body", encoding="utf-8")
        s.add_node(_doc_node("dlref01", "Ref DL", {
            "mode": "reference", "sha256": sha}, "doc:dddddddddddd"))

        r = c.get("/api/document/dlref01/download?token=testtok")
        assert r.status_code == 200
        assert r.text == "extracted text body", "reference mode should serve the sidecar text"

    def test_download_rejects_blob_path_escaping_documents_dir(self, client):
        """Path-safety: a tampered blob_path that resolves OUTSIDE documents_dir must
        be rejected (404), not served — a REAL escaping path to a real file, not a
        string check. Fails-before without the is_relative_to(documents_dir) guard."""
        c, lc = client
        s = lc["cognition_storage"]
        secret = s.cognition_dir.parent / "secret.txt"  # outside .cognition/documents
        secret.write_text("TOP SECRET", encoding="utf-8")
        s.add_node(_doc_node("dlevil01", "Evil", {
            "mode": "copy", "blob_path": "../../secret.txt", "sha256": "e" * 64,
            "filename": "x"}, "doc:eeeeeeeeeeee"))

        r = c.get("/api/document/dlevil01/download?token=testtok")
        assert r.status_code == 404, "path-escaping blob_path was served"
        assert "TOP SECRET" not in r.text

    def test_download_non_document_is_404(self, client):
        c, lc = client
        # n1/n2 are decision/discovery, not documents
        any_node = next(iter(lc["cognition_storage"].get_all_nodes()))["id"]
        r = c.get(f"/api/document/{any_node}/download?token=testtok")
        assert r.status_code == 404

    def test_download_requires_token(self, client):
        c, _ = client
        assert c.get("/api/document/whatever/download").status_code == 403

    def test_download_clamps_agent_controlled_mime(self, client):
        """The mime is agent-controlled (cognition_store_document mime=) and flows into
        Content-Type — clamp it like the filename. A CRLF-injection mime falls back to
        application/octet-stream; a valid mime is preserved. (ledger 17: don't rely on
        the HTTP parser to reject our own header injection.)"""
        c, lc = client
        s = lc["cognition_storage"]
        sha = "f" * 64
        rel = blob_rel_path(sha, ".bin")
        blob = documents_dir(s.cognition_dir) / rel
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(b"data")
        s.add_node(_doc_node("dlmime01", "Mime", {
            "mode": "copy", "blob_path": rel, "sha256": sha,
            "mime": "text/html\r\nX-Injected: evil", "filename": "m.bin"}, "doc:ffffffffffff"))

        r = c.get("/api/document/dlmime01/download?token=testtok")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/octet-stream"), \
            "injection-bearing mime not clamped"
        assert "x-injected" not in {k.lower() for k in r.headers}, "header injected via mime"

    def test_download_clamps_preserves_valid_mime(self, client):
        c, lc = client
        s = lc["cognition_storage"]
        sha = "9" * 64
        rel = blob_rel_path(sha, ".pdf")
        blob = documents_dir(s.cognition_dir) / rel
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(b"%PDF")
        s.add_node(_doc_node("dlmime02", "Mime2", {
            "mode": "copy", "blob_path": rel, "sha256": sha,
            "mime": "application/pdf", "filename": "m.pdf"}, "doc:999999999999"))

        r = c.get("/api/document/dlmime02/download?token=testtok")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/pdf"), "valid mime not preserved"

    def test_download_rejects_absolute_blob_path(self, client):
        """Traversal vector: an ABSOLUTE blob_path (pathlib join drops the base) must
        still be rejected by the resolved-under-documents_dir check."""
        c, lc = client
        s = lc["cognition_storage"]
        secret = s.cognition_dir.parent / "abs_secret.txt"
        secret.write_text("ABS SECRET", encoding="utf-8")
        s.add_node(_doc_node("dlabs01", "Abs", {
            "mode": "copy", "blob_path": str(secret), "sha256": "1" * 64}, "doc:111111111111"))

        r = c.get("/api/document/dlabs01/download?token=testtok")
        assert r.status_code == 404, "absolute blob_path escaped documents_dir"
        assert "ABS SECRET" not in r.text

    def test_download_rejects_symlink_escaping_documents_dir(self, client):
        """Traversal vector: a SYMLINK inside documents_dir pointing OUTSIDE must be
        defeated — resolve() follows the link and the comparison is on the resolved
        path. (Skipped where symlinks aren't permitted, e.g. unprivileged Windows.)"""
        c, lc = client
        s = lc["cognition_storage"]
        secret = s.cognition_dir.parent / "link_secret.txt"
        secret.write_text("LINK SECRET", encoding="utf-8")
        docs = documents_dir(s.cognition_dir)
        docs.mkdir(parents=True, exist_ok=True)
        link = docs / "evil_link.bin"
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            import pytest
            pytest.skip("symlinks not permitted on this platform")
        s.add_node(_doc_node("dllink01", "Link", {
            "mode": "copy", "blob_path": "evil_link.bin", "sha256": "2" * 64}, "doc:222222222222"))

        r = c.get("/api/document/dllink01/download?token=testtok")
        assert r.status_code == 404, "symlink escaping documents_dir was served"
        assert "LINK SECRET" not in r.text


class TestWorkflowsAPI:
    """WP-DashV2: GET /api/workflows — HEAD-only cards with inline version chains."""

    def _wf(self, node_id, summary, timestamp, author="Colton"):
        return CognitionNode(
            id=node_id, type=CognitionNodeType.WORKFLOW, summary=summary, detail="d",
            context=[], references=[], timestamp=timestamp, author=author, metadata={},
        )

    def test_no_token_rejected(self, client):
        c, _ = client
        assert c.get("/api/workflows").status_code == 403

    def test_zero_workflow_graph(self, client):
        c, _ = client  # base fixture graph has only a decision + a discovery
        r = c.get("/api/workflows", headers=_hdr())
        assert r.status_code == 200
        assert r.json() == {"workflows": [], "count": 0}

    def test_single_version_chain_of_one(self, client):
        c, lc = client
        s = lc["cognition_storage"]
        s.add_node(self._wf("wf1", "Deploy process", datetime.now(UTC).isoformat()))

        body = c.get("/api/workflows", headers=_hdr()).json()
        assert body["count"] == 1
        wf = body["workflows"][0]
        assert wf["id"] == "wf1"
        assert wf["summary"] == "Deploy process"
        assert len(wf["chain"]) == 1
        assert wf["chain"][0]["id"] == "wf1"

    def test_superseded_workflow_absent_as_card_present_in_chain_newest_first(self, client):
        """Fails-before: the HEAD filter must exclude the non-HEAD version from
        the top-level workflows list; the chain must be newest -> oldest
        including the HEAD itself."""
        c, lc = client
        s = lc["cognition_storage"]
        now = datetime.now(UTC)
        s.add_node(self._wf("wfv1", "Deploy v1", (now - timedelta(days=2)).isoformat()))
        s.add_node(self._wf("wfv2", "Deploy v2", now.isoformat()))
        # SUPERSEDES points newer -> older ("B replaces A": edge is B -> A;
        # cognition_tools.py:3741/3824, prime.py:331). wfv2 is newer, so it
        # supersedes wfv1.
        s.add_edge(CognitionEdge(
            from_id="wfv2", to_id="wfv1", edge_type=CognitionEdgeType.SUPERSEDES,
            timestamp=now.isoformat(), source="test",
        ))

        body = c.get("/api/workflows", headers=_hdr()).json()
        ids = {w["id"] for w in body["workflows"]}
        assert ids == {"wfv2"}, "superseded (non-HEAD) workflow must not appear as a top-level card"
        head = next(w for w in body["workflows"] if w["id"] == "wfv2")
        assert [c_["id"] for c_ in head["chain"]] == ["wfv2", "wfv1"]

    def test_branch_warned_chain_does_not_crash(self, client):
        """A HEAD with 2 OUTGOING SUPERSEDES edges (it claims to replace two
        different older nodes) must be tolerated by get_superseded_chain's own
        walk -- mirrors its ``len(successors) > 1`` warning path (first match,
        warns, never raises/500s). get_workflow_head's predecessor-side
        tolerance is a different code path this endpoint never calls (it only
        checks get_predecessors(..., SUPERSEDES) for the HEAD filter), so the
        branching that matters here is on the successor (chain-walk) side.
        wfhead has zero incoming SUPERSEDES, so it stays the sole HEAD."""
        c, lc = client
        s = lc["cognition_storage"]
        now = datetime.now(UTC)
        s.add_node(self._wf("wfhead", "Head", now.isoformat()))
        s.add_node(self._wf("wfolda", "Old A", (now - timedelta(days=1)).isoformat()))
        s.add_node(self._wf("wfoldb", "Old B", (now - timedelta(days=1)).isoformat()))
        s.add_edge(CognitionEdge(
            from_id="wfhead", to_id="wfolda", edge_type=CognitionEdgeType.SUPERSEDES,
            timestamp=now.isoformat(), source="test",
        ))
        s.add_edge(CognitionEdge(
            from_id="wfhead", to_id="wfoldb", edge_type=CognitionEdgeType.SUPERSEDES,
            timestamp=now.isoformat(), source="test",
        ))

        r = c.get("/api/workflows", headers=_hdr())
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["workflows"][0]["id"] == "wfhead"


class TestDocumentsFreshnessAndCitations:
    """WP-DashV2: GET /api/documents gains freshness + cited_by per row."""

    def test_no_token_rejected(self, client):
        c, _ = client
        assert c.get("/api/documents").status_code == 403

    def test_reference_mode_with_path_unchanged(self, client, tmp_path):
        c, lc = client
        s = lc["cognition_storage"]
        src = tmp_path / "source.txt"
        src.write_bytes(b"original content")
        sha = sha256_bytes(b"original content")
        s.add_node(_doc_node("freshok1", "Ref unchanged", {
            "mode": "reference", "path": str(src), "sha256": sha,
        }, "doc:aaaaaaaaaaaa"))

        docs = {d["node_id"]: d for d in c.get("/api/documents", headers=_hdr()).json()["documents"]}
        assert docs["freshok1"]["freshness"] == "unchanged"

    def test_reference_mode_with_path_modified_fails_before(self, client, tmp_path):
        """Fails-before: must fail if the row's freshness were computed as
        anything other than 'modified' once the source content diverges from
        the stored sha256."""
        c, lc = client
        s = lc["cognition_storage"]
        src = tmp_path / "source.txt"
        src.write_bytes(b"original content")
        sha = sha256_bytes(b"original content")
        s.add_node(_doc_node("freshmod1", "Ref modified", {
            "mode": "reference", "path": str(src), "sha256": sha,
        }, "doc:bbbbbbbbbbbb"))
        src.write_bytes(b"edited content, same-ish size")

        docs = {d["node_id"]: d for d in c.get("/api/documents", headers=_hdr()).json()["documents"]}
        assert docs["freshmod1"]["freshness"] == "modified"

    def test_reference_mode_with_path_missing(self, client, tmp_path):
        c, lc = client
        s = lc["cognition_storage"]
        src = tmp_path / "source.txt"
        src.write_bytes(b"gone soon")
        sha = sha256_bytes(b"gone soon")
        s.add_node(_doc_node("freshmiss1", "Ref missing", {
            "mode": "reference", "path": str(src), "sha256": sha,
        }, "doc:cccccccccccc"))
        src.unlink()

        docs = {d["node_id"]: d for d in c.get("/api/documents", headers=_hdr()).json()["documents"]}
        assert docs["freshmiss1"]["freshness"] == "missing"

    def test_copy_mode_freshness_null_even_with_path_key_retained(self, client, tmp_path):
        """Peer-review H1: a copy-mode doc stored via file_path retains a path
        key (cognition_tools._store_document sets it independent of
        store_copy) -- freshness must still be null, gated on mode alone."""
        c, lc = client
        s = lc["cognition_storage"]
        src = tmp_path / "source.pdf"
        src.write_bytes(b"%PDF-ish")
        sha = sha256_bytes(b"%PDF-ish")
        s.add_node(_doc_node("freshcopy1", "Copy with path", {
            "mode": "copy", "path": str(src), "sha256": sha, "blob_path": "xx/" + sha + ".pdf",
        }, "doc:dddddddddddd"))

        docs = {d["node_id"]: d for d in c.get("/api/documents", headers=_hdr()).json()["documents"]}
        assert docs["freshcopy1"]["freshness"] is None

    def test_reference_mode_no_path_key_freshness_null(self, client):
        """Peer-review H2: reference mode stored via content_text (no path key
        at all — a common state, not just legacy) must render null, not the
        misleading 'unchanged' that freshness_by_rehash's own no-path default
        would otherwise imply was a real check."""
        c, lc = client
        s = lc["cognition_storage"]
        s.add_node(_doc_node("freshnopath1", "Ref no path", {
            "mode": "reference", "sha256": "e" * 64,
        }, "doc:eeeeeeeeeeee"))

        docs = {d["node_id"]: d for d in c.get("/api/documents", headers=_hdr()).json()["documents"]}
        assert docs["freshnopath1"]["freshness"] is None

    def test_cited_by_correct_direction_fails_before(self, client):
        """Fails-before: cited_by must count entity nodes citing the document
        via a PART_OF edge INTO it (entity_id -> document_id, the direction
        _deterministic_edge_for_pair mints) -- a backwards edge walk would
        report 0 here instead of 1."""
        c, lc = client
        s = lc["cognition_storage"]
        doc = _doc_node("cited01", "Cited doc", {"mode": "reference", "sha256": "f" * 64}, "doc:ffffffffffff")
        s.add_node(doc)
        citer = CognitionNode(
            id="citer01", type=CognitionNodeType.DECISION, summary="Cites the doc",
            detail="see doc:ffffffffffff", context=[], references=["doc:ffffffffffff"],
            timestamp=datetime.now(UTC).isoformat(), author="Colton",
        )
        # storage.add_node only indexes references -- it never mints edges itself;
        # the MCP tool layer calls create_deterministic_edges(node_id) explicitly
        # after add_node (cognition_tools.py:306/1168), so tests must do the same.
        s.add_node(citer)
        s.create_deterministic_edges(citer.id)

        docs = {d["node_id"]: d for d in c.get("/api/documents", headers=_hdr()).json()["documents"]}
        assert docs["cited01"]["cited_by"] == 1

    def test_zero_citations(self, client):
        c, lc = client
        s = lc["cognition_storage"]
        s.add_node(_doc_node("uncited01", "Uncited doc", {
            "mode": "reference", "sha256": "0" * 64,
        }, "doc:000000000000"))

        docs = {d["node_id"]: d for d in c.get("/api/documents", headers=_hdr()).json()["documents"]}
        assert docs["uncited01"]["cited_by"] == 0


class TestActivityAPI:
    """WP-DashV2: GET /api/activity — chronological feed across 8 entity types."""

    def _n(self, node_id, ntype, summary, timestamp, **meta):
        return CognitionNode(
            id=node_id, type=ntype, summary=summary, detail="d", context=[], references=[],
            timestamp=timestamp, author="Colton", metadata=meta,
        )

    def test_no_token_rejected(self, client):
        c, _ = client
        assert c.get("/api/activity").status_code == 403

    def test_type_set_exact_task_document_workflow_person_excluded(self, client):
        c, lc = client
        s = lc["cognition_storage"]
        now = datetime.now(UTC).isoformat()
        for ntype, _included in (
            (CognitionNodeType.EPISODE, True), (CognitionNodeType.DECISION, True),
            (CognitionNodeType.FAIL, True), (CognitionNodeType.DISCOVERY, True),
            (CognitionNodeType.INCIDENT, True), (CognitionNodeType.CONSTRAINT, True),
            (CognitionNodeType.PATTERN, True), (CognitionNodeType.ASSUMPTION, True),
            (CognitionNodeType.TASK, False), (CognitionNodeType.DOCUMENT, False),
            (CognitionNodeType.WORKFLOW, False), (CognitionNodeType.PERSON, False),
        ):
            node_id = f"act-{ntype.value}"
            if ntype == CognitionNodeType.TASK:
                s.add_node(CognitionNode(
                    id=node_id, type=ntype, summary=f"a {ntype.value}", detail="d",
                    context=[], references=[], timestamp=now, author="Colton",
                    metadata={"status": "open"},
                ))
            else:
                s.add_node(self._n(node_id, ntype, f"a {ntype.value}", now))

        body = c.get("/api/activity", headers=_hdr()).json()
        types_seen = {r["type"] for r in body["activity"]}
        assert types_seen == {
            "episode", "decision", "fail", "discovery", "incident",
            "constraint", "pattern", "assumption",
        }
        assert "task" not in types_seen and "document" not in types_seen and "workflow" not in types_seen
        assert "person" not in types_seen

    def test_newest_first(self, client):
        c, lc = client
        s = lc["cognition_storage"]
        now = datetime.now(UTC)
        s.add_node(self._n("actold", CognitionNodeType.DECISION, "Older", (now - timedelta(days=2)).isoformat()))
        s.add_node(self._n("actnew", CognitionNodeType.DECISION, "Newer", now.isoformat()))

        rows = c.get("/api/activity", headers=_hdr()).json()["activity"]
        ids = [r["id"] for r in rows]
        assert ids.index("actnew") < ids.index("actold")

    def test_rows_carry_recorded_by_and_author_separately(self, client):
        c, lc = client
        s = lc["cognition_storage"]
        s.add_node(self._n(
            "actprov", CognitionNodeType.EPISODE, "Provenance row",
            datetime.now(UTC).isoformat(),
            recorded_by={"name": "Vince", "email": "v@x.com"}, from_agent=True,
        ))

        row = next(r for r in c.get("/api/activity", headers=_hdr()).json()["activity"] if r["id"] == "actprov")
        assert row["recorded_by"] == {"name": "Vince", "email": "v@x.com"}
        assert row["author"] == "Colton"
        assert row["from_agent"] is True

    def test_limit_and_cap_enforced(self, client):
        c, lc = client
        s = lc["cognition_storage"]
        now = datetime.now(UTC)
        for i in range(10):
            s.add_node(self._n(f"actlim{i}", CognitionNodeType.DECISION, f"row {i}",
                                (now - timedelta(minutes=i)).isoformat()))

        body = c.get("/api/activity?limit=3", headers=_hdr()).json()
        assert body["count"] == 3
        assert len(body["activity"]) == 3

        over = c.get("/api/activity?limit=99999", headers=_hdr()).json()
        assert len(over["activity"]) <= 500

    def test_recent_excluded_type_flood_does_not_displace_included_rows_fails_before(self, client):
        """Peer-review H3: the N-per-type merge means a flood of recent TASK
        nodes (excluded from the feed) must never crowd out genuinely-recent
        DECISION rows within a small limit window -- fails-before against a
        naive 'fetch all types, filter afterward' implementation, which would
        have already discarded the decision rows before the type filter runs."""
        c, lc = client
        s = lc["cognition_storage"]
        now = datetime.now(UTC)
        s.add_node(self._n("floodtarget", CognitionNodeType.DECISION, "Must survive",
                            (now - timedelta(days=1)).isoformat()))
        for i in range(20):
            s.add_node(CognitionNode(
                id=f"floodtask{i}", type=CognitionNodeType.TASK, summary=f"flood {i}",
                detail="d", context=[], references=[], timestamp=now.isoformat(),
                author="Colton", metadata={"status": "open"},
            ))

        body = c.get("/api/activity?limit=5", headers=_hdr()).json()
        ids = {r["id"] for r in body["activity"]}
        assert "floodtarget" in ids


class TestStats:
    def test_stats_shape(self, client):
        c, _ = client
        r = c.get("/api/stats", headers=_hdr())
        assert r.status_code == 200
        body = r.json()
        assert "graph" in body
        assert body["graph"]["nodes"] == 2
        assert body["graph"]["edges"] == 1
        assert body["embedding_ready"] is False
        assert body["embedding_status"] == "loading"


class TestLifecycle:
    def test_start_dashboard_idempotent(self, lifespan_ctx):
        first = start_dashboard(lifespan_ctx, port=0, open_browser=False)
        try:
            assert first["status"] == "running"
            second = start_dashboard(lifespan_ctx, port=0, open_browser=False)
            assert second["status"] == "already_running"
            assert second["url"] == first["url"]
        finally:
            stop_dashboard(lifespan_ctx, join_timeout=3.0)

    def test_start_dashboard_never_logs_the_token(self, lifespan_ctx, caplog):
        """WP-13 (ebe050e78923): the token-gated URL (read/download/DELETE
        access) must never appear in a log line -- only host:port, so a log
        line is not a second leak surface for the token.

        Fails-before: logger.info(f"Dashboard launched at {url}") included the
        full ?token=... query string.
        """
        import logging

        with caplog.at_level(logging.INFO):
            result = start_dashboard(lifespan_ctx, port=0, open_browser=False)
        try:
            token = result["url"].split("token=")[1]
            assert token, "test setup: no token found in the returned URL"
            log_text = "\n".join(r.message for r in caplog.records)
            assert token not in log_text, "token appeared in a log line"
            assert "redacted" in log_text.lower()
            assert "127.0.0.1" in log_text
        finally:
            stop_dashboard(lifespan_ctx, join_timeout=3.0)

    def test_stop_dashboard_joins_thread(self, lifespan_ctx):
        start_dashboard(lifespan_ctx, port=0, open_browser=False)
        thread = lifespan_ctx["dashboard"]["thread"]
        # Server is starting; allow a brief window
        time.sleep(0.3)
        stop_dashboard(lifespan_ctx, join_timeout=3.0)
        assert not thread.is_alive()
        assert "dashboard" not in lifespan_ctx

    def test_start_dashboard_reports_failure_when_server_never_starts(self, lifespan_ctx, monkeypatch):
        """D-1: if the server never comes up (e.g. bind fails → daemon thread dies),
        start_dashboard returns an error status and does NOT cache a dead URL."""
        import uvicorn

        # Simulate a server that exits immediately without ever setting `started`.
        monkeypatch.setattr(uvicorn.Server, "run", lambda self: None)
        result = start_dashboard(lifespan_ctx, port=0, open_browser=False)
        assert result["status"] == "failed", f"expected failed, got {result}"
        assert result["url"] is None
        assert "dashboard" not in lifespan_ctx, "a dead dashboard was cached"

    def test_stop_dashboard_does_not_close_stack_on_join_timeout(self, lifespan_ctx):
        """D-5h(a): if the thread is still alive after join, stack.close() must NOT be called —
        closing the static-files context under a live server thread is unsafe."""
        closed = []

        class _FakeStack:
            def close(self): closed.append(True)

        class _AliveThread:
            def is_alive(self): return True
            def join(self, timeout=None): pass

        class _FakeServer:
            should_exit = False

        lifespan_ctx["dashboard"] = {
            "thread": _AliveThread(), "server": _FakeServer(), "stack": _FakeStack(),
            "url": "http://127.0.0.1:7842/", "token": "tok", "port": 7842,
        }
        stop_dashboard(lifespan_ctx, join_timeout=0.01)
        assert not closed, "stack.close() called despite thread still alive after join"

    def test_stop_dashboard_closes_stack_on_clean_join(self, lifespan_ctx):
        """D-5h(b): if the thread stops cleanly, stack.close() MUST be called."""
        closed = []

        class _FakeStack:
            def close(self): closed.append(True)

        class _DeadThread:
            def is_alive(self): return False
            def join(self, timeout=None): pass

        class _FakeServer:
            should_exit = False

        lifespan_ctx["dashboard"] = {
            "thread": _DeadThread(), "server": _FakeServer(), "stack": _FakeStack(),
            "url": "http://127.0.0.1:7842/", "token": "tok", "port": 7842,
        }
        stop_dashboard(lifespan_ctx, join_timeout=0.01)
        assert closed, "stack.close() not called after clean join"


# ── WP-TC11: /api/tasks + /api/overview (scope-dashboard-v1, doc:4c0b9d426f4c) ──

def _who(name="Colton", email="c@example.com"):
    return {"name": name, "email": email}


@pytest.fixture
def pm_storage(tmp_path: Path) -> CognitionStorage:
    """A graph shaped to exercise every /api/tasks + /api/overview branch:
    open/in_progress/blocked/done/cancelled tasks (one nested child), a stale
    claim, a done-this-week task, a legacy author-only task (no created_by), an
    active vs. a low-severity constraint, a superseded (non-HEAD) workflow, a
    recent vs. a >14d-old high-severity incident, one episode, one document."""
    s = CognitionStorage(tmp_path / ".cognition")
    now = datetime.now(UTC)

    def ts(dt=None):
        return (dt or now).isoformat()

    open_task = CognitionNode(
        id=generate_node_id("task", "open"), type=CognitionNodeType.TASK,
        summary="Open task", detail="d", context=[], references=[], severity="high",
        timestamp=ts(), author="Colton",
        metadata={
            "status": "open", "created_by": _who(), "owner": None, "parent_id": None,
            "transitions": [{"status": "open", "at": ts(), "by": _who()}],
        },
    )
    s.add_node(open_task)

    child_task = CognitionNode(
        id=generate_node_id("task", "child"), type=CognitionNodeType.TASK,
        summary="Child task", detail="d", context=[], references=[], severity="normal",
        timestamp=ts(), author="Colton",
        metadata={
            "status": "open", "created_by": _who(), "owner": None, "parent_id": open_task.id,
            "transitions": [{"status": "open", "at": ts(), "by": _who()}],
        },
    )
    s.add_node(child_task)

    stale_claimed_at = ts(now - timedelta(days=6))
    stale_task = CognitionNode(
        id=generate_node_id("task", "stale"), type=CognitionNodeType.TASK,
        summary="Stale claim", detail="d", context=[], references=[], severity="normal",
        timestamp=ts(now - timedelta(days=7)), author="Colton",
        metadata={
            "status": "in_progress", "created_by": _who(), "claimed_by": _who("Vorpid"),
            "owner": None, "parent_id": None,
            "transitions": [
                {"status": "open", "at": ts(now - timedelta(days=7)), "by": _who()},
                {"status": "in_progress", "at": stale_claimed_at, "by": _who("Vorpid")},
            ],
        },
    )
    s.add_node(stale_task)

    fresh_claimed_at = ts(now - timedelta(days=1))
    fresh_task = CognitionNode(
        id=generate_node_id("task", "fresh"), type=CognitionNodeType.TASK,
        summary="Fresh claim", detail="d", context=[], references=[], severity="normal",
        timestamp=ts(now - timedelta(days=1)), author="Colton",
        metadata={
            "status": "in_progress", "created_by": _who(), "claimed_by": _who(),
            "owner": None, "parent_id": None,
            "transitions": [
                {"status": "open", "at": ts(now - timedelta(days=1)), "by": _who()},
                {"status": "in_progress", "at": fresh_claimed_at, "by": _who()},
            ],
        },
    )
    s.add_node(fresh_task)

    done_at = ts(now - timedelta(days=2))
    done_task = CognitionNode(
        id=generate_node_id("task", "done"), type=CognitionNodeType.TASK,
        summary="Done this week", detail="d", context=[], references=[], severity="normal",
        timestamp=ts(now - timedelta(days=10)), author="Colton",
        metadata={
            "status": "done", "created_by": _who(), "owner": None, "parent_id": None,
            "transitions": [
                {"status": "open", "at": ts(now - timedelta(days=10)), "by": _who()},
                {"status": "in_progress", "at": ts(now - timedelta(days=5)), "by": _who()},
                {"status": "done", "at": done_at, "by": _who()},
            ],
        },
    )
    s.add_node(done_task)

    old_done_task = CognitionNode(
        id=generate_node_id("task", "old-done"), type=CognitionNodeType.TASK,
        summary="Done last month", detail="d", context=[], references=[], severity="normal",
        timestamp=ts(now - timedelta(days=40)), author="Colton",
        metadata={
            "status": "done", "created_by": _who(), "owner": None, "parent_id": None,
            "transitions": [
                {"status": "open", "at": ts(now - timedelta(days=40)), "by": _who()},
                {"status": "done", "at": ts(now - timedelta(days=35)), "by": _who()},
            ],
        },
    )
    s.add_node(old_done_task)

    blocked_task = CognitionNode(
        id=generate_node_id("task", "blocked"), type=CognitionNodeType.TASK,
        summary="Blocked task", detail="d", context=[], references=[], severity="low",
        timestamp=ts(), author="Colton",
        metadata={
            "status": "blocked", "created_by": _who(), "owner": None, "parent_id": None,
            "transitions": [
                {"status": "open", "at": ts(), "by": _who()},
                {"status": "blocked", "at": ts(), "by": _who()},
            ],
        },
    )
    s.add_node(blocked_task)

    cancelled_task = CognitionNode(
        id=generate_node_id("task", "cancelled"), type=CognitionNodeType.TASK,
        summary="Cancelled task", detail="d", context=[], references=[], severity="normal",
        timestamp=ts(), author="Colton",
        metadata={
            "status": "cancelled", "created_by": _who(), "owner": None, "parent_id": None,
            "transitions": [
                {"status": "open", "at": ts(), "by": _who()},
                {"status": "cancelled", "at": ts(), "by": _who()},
            ],
        },
    )
    s.add_node(cancelled_task)

    # Legacy: predates WP-P13n -- no created_by/recorded_by, only free-text author.
    # This is the trust-class fallback fixture the drawer's "unverified" chip depends on.
    legacy_task = CognitionNode(
        id=generate_node_id("task", "legacy"), type=CognitionNodeType.TASK,
        summary="Legacy author-only task", detail="d", context=[], references=[],
        severity="normal", timestamp=ts(), author="OldAuthor",
        metadata={
            "status": "open", "owner": None, "parent_id": None,
            "transitions": [{"status": "open", "at": ts(), "by": "OldAuthor"}],
        },
    )
    s.add_node(legacy_task)

    active_constraint = CognitionNode(
        id=generate_node_id("constraint", "active"), type=CognitionNodeType.CONSTRAINT,
        summary="Active constraint", detail="d", context=[], references=[], severity="high",
        timestamp=ts(), author="Colton", metadata={"recorded_by": _who()},
    )
    s.add_node(active_constraint)

    low_constraint = CognitionNode(
        id=generate_node_id("constraint", "low"), type=CognitionNodeType.CONSTRAINT,
        summary="Low-severity constraint", detail="d", context=[], references=[], severity="low",
        timestamp=ts(), author="Colton", metadata={},
    )
    s.add_node(low_constraint)

    workflow_v1 = CognitionNode(
        id=generate_node_id("workflow", "v1"), type=CognitionNodeType.WORKFLOW,
        summary="Workflow v1", detail="d", context=[], references=[],
        timestamp=ts(now - timedelta(days=5)), author="Colton", metadata={},
    )
    s.add_node(workflow_v1)
    workflow_v2 = CognitionNode(
        id=generate_node_id("workflow", "v2"), type=CognitionNodeType.WORKFLOW,
        summary="Workflow v2", detail="d", context=[], references=[],
        timestamp=ts(), author="Colton", metadata={},
    )
    s.add_node(workflow_v2)
    # SUPERSEDES points newer -> older ("B replaces A": edge is B -> A;
    # cognition_tools.py:3741/3824, prime.py:331). workflow_v2 is newer, so it
    # supersedes workflow_v1 (previously backwards -- test_workflows_counts_
    # head_only only asserted a bare count, so the direction bug was masked).
    s.add_edge(CognitionEdge(
        from_id=workflow_v2.id, to_id=workflow_v1.id,
        edge_type=CognitionEdgeType.SUPERSEDES, timestamp=ts(), source="test",
    ))

    episode = CognitionNode(
        id=generate_node_id("episode", "e1"), type=CognitionNodeType.EPISODE,
        summary="An episode", detail="d", context=[], references=[],
        timestamp=ts(), author="Colton", metadata={"recorded_by": _who(), "from_agent": True},
    )
    s.add_node(episode)

    recent_incident = CognitionNode(
        id=generate_node_id("incident", "recent"), type=CognitionNodeType.INCIDENT,
        summary="Recent high-severity incident", detail="d", context=[], references=[],
        severity="high", timestamp=ts(now - timedelta(days=3)), author="Colton", metadata={},
    )
    s.add_node(recent_incident)

    old_incident = CognitionNode(
        id=generate_node_id("incident", "old"), type=CognitionNodeType.INCIDENT,
        summary="Old high-severity incident", detail="d", context=[], references=[],
        severity="high", timestamp=ts(now - timedelta(days=20)), author="Colton", metadata={},
    )
    s.add_node(old_incident)

    document = CognitionNode(
        id=generate_node_id("document", "d1"), type=CognitionNodeType.DOCUMENT,
        summary="A document", detail="d", context=[], references=[],
        timestamp=ts(), author="Colton", metadata={"mode": "reference"},
    )
    s.add_node(document)

    return s


@pytest.fixture
def pm_lifespan_ctx(pm_storage):
    return {
        "config": None,
        "cognition_storage": pm_storage,
        "cognition_embedding_storage": _FakeEmbeddingStorage(),
        "embedding_generator": None,
        "embedding_ready": threading.Event(),
        "embedding_error": None,
    }


@pytest.fixture
def pm_client(pm_lifespan_ctx):
    app, stack = build_app(pm_lifespan_ctx, token="testtok")
    with TestClient(app, base_url="http://127.0.0.1:7842") as c:
        yield c, pm_lifespan_ctx
    stack.close()


class TestTasksAPI:
    def test_no_token_rejected(self, pm_client):
        c, _ = pm_client
        r = c.get("/api/tasks")
        assert r.status_code == 403

    def test_shape_and_count(self, pm_client):
        c, _ = pm_client
        r = c.get("/api/tasks", headers=_hdr())
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 9
        assert len(body["tasks"]) == 9
        row = next(t for t in body["tasks"] if t["summary"] == "Open task")
        for key in (
            "id", "summary", "status", "priority", "owner", "parent_id", "depth",
            "created_by", "claimed_by", "timestamp", "claimed_at", "last_transition_at",
            "transitions_count",
        ):
            assert key in row, f"missing key: {key}"

    def test_depth_from_parent_chain(self, pm_client):
        c, _ = pm_client
        body = c.get("/api/tasks", headers=_hdr()).json()
        rows = {t["summary"]: t for t in body["tasks"]}
        assert rows["Open task"]["depth"] == 0
        assert rows["Child task"]["depth"] == 1
        assert rows["Child task"]["parent_id"] == rows["Open task"]["id"]

    def test_claimed_at_is_latest_in_progress_transition(self, pm_client):
        c, _ = pm_client
        body = c.get("/api/tasks", headers=_hdr()).json()
        rows = {t["summary"]: t for t in body["tasks"]}
        stale = rows["Stale claim"]
        assert stale["claimed_at"] is not None
        assert stale["claimed_at"] == stale["last_transition_at"]
        assert stale["claimed_by"]["name"] == "Vorpid"
        assert stale["transitions_count"] == 2

    def test_legacy_author_only_task_has_no_created_by(self, pm_client):
        """Trust-class fallback fixture: created_by/claimed_by absent (None), author
        present -- the drawer renders `author` with the dashed 'unverified' chip in
        this shape, never upgrading it to look like a server-resolved identity."""
        c, _ = pm_client
        body = c.get("/api/tasks", headers=_hdr()).json()
        row = next(t for t in body["tasks"] if t["summary"] == "Legacy author-only task")
        assert row["created_by"] is None
        assert row["claimed_by"] is None
        # author isn't part of the row per the brief's literal shape, but the full
        # node (author fallback source) is asserted via /api/node/{id} below.
        node = c.get(f"/api/node/{row['id']}", headers=_hdr()).json()
        assert node["author"] == "OldAuthor"
        assert node.get("metadata", {}).get("created_by") is None
        assert node.get("metadata", {}).get("recorded_by") is None


class TestOverviewAPI:
    def test_no_token_rejected(self, pm_client):
        c, _ = pm_client
        r = c.get("/api/overview")
        assert r.status_code == 403

    def test_task_counts_and_done_this_week(self, pm_client):
        c, _ = pm_client
        r = c.get("/api/overview", headers=_hdr())
        assert r.status_code == 200
        body = r.json()
        tasks = body["tasks"]
        assert tasks["open"] == 3  # open, child, legacy
        assert tasks["in_progress"] == 2  # stale, fresh
        assert tasks["blocked"] == 1
        assert tasks["done"] == 2  # done-this-week + old-done
        assert tasks["cancelled"] == 1
        # done-cap is driven by last_transition_at (the "done" transition), NOT
        # node creation timestamp -- old_done_task was CREATED 40d ago but its
        # done transition landed 35d ago, so it must NOT count; done_task's done
        # transition landed 2d ago and DOES count.
        assert tasks["done_this_week"] == 1

    def test_stale_claim_uses_claimed_at_not_creation_timestamp(self, pm_client):
        c, _ = pm_client
        body = c.get("/api/overview", headers=_hdr()).json()
        stale_ids = {s["id"] for s in body["needs_attention"]["stale_claims"]}
        fresh_summaries = {s["summary"] for s in body["needs_attention"]["stale_claims"]}
        assert "Stale claim" in fresh_summaries
        assert "Fresh claim" not in fresh_summaries
        assert len(stale_ids) == 1

    def test_blocked_in_needs_attention(self, pm_client):
        c, _ = pm_client
        body = c.get("/api/overview", headers=_hdr()).json()
        blocked_summaries = {b["summary"] for b in body["needs_attention"]["blocked"]}
        assert "Blocked task" in blocked_summaries

    def test_active_constraints_excludes_low_severity(self, pm_client):
        c, _ = pm_client
        body = c.get("/api/overview", headers=_hdr()).json()
        summaries = {c_["summary"] for c_ in body["constraints"]}
        assert "Active constraint" in summaries
        assert "Low-severity constraint" not in summaries

    def test_workflows_counts_head_only(self, pm_client):
        """A workflow with an incoming SUPERSEDES edge is an old version -- only
        the HEAD (v2) counts, mirroring prime.py's _format_workflows filter."""
        c, _ = pm_client
        body = c.get("/api/overview", headers=_hdr()).json()
        assert body["workflows"] == 1

    def test_documents_count(self, pm_client):
        c, _ = pm_client
        body = c.get("/api/overview", headers=_hdr()).json()
        assert body["documents"] == 1

    def test_recent_incidents_excludes_older_than_14_days(self, pm_client):
        c, _ = pm_client
        body = c.get("/api/overview", headers=_hdr()).json()
        summaries = {i["summary"] for i in body["recent_incidents"]}
        assert "Recent high-severity incident" in summaries
        assert "Old high-severity incident" not in summaries

    def test_recent_episodes_include_provenance(self, pm_client):
        c, _ = pm_client
        body = c.get("/api/overview", headers=_hdr()).json()
        assert len(body["recent_episodes"]) == 1
        ep = body["recent_episodes"][0]
        assert ep["recorded_by"]["name"] == "Colton"
        assert ep["from_agent"] is True

    def test_prime_import_not_used(self):
        """Brief constraint (doc:4c0b9d426f4c): the dashboard must not import
        prime.py (markdown-formatting + CLI deps this JSON aggregation has no
        business pulling in). A local SEVERITY_ORDER/closed-statuses copy keeps
        api.py's import surface independent of it."""
        import vibe_cognition.dashboard.api as api_module
        assert not hasattr(api_module, "SEVERITY_ORDER"), "must not import prime.py's SEVERITY_ORDER directly"
        assert api_module._SEVERITY_ORDER == {"critical": 0, "high": 1, "normal": 2, "low": 3}


class TestEmptyGraphFixture:
    """Acceptance (doc:4c0b9d426f4c): V1 views must work on an empty-graph fixture,
    not just a populated one -- a fresh install has zero tasks/constraints/episodes."""

    @pytest.fixture
    def empty_client(self, tmp_path: Path):
        storage = CognitionStorage(tmp_path / ".cognition")
        lc = {
            "config": None, "cognition_storage": storage,
            "cognition_embedding_storage": _FakeEmbeddingStorage(),
            "embedding_generator": None, "embedding_ready": threading.Event(),
            "embedding_error": None,
        }
        app, stack = build_app(lc, token="testtok")
        with TestClient(app, base_url="http://127.0.0.1:7842") as c:
            yield c
        stack.close()

    def test_tasks_empty(self, empty_client):
        r = empty_client.get("/api/tasks", headers=_hdr())
        assert r.status_code == 200
        assert r.json() == {"tasks": [], "count": 0}

    def test_overview_empty(self, empty_client):
        r = empty_client.get("/api/overview", headers=_hdr())
        assert r.status_code == 200
        body = r.json()
        assert body["tasks"] == {
            "open": 0, "in_progress": 0, "blocked": 0, "done": 0, "cancelled": 0,
            "done_this_week": 0,
        }
        assert body["documents"] == 0
        assert body["workflows"] == 0
        assert body["constraints"] == []
        assert body["needs_attention"] == {"stale_claims": [], "blocked": []}
        assert body["recent_episodes"] == []
        assert body["recent_incidents"] == []

    def test_graph_empty(self, empty_client):
        r = empty_client.get("/api/graph", headers=_hdr())
        assert r.status_code == 200
        assert r.json() == {"nodes": [], "edges": []}


class TestFrontendStructure:
    """Structural/text assertions in place of a JS test runner (none exists in this
    repo) -- mirrors TestPackaging's existing string-presence-check idiom."""

    def _read(self, name: str) -> str:
        traversable = resources.files("vibe_cognition.dashboard") / "static"
        with resources.as_file(traversable) as path:
            return (path / name).read_text(encoding="utf-8")

    def test_nav_rail_has_six_v2_views(self):
        """WP-DashV2: Workflows/Documents/Activity join the V1 nav rail
        (overview/board/graph) -- six entries total, none removed."""
        html = self._read("index.html")
        for view in ("overview", "board", "workflows", "documents", "activity", "graph"):
            assert f'data-view="{view}"' in html, f"nav rail missing {view}"

    def test_drawer_is_shared_not_inline(self):
        html = self._read("index.html")
        assert 'id="drawer"' in html
        assert 'id="detail-tpl"' not in html, "old inline detail template should be gone"

    def test_lazy_graph_build_not_reconstructed_on_poll(self):
        """D-3 fails-before: the pre-redesign app.js called loadGraph()->buildCy()
        unconditionally from init() AND every 30s tick, rebuilding a fresh
        cytoscape() instance each cycle regardless of which view was active. The
        fix: buildCy() is called from exactly one place (ensureGraphLoaded, guarded
        by a graphLoaded flag), and the poll path only ever calls the in-place
        refresh (refreshGraphInPlace / cy.json), never buildCy directly."""
        js = self._read("app.js")
        assert js.count("buildCy(") == 2, (
            "buildCy should appear exactly twice: its definition and its single "
            "call site inside ensureGraphLoaded"
        )
        ensure_start = js.index("async function ensureGraphLoaded")
        ensure_body = js[ensure_start:js.index("\n}\n", ensure_start)]
        assert "buildCy(" in ensure_body
        poll_start = js.index("async function pollTick")
        poll_body = js[poll_start:js.index("\n}\n", poll_start)]
        assert "buildCy(" not in poll_body
        assert "refreshGraphInPlace" in poll_body

    def test_overview_always_polled_regardless_of_active_view(self):
        """Acceptance (doc:4c0b9d426f4c): 'stats/overview polling always' -- unlike
        the graph fetch, loadOverview() in the poll tick must not be conditioned on
        which nav view is currently active."""
        js = self._read("app.js")
        poll_start = js.index("async function pollTick")
        poll_body = js[poll_start:js.index("\n}\n", poll_start)]
        assert "await loadOverview();" in poll_body
        for line in poll_body.splitlines():
            if "loadOverview()" in line:
                assert "if" not in line and "?" not in line, \
                    f"loadOverview() call looks conditionally gated: {line!r}"
