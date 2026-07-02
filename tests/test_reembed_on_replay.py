"""WP-3 tests: re-embed on journal replay (8606d59905a5).

A node journaled by ANOTHER process (discovered via catch-up/rehydrate rather
than this process's own _record_node call) must become searchable without a
server restart. storage.py's pop_replayed_node_ids() queues the ids;
cognition_tools._reembed_replayed_nodes() drains the queue and embeds via the
SAME shared paths _record_node uses (_embed_entity_node / _embed_workflow).
"""

from __future__ import annotations

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
from vibe_cognition.embeddings import ChromaDBStorage
from vibe_cognition.tools.cognition_tools import _reembed_replayed_nodes


class _Spy:
    """Records every embed call; returns a fixed 3-D vector."""

    DIM = 3

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate(self, text: str, input_type: str = "document") -> list[float]:
        self.calls.append((input_type, text))
        return [0.1] * self.DIM


def _node(node_id: str, ntype: CognitionNodeType = CognitionNodeType.DECISION,
          summary: str = "s", detail: str = "d") -> CognitionNode:
    return CognitionNode(
        id=node_id, type=ntype, summary=summary, detail=detail,
        context=[], references=[], timestamp="2026-06-21T00:00:00+00:00", author="t",
    )


def _make_chroma(tmp_path) -> ChromaDBStorage:
    return ChromaDBStorage(
        persist_directory=tmp_path / "chroma",
        embedding_model="m",
        embedding_dimensions=_Spy.DIM,
    )


class TestReembedReplayedNodes:
    def test_cross_process_node_becomes_searchable(self, tmp_path):
        """The exact reported symptom (discovery 4b99fa9f44d5): a node another
        process wrote is invisible to search until this fix. Fails-before:
        pop_replayed_node_ids/_reembed_replayed_nodes didn't exist, so a
        catch-up never triggered an embed and count_documents() stayed 0."""
        cog_dir = tmp_path / ".cognition"
        store_a = CognitionStorage(cog_dir)
        store_b = CognitionStorage(cog_dir)
        chroma = _make_chroma(tmp_path)
        spy = _Spy()

        store_b.add_node(_node("b1", summary="teammate wrote this"))
        assert store_a.has_node("b1")  # catch-up: b1 enters store_a's graph
        assert chroma.count_documents() == 0, "not embedded yet"

        embedded = _reembed_replayed_nodes(store_a, chroma, spy)  # type: ignore[arg-type]

        assert embedded == 1
        assert chroma.count_documents() == 1
        assert spy.calls == [("document", "decision: teammate wrote this\nd")]

    def test_second_drain_is_a_noop(self, tmp_path):
        """The queue is drained (not re-read) -- a second call with nothing new
        replayed must do no work and cost no model call."""
        cog_dir = tmp_path / ".cognition"
        store_a = CognitionStorage(cog_dir)
        store_b = CognitionStorage(cog_dir)
        chroma = _make_chroma(tmp_path)
        spy = _Spy()

        store_b.add_node(_node("b1"))
        store_a.has_node("b1")
        _reembed_replayed_nodes(store_a, chroma, spy)  # type: ignore[arg-type]
        spy.calls.clear()

        embedded_again = _reembed_replayed_nodes(store_a, chroma, spy)  # type: ignore[arg-type]

        assert embedded_again == 0
        assert spy.calls == []

    def test_already_embedded_id_is_not_reembedded(self, tmp_path):
        """Own-write catch-up queues ids too (by design, per storage.py's
        comment) -- but since this process's own _record_node already embeds
        synchronously at write time, the existence check must filter it out
        before any model call, not redundantly re-embed it."""
        cog_dir = tmp_path / ".cognition"
        store = CognitionStorage(cog_dir)
        chroma = _make_chroma(tmp_path)
        spy = _Spy()

        node = _node("n1")
        store.add_node(node)
        # Simulate _record_node's synchronous embed at write time.
        chroma.upsert_embedding("n1", spy.generate("decision: s\nd"), {"entity_type": "decision"})
        spy.calls.clear()
        store.has_node("n1")  # own-write catch-up queues "n1" again

        embedded = _reembed_replayed_nodes(store, chroma, spy)  # type: ignore[arg-type]

        assert embedded == 0, "already-embedded id must be filtered before any embed call"
        assert spy.calls == [], "no redundant model call for an id already in Chroma"

    def test_document_nodes_are_skipped(self, tmp_path):
        """DOCUMENT nodes need sidecar-text + chunk handling this reconciler
        deliberately doesn't do (left to the startup sync) -- must not be
        blindly routed through _embed_entity_node (wrong vector, no chunks)."""
        cog_dir = tmp_path / ".cognition"
        store_a = CognitionStorage(cog_dir)
        store_b = CognitionStorage(cog_dir)
        chroma = _make_chroma(tmp_path)
        spy = _Spy()

        store_b.add_node(_node("doc1", ntype=CognitionNodeType.DOCUMENT, summary="a doc"))
        store_a.has_node("doc1")

        embedded = _reembed_replayed_nodes(store_a, chroma, spy)  # type: ignore[arg-type]

        assert embedded == 0
        assert chroma.count_documents() == 0
        assert spy.calls == []

    def test_workflow_node_routes_through_embed_workflow(self, tmp_path):
        """WORKFLOW nodes must chunk (proving _embed_workflow ran, not
        _embed_entity_node, which never writes #chunk-N vectors)."""
        cog_dir = tmp_path / ".cognition"
        store_a = CognitionStorage(cog_dir)
        store_b = CognitionStorage(cog_dir)
        chroma = _make_chroma(tmp_path)
        spy = _Spy()

        long_body = "step one. " * 200  # long enough to force >1 chunk
        store_b.add_node(_node("wf1", ntype=CognitionNodeType.WORKFLOW,
                                summary="a workflow", detail=long_body))
        store_a.has_node("wf1")

        embedded = _reembed_replayed_nodes(store_a, chroma, spy)  # type: ignore[arg-type]

        assert embedded == 1
        ids = chroma._collection.get()["ids"]
        assert "wf1" in ids
        assert any(i.startswith("wf1#chunk-") for i in ids), (
            f"expected chunk vectors from _embed_workflow, got: {ids}"
        )

    def test_no_pending_ids_is_a_cheap_noop(self, tmp_path):
        """The common case (nothing replayed) must not touch Chroma or the
        generator at all."""
        cog_dir = tmp_path / ".cognition"
        store = CognitionStorage(cog_dir)
        chroma = _make_chroma(tmp_path)
        spy = _Spy()

        embedded = _reembed_replayed_nodes(store, chroma, spy)  # type: ignore[arg-type]

        assert embedded == 0
        assert spy.calls == []

    def test_deleted_since_queued_node_is_skipped_not_raised(self, tmp_path):
        """A node queued for re-embed but removed before the drain runs (raced
        with a delete) must be skipped silently, never raise."""
        cog_dir = tmp_path / ".cognition"
        store_a = CognitionStorage(cog_dir)
        store_b = CognitionStorage(cog_dir)
        chroma = _make_chroma(tmp_path)
        spy = _Spy()

        store_b.add_node(_node("gone"))
        store_a.has_node("gone")  # queues it
        store_a.remove_node("gone")  # ...then it's deleted before the drain

        embedded = _reembed_replayed_nodes(store_a, chroma, spy)  # type: ignore[arg-type]

        assert embedded == 0
        assert spy.calls == []
