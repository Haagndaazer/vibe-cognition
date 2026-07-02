"""Tests for the dashboard server, API, and packaging."""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import pytest
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
