"""Tests for Phase 2: MultiDiGraph migration, remove_edge, and related changes."""

import json

import pytest

from vibe_cognition.cognition import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    CognitionStorage,
    get_superseded_chain,
)


def _make_node(
    node_id,
    node_type=CognitionNodeType.DECISION,
    summary="Test",
    detail="Detail",
    context=None,
    references=None,
    timestamp="2026-03-15T10:00:00Z",
):
    return CognitionNode(
        id=node_id,
        type=node_type,
        summary=summary,
        detail=detail,
        context=context or [],
        references=references or [],
        timestamp=timestamp,
        author="tester",
    )


class TestMultiDiGraph:
    """Tests for MultiDiGraph behavior — multiple edge types per pair."""

    @pytest.fixture
    def storage(self, tmp_path):
        return CognitionStorage(tmp_path / ".cognition")

    def test_two_edge_types_between_same_pair(self, storage):
        """Two different edge types between the same pair both survive."""
        storage.add_node(_make_node("a"))
        storage.add_node(_make_node("b"))

        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
        ))
        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.PART_OF,
            timestamp="2026-03-15T10:01:00Z",
        ))

        all_succ = storage.get_successors("a")
        assert len(all_succ) == 2
        types = {s[1]["type"] for s in all_succ}
        assert types == {"led_to", "part_of"}

    def test_filter_by_edge_type_with_multiple(self, storage):
        """get_successors with edge_type filter returns only matching edges."""
        storage.add_node(_make_node("a"))
        storage.add_node(_make_node("b"))

        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
        ))
        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.PART_OF,
            timestamp="2026-03-15T10:01:00Z",
        ))

        led_to = storage.get_successors("a", CognitionEdgeType.LED_TO)
        assert len(led_to) == 1
        assert led_to[0][1]["type"] == "led_to"

        part_of = storage.get_successors("a", CognitionEdgeType.PART_OF)
        assert len(part_of) == 1
        assert part_of[0][1]["type"] == "part_of"

    def test_same_triple_is_idempotent(self, storage):
        """Adding the same (from, to, type) triple twice doesn't create duplicates."""
        storage.add_node(_make_node("a"))
        storage.add_node(_make_node("b"))

        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
        ))
        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:01:00Z",  # later timestamp
        ))

        succ = storage.get_successors("a", CognitionEdgeType.LED_TO)
        assert len(succ) == 1  # Only one edge, not two
        # Second write overwrites — latest timestamp wins
        assert succ[0][1]["timestamp"] == "2026-03-15T10:01:00Z"

    def test_edge_count_with_multi_edges(self, storage):
        """Statistics count parallel edges correctly."""
        storage.add_node(_make_node("a"))
        storage.add_node(_make_node("b"))

        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
        ))
        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.PART_OF,
            timestamp="2026-03-15T10:01:00Z",
        ))

        stats = storage.get_statistics()
        assert stats["edges"] == 2
        assert stats["edge_led_to"] == 1
        assert stats["edge_part_of"] == 1


class TestMultiDiGraphHydration:
    """Tests for JSONL hydration with MultiDiGraph."""

    def test_multi_edge_types_survive_hydration(self, tmp_path):
        """Two edge types between same pair both survive JSONL round-trip."""
        cog_dir = tmp_path / ".cognition"
        s1 = CognitionStorage(cog_dir)
        s1.add_node(_make_node("a"))
        s1.add_node(_make_node("b"))
        s1.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
        ))
        s1.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.PART_OF,
            timestamp="2026-03-15T10:01:00Z",
        ))

        # Re-hydrate
        s2 = CognitionStorage(cog_dir)
        all_succ = s2.get_successors("a")
        assert len(all_succ) == 2
        types = {s[1]["type"] for s in all_succ}
        assert types == {"led_to", "part_of"}

    def test_duplicate_triple_in_journal_is_idempotent(self, tmp_path):
        """Duplicate (from, to, type) entries in journal produce only one edge."""
        cog_dir = tmp_path / ".cognition"
        cog_dir.mkdir(parents=True)

        journal = cog_dir / "journal.jsonl"
        journal.write_text(
            '{"action":"add_node","data":{"id":"a","type":"decision","summary":"A","detail":"D","context":[],"references":[],"severity":null,"timestamp":"2026-03-15T10:00:00Z","author":"test"}}\n'
            '{"action":"add_node","data":{"id":"b","type":"fail","summary":"B","detail":"D","context":[],"references":[],"severity":null,"timestamp":"2026-03-15T10:01:00Z","author":"test"}}\n'
            '{"action":"add_edge","data":{"from_id":"a","to_id":"b","edge_type":"led_to","timestamp":"t1"}}\n'
            '{"action":"add_edge","data":{"from_id":"a","to_id":"b","edge_type":"led_to","timestamp":"t2"}}\n',
            encoding="utf-8",
        )

        storage = CognitionStorage(cog_dir)
        succ = storage.get_successors("a", CognitionEdgeType.LED_TO)
        assert len(succ) == 1  # Deduped by key
        assert succ[0][1]["timestamp"] == "t2"  # Latest wins

    def test_old_journal_overwrite_now_creates_both(self, tmp_path):
        """Old journals with same pair but different types now get both edges."""
        cog_dir = tmp_path / ".cognition"
        cog_dir.mkdir(parents=True)

        journal = cog_dir / "journal.jsonl"
        journal.write_text(
            '{"action":"add_node","data":{"id":"a","type":"decision","summary":"A","detail":"D","context":[],"references":[],"severity":null,"timestamp":"2026-03-15T10:00:00Z","author":"test"}}\n'
            '{"action":"add_node","data":{"id":"b","type":"fail","summary":"B","detail":"D","context":[],"references":[],"severity":null,"timestamp":"2026-03-15T10:01:00Z","author":"test"}}\n'
            '{"action":"add_edge","data":{"from_id":"a","to_id":"b","edge_type":"relates_to","timestamp":"t1"}}\n'
            '{"action":"add_edge","data":{"from_id":"a","to_id":"b","edge_type":"led_to","timestamp":"t2"}}\n',
            encoding="utf-8",
        )

        storage = CognitionStorage(cog_dir)
        all_succ = storage.get_successors("a")
        assert len(all_succ) == 2  # Both survive
        types = {s[1]["type"] for s in all_succ}
        assert types == {"relates_to", "led_to"}


class TestRemoveEdge:
    """Tests for remove_edge operation."""

    @pytest.fixture
    def storage(self, tmp_path):
        return CognitionStorage(tmp_path / ".cognition")

    def test_remove_specific_edge_type(self, storage):
        """Remove a specific edge type between two nodes."""
        storage.add_node(_make_node("a"))
        storage.add_node(_make_node("b"))
        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
        ))
        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.PART_OF,
            timestamp="2026-03-15T10:01:00Z",
        ))

        result = storage.remove_edge("a", "b", CognitionEdgeType.LED_TO)
        assert result is True

        # part_of should remain
        succ = storage.get_successors("a")
        assert len(succ) == 1
        assert succ[0][1]["type"] == "part_of"

    def test_remove_all_edges_between_pair(self, storage):
        """Remove all edges between two nodes when no type specified."""
        storage.add_node(_make_node("a"))
        storage.add_node(_make_node("b"))
        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
        ))
        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.PART_OF,
            timestamp="2026-03-15T10:01:00Z",
        ))

        result = storage.remove_edge("a", "b")
        assert result is True

        succ = storage.get_successors("a")
        assert len(succ) == 0

    def test_remove_nonexistent_edge(self, storage):
        """Removing an edge that doesn't exist returns False."""
        storage.add_node(_make_node("a"))
        storage.add_node(_make_node("b"))

        result = storage.remove_edge("a", "b", CognitionEdgeType.LED_TO)
        assert result is False

    def test_remove_wrong_type(self, storage):
        """Removing an edge type that doesn't exist between the pair returns False."""
        storage.add_node(_make_node("a"))
        storage.add_node(_make_node("b"))
        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
        ))

        result = storage.remove_edge("a", "b", CognitionEdgeType.PART_OF)
        assert result is False

        # Original edge untouched
        succ = storage.get_successors("a", CognitionEdgeType.LED_TO)
        assert len(succ) == 1

    def test_remove_edge_persisted_in_journal(self, tmp_path):
        """remove_edge action is written to JSONL."""
        cog_dir = tmp_path / ".cognition"
        storage = CognitionStorage(cog_dir)
        storage.add_node(_make_node("a"))
        storage.add_node(_make_node("b"))
        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
        ))
        storage.remove_edge("a", "b", CognitionEdgeType.LED_TO)

        journal = (cog_dir / "journal.jsonl").read_text(encoding="utf-8")
        lines = [json.loads(l) for l in journal.strip().split("\n") if l]
        remove_entries = [l for l in lines if l["action"] == "remove_edge"]
        assert len(remove_entries) == 1
        assert remove_entries[0]["data"]["from_id"] == "a"
        assert remove_entries[0]["data"]["to_id"] == "b"
        assert remove_entries[0]["data"]["edge_type"] == "led_to"

    def test_remove_edge_survives_hydration(self, tmp_path):
        """Edge removed via JSONL is gone after re-hydration."""
        cog_dir = tmp_path / ".cognition"
        s1 = CognitionStorage(cog_dir)
        s1.add_node(_make_node("a"))
        s1.add_node(_make_node("b"))
        s1.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
        ))
        s1.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.PART_OF,
            timestamp="2026-03-15T10:01:00Z",
        ))
        s1.remove_edge("a", "b", CognitionEdgeType.LED_TO)

        # Re-hydrate
        s2 = CognitionStorage(cog_dir)
        succ = s2.get_successors("a")
        assert len(succ) == 1
        assert succ[0][1]["type"] == "part_of"


class TestRedirectEdgesMultiDiGraph:
    """Tests for redirect_edges with MultiDiGraph."""

    def test_redirect_preserves_parallel_edges(self, tmp_path):
        """Both edge types from old node are redirected to new node."""
        storage = CognitionStorage(tmp_path / ".cognition")
        storage.add_node(_make_node("old"))
        storage.add_node(_make_node("new"))
        storage.add_node(_make_node("target"))

        storage.add_edge(CognitionEdge(
            from_id="old", to_id="target",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
        ))
        storage.add_edge(CognitionEdge(
            from_id="old", to_id="target",
            edge_type=CognitionEdgeType.PART_OF,
            timestamp="2026-03-15T10:01:00Z",
        ))

        redirected = storage.redirect_edges("old", "new")
        assert redirected == 2

        # Both types should exist on new -> target
        succ = storage.get_successors("new")
        assert len(succ) == 2
        types = {s[1]["type"] for s in succ}
        assert types == {"led_to", "part_of"}


class TestGetSupersededChainMultiDiGraph:
    """Tests for get_superseded_chain with MultiDiGraph."""

    def test_linear_chain_still_works(self, tmp_path):
        """Standard supersedes chain works correctly."""
        storage = CognitionStorage(tmp_path / ".cognition")
        storage.add_node(_make_node("v2", timestamp="2026-03-15T12:00:00Z"))
        storage.add_node(_make_node("v1", timestamp="2026-03-14T12:00:00Z"))

        storage.add_edge(CognitionEdge(
            from_id="v2", to_id="v1",
            edge_type=CognitionEdgeType.SUPERSEDES,
            timestamp="2026-03-15T12:00:00Z",
        ))

        chain = get_superseded_chain(storage, "v2")
        assert len(chain) == 2
        assert chain[0]["id"] == "v2"
        assert chain[1]["id"] == "v1"

    def test_supersedes_with_parallel_edge(self, tmp_path):
        """Supersedes chain works even when pair has other edge types."""
        storage = CognitionStorage(tmp_path / ".cognition")
        storage.add_node(_make_node("v2", timestamp="2026-03-15T12:00:00Z"))
        storage.add_node(_make_node("v1", timestamp="2026-03-14T12:00:00Z"))

        storage.add_edge(CognitionEdge(
            from_id="v2", to_id="v1",
            edge_type=CognitionEdgeType.SUPERSEDES,
            timestamp="2026-03-15T12:00:00Z",
        ))
        # Also add a led_to edge between the same pair
        storage.add_edge(CognitionEdge(
            from_id="v2", to_id="v1",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T12:01:00Z",
        ))

        chain = get_superseded_chain(storage, "v2")
        assert len(chain) == 2  # Still a 2-node chain, not confused by led_to
