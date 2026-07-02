"""WP-Core-tail (P3 core robustness): C-4 journal-first writes, C-6 self-replay
offset/log noise, C-7 get_reasoning_chain diamond-vs-cycle.

Each test names the specific failure mode it guards (rule 20) and is written to
fail before its fix exists (rule 12)."""

import pytest

from vibe_cognition.cognition import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    CognitionStorage,
    get_reasoning_chain,
)


def _node(node_id, *, summary="s", detail="d"):
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DECISION, summary=summary, detail=detail,
        context=["ctx"], references=["commit:abc"], severity=None,
        timestamp="2026-06-13T00:00:00+00:00", author="t",
    )


def _edge(a, b, et=CognitionEdgeType.LED_TO):
    return CognitionEdge(
        from_id=a, to_id=b, edge_type=et, timestamp="2026-06-13T00:00:00+00:00", source="manual",
    )


class _BoomError(RuntimeError):
    pass


def _raise(*_a, **_k):
    raise _BoomError("journal append failed (simulated disk-full / AV lock)")


# --- C-4: journal-FIRST, no phantom mutation on append failure ---------------
# CRITICAL (review #14): assert against the RAW in-memory graph (storage.graph /
# storage._reference_index), NOT a _synced accessor. A synced read runs _catch_up,
# which cannot see the un-journaled phantom (the failed append wrote nothing), so it
# would HIDE the phantom and tautologize the guard. The phantom lives only in the
# unsynced graph — assert there.

def test_add_node_append_failure_leaves_no_phantom(tmp_path, monkeypatch):
    """If _append_journal raises, add_node must mutate NOTHING — no phantom node in
    the in-memory graph (invisible to other processes, lost on next re-hydrate) and
    no orphan reference-index entry. Fails-before (mutate-first): the node + ref index
    are written before the append raises."""
    s = CognitionStorage(tmp_path / ".cognition")
    monkeypatch.setattr(s, "_append_journal", _raise)

    with pytest.raises(_BoomError):
        s.add_node(_node("n1"))

    assert not s.graph.has_node("n1"), "phantom node survived a failed journal append"
    assert "n1" not in s._reference_index.get("commit:abc", []), "orphan ref-index entry"


def test_add_edge_append_failure_leaves_no_phantom(tmp_path, monkeypatch):
    """A failed append on add_edge must leave no phantom edge."""
    s = CognitionStorage(tmp_path / ".cognition")
    s.add_node(_node("a"))
    s.add_node(_node("b"))
    monkeypatch.setattr(s, "_append_journal", _raise)

    with pytest.raises(_BoomError):
        s.add_edge(_edge("a", "b"))

    assert not s.graph.has_edge("a", "b"), "phantom edge survived a failed journal append"


def test_update_node_append_failure_leaves_no_mutation(tmp_path, monkeypatch):
    """A failed append on update_node must leave the field at its original value."""
    s = CognitionStorage(tmp_path / ".cognition")
    s.add_node(_node("n1", summary="orig"))
    monkeypatch.setattr(s, "_append_journal", _raise)

    with pytest.raises(_BoomError):
        s.update_node("n1", summary="changed")

    assert s.graph.nodes["n1"]["summary"] == "orig", "phantom field mutation survived a failed append"


def test_remove_node_append_failure_leaves_node_present(tmp_path, monkeypatch):
    """A failed append on remove_node must leave the node (and its ref index) present."""
    s = CognitionStorage(tmp_path / ".cognition")
    s.add_node(_node("n1"))
    monkeypatch.setattr(s, "_append_journal", _raise)

    with pytest.raises(_BoomError):
        s.remove_node("n1")

    assert s.graph.has_node("n1"), "node removed despite a failed journal append"
    assert "n1" in s._reference_index.get("commit:abc", []), "ref index dropped despite a failed append"


def test_remove_edge_single_append_failure_leaves_edge_present(tmp_path, monkeypatch):
    """remove_edge (single, keyed) must not phantom-remove on a failed append — the
    edge stays in the raw graph. Fails-before (mutate-first): the edge is removed
    before the append raises."""
    s = CognitionStorage(tmp_path / ".cognition")
    s.add_node(_node("a"))
    s.add_node(_node("b"))
    s.add_edge(_edge("a", "b"))
    monkeypatch.setattr(s, "_append_journal", _raise)

    with pytest.raises(_BoomError):
        s.remove_edge("a", "b", CognitionEdgeType.LED_TO)

    assert s.graph.has_edge("a", "b"), "phantom edge removal survived a failed append"


def test_remove_edge_remove_all_append_failure_on_first_leaves_all(tmp_path, monkeypatch):
    """remove_edge (remove-ALL loop, edge_type=None) with the append raising on the
    FIRST iteration must remove NOTHING — both edges between the pair stay. Fails-before
    (mutate-first): the first edge is removed before its append raises, so one is gone."""
    s = CognitionStorage(tmp_path / ".cognition")
    s.add_node(_node("a"))
    s.add_node(_node("b"))
    s.add_edge(_edge("a", "b", CognitionEdgeType.LED_TO))
    s.add_edge(_edge("a", "b", CognitionEdgeType.RELATES_TO))
    monkeypatch.setattr(s, "_append_journal", _raise)  # raises on the first iteration

    with pytest.raises(_BoomError):
        s.remove_edge("a", "b")  # no edge_type → remove-all loop

    assert s.graph.has_edge("a", "b", key=CognitionEdgeType.LED_TO.value), "first edge phantom-removed"
    assert s.graph.has_edge("a", "b", key=CognitionEdgeType.RELATES_TO.value), "second edge phantom-removed"


# --- C-4: journal-first still replays + converges, idempotently --------------

def test_add_node_journal_first_visible_to_replay_and_idempotent(tmp_path):
    """A successful journal-first add is durable (a second instance replaying the
    journal sees it) AND idempotent on this process's own next catch-up: re-reading
    its own appended line does not duplicate the node or its ref-index entry."""
    cog = tmp_path / ".cognition"
    s1 = CognitionStorage(cog)
    s1.add_node(_node("n1"))

    s2 = CognitionStorage(cog)  # fresh replay of the same journal
    assert s2.get_node("n1") is not None, "journal-first write not durable across replay"

    # s1's offset is behind its own append; a synced op forces it to re-read + replay.
    s1.get_all_nodes()
    assert sum(1 for n in s1.get_all_nodes() if n["id"] == "n1") == 1, "self-replay duplicated the node"
    assert s1._reference_index["commit:abc"].count("n1") == 1, "self-replay duplicated the ref-index entry"


def test_remove_node_journal_first_replay_does_not_resurrect(tmp_path):
    """add then remove (both journal-first), then force self-replay: re-reading
    [add n1][remove n1] converges to no n1 (the remove replay is a guarded no-op on
    the second pass; the sequence still nets to removed). A fresh replay agrees."""
    cog = tmp_path / ".cognition"
    s1 = CognitionStorage(cog)
    s1.add_node(_node("n1"))
    s1.remove_node("n1")

    s1.get_all_nodes()  # forces catch_up to re-read its own [add][remove] lines
    assert not s1.has_node("n1"), "tombstone resurrected on self-replay"

    s2 = CognitionStorage(cog)
    assert not s2.has_node("n1"), "fresh replay disagrees on the removal"


# --- C-6: self-replay is stable; appends deliberately don't advance the offset ---

def test_c6_steady_state_self_replay_no_false_rehydrate(tmp_path):
    """C-6 (guard, no behavioral delta): because appends don't advance the offset, a
    write + forced self-catch_up re-reads our own line idempotently. In STEADY STATE
    (offset > 0) this must NOT trigger a spurious re-hydrate, must land the offset at
    EOF, must keep the C-3 prefix-hash matching the whole file, and must not dup nodes.

    SINGLE-PROCESS ONLY: `_offset == file size` holds because there is no concurrent
    appender. Do NOT strengthen this into a cross-process test — the whole C-6 point
    is that offset != our-bytes under concurrency."""
    import hashlib

    cog = tmp_path / ".cognition"
    s = CognitionStorage(cog)
    s.add_node(_node("n1"))
    s.get_all_nodes()  # first catch_up from offset 0 (rehydrate-from-top) → offset = EOF

    graph_obj = s.graph  # identity sentinel: _rehydrate_reset() would replace it
    s.add_node(_node("n2"))  # second append; offset now behind this line
    s.get_all_nodes()  # STEADY-STATE self-replay (offset > 0, prefix-hash path)

    assert s.graph is graph_obj, "steady-state self-replay triggered a spurious re-hydrate"
    data = s._journal_path.read_bytes()
    assert s._offset == len(data), "offset did not reach EOF after steady-state self-replay"
    assert s._journal_hasher.digest() == hashlib.sha256(data).digest(), "C-3 prefix-hash diverged"
    assert s.graph.number_of_nodes() == 2, "self-replay duplicated or dropped a node"


# --- C-7: get_reasoning_chain — diamonds are not cycles ----------------------

def _child(node, child_id):
    for c in node.get("chain", []):
        if c["id"] == child_id:
            return c
    return None


def test_c7_diamond_reconvergence_not_flagged_as_cycle(tmp_path):
    """A diamond A→B→D and A→C→D is a re-convergent DAG, NOT a cycle. With path-based
    detection both arms' D must be expanded with cycle=False. Fails-before (global
    `visited`, never popped): whichever arm is traversed second sees D already visited
    and flags it cycle=true."""
    s = CognitionStorage(tmp_path / ".cognition")
    for nid in ("A", "B", "C", "D"):
        s.add_node(_node(nid))
    s.add_edge(_edge("A", "B"))
    s.add_edge(_edge("A", "C"))
    s.add_edge(_edge("B", "D"))
    s.add_edge(_edge("C", "D"))

    tree = get_reasoning_chain(s, "A", max_depth=5, direction="outgoing")
    b, c = _child(tree, "B"), _child(tree, "C")
    assert b is not None and c is not None
    d_under_b, d_under_c = _child(b, "D"), _child(c, "D")
    assert d_under_b is not None and d_under_c is not None, "D missing on a diamond arm"
    assert d_under_b["cycle"] is False and d_under_c["cycle"] is False, (
        "diamond reconvergence falsely flagged as a cycle"
    )


def test_c7_true_cycle_still_flagged(tmp_path):
    """A→B→A is a real cycle — the A reached under B (an ancestor on the path) must
    still be cycle=true. Regression guard (green before and after the fix)."""
    s = CognitionStorage(tmp_path / ".cognition")
    for nid in ("A", "B"):
        s.add_node(_node(nid))
    s.add_edge(_edge("A", "B"))
    s.add_edge(_edge("B", "A"))

    tree = get_reasoning_chain(s, "A", max_depth=5, direction="outgoing")
    b = _child(tree, "B")
    assert b is not None
    a_under_b = _child(b, "A")
    assert a_under_b is not None and a_under_b["cycle"] is True, "true cycle no longer flagged"


def test_c7_truncation_past_max_depth(tmp_path):
    """Depth truncation is independent of cycle detection — a chain deeper than
    max_depth still truncates. Regression guard."""
    s = CognitionStorage(tmp_path / ".cognition")
    ids = ["A", "B", "C", "D"]
    for nid in ids:
        s.add_node(_node(nid))
    for a, b in zip(ids, ids[1:], strict=False):
        s.add_edge(_edge(a, b))

    tree = get_reasoning_chain(s, "A", max_depth=2, direction="outgoing")
    node = tree
    for _ in range(3):  # A(0) → B(1) → C(2) → D(3, beyond max_depth)
        assert node.get("chain"), f"chain ended early at {node['id']}"
        node = node["chain"][0]
    assert node["id"] == "D" and node["truncated"] is True, "deep node not truncated past max_depth"


# --- Composition: the whole reordered write surface round-trips through replay ---

def test_composition_all_reordered_writes_round_trip_through_replay(tmp_path):
    """Exercise every journal-first write path on one instance, then rebuild a second
    instance from the same journal — nodes and edge-keys must match exactly, and the
    update must survive. Proves the C-4 reorder preserved replay-equivalence across the
    whole write surface (add_node, add_edge, update_node, remove_edge single, remove_node)
    — not just per-op in isolation. (redirect_edges was retired in WP-14 and dropped from
    this composition — it had zero production callers.)"""
    cog = tmp_path / ".cognition"
    s1 = CognitionStorage(cog)
    s1.add_node(_node("a"))
    s1.add_node(_node("b"))
    s1.add_node(_node("c"))
    s1.add_edge(_edge("a", "b"))
    s1.add_edge(_edge("a", "c"))
    s1.update_node("a", summary="updated")
    s1.remove_edge("a", "c", CognitionEdgeType.LED_TO)  # single-edge removal
    s1.remove_node("b")

    s2 = CognitionStorage(cog)  # fresh replay of the same journal

    assert {n["id"] for n in s1.get_all_nodes()} == {n["id"] for n in s2.get_all_nodes()}, (
        "replay diverged on nodes"
    )
    assert set(s1.graph.edges(keys=True)) == set(s2.graph.edges(keys=True)), (
        "replay diverged on edges"
    )
    a = s2.get_node("a")
    assert a is not None and a["summary"] == "updated", "update_node did not survive replay"
