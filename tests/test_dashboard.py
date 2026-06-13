"""Tests for the dashboard server, API, and packaging."""

from __future__ import annotations

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
