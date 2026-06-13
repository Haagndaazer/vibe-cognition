"""WP-T: the get_lifespan accessor (T-9) + the node_type/direction parsers (T-6)."""

import json
from types import SimpleNamespace
from typing import cast

import pytest
from fastmcp import Context

from vibe_cognition.cognition import CognitionNode, CognitionNodeType, CognitionStorage
from vibe_cognition.tools.cognition_tools import (
    _add_edge_core,
    _add_edges_batch_core,
    _parse_node_type,
    _validate_direction,
)
from vibe_cognition.tools.utils import get_lifespan


def _node(node_id):
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DECISION, summary="s", detail="d",
        context=[], references=[], timestamp="2026-06-13T00:00:00+00:00", author="t",
    )


class _FakeEdgeStorage:
    """has_node always True, no existing edges — but add_edge returns a fixed result,
    to drive the C-5 'add_edge returned False' (node-vanished race) path."""

    def __init__(self, add_result):
        self._add_result = add_result

    def has_node(self, nid):
        return True

    def get_successors(self, nid, edge_type=None):
        return []

    def add_edge(self, edge):
        return self._add_result


def test_get_lifespan_returns_lifespan_context():
    ctx = cast(Context, SimpleNamespace(request_context=SimpleNamespace(lifespan_context={"k": "v"})))
    assert get_lifespan(ctx) == {"k": "v"}


def test_get_lifespan_raises_when_request_context_is_none():
    """The accessor exists to narrow fastmcp's Optional request_context — when it IS
    None (never inside a live tool call) it must raise, not return None-attribute soup."""
    ctx = cast(Context, SimpleNamespace(request_context=None))
    with pytest.raises(RuntimeError):
        get_lifespan(ctx)


def test_parse_node_type_valid_none_and_bad():
    """T-6: one parser, one error shape — bad type returns an error dict, never raises
    (the old get_uncurated did a bare CognitionNodeType(node_type) that RAISED)."""
    assert _parse_node_type("decision") == (CognitionNodeType.DECISION, None)
    assert _parse_node_type(None) == (None, None)
    nt, err = _parse_node_type("bogus")
    assert nt is None
    assert err is not None and "Invalid node_type" in err["error"]
    # Fails-before contrast: the bare enum call the old tool used does raise.
    with pytest.raises(ValueError):
        CognitionNodeType("bogus")


def test_validate_direction():
    """T-6: an unknown direction is rejected, not silently treated as incoming /
    returned as an empty success."""
    assert _validate_direction("outgoing", ("outgoing", "incoming")) is None
    assert _validate_direction("both", ("incoming", "outgoing", "both")) is None
    err = _validate_direction("sideways", ("incoming", "outgoing", "both"))
    assert err is not None and "Invalid direction" in err["error"]


def test_add_edge_core_surfaces_failed_add():
    """C-5: add_edge returns False when a node vanished between has_node and the write
    (cross-process race) — the tool must surface that, not report created:True."""
    storage = cast(CognitionStorage, _FakeEdgeStorage(add_result=False))
    res = _add_edge_core(storage, "a", "b", "led_to")
    assert "error" in res and not res.get("created"), "a failed add reported as created (C-5)"
    # Positive control: a successful add reports created.
    ok = cast(CognitionStorage, _FakeEdgeStorage(add_result=True))
    assert _add_edge_core(ok, "a", "b", "led_to")["created"] is True


def test_add_edges_batch_skips_non_dict_element_without_crashing(tmp_path):
    """T-3: a non-dict element mid-array is skipped-and-reported, and the valid edges
    BEFORE and AFTER it are still committed — no AttributeError partial-commit crash."""
    storage = CognitionStorage(tmp_path / ".cognition")
    for nid in ("a", "b", "c"):
        storage.add_node(_node(nid))
    edges = json.dumps([
        {"from_id": "a", "to_id": "b", "edge_type": "led_to"},
        "i am not an edge object",
        {"from_id": "a", "to_id": "c", "edge_type": "led_to"},
    ])
    res = _add_edges_batch_core(storage, edges)
    assert res["created"] == 2, "valid edges around the bad element were not committed"
    assert res["skipped"] == 1
    assert any("Not an edge object" in e for e in res["errors"])


def test_add_edges_batch_core_surfaces_failed_add():
    """C-5 (batch): a False add_edge is reported + skipped, not counted as created."""
    storage = cast(CognitionStorage, _FakeEdgeStorage(add_result=False))
    edges = json.dumps([{"from_id": "a", "to_id": "b", "edge_type": "led_to"}])
    res = _add_edges_batch_core(storage, edges)
    assert res["created"] == 0 and res["skipped"] == 1, "failed add counted as created (C-5)"
