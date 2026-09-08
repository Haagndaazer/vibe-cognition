"""WP-P1: edge writes and curation marks require a curation-session token
minted by cognition_begin_curation; accepted edges carry the session id."""

import json

from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
from vibe_cognition.cognition.storage import CognitionStorage
from vibe_cognition.tools.cognition_tools import register_cognition_tools
from vibe_cognition.tools.service_tools import register_service_tools


def _node(node_id: str) -> CognitionNode:
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DECISION, summary=f"s {node_id}", detail="d",
        context=[], references=[], timestamp="2026-09-08T00:00:00+00:00", author="t",
    )


def _setup(tmp_path, mock_mcp, build_lc, make_ctx):
    lc = build_lc(tmp_path, embeddings_ready=True)
    storage: CognitionStorage = lc["cognition_storage"]
    storage.add_node(_node("a"))
    storage.add_node(_node("b"))
    register_cognition_tools(mock_mcp)
    register_service_tools(mock_mcp)
    return lc, storage, make_ctx(lc)


def test_tokenless_edge_write_is_refused_for_the_token_reason(tmp_path, mock_mcp, build_lc, make_ctx, monkeypatch):
    monkeypatch.delenv("VIBE_HARNESS", raising=False)
    lc, storage, ctx = _setup(tmp_path, mock_mcp, build_lc, make_ctx)
    before = storage.get_statistics()["edges"]

    result = mock_mcp.tools["cognition_add_edge"](ctx, from_id="a", to_id="b", edge_type="led_to")

    assert "error" in result
    assert "curation token" in result["error"]
    assert "/vibe-curate" in result["error"]
    assert storage.get_statistics()["edges"] == before


def test_tokenless_batch_and_mark_curated_are_refused(tmp_path, mock_mcp, build_lc, make_ctx):
    lc, storage, ctx = _setup(tmp_path, mock_mcp, build_lc, make_ctx)
    edges = json.dumps([{"from_id": "a", "to_id": "b", "edge_type": "led_to"}])
    r1 = mock_mcp.tools["cognition_add_edges_batch"](ctx, edges=edges)
    r2 = mock_mcp.tools["cognition_mark_curated"](ctx, node_ids="a")
    assert "curation token" in r1["error"] and "curation token" in r2["error"]
    assert "/vibe-curate" in r1["error"] and "/vibe-curate" in r2["error"]
    assert storage.get_statistics()["edges"] == 0
    assert storage.get_uncurated_nodes(limit=10)


def test_wrong_token_is_refused(tmp_path, mock_mcp, build_lc, make_ctx):
    lc, storage, ctx = _setup(tmp_path, mock_mcp, build_lc, make_ctx)
    mock_mcp.tools["cognition_begin_curation"](ctx)
    result = mock_mcp.tools["cognition_add_edge"](
        ctx, from_id="a", to_id="b", edge_type="led_to", curation_token="not-the-token",
    )
    assert "curation token" in result["error"]
    assert storage.get_statistics()["edges"] == 0


def test_valid_token_writes_and_stamps_session(tmp_path, mock_mcp, build_lc, make_ctx):
    lc, storage, ctx = _setup(tmp_path, mock_mcp, build_lc, make_ctx)
    begun = mock_mcp.tools["cognition_begin_curation"](ctx)
    token, session_id = begun["curation_token"], begun["session_id"]
    assert begun["uncurated"] == 2

    result = mock_mcp.tools["cognition_add_edge"](
        ctx, from_id="a", to_id="b", edge_type="led_to", curation_token=token, source="curate-skill",
    )
    assert result.get("created") is True, result

    neighbors = mock_mcp.tools["cognition_get_neighbors"](ctx, node_id="a")
    edge = next(e for e in neighbors["outgoing"] if e["id"] == "b")
    assert edge["curation_session"] == session_id

    marked = mock_mcp.tools["cognition_mark_curated"](ctx, node_ids="a,b", curation_token=token)
    assert marked["marked"] == 2

    status = mock_mcp.tools["get_status"](ctx)
    assert status["curation_sessions"] == {"started": 1, "writes": 3}

    replayed = CognitionStorage(storage.cognition_dir)
    attrs = replayed.graph.get_edge_data("a", "b")
    assert any(v.get("curation_session") == session_id for v in attrs.values())


def test_batch_with_token_stamps_every_edge(tmp_path, mock_mcp, build_lc, make_ctx):
    lc, storage, ctx = _setup(tmp_path, mock_mcp, build_lc, make_ctx)
    storage.add_node(_node("c"))
    token = mock_mcp.tools["cognition_begin_curation"](ctx)["curation_token"]
    edges = json.dumps([
        {"from_id": "a", "to_id": "b", "edge_type": "led_to", "source": "curate-skill"},
        {"from_id": "b", "to_id": "c", "edge_type": "relates_to", "source": "curate-skill"},
    ])
    result = mock_mcp.tools["cognition_add_edges_batch"](ctx, edges=edges, curation_token=token)
    assert result["created"] == 2
    for u, v, data in storage.graph.edges(data=True):
        assert data["curation_session"], (u, v)


def test_codex_refusal_names_the_codex_invocation(tmp_path, mock_mcp, build_lc, make_ctx, monkeypatch):
    monkeypatch.setenv("VIBE_HARNESS", "codex")
    lc, storage, ctx = _setup(tmp_path, mock_mcp, build_lc, make_ctx)
    result = mock_mcp.tools["cognition_add_edge"](ctx, from_id="a", to_id="b", edge_type="led_to")
    assert "curation token" in result["error"]
    assert "$vibe-curate" in result["error"] and "/vibe-curate" not in result["error"]
