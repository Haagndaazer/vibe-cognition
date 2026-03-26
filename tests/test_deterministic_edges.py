"""Tests for Phase 1: deterministic part_of matching, provenance, and related changes."""

import json

import pytest

from vibe_cognition.cognition import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    CognitionStorage,
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


class TestReferenceIndex:
    """Tests for the reference index in CognitionStorage."""

    @pytest.fixture
    def storage(self, tmp_path):
        return CognitionStorage(tmp_path / ".cognition")

    def test_index_populated_on_add_node(self, storage):
        """References are indexed when a node is added."""
        node = _make_node("n1", references=["commit:abc123", "issue:LL-298"])
        storage.add_node(node)

        assert "n1" in storage._reference_index["commit:abc123"]
        assert "n1" in storage._reference_index["issue:ll-298"]  # normalized lowercase

    def test_index_cleared_on_remove_node(self, storage):
        """References are unindexed when a node is removed."""
        node = _make_node("n1", references=["commit:abc123"])
        storage.add_node(node)
        assert "n1" in storage._reference_index["commit:abc123"]

        storage.remove_node("n1")
        assert "n1" not in storage._reference_index.get("commit:abc123", [])

    def test_index_populated_during_hydration(self, tmp_path):
        """References are indexed when hydrating from JSONL."""
        cog_dir = tmp_path / ".cognition"
        s1 = CognitionStorage(cog_dir)
        s1.add_node(_make_node("n1", references=["commit:abc123"]))
        s1.add_node(_make_node("n2", references=["commit:abc123", "pr:42"]))

        # Re-hydrate
        s2 = CognitionStorage(cog_dir)
        assert "n1" in s2._reference_index["commit:abc123"]
        assert "n2" in s2._reference_index["commit:abc123"]
        assert "n2" in s2._reference_index["pr:42"]

    def test_commit_short_prefix_indexed(self, storage):
        """Long commit SHAs also get a short prefix entry."""
        full_sha = "abc1234def5678901234567890abcdef12345678"
        node = _make_node("n1", references=[f"commit:{full_sha}"])
        storage.add_node(node)

        assert "n1" in storage._reference_index[f"commit:{full_sha}"]
        assert "n1" in storage._reference_index["commit:abc1234"]  # 7-char prefix

    def test_normalization_case_insensitive(self, storage):
        """References are normalized to lowercase."""
        storage.add_node(_make_node("n1", references=["Issue:LL-298"]))
        assert "n1" in storage._reference_index["issue:ll-298"]

    def test_normalization_strips_whitespace(self, storage):
        """References have whitespace stripped."""
        storage.add_node(_make_node("n1", references=["  commit:abc123  "]))
        assert "n1" in storage._reference_index["commit:abc123"]


class TestDeterministicEdges:
    """Tests for create_deterministic_edges."""

    @pytest.fixture
    def storage(self, tmp_path):
        return CognitionStorage(tmp_path / ".cognition")

    def test_entity_then_episode_creates_edge(self, storage):
        """Recording entity first, then episode creates part_of edge."""
        storage.add_node(_make_node(
            "dec1", CognitionNodeType.DECISION,
            references=["commit:abc123"],
        ))
        storage.add_node(_make_node(
            "ep1", CognitionNodeType.EPISODE,
            references=["commit:abc123"],
        ))

        created = storage.create_deterministic_edges("ep1")
        assert created == 1

        # Edge should be entity -> episode
        successors = storage.get_successors("dec1", CognitionEdgeType.PART_OF)
        assert len(successors) == 1
        assert successors[0][0] == "ep1"

    def test_episode_then_entity_creates_edge(self, storage):
        """Recording episode first, then entity creates part_of edge."""
        storage.add_node(_make_node(
            "ep1", CognitionNodeType.EPISODE,
            references=["commit:abc123"],
        ))
        storage.add_node(_make_node(
            "dec1", CognitionNodeType.DECISION,
            references=["commit:abc123"],
        ))

        created = storage.create_deterministic_edges("dec1")
        assert created == 1

        # Edge should be entity -> episode
        successors = storage.get_successors("dec1", CognitionEdgeType.PART_OF)
        assert len(successors) == 1
        assert successors[0][0] == "ep1"

    def test_multiple_entities_same_commit(self, storage):
        """Multiple entities sharing a commit all link to the episode."""
        storage.add_node(_make_node(
            "dec1", CognitionNodeType.DECISION, references=["commit:abc123"],
        ))
        storage.add_node(_make_node(
            "disc1", CognitionNodeType.DISCOVERY, references=["commit:abc123"],
        ))
        storage.add_node(_make_node(
            "ep1", CognitionNodeType.EPISODE, references=["commit:abc123"],
        ))

        created = storage.create_deterministic_edges("ep1")
        assert created == 2

        # Both entities should point to episode
        preds = storage.get_predecessors("ep1", CognitionEdgeType.PART_OF)
        pred_ids = {p[0] for p in preds}
        assert pred_ids == {"dec1", "disc1"}

    def test_no_edge_between_two_entities(self, storage):
        """Two entities with same ref but no episode → no part_of edge."""
        storage.add_node(_make_node(
            "dec1", CognitionNodeType.DECISION, references=["commit:abc123"],
        ))
        storage.add_node(_make_node(
            "disc1", CognitionNodeType.DISCOVERY, references=["commit:abc123"],
        ))

        created = storage.create_deterministic_edges("disc1")
        assert created == 0

    def test_no_edge_between_two_episodes(self, storage):
        """Two episodes with same ref → no part_of edge."""
        storage.add_node(_make_node(
            "ep1", CognitionNodeType.EPISODE, references=["commit:abc123"],
        ))
        storage.add_node(_make_node(
            "ep2", CognitionNodeType.EPISODE, references=["commit:abc123"],
        ))

        created = storage.create_deterministic_edges("ep2")
        assert created == 0

    def test_no_duplicate_edges(self, storage):
        """Running create_deterministic_edges twice doesn't create duplicates."""
        storage.add_node(_make_node(
            "dec1", CognitionNodeType.DECISION, references=["commit:abc123"],
        ))
        storage.add_node(_make_node(
            "ep1", CognitionNodeType.EPISODE, references=["commit:abc123"],
        ))

        created1 = storage.create_deterministic_edges("ep1")
        created2 = storage.create_deterministic_edges("ep1")
        assert created1 == 1
        assert created2 == 0

    def test_creates_part_of_even_with_different_edge_type(self, storage):
        """With MultiDiGraph, a led_to edge does NOT block part_of creation."""
        storage.add_node(_make_node(
            "dec1", CognitionNodeType.DECISION, references=["commit:abc123"],
        ))
        storage.add_node(_make_node(
            "ep1", CognitionNodeType.EPISODE, references=["commit:abc123"],
        ))
        # Pre-existing led_to edge
        storage.add_edge(CognitionEdge(
            from_id="dec1", to_id="ep1",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
        ))

        created = storage.create_deterministic_edges("ep1")
        assert created == 1  # part_of created alongside existing led_to

        # Both edges should exist
        all_successors = storage.get_successors("dec1")
        assert len(all_successors) == 2
        types = {s[1]["type"] for s in all_successors}
        assert types == {"led_to", "part_of"}

    def test_issue_ref_matching(self, storage):
        """part_of matching works with issue references, not just commits."""
        storage.add_node(_make_node(
            "dec1", CognitionNodeType.DECISION, references=["issue:LL-298"],
        ))
        storage.add_node(_make_node(
            "ep1", CognitionNodeType.EPISODE, references=["issue:LL-298"],
        ))

        created = storage.create_deterministic_edges("ep1")
        assert created == 1

    def test_no_refs_returns_zero(self, storage):
        """Node with no references creates no edges."""
        storage.add_node(_make_node("dec1", CognitionNodeType.DECISION))
        created = storage.create_deterministic_edges("dec1")
        assert created == 0

    def test_nonexistent_node_returns_zero(self, storage):
        """Calling with nonexistent node ID returns 0."""
        created = storage.create_deterministic_edges("nonexistent")
        assert created == 0

    def test_edge_has_deterministic_source(self, storage):
        """Deterministic edges have source='deterministic'."""
        storage.add_node(_make_node(
            "dec1", CognitionNodeType.DECISION, references=["commit:abc123"],
        ))
        storage.add_node(_make_node(
            "ep1", CognitionNodeType.EPISODE, references=["commit:abc123"],
        ))

        storage.create_deterministic_edges("ep1")

        successors = storage.get_successors("dec1", CognitionEdgeType.PART_OF)
        assert len(successors) == 1
        assert successors[0][1].get("source") == "deterministic"


class TestProvenanceField:
    """Tests for the source provenance field on CognitionEdge."""

    def test_edge_default_source(self):
        """CognitionEdge defaults to source='curator'."""
        edge = CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
        )
        assert edge.source == "curator"

    def test_edge_custom_source(self):
        """CognitionEdge accepts custom source."""
        edge = CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.PART_OF,
            timestamp="2026-03-15T10:00:00Z",
            source="deterministic",
        )
        assert edge.source == "deterministic"

    def test_source_persisted_in_journal(self, tmp_path):
        """Source field is written to JSONL."""
        cog_dir = tmp_path / ".cognition"
        storage = CognitionStorage(cog_dir)
        storage.add_node(_make_node("a"))
        storage.add_node(_make_node("b"))

        edge = CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
            source="manual",
        )
        storage.add_edge(edge)

        journal = (cog_dir / "journal.jsonl").read_text(encoding="utf-8")
        for line in journal.strip().split("\n"):
            entry = json.loads(line)
            if entry["action"] == "add_edge":
                assert entry["data"]["source"] == "manual"

    def test_source_survives_hydration(self, tmp_path):
        """Source field is preserved through JSONL hydration."""
        cog_dir = tmp_path / ".cognition"
        s1 = CognitionStorage(cog_dir)
        s1.add_node(_make_node("a"))
        s1.add_node(_make_node("b"))
        s1.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
            source="manual",
        ))

        # Re-hydrate
        s2 = CognitionStorage(cog_dir)
        successors = s2.get_successors("a", CognitionEdgeType.LED_TO)
        assert len(successors) == 1
        assert successors[0][1].get("source") == "manual"

    def test_old_journal_without_source_defaults_to_curator(self, tmp_path):
        """JSONL entries without source field default to 'curator' on hydration."""
        cog_dir = tmp_path / ".cognition"
        cog_dir.mkdir(parents=True)

        journal = cog_dir / "journal.jsonl"
        # Write old-format entries (no source field)
        journal.write_text(
            '{"action":"add_node","data":{"id":"a","type":"decision","summary":"A","detail":"D","context":[],"references":[],"severity":null,"timestamp":"2026-03-15T10:00:00Z","author":"test"}}\n'
            '{"action":"add_node","data":{"id":"b","type":"fail","summary":"B","detail":"D","context":[],"references":[],"severity":null,"timestamp":"2026-03-15T10:01:00Z","author":"test"}}\n'
            '{"action":"add_edge","data":{"from_id":"a","to_id":"b","edge_type":"led_to","timestamp":"2026-03-15T10:02:00Z"}}\n',
            encoding="utf-8",
        )

        storage = CognitionStorage(cog_dir)
        successors = storage.get_successors("a", CognitionEdgeType.LED_TO)
        assert len(successors) == 1
        assert successors[0][1].get("source") == "curator"  # default


class TestReplayEntryMutationFix:
    """Test that _replay_entry no longer mutates data dicts."""

    def test_update_node_data_not_mutated(self, tmp_path):
        """Verify that update_node replay doesn't pop 'id' from the data dict."""
        cog_dir = tmp_path / ".cognition"
        cog_dir.mkdir(parents=True)

        journal = cog_dir / "journal.jsonl"
        journal.write_text(
            '{"action":"add_node","data":{"id":"n1","type":"decision","summary":"Original","detail":"D","context":[],"references":[],"severity":null,"timestamp":"2026-03-15T10:00:00Z","author":"test"}}\n'
            '{"action":"update_node","data":{"id":"n1","summary":"Updated"}}\n',
            encoding="utf-8",
        )

        storage = CognitionStorage(cog_dir)
        node = storage.get_node("n1")
        assert node is not None
        assert node["summary"] == "Updated"

        # Verify the journal data wasn't mutated by reading it back
        with open(journal, encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        update_entry = lines[1]
        assert "id" in update_entry["data"]  # Should NOT have been popped


class TestEdgeTypeStatistics:
    """Tests for edge-type breakdown in get_statistics."""

    def test_edge_type_counts(self, tmp_path):
        """Statistics include per-edge-type counts."""
        storage = CognitionStorage(tmp_path / ".cognition")
        storage.add_node(_make_node("a"))
        storage.add_node(_make_node("b"))
        storage.add_node(_make_node("c"))

        storage.add_edge(CognitionEdge(
            from_id="a", to_id="b",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:00:00Z",
        ))
        storage.add_edge(CognitionEdge(
            from_id="b", to_id="c",
            edge_type=CognitionEdgeType.LED_TO,
            timestamp="2026-03-15T10:01:00Z",
        ))
        storage.add_edge(CognitionEdge(
            from_id="a", to_id="c",
            edge_type=CognitionEdgeType.RESOLVED_BY,
            timestamp="2026-03-15T10:02:00Z",
        ))

        stats = storage.get_statistics()
        assert stats["edges"] == 3
        assert stats["edge_led_to"] == 2
        assert stats["edge_resolved_by"] == 1
        assert stats["edge_part_of"] == 0
