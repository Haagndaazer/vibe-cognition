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
