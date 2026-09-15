"""Per-user journal shards (docs/wp-journal-shards-plan.md rev 4).

Each person appends only to .cognition/journal/<email>.jsonl; the graph is replayed
from the frozen legacy journal plus every shard, and conflicting writes resolve by
stamp -- never by the order a process happened to read the files in.
"""

import json
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import TEST_IDENTITY, append_legacy, journal_lines, own_shard
from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.identity import write_confirmed_identity
from vibe_cognition.cognition.journal_shards import (
    adoption,
    encode_entry,
    format_at,
    shard_dir,
    shard_path,
    split_entries,
    straggler_report,
)
from vibe_cognition.cognition.models import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
)
from vibe_cognition.cognition.storage import JournalWriterUnavailableError

TEAMMATE = "teammate@example.invalid"


def _node(node_id, summary="s", author="a", timestamp="2026-01-01T00:00:00+00:00"):
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DECISION, summary=summary, detail="d",
        context=[], references=[], timestamp=timestamp, author=author,
    )


def _at(offset_seconds=0):
    return format_at(datetime.now(UTC) + timedelta(seconds=offset_seconds))


def _write_shard(cognition, email, entries):
    """A teammate's shard as it arrives through version control."""
    path = shard_path(cognition, email)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        for action, data, at in entries:
            fh.write(encode_entry(action, data, at) + "\n")
    return path


# ── routing ──────────────────────────────────────────────────────────────────


def test_writes_land_in_the_confirmed_persons_shard_never_the_legacy_journal(tmp_path):
    cognition = tmp_path / ".cognition"
    store = CognitionStorage(cognition)
    store.add_node(_node("n1"))

    shard = own_shard(cognition)
    assert shard.exists()
    assert not (cognition / "journal.jsonl").exists()
    lines = [json.loads(line) for line in shard.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["action"] == "shard_start"
    assert lines[1]["action"] == "add_node" and "at" in lines[1]


def test_no_confirmed_identity_means_no_journal_write(tmp_path, graph_identity):
    graph_identity.unonboarded()
    cognition = tmp_path / ".cognition"
    store = CognitionStorage(cognition)
    assert store.can_write() is False
    with pytest.raises(JournalWriterUnavailableError):
        store.add_node(_node("n1"))
    assert journal_lines(cognition) == []
    assert not store.has_node("n1"), "a refused write must mutate nothing"


def test_changing_identity_switches_shards_from_the_next_write(tmp_path):
    cognition = tmp_path / ".cognition"
    store = CognitionStorage(cognition)
    store.add_node(_node("mine"))
    write_confirmed_identity(cognition, "Teammate", TEAMMATE)
    store.add_node(_node("theirs"))

    assert "mine" in own_shard(cognition).read_text(encoding="utf-8")
    assert "theirs" in shard_path(cognition, TEAMMATE).read_text(encoding="utf-8")
    assert "theirs" not in own_shard(cognition).read_text(encoding="utf-8")


def test_legacy_journal_and_shards_hydrate_into_one_graph(tmp_path):
    cognition = tmp_path / ".cognition"
    append_legacy(cognition, "add_node", _node("old").model_dump(mode="json"))
    _write_shard(cognition, TEAMMATE, [("add_node", _node("theirs").model_dump(mode="json"), _at())])
    store = CognitionStorage(cognition)
    store.add_node(_node("mine"))
    store.add_edge(CognitionEdge(from_id="mine", to_id="old", edge_type=CognitionEdgeType.LED_TO,
                                 timestamp="2026-01-01T00:00:00+00:00"))

    fresh = CognitionStorage(cognition)
    assert {"old", "theirs", "mine"} <= {n["id"] for n in fresh.get_all_nodes()}
    assert [t for t, _ in fresh.get_successors("mine")] == ["old"]


# ── discovery never resets ───────────────────────────────────────────────────


def test_a_new_teammate_shard_is_discovered_without_rebuilding_the_graph(tmp_path):
    cognition = tmp_path / ".cognition"
    store = CognitionStorage(cognition)
    store.add_node(_node("mine"))
    store.get_all_nodes()
    graph_before = store.graph

    _write_shard(cognition, TEAMMATE, [("add_node", _node("theirs").model_dump(mode="json"), _at())])
    store._shard_dir_mtime_ns = None

    assert store.has_node("theirs")
    assert store.graph is graph_before, "discovering a shard rebuilt the whole graph"
    assert store.rehydrate_count == 0


# ── convergence ──────────────────────────────────────────────────────────────


def _two_conflicting_shards(root):
    """Two people edited the same field of the same node; the teammate's edit is later."""
    cognition = root / ".cognition"
    base = _at(-60)
    mine_at, theirs_at = _at(-30), _at(-10)
    _write_shard(cognition, TEST_IDENTITY["email"], [
        ("add_node", _node("shared", summary="original").model_dump(mode="json"), base),
        ("update_node", {"id": "shared", "summary": "my edit"}, mine_at),
        ("add_edge", {"from_id": "shared", "to_id": "shared2", "edge_type": "led_to",
                      "timestamp": "t"}, mine_at),
        ("add_node", _node("shared2").model_dump(mode="json"), base),
    ])
    _write_shard(cognition, TEAMMATE, [
        ("update_node", {"id": "shared", "summary": "their later edit"}, theirs_at),
        ("remove_edge", {"from_id": "shared", "to_id": "shared2", "edge_type": "led_to"}, theirs_at),
    ])
    return cognition


def _state(store):
    snap = store.snapshot()
    nodes = {n["id"]: n.get("summary") for n in snap["nodes"]}
    edges = sorted((u, v, k) for u, v, k, _ in snap["edges"])
    return nodes, edges


def test_the_same_writes_read_in_opposite_orders_give_the_same_graph(tmp_path):
    """Rev 2 sorted shard names, which only fixes a cold start: a running process
    reads whichever file changed first. Stamps make the order irrelevant."""
    source = _two_conflicting_shards(tmp_path / "source")
    mine_file = shard_path(source, TEST_IDENTITY["email"])
    theirs_file = shard_path(source, TEAMMATE)

    def replay(order, where):
        cognition = where / ".cognition"
        cognition.mkdir(parents=True)
        store = CognitionStorage(cognition)
        for f in order:
            shard_dir(cognition).mkdir(exist_ok=True)
            shutil.copyfile(f, shard_dir(cognition) / f.name)
            store._shard_dir_mtime_ns = None
            store.get_all_nodes()
        return _state(store)

    forward = replay([mine_file, theirs_file], tmp_path / "forward")
    backward = replay([theirs_file, mine_file], tmp_path / "backward")
    assert forward == backward
    nodes, edges = forward
    assert nodes["shared"] == "their later edit"
    assert edges == [], "the later removal wins whichever file was read first"


def test_a_later_edit_read_first_is_not_overwritten_by_an_earlier_one_read_second(tmp_path):
    """The node exists before either edit arrives, so each edit applies the moment it
    is read. Arrival order would let the older edit win; the stamp must not."""
    cognition = tmp_path / ".cognition"
    append_legacy(cognition, "add_node", _node("n1", summary="original").model_dump(mode="json"))
    store = CognitionStorage(cognition)
    assert store.get_node("n1")["summary"] == "original"

    _write_shard(cognition, TEAMMATE, [("update_node", {"id": "n1", "summary": "later"}, _at(-10))])
    store._shard_dir_mtime_ns = None
    assert store.get_node("n1")["summary"] == "later"

    _write_shard(cognition, "third@example.invalid", [
        ("update_node", {"id": "n1", "summary": "earlier"}, _at(-60)),
    ])
    store._shard_dir_mtime_ns = None
    assert store.get_node("n1")["summary"] == "later"
    assert CognitionStorage(cognition).get_node("n1")["summary"] == "later"


def test_a_teammate_with_a_fast_clock_cannot_make_my_later_edit_lose(tmp_path):
    cognition = tmp_path / ".cognition"
    store = CognitionStorage(cognition)
    store.add_node(_node("n1", summary="original"))
    _write_shard(cognition, TEAMMATE, [
        ("update_node", {"id": "n1", "summary": "their edit, clock an hour fast"}, _at(3600)),
    ])
    store._shard_dir_mtime_ns = None
    assert store.get_node("n1")["summary"] == "their edit, clock an hour fast"

    store.update_node("n1", summary="my edit, made after seeing theirs")
    assert store.get_node("n1")["summary"] == "my edit, made after seeing theirs"
    assert CognitionStorage(cognition).get_node("n1")["summary"] == "my edit, made after seeing theirs"


def test_re_adding_a_node_a_fast_clocked_teammate_deleted_is_not_silently_blocked(tmp_path):
    cognition = tmp_path / ".cognition"
    store = CognitionStorage(cognition)
    store.add_node(_node("n1"))
    _write_shard(cognition, TEAMMATE, [("remove_node", {"id": "n1"}, _at(3600))])
    store._shard_dir_mtime_ns = None
    assert not store.has_node("n1")

    store.add_node(_node("n1", summary="restored"))
    assert store.has_node("n1")
    assert CognitionStorage(cognition).get_node("n1")["summary"] == "restored"


# ── merges and replacements ──────────────────────────────────────────────────


def test_a_merge_that_only_inserts_lines_applies_them_without_a_rebuild(tmp_path):
    """One person in two clones: git's union merge inserts the other clone's lines
    into the middle of the shard. That is routine, not a replacement."""
    cognition = tmp_path / ".cognition"
    store = CognitionStorage(cognition)
    store.add_node(_node("n1"))
    store.add_node(_node("n2"))
    store.get_all_nodes()
    graph_before = store.graph

    shard = own_shard(cognition)
    lines = shard.read_text(encoding="utf-8").splitlines(keepends=True)
    other_clone = encode_entry("add_node", _node("from-other-clone").model_dump(mode="json"), _at(-5))
    shard.write_text("".join(lines[:2] + [other_clone + "\n"] + lines[2:]), encoding="utf-8")

    assert store.has_node("from-other-clone")
    assert {"n1", "n2"} <= {n["id"] for n in store.get_all_nodes()}
    assert store.graph is graph_before, "an insert-only merge rebuilt the whole graph"
    assert store.rehydrate_count == 0


def test_my_own_unread_lines_vanishing_before_i_read_them_back_is_a_loss(tmp_path):
    """Appends never advance the offset (C-6), so a brand-new shard is read from the
    top on the next operation. If it was replaced in between, the only evidence is
    that lines this process wrote are not there."""
    cognition = tmp_path / ".cognition"
    store = CognitionStorage(cognition)
    store.add_node(_node("written-then-clobbered"))

    shard = own_shard(cognition)
    first = shard.read_text(encoding="utf-8").splitlines(keepends=True)[0]
    shard.write_text(first, encoding="utf-8")

    assert not store.has_node("written-then-clobbered")
    assert store.rehydrate_count == 1
    assert store.last_rehydrate["sample_missing_ids"] == ["written-then-clobbered"]


def test_lines_vanishing_from_a_shard_rebuild_and_raise_the_loss_alert(tmp_path):
    cognition = tmp_path / ".cognition"
    store = CognitionStorage(cognition)
    store.add_node(_node("keep"))
    store.add_node(_node("lost"))
    store.get_all_nodes()

    shard = own_shard(cognition)
    kept = [line for line in shard.read_text(encoding="utf-8").splitlines(keepends=True) if '"lost"' not in line]
    shard.write_text("".join(kept), encoding="utf-8")

    assert not store.has_node("lost")
    assert store.rehydrate_count == 1
    assert store.last_rehydrate["sample_missing_ids"] == ["lost"]


# ── collisions, dependencies, glued lines ────────────────────────────────────


def test_two_people_minting_the_same_id_keep_the_earlier_node_whole(tmp_path):
    cognition = tmp_path / ".cognition"
    _write_shard(cognition, TEAMMATE, [
        ("add_node", _node("dup", summary="teammate's node", author="t",
                           timestamp="2026-01-01T00:00:00+00:00").model_dump(mode="json"), _at(-100)),
    ])
    _write_shard(cognition, TEST_IDENTITY["email"], [
        ("add_node", _node("dup", summary="my different node", author="me",
                           timestamp="2026-02-02T00:00:00+00:00").model_dump(mode="json"), _at(-50)),
    ])
    store = CognitionStorage(cognition)
    node = store.get_node("dup")
    assert node["summary"] == "teammate's node" and node["author"] == "t"
    assert store.id_collisions == 1


def test_a_dependency_in_a_shard_discovered_later_is_not_dropped(tmp_path):
    cognition = tmp_path / ".cognition"
    store = CognitionStorage(cognition)
    store.add_node(_node("mine"))
    _write_shard(cognition, TEAMMATE, [
        ("add_edge", {"from_id": "mine", "to_id": "later", "edge_type": "led_to", "timestamp": "t"}, _at()),
    ])
    store._shard_dir_mtime_ns = None
    store.get_all_nodes()
    assert store.unresolved_entries == 1

    _write_shard(cognition, "third@example.invalid", [
        ("add_node", _node("later").model_dump(mode="json"), _at()),
    ])
    store._shard_dir_mtime_ns = None
    assert [t for t, _ in store.get_successors("mine")] == ["later"]
    assert store.unresolved_entries == 0


def test_two_entries_glued_onto_one_line_are_both_read(tmp_path):
    """Northstar lost an episode and 13 entries for 25 days to exactly this."""
    cognition = tmp_path / ".cognition"
    cognition.mkdir()
    first = json.dumps({"action": "add_node", "data": _node("a").model_dump(mode="json")})
    second = json.dumps({"action": "add_node", "data": _node("b").model_dump(mode="json")})
    (cognition / "journal.jsonl").write_text(first + second + "\n", encoding="utf-8")

    store = CognitionStorage(cognition)
    assert store.has_node("a") and store.has_node("b")
    entries, rest = split_entries(first + second + "garbage")
    assert len(entries) == 2 and rest == "garbage"


# ── adoption and stragglers ──────────────────────────────────────────────────


def test_adoption_is_recorded_by_the_first_shard(tmp_path):
    cognition = tmp_path / ".cognition"
    append_legacy(cognition, "add_node", _node("old").model_dump(mode="json"))
    legacy_size = (cognition / "journal.jsonl").stat().st_size
    CognitionStorage(cognition).add_node(_node("new"))

    start = adoption(cognition)
    assert start is not None and start["legacy_bytes"] == legacy_size


def test_a_teammate_on_an_old_plugin_writing_the_legacy_journal_is_flagged(tmp_path):
    cognition = tmp_path / ".cognition"
    CognitionStorage(cognition).add_node(_node("mine"))
    late = format_at(datetime.now(UTC) + timedelta(minutes=5))
    straggler = _node("old-plugin", author="Bob").model_dump(mode="json")
    straggler["timestamp"] = late
    straggler["metadata"] = {"recorded_by": {"name": "Bob", "email": "bob@example.invalid"}}
    append_legacy(cognition, "add_node", straggler)

    report = straggler_report(cognition)
    assert report is not None
    assert report["entries_after_adoption"] == 1
    assert report["authors"] == ["bob@example.invalid"]
    assert CognitionStorage(cognition).has_node("old-plugin"), "straggler writes are still read"


def test_pre_upgrade_work_that_merges_in_late_is_not_flagged(tmp_path):
    cognition = tmp_path / ".cognition"
    CognitionStorage(cognition).add_node(_node("mine"))
    append_legacy(cognition, "add_node", _node("written-last-week",
                  timestamp="2026-01-01T00:00:00+00:00").model_dump(mode="json"))
    assert straggler_report(cognition) is None


def test_a_shards_only_project_can_be_loaded_read_only(tmp_path):
    from vibe_cognition.cognition.journal_shards import has_journal

    other = tmp_path / "other" / ".cognition"
    CognitionStorage(other).add_node(_node("remote"))
    assert not (other / "journal.jsonl").exists()
    assert has_journal(other)
    assert CognitionStorage(other, read_only=True).has_node("remote")


# ── surfaces ─────────────────────────────────────────────────────────────────


def test_journal_status_names_where_writes_go(tmp_path):
    cognition = tmp_path / ".cognition"
    store = CognitionStorage(cognition)
    store.add_node(_node("n1"))
    status = store.journal_status()
    assert status["writing_to"] == f"journal/{own_shard(cognition).name}"
    assert [s["file"] for s in status["shards"]] == [own_shard(cognition).name]
    assert status["adopted_at"] is not None
    assert status["stragglers"] is None


def test_the_startup_edge_sweep_skips_quietly_without_an_identity(tmp_path, graph_identity):
    """Review finding B2: the sweep runs on every server start, including a brand-new
    project's first session, and must not raise for the missing identity."""
    from vibe_cognition.server import _create_deterministic_edges_for_edgeless

    cognition = tmp_path / ".cognition"
    append_legacy(cognition, "add_node", CognitionNode(
        id="dec", type=CognitionNodeType.DECISION, summary="d", detail="d", context=[],
        references=["commit:abc1234"], timestamp="2026-01-01T00:00:00+00:00", author="a",
    ).model_dump(mode="json"))
    append_legacy(cognition, "add_node", CognitionNode(
        id="ep", type=CognitionNodeType.EPISODE, summary="e", detail="e", context=[],
        references=["commit:abc1234"], timestamp="2026-01-01T00:00:00+00:00", author="a",
    ).model_dump(mode="json"))
    graph_identity.unonboarded()
    from vibe_cognition.cognition.identity import identity_write_path

    store = CognitionStorage(cognition)
    identity_write_path(cognition).unlink(missing_ok=True)
    _create_deterministic_edges_for_edgeless(store)
    assert store.get_successors("dec") == []
    assert not shard_dir(cognition).exists()


def test_the_journal_command_reports_and_adopts(tmp_path, capsys):
    from vibe_cognition.cognition.journal_cli import main

    cognition = tmp_path / ".cognition"
    append_legacy(cognition, "add_node", _node("old").model_dump(mode="json"))
    CognitionStorage(cognition)

    assert main(["status", str(tmp_path)]) == 0
    assert "none yet" in capsys.readouterr().out
    assert main(["adopt", str(tmp_path)]) == 0
    assert own_shard(cognition).exists()
    assert main(["status", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert own_shard(cognition).name in out and "not yet" not in out


def test_snapshot_of_a_cognition_folder_copies_every_journal_file(tmp_path):
    from vibe_cognition.cognition.snapshot_cli import snapshot_directory

    cognition = tmp_path / ".cognition"
    append_legacy(cognition, "add_node", _node("old").model_dump(mode="json"))
    CognitionStorage(cognition).add_node(_node("new"))
    copied = snapshot_directory(cognition, tmp_path / "flush" / ".cognition")
    names = {p.relative_to(tmp_path / "flush" / ".cognition").as_posix() for p in copied}
    assert "journal.jsonl" in names
    assert f"journal/{own_shard(cognition).name}" in names
    restored = CognitionStorage(tmp_path / "flush" / ".cognition", read_only=True)
    assert restored.has_node("old") and restored.has_node("new")


def test_the_straggler_warning_is_shown_once_per_new_straggler_write(tmp_path, monkeypatch, capsys):
    """Legacy entries stay forever; repeating the warning every session after the
    teammate upgraded would train people to ignore it."""
    from vibe_cognition.cognition import prime

    cognition = tmp_path / ".cognition"
    CognitionStorage(cognition).add_node(_node("mine"))
    monkeypatch.setenv("REPO_PATH", str(tmp_path))

    def straggle(node_id, minutes):
        data = _node(node_id).model_dump(mode="json")
        data["timestamp"] = format_at(datetime.now(UTC) + timedelta(minutes=minutes))
        append_legacy(cognition, "add_node", data)

    def context():
        prime.main([])
        return json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]

    straggle("old-1", 5)
    assert "old plugin version" in context()
    assert "old plugin version" not in context(), "the same straggler write warned twice"
    straggle("old-2", 10)
    assert "old plugin version" in context(), "a new straggler write must warn again"


def test_an_edit_to_a_deleted_node_survives_the_node_being_restored_with_an_older_stamp(tmp_path):
    """Review finding, reproduced: an edit newer than a deletion was dropped while the
    node was gone, so a restore stamped between the two lost it in a running session
    but kept it in a fresh rebuild."""
    cognition = tmp_path / ".cognition"
    s1, s2, s3, s4 = _at(-40), _at(-30), _at(-10), _at(-20)
    _write_shard(cognition, TEST_IDENTITY["email"], [
        ("add_node", _node("n1", summary="original").model_dump(mode="json"), s1),
        ("remove_node", {"id": "n1"}, s2),
    ])
    store = CognitionStorage(cognition)
    assert not store.has_node("n1")

    _write_shard(cognition, TEAMMATE, [("update_node", {"id": "n1", "summary": "their later edit"}, s3)])
    store._shard_dir_mtime_ns = None
    assert not store.has_node("n1")
    assert store.unresolved_entries == 0, "an edit waiting on a deleted node is not an unresolved entry"

    _write_shard(cognition, TEST_IDENTITY["email"], [
        ("add_node", _node("n1", summary="restored").model_dump(mode="json"), s4),
    ])
    store._shard_dir_mtime_ns = None
    running = store.get_node("n1")["summary"]
    fresh = CognitionStorage(cognition).get_node("n1")["summary"]
    assert running == fresh == "their later edit"


def test_a_shard_node_reusing_a_legacy_id_does_not_blend_into_it(tmp_path):
    cognition = tmp_path / ".cognition"
    append_legacy(cognition, "add_node", _node("dup", summary="legacy node", author="old",
                  timestamp="2025-01-01T00:00:00+00:00").model_dump(mode="json"))
    _write_shard(cognition, TEAMMATE, [
        ("add_node", _node("dup", summary="unrelated new node", author="new",
                           timestamp="2026-09-01T00:00:00+00:00").model_dump(mode="json"), _at()),
    ])
    node = CognitionStorage(cognition).get_node("dup")
    assert (node["summary"], node["author"]) == ("legacy node", "old")
