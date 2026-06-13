"""WP-D1a: DOCUMENT node type, graph-inert guard, store/get tools, sidecar, sync guard."""

from vibe_cognition.cognition.models import CognitionEdgeType, CognitionNode, CognitionNodeType
from vibe_cognition.cognition.storage import CognitionStorage


def _node(node_id, node_type, refs=None, summary="s", detail="d"):
    return CognitionNode(
        id=node_id, type=node_type, summary=summary, detail=detail,
        context=[], references=refs or [], severity=None,
        timestamp="2026-06-13T00:00:00+00:00", author="t",
    )


def test_document_is_graph_inert_no_part_of_from_citing_episode(tmp_path):
    """D1a: a document is graph-inert. An episode citing the document's doc:<hash>
    ref must NOT mint a part_of edge — the existing matcher would otherwise treat
    the document as an entity and link it (the wrong edge fires from the EPISODE's
    record call, which is why the guard is pair-level). Asserts the SPECIFIC edge
    is absent in BOTH directions, not just an edge count."""
    s = CognitionStorage(tmp_path)
    doc_ref = "doc:abc123def456"
    s.add_node(_node("doc00001", CognitionNodeType.DOCUMENT, refs=[doc_ref]))
    s.add_node(_node("ep000001", CognitionNodeType.EPISODE, refs=[doc_ref]))

    s.create_deterministic_edges("ep000001")  # the edge would fire here
    s.create_deterministic_edges("doc00001")

    g = s.graph
    assert not g.has_edge("ep000001", "doc00001"), "episode→document edge minted (inert guard failed)"
    assert not g.has_edge("doc00001", "ep000001"), "document→episode part_of minted (inert guard failed)"


def test_entity_episode_matcher_still_links(tmp_path):
    """Positive control: the document guard did not over-reach — a normal entity
    and episode sharing a commit ref still get their entity→episode part_of."""
    s = CognitionStorage(tmp_path)
    s.add_node(_node("ep000002", CognitionNodeType.EPISODE, refs=["commit:deadbeef1234"]))
    s.add_node(_node("dec00001", CognitionNodeType.DECISION, refs=["commit:deadbeef1234"]))
    s.create_deterministic_edges("dec00001")

    g = s.graph
    assert g.has_edge("dec00001", "ep000002"), "entity→episode part_of missing (guard over-reached)"
    assert CognitionEdgeType.PART_OF.value in g["dec00001"]["ep000002"]
