"""WP-TC15: curation-containment observability. get_statistics gains an
edge_sources histogram and edges_outside_curation count -- derived at read
time from the SAME edge-iteration pass that already computes per-edge-type
counts, no new persistence or write-path changes. Each test names the
specific failure mode it guards and, where the brief flags it fails-before,
is written to fail before its fix exists.
"""

import json

from vibe_cognition.cognition import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    CognitionStorage,
)


def _node(node_id: str, timestamp: str = "2026-01-01T00:00:00+00:00") -> CognitionNode:
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DECISION, summary=f"s-{node_id}",
        detail="d", context=[], timestamp=timestamp, author="t",
    )


def _edge(from_id: str, to_id: str, source: str, *, edge_type: CognitionEdgeType = CognitionEdgeType.LED_TO,
          timestamp: str = "2026-01-01T00:00:02+00:00") -> CognitionEdge:
    return CognitionEdge(from_id=from_id, to_id=to_id, edge_type=edge_type, timestamp=timestamp, source=source)


# ── edge_sources histogram + edges_outside_curation count ──────────────────


def test_zero_edge_graph_reports_both_keys_empty(tmp_path):
    """Both keys are always present, even with no edges at all -- no
    absent-key ambiguity."""
    storage = CognitionStorage(tmp_path / ".cognition")
    storage.add_node(_node("a"))

    stats = storage.get_statistics()
    assert stats["edge_sources"] == {}
    assert stats["edges_outside_curation"] == 0


def test_histogram_counts_every_known_source_exactly(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    ids = ["a", "b", "c", "d", "e", "f", "g", "h"]
    for i in ids:
        storage.add_node(_node(i))

    sources = [
        "deterministic", "task-parent", "curate-skill", "curate-conflict",
        "curate-cluster", "curator", "manual", "batch",
    ]
    for i, src in enumerate(sources):
        storage.add_edge(_edge(ids[i], ids[(i + 1) % len(ids)], src))

    stats = storage.get_statistics()
    for src in sources:
        assert stats["edge_sources"][src] == 1


def test_unknown_source_appears_in_histogram_and_is_counted(tmp_path):
    """Conservative-by-construction: an unrecognized source value is neither
    silently dropped from the histogram nor silently exempted from the
    count."""
    storage = CognitionStorage(tmp_path / ".cognition")
    storage.add_node(_node("a"))
    storage.add_node(_node("b"))
    storage.add_edge(_edge("a", "b", "mystery-tool"))

    stats = storage.get_statistics()
    assert stats["edge_sources"]["mystery-tool"] == 1
    assert stats["edges_outside_curation"] == 1


def test_manual_and_batch_are_counted_fails_before(tmp_path):
    """cognition_add_edge's default source ("manual") and
    cognition_add_edges_batch's default ("batch") are exactly the writes this
    counter exists to catch. Fails-before: must fail if the outside-curation
    count is computed as 0 regardless of source (i.e. the check is missing
    entirely)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    storage.add_node(_node("a"))
    storage.add_node(_node("b"))
    storage.add_node(_node("c"))
    storage.add_edge(_edge("a", "b", "manual"))
    storage.add_edge(_edge("b", "c", "batch", edge_type=CognitionEdgeType.RELATES_TO))

    stats = storage.get_statistics()
    assert stats["edges_outside_curation"] == 2


def test_curation_legitimate_sources_are_never_counted_fails_before(tmp_path):
    """deterministic/task-parent/curate-skill/curate-conflict/curate-cluster/
    curator must all be exempt. Fails-before: must fail if any legitimate
    source is miscounted as outside-curation."""
    storage = CognitionStorage(tmp_path / ".cognition")
    ids = ["a", "b", "c", "d", "e", "f", "g"]
    for i in ids:
        storage.add_node(_node(i))

    exempt_sources = [
        "deterministic", "task-parent", "curate-skill", "curate-conflict",
        "curate-cluster", "curator",
    ]
    for i, src in enumerate(exempt_sources):
        storage.add_edge(_edge(ids[i], ids[i + 1], src))

    stats = storage.get_statistics()
    assert stats["edges_outside_curation"] == 0


def test_missing_source_in_replayed_journal_falls_back_to_curator_not_counted(tmp_path):
    """Legacy journals predate the source field entirely. The existing replay
    fallback (storage.py's _catch_up 'add_edge' branch, data.get("source",
    "curator")) must keep such edges exempt -- fails-before: this test must
    fail if a genuinely-missing source were counted as outside-curation
    instead of falling back to the legacy "curator" default."""
    cog_dir = tmp_path / ".cognition"
    storage1 = CognitionStorage(cog_dir)
    storage1.add_node(_node("a", "2026-01-01T00:00:00+00:00"))
    storage1.add_node(_node("b", "2026-01-01T00:00:01+00:00"))
    storage1.add_edge(_edge("a", "b", "curator"))  # written source, will be stripped below

    # Strip the "source" key entirely from the persisted add_edge line --
    # simulating a genuinely pre-source-field legacy journal, not merely the
    # "curator" default VALUE (which the model already writes and would not
    # exercise the missing-key fallback at all).
    journal_path = cog_dir / "journal.jsonl"
    rewritten = []
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry.get("action") == "add_edge":
            entry["data"].pop("source", None)
        rewritten.append(json.dumps(entry))
    journal_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    storage2 = CognitionStorage(cog_dir)  # fresh instance forces replay from journal
    stats = storage2.get_statistics()
    assert stats["edge_sources"] == {"curator": 1}
    assert stats["edges_outside_curation"] == 0


def test_multiple_edges_same_unknown_source_aggregate_in_histogram(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    storage.add_node(_node("a"))
    storage.add_node(_node("b"))
    storage.add_node(_node("c"))
    storage.add_edge(_edge("a", "b", "manual"))
    storage.add_edge(_edge("b", "c", "manual", edge_type=CognitionEdgeType.RELATES_TO))

    stats = storage.get_statistics()
    assert stats["edge_sources"]["manual"] == 2
    assert stats["edges_outside_curation"] == 2


# ── orchestrator Step 4 conflation repair (textual gate check) ─────────────


def test_orchestrator_step4_sets_curate_cluster_source():
    """agents/curate-orchestrator.md Step 4's part_of edges must carry
    source="curate-cluster" (mirroring Step 2/3's own source lines) --
    without it, the very next cluster-producing curation run would
    false-positive as an outside-curation write."""
    from pathlib import Path

    md_path = Path(__file__).resolve().parents[1] / "agents" / "curate-orchestrator.md"
    text = md_path.read_text(encoding="utf-8")
    step4 = text.split("## Step 4:", 1)[1].split("## Embedded analyzer protocol", 1)[0]
    assert '"source": "curate-cluster"' in step4
