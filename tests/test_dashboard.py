"""Tests for the dashboard server, API, and packaging."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
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

    def count_documents(self) -> int:
        return self._count

    def delete_embedding(self, entity_id: str) -> bool:
        self._deleted.append(entity_id)
        return True

    def vector_search(self, query_embedding, limit, entity_type=None):
        return self._search_results[:limit]


class _FakeEmbeddingGenerator:
    def generate_query_embedding(self, text: str) -> list[float]:
        return [0.0, 0.1, 0.2]


@pytest.fixture
def storage(tmp_path: Path) -> CognitionStorage:
    s = CognitionStorage(tmp_path / ".cognition")
    now = datetime.now(timezone.utc).isoformat()
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

    def test_search_missing_query(self, client):
        c, _ = client
        r = c.post("/api/search", json={"query": ""}, headers=_hdr())
        assert r.status_code == 400


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
