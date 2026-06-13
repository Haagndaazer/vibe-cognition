"""WP-Cap (P2 capability gaps): cognition_get_node, edge reason persistence,
cognition_update_node + re-embed, and the exposed superseded/incident queries.

Each test names the specific failure mode it guards (rule 20) and is written to
fail before its fix exists (rule 12)."""

from vibe_cognition.cognition import CognitionNode, CognitionNodeType, CognitionStorage
from vibe_cognition.tools.cognition_tools import _get_node


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
