"""A shorter journal never destroys live nodes (docs/wp-append-only-replay-plan.md).

The incident these guard: an ordinary `git checkout -- .cognition` put an older
committed copy of a shard on disk, replay read it as truth, and 15 live memories were
destroyed. Five recorded events, up to 93 nodes, two unrecoverable.
"""

import json
from pathlib import Path

import pytest

from tests.conftest import journal_paths, own_shard
from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.journal_cli import main as journal_cli
from vibe_cognition.cognition.loss_notices import read_notices
from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
from vibe_cognition.cognition.own_ledger import OWN_LEDGER_FILENAME
from vibe_cognition.cognition.prime import _consume_rehydrate_flag
from vibe_cognition.cognition.storage import REHYDRATE_FLAG_FILENAME

ALICE = ("Alice", "alice@corp.example")


def _node(node_id: str, summary: str = "s") -> CognitionNode:
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DECISION, summary=summary, detail="d",
        context=[], references=[], timestamp="2026-09-18T00:00:00+00:00", author="Alice",
    )


def _flag(cognition: Path):
    """The repair/loss notice, if one is outstanding (the flag holds a LIST now)."""
    notices = read_notices(cognition / "local" / REHYDRATE_FLAG_FILENAME)
    return notices[0] if notices else None


def _rollback(cognition: Path, *, drop: str) -> None:
    """What `git checkout -- .cognition` does: an older copy of the file appears."""
    shard = own_shard(cognition)
    kept = [
        line for line in shard.read_text(encoding="utf-8").splitlines(keepends=True)
        if f'"{drop}"' not in line
    ]
    shard.write_text("".join(kept), encoding="utf-8")


@pytest.fixture
def cognition(tmp_path, graph_identity):
    """The default onboarded checkout; own_shard() resolves the same identity."""
    return tmp_path / ".cognition"


def test_the_reported_incident_a_cold_start_after_a_rollback(cognition):
    """THE case this work exists for: exactly one write, the process ends with nothing
    reading that shard again, the file is rolled back, and a NEW process starts.

    Nothing may read the shard between the write and the rollback -- an earlier design
    kept the graph in memory, which passes only when a live process witnessed the
    rollback, and would fail this test.
    """
    store = CognitionStorage(cognition)
    store.add_node(_node("only-write"))
    del store  # the session ends here

    _rollback(cognition, drop="only-write")

    fresh = CognitionStorage(cognition)
    assert fresh.has_node("only-write"), "a rollback destroyed a memory across a restart"
    assert fresh.healed_lines == 1
    assert "only-write" in own_shard(cognition).read_text(encoding="utf-8")
    assert _flag(cognition)["kind"] == "own_shard_healed"


def test_a_live_process_sees_the_same_repair(cognition):
    store = CognitionStorage(cognition)
    store.add_node(_node("keep"))
    store.add_node(_node("rolled-back"))
    store.get_all_nodes()

    _rollback(cognition, drop="rolled-back")

    assert store.has_node("rolled-back")
    assert store.healed_lines == 1
    assert '"rolled-back"' in own_shard(cognition).read_text(encoding="utf-8")


def test_a_deliberate_deletion_still_removes_before_and_after_a_rebuild(cognition):
    """Append-only must not resurrect: a tombstone is an APPENDED line, so it survives
    a rollback of the file and still removes its node."""
    store = CognitionStorage(cognition)
    store.add_node(_node("doomed"))
    store.remove_node("doomed", removed_by={"name": "Test User", "email": "test-user@example.invalid"})
    assert not store.has_node("doomed")

    own_shard(cognition).write_bytes(b"")  # force a rebuild + repair
    store.get_all_nodes()
    assert not store.has_node("doomed"), "a repair resurrected a deleted node"

    fresh = CognitionStorage(cognition)
    assert not fresh.has_node("doomed"), "the repaired file lost its tombstone"


def test_the_legacy_journal_is_retained_but_never_written(cognition):
    store = CognitionStorage(cognition)
    store.add_node(_node("mine"))
    legacy = cognition / "journal.jsonl"
    legacy.write_text(json.dumps({
        "action": "add_node",
        "data": {
            "id": "legacy-node", "type": "decision", "summary": "old", "detail": "d",
            "context": [], "references": [], "severity": None,
            "timestamp": "2026-01-01T00:00:00+00:00", "author": "someone", "metadata": {},
        },
    }) + "\n", encoding="utf-8")
    assert store.has_node("legacy-node")

    legacy.write_text("", encoding="utf-8")
    assert store.has_node("legacy-node"), "a truncated legacy journal destroyed a node"
    assert legacy.read_text(encoding="utf-8") == "", "the frozen legacy journal was written"
    flag = _flag(cognition)
    assert flag["kind"] == "retained_only"
    assert any("journal.jsonl" in f for f in flag["retained_files"])


def test_a_missing_shard_start_line_is_reported_not_appended(cognition):
    """adoption() reads only a file's FIRST line, so putting that line back at the tail
    would quietly break adoption and straggler detection."""
    store = CognitionStorage(cognition)
    store.add_node(_node("n1"))
    shard = own_shard(cognition)
    body = [
        line for line in shard.read_text(encoding="utf-8").splitlines(keepends=True)
        if "shard_start" not in line
    ]
    shard.write_text("".join(body), encoding="utf-8")

    store.get_all_nodes()
    assert store.healed_lines == 0, "the shard_start line must never be appended back"
    assert "shard_start" not in shard.read_text(encoding="utf-8")
    assert store.has_node("n1"), "the node was dropped instead of retained"


def test_a_conflicted_file_is_left_alone(cognition, tmp_path):
    store = CognitionStorage(cognition)
    store.add_node(_node("n1"))
    shard = own_shard(cognition)
    original = shard.read_text(encoding="utf-8")
    shard.write_text(
        original.splitlines(keepends=True)[0] + "<<<<<<< .mine\n{}\n>>>>>>> .r5\n",
        encoding="utf-8",
    )

    store.get_all_nodes()
    assert store.healed_lines == 0, "a repair wrote into a conflicted file"
    assert "<<<<<<<" in shard.read_text(encoding="utf-8")
    assert store.has_node("n1")


def test_a_mid_merge_working_tree_is_left_alone(tmp_path, graph_identity):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    cognition = root / ".cognition"
    store = CognitionStorage(cognition)
    store.add_node(_node("n1"))
    _rollback(cognition, drop="n1")
    (root / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")

    store.get_all_nodes()
    assert store.healed_lines == 0, "a repair ran during a merge"
    assert store.has_node("n1"), "the node was dropped instead of retained"


def test_the_repair_notice_survives_being_read_until_it_is_resolved(cognition):
    """The reported incident went unnoticed for 90 minutes because the old alert was
    read once and deleted."""
    store = CognitionStorage(cognition)
    store.add_node(_node("mine"))
    mate = cognition / "journal" / "mate%40corp.example.jsonl"
    mate.write_text(json.dumps({
        "action": "add_node",
        "at": "2026-09-18T00:00:00.000001+00:00",
        "data": {
            "id": "theirs", "type": "decision", "summary": "t", "detail": "d",
            "context": [], "references": [], "severity": None,
            "timestamp": "2026-09-18T00:00:00+00:00", "author": "Mate",
            "metadata": {"recorded_by": {"name": "Mate", "email": "mate@corp.example"}},
        },
    }) + "\n", encoding="utf-8")
    assert store.has_node("theirs")
    mate.write_text("", encoding="utf-8")
    store.get_all_nodes()

    assert "readable in THIS session" in _consume_rehydrate_flag(cognition)
    assert _flag(cognition) is not None, "the notice was consumed on first read"
    assert "readable in THIS session" in _consume_rehydrate_flag(cognition)


def test_the_ledger_is_ignored_when_it_came_from_another_checkout(cognition):
    store = CognitionStorage(cognition)
    store.add_node(_node("n1"))
    ledger = cognition / "local" / OWN_LEDGER_FILENAME
    rows = ledger.read_text(encoding="utf-8").splitlines()
    header = json.loads(rows[0])
    header["binding"]["machine"] = "someone-elses-laptop"
    ledger.write_text("\n".join([json.dumps(header), *rows[1:]]) + "\n", encoding="utf-8")

    _rollback(cognition, drop="n1")
    fresh = CognitionStorage(cognition)
    assert fresh.healed_lines == 0, "a ledger from another machine was trusted"


def test_accept_disk_drops_retained_entries_and_records_an_audit(cognition, monkeypatch, capsys):
    store = CognitionStorage(cognition)
    store.add_node(_node("keep"))
    store.add_node(_node("rolled-back"))
    store.get_all_nodes()
    _rollback(cognition, drop="rolled-back")
    store.get_all_nodes()
    # Put the file back to the rolled-back state: accept-disk must honour THIS content.
    _rollback(cognition, drop="rolled-back")

    monkeypatch.setattr("builtins.input", lambda *_: "accept")
    assert journal_cli(["accept-disk", str(cognition.parent)]) == 0

    after = CognitionStorage(cognition)
    assert after.has_node("keep")
    assert not after.has_node("rolled-back"), "accept-disk kept what the human dropped"
    audit = [n for n in after.get_all_nodes() if "accept-disk dropped" in (n.get("summary") or "")]
    assert audit, "accept-disk left no audit record"
    assert "rolled-back" in audit[0]["detail"]
    assert _flag(cognition) is None, "accept-disk left the alert up"


def test_accept_disk_without_confirmation_changes_nothing(cognition, monkeypatch):
    store = CognitionStorage(cognition)
    store.add_node(_node("keep"))
    del store
    monkeypatch.setattr("builtins.input", lambda *_: "no")
    assert journal_cli(["accept-disk", str(cognition.parent)]) == 1
    assert CognitionStorage(cognition).has_node("keep")


def test_two_processes_repairing_one_shard_converge(cognition):
    a = CognitionStorage(cognition)
    a.add_node(_node("shared"))
    b = CognitionStorage(cognition)
    assert b.has_node("shared")

    _rollback(cognition, drop="shared")
    a.get_all_nodes()
    b.get_all_nodes()

    assert a.has_node("shared") and b.has_node("shared")
    assert {n["id"] for n in a.get_all_nodes()} == {n["id"] for n in b.get_all_nodes()}
    fresh = CognitionStorage(cognition)
    assert fresh.has_node("shared"), "duplicated repairs left the file unreadable"


def test_only_one_alert_is_raised_for_one_event(cognition):
    store = CognitionStorage(cognition)
    store.add_node(_node("n1"))
    del store
    _rollback(cognition, drop="n1")
    CognitionStorage(cognition)

    notices = read_notices(cognition / "local" / REHYDRATE_FLAG_FILENAME)
    assert len(notices) == 1, f"one event raised {len(notices)} notices: {notices}"
    message = _consume_rehydrate_flag(cognition)
    assert message.count("WARNING") + message.count("NOTE") == 1


def test_nothing_is_written_into_a_read_only_open(cognition):
    store = CognitionStorage(cognition)
    store.add_node(_node("n1"))
    del store
    _rollback(cognition, drop="n1")
    before = sorted(p.name for p in (cognition / "local").glob("*"))

    reader = CognitionStorage(cognition, read_only=True)
    reader.get_all_nodes()
    assert reader.healed_lines == 0, "a read-only open repaired a file"
    assert sorted(p.name for p in (cognition / "local").glob("*")) == before
    assert "n1" not in own_shard(cognition).read_text(encoding="utf-8")


def test_the_journal_files_helper_still_sees_both_kinds(cognition):
    store = CognitionStorage(cognition)
    store.add_node(_node("n1"))
    assert any(p.name.endswith(".jsonl") for p in journal_paths(cognition))


def test_a_retained_notice_survives_later_unrelated_rebuilds(cognition):
    """Peer review BLOCKER, reproduced: the notice used to be recomputed per rebuild, so
    the next ordinary tool call dropped it while the teammate's file was still broken."""
    store = CognitionStorage(cognition)
    store.add_node(_node("mine"))
    mate = cognition / "journal" / "mate%40corp.example.jsonl"
    mate.write_text(json.dumps({
        "action": "add_node",
        "at": "2026-09-18T00:00:00.000001+00:00",
        "data": {
            "id": "theirs", "type": "decision", "summary": "t", "detail": "d",
            "context": [], "references": [], "severity": None,
            "timestamp": "2026-09-18T00:00:00+00:00", "author": "Mate",
            "metadata": {"recorded_by": {"name": "Mate", "email": "mate@corp.example"}},
        },
    }) + "\n", encoding="utf-8")
    assert store.has_node("theirs")
    mate.write_text("", encoding="utf-8")
    store.get_all_nodes()
    assert _flag(cognition)["kind"] == "retained_only"

    # Any number of further events, including a repair of our OWN shard.
    store.add_node(_node("later"))
    _rollback(cognition, drop="later")
    store.get_all_nodes()
    store.get_all_nodes()

    kinds = {n["kind"] for n in read_notices(cognition / "local" / REHYDRATE_FLAG_FILENAME)}
    assert "retained_only" in kinds, "the teammate's file is still broken but the notice went"
    assert "own_shard_healed" in kinds, "the repair notice was lost too"
    assert store.has_node("theirs")


def test_a_repair_notice_is_not_clobbered_by_a_replayed_deletion(cognition):
    """Peer review BLOCKER, reproduced: a legitimate remove_node replayed in the same
    rebuild overwrote the whole flag with the old 'check git log' text."""
    store = CognitionStorage(cognition)
    store.add_node(_node("doomed"))
    store.add_node(_node("kept"))
    store.get_all_nodes()

    mate = cognition / "journal" / "mate%40corp.example.jsonl"
    mate.write_text(json.dumps({
        "action": "remove_node", "at": "2030-01-01T00:00:00.000002+00:00",
        "data": {"id": "doomed", "removed_by": {"name": "Mate", "email": "mate@corp.example"}},
    }) + "\n", encoding="utf-8")
    _rollback(cognition, drop="kept")
    store.get_all_nodes()

    assert not store.has_node("doomed"), "a real deletion must still delete"
    assert store.has_node("kept"), "the rollback destroyed a node"
    message = _consume_rehydrate_flag(cognition)
    assert "COMMIT" in message, "the repair notice was clobbered by the deletion report"
    assert "check `git log`" not in message


def test_a_teammates_rollback_seen_only_after_a_restart_is_still_reported(tmp_path, graph_identity):
    """The cold-start half for a file we may not repair: a teammate's shard was rolled
    back before this session opened, so nothing in memory witnessed it."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    cognition = root / ".cognition"
    mate = cognition / "journal" / "mate%40corp.example.jsonl"
    store = CognitionStorage(cognition)
    store.add_node(_node("mine"))
    mate.parent.mkdir(parents=True, exist_ok=True)
    mate.write_text(json.dumps({
        "action": "add_node",
        "at": "2026-09-18T00:00:00.000001+00:00",
        "data": {
            "id": "theirs", "type": "decision", "summary": "t", "detail": "d",
            "context": [], "references": [], "severity": None,
            "timestamp": "2026-09-18T00:00:00+00:00", "author": "Mate",
            "metadata": {"recorded_by": {"name": "Mate", "email": "mate@corp.example"}},
        },
    }) + "\n", encoding="utf-8")
    assert store.has_node("theirs")
    del store  # the session ends, having seen their entry

    mate.write_text("", encoding="utf-8")  # their rollback lands via git pull

    fresh = CognitionStorage(cognition)
    assert not fresh.has_node("theirs"), "nothing in memory could have kept it"
    flag = _flag(cognition)
    assert flag is not None, "a teammate's rollback between sessions went unreported on git"
    assert "theirs" in json.dumps(flag)


def test_accept_disk_refuses_when_its_audit_cannot_be_written(cognition, monkeypatch):
    """The audit IS the accountability for this command (ruling 2026-09-18), so a run
    that cannot be recorded must not happen at all."""
    store = CognitionStorage(cognition)
    store.add_node(_node("precious"))
    del store
    _rollback(cognition, drop="precious")

    from vibe_cognition.cognition import journal_cli as cli_mod

    def _explode(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(cli_mod, "_record_audit", _explode)
    monkeypatch.setattr("builtins.input", lambda *_: "accept")
    assert cli_mod.main(["accept-disk", str(cognition.parent)]) == 4

    after = CognitionStorage(cognition)
    assert after.has_node("precious"), "entries were dropped despite the audit failing"


def test_an_own_shard_we_cannot_repair_is_not_blamed_on_a_teammate(cognition):
    """The notice used to say 'it belongs to a teammate' for the reader's OWN file."""
    store = CognitionStorage(cognition)
    store.add_node(_node("n1"))
    shard = own_shard(cognition)
    shard.write_text(
        shard.read_text(encoding="utf-8").splitlines(keepends=True)[0]
        + "<<<<<<< .mine\n{}\n>>>>>>> .r5\n",
        encoding="utf-8",
    )
    store.get_all_nodes()

    message = _consume_rehydrate_flag(cognition)
    assert "OWN file" in message, message
    assert "belongs to a teammate" not in message


def test_a_newly_broken_file_re_expands_a_collapsed_notice(cognition):
    """A collapsed '(Repeat notice.)' must not swallow a file that broke later."""
    store = CognitionStorage(cognition)
    store.add_node(_node("mine"))
    mate = cognition / "journal" / "mate%40corp.example.jsonl"
    mate.write_text(json.dumps({
        "action": "add_node", "at": "2026-09-18T00:00:00.000001+00:00",
        "data": {
            "id": "theirs", "type": "decision", "summary": "t", "detail": "d",
            "context": [], "references": [], "severity": None,
            "timestamp": "2026-09-18T00:00:00+00:00", "author": "Mate",
            "metadata": {"recorded_by": {"name": "Mate", "email": "mate@corp.example"}},
        },
    }) + "\n", encoding="utf-8")
    assert store.has_node("theirs")
    mate.write_text("", encoding="utf-8")
    store.get_all_nodes()
    for _ in range(4):
        _consume_rehydrate_flag(cognition)
    assert "(Repeat notice.)" in _consume_rehydrate_flag(cognition)

    legacy = cognition / "journal.jsonl"
    legacy.write_text(json.dumps({
        "action": "add_node",
        "data": {
            "id": "legacy-node", "type": "decision", "summary": "old", "detail": "d",
            "context": [], "references": [], "severity": None,
            "timestamp": "2026-01-01T00:00:00+00:00", "author": "someone", "metadata": {},
        },
    }) + "\n", encoding="utf-8")
    assert store.has_node("legacy-node")
    legacy.write_text("", encoding="utf-8")
    store.get_all_nodes()

    message = _consume_rehydrate_flag(cognition)
    assert "(Repeat notice.)" not in message, "a newly broken file was buried in a repeat"
    assert "journal.jsonl" in message
