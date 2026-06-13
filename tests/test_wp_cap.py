"""WP-Cap (P2 capability gaps): cognition_get_node, edge reason persistence,
cognition_update_node + re-embed, and the exposed superseded/incident queries.

Each test names the specific failure mode it guards (rule 20) and is written to
fail before its fix exists (rule 12)."""

from vibe_cognition.cognition import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    CognitionStorage,
)
from vibe_cognition.tools.cognition_tools import (
    _add_edge_core,
    _add_edges_batch_core,
    _get_node,
)


def _node(node_id, *, summary="s", detail="d"):
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DECISION, summary=summary, detail=detail,
        context=["ctx"], references=["commit:abc"], severity=None,
        timestamp="2026-06-13T00:00:00+00:00", author="t",
    )


# --- Commit 1: cognition_get_node -------------------------------------------

def test_get_node_returns_full_narrative_including_detail(tmp_path):
    """get_node must return the FULL node dict — including `detail`, which search
    results and get_neighbors omit. Fails-before: no _get_node surface at all."""
    s = CognitionStorage(tmp_path / ".cognition")
    s.add_node(_node("n1", summary="the summary", detail="the long detail body"))

    out = _get_node(s, "n1")

    assert out.get("id") == "n1", "result must carry the node id (get_node omits it)"
    assert out.get("detail") == "the long detail body", "detail must be present"
    assert out.get("summary") == "the summary"
    assert out.get("type") == CognitionNodeType.DECISION.value
    assert out.get("context") == ["ctx"]
    assert out.get("references") == ["commit:abc"]


def test_get_node_missing_id_returns_error(tmp_path):
    """A missing id returns an error dict, not a raise or a None."""
    s = CognitionStorage(tmp_path / ".cognition")
    out = _get_node(s, "nope")
    assert "error" in out, "missing node must return an error dict"


# --- Commit 2: persist the edge `reason` ------------------------------------

def _edge_reason(storage, from_id, to_id):
    for tid, edata in storage.get_successors(from_id):
        if tid == to_id:
            return edata.get("reason")
    raise AssertionError(f"no edge {from_id} -> {to_id}")


def test_edge_reason_round_trips_through_replay(tmp_path):
    """An edge's `reason` must survive a journal REPLAY in a fresh storage instance —
    it rides `model_dump` into the journal and `data.get('reason')` back out on replay.
    Fails-before (no model field / no replay read): the replayed edge's reason is None,
    the curation rationale is silently lost across a reload."""
    cog = tmp_path / ".cognition"
    s1 = CognitionStorage(cog)
    s1.add_node(_node("a"))
    s1.add_node(_node("b"))
    s1.add_edge(CognitionEdge(
        from_id="a", to_id="b", edge_type=CognitionEdgeType.LED_TO,
        timestamp="2026-06-13T00:00:00+00:00", source="manual",
        reason="a forced b because of the deadline",
    ))
    assert _edge_reason(s1, "a", "b") == "a forced b because of the deadline"

    s2 = CognitionStorage(cog)  # fresh replay of the same journal
    assert _edge_reason(s2, "a", "b") == "a forced b because of the deadline", (
        "edge reason did not survive journal replay (lost on reload)"
    )


def test_add_edge_core_persists_reason(tmp_path):
    """The single-edge tool path carries the agent's reason onto the edge."""
    s = CognitionStorage(tmp_path / ".cognition")
    s.add_node(_node("a"))
    s.add_node(_node("b"))
    out = _add_edge_core(s, "a", "b", "led_to", reason="my rationale")
    assert out.get("created") is True
    assert _edge_reason(s, "a", "b") == "my rationale"


def test_add_edges_batch_persists_reason(tmp_path):
    """The batch tool path carries each edge's reason (e.get('reason'))."""
    s = CognitionStorage(tmp_path / ".cognition")
    for nid in ("a", "b"):
        s.add_node(_node(nid))
    out = _add_edges_batch_core(
        s, '[{"from_id":"a","to_id":"b","edge_type":"led_to","reason":"batch why"}]'
    )
    assert out["created"] == 1, out
    assert _edge_reason(s, "a", "b") == "batch why"
