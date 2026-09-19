"""Journal loss that happens BETWEEN sessions, on Subversion.

Found in the SVN abuse lab: resolving a journal conflict with "use mine" or "use
theirs" -- the two buttons every SVN client puts first -- permanently deleted one
side's memories with no warning anywhere, because a live server was not running to
see it. These tests simulate the between-session loss directly by rewriting the
journal while no CognitionStorage is open.
"""

import json

import pytest

from tests.conftest import journal_paths
from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.journal_watch import (
    KNOWN_IDS_FILENAME,
    KNOWN_IDS_LOG_FILENAME,
    check_between_sessions,
)
from vibe_cognition.cognition.loss_notices import read_notices
from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
from vibe_cognition.cognition.prime import _consume_rehydrate_flag
from vibe_cognition.cognition.storage import REHYDRATE_FLAG_FILENAME


def _node(node_id, summary):
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DECISION, summary=summary, detail="d",
        context=[], references=[], timestamp="2026-01-01T00:00:00+00:00", author="a",
    )


@pytest.fixture
def svn_checkout(tmp_path, graph_identity):
    root = tmp_path / "wc"
    (root / ".svn").mkdir(parents=True)
    return root / ".cognition"


def _drop_lines_containing(cognition, *needles):
    """What 'resolve using theirs' does to your own unpushed lines."""
    for journal in journal_paths(cognition):
        kept = [
            line for line in journal.read_text(encoding="utf-8").splitlines(keepends=True)
            if not any(n in line for n in needles)
        ]
        journal.write_text("".join(kept), encoding="utf-8")


def _flag(cognition):
    """The outstanding notice, if any (the flag holds a LIST of them since 0.44.0)."""
    notices = read_notices(cognition / "local" / REHYDRATE_FLAG_FILENAME)
    return notices[0] if notices else None


def _lost(cognition) -> dict:
    flag = _flag(cognition)
    assert flag is not None, "the loss went unnoticed"
    return flag


def _healed(cognition) -> dict:
    """WP-Append-Only-Replay: a loss from the reader's OWN shard is repaired from
    this checkout's own-append ledger, so the alert reports a repair, not a loss."""
    flag = _flag(cognition)
    assert flag is not None, "the rollback went unnoticed"
    assert flag["kind"] == "own_shard_healed", flag
    return flag


def test_losing_your_own_work_between_sessions_is_repaired_not_lost(svn_checkout):
    """WP-Append-Only-Replay: this used to assert the memories were GONE.

    A 'resolve using theirs' still deletes this checkout's lines from the file, but
    they were written by this checkout, so they come back from its own ledger and the
    alert tells the reader to commit the repaired file."""
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("keep1", "stays"))
    s.add_node(_node("gone1", "lost to use-theirs"))
    s.add_node(_node("gone2", "also lost"))
    del s  # session ends

    _drop_lines_containing(svn_checkout, "gone1", "gone2")

    store = CognitionStorage(svn_checkout)  # next session start
    assert store.has_node("gone1") and store.has_node("gone2"), "a rollback destroyed memories"
    assert _healed(svn_checkout)["healed_lines"] == 2
    restored = "".join(j.read_text(encoding="utf-8") for j in journal_paths(svn_checkout))
    assert "gone1" in restored and "gone2" in restored, "the lines were not put back on disk"


def test_the_repair_notice_names_the_cause_and_what_to_do(svn_checkout):
    """WP-Append-Only-Replay: the old text sent the reader hunting `svn cat` for data
    that is no longer lost. A repaired rollback says what happened and asks for a
    commit, because the fix is local until the file is pushed."""
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("gone1", "x"))
    del s
    _drop_lines_containing(svn_checkout, "gone1")
    CognitionStorage(svn_checkout)

    message = _consume_rehydrate_flag(svn_checkout)
    assert "appended back automatically" in message
    assert "COMMIT" in message
    assert "tell the user" in message.lower()


def test_a_teammates_lost_lines_are_retained_and_reported_not_repaired(svn_checkout):
    """The reader may not write someone else's shard, so their loss is held in memory
    and reported -- and the notice stays up, because the condition is still true."""
    store = CognitionStorage(svn_checkout)
    store.add_node(_node("mine", "x"))
    mate = svn_checkout / "journal" / "mate%40corp.example.jsonl"
    mate.write_text(json.dumps({
        "action": "add_node",
        "at": "2026-01-02T00:00:00.000001+00:00",
        "data": {
            "id": "theirs", "type": "decision", "summary": "theirs", "detail": "d",
            "context": [], "references": [], "severity": None,
            "timestamp": "2026-01-02T00:00:00+00:00", "author": "Mate",
            "metadata": {"recorded_by": {"name": "Mate", "email": "mate@corp.example"}},
        },
    }) + "\n", encoding="utf-8")
    assert store.has_node("theirs")

    mate.write_text("", encoding="utf-8")
    assert store.has_node("theirs"), "a teammate's truncated shard destroyed a live node"
    flag = _flag(svn_checkout)
    assert flag is not None and flag["kind"] == "retained_only"
    assert any("mate" in f for f in flag["retained_files"])
    assert mate.read_text(encoding="utf-8") == "", "we must never write a teammate's shard"

    message = _consume_rehydrate_flag(svn_checkout)
    assert "readable in THIS session" in message
    assert _flag(svn_checkout) is not None, "the retained notice must survive being read"


def test_a_repaired_rollback_is_reported_once_not_on_every_later_startup(svn_checkout):
    """A first draft kept lost ids in the log, so every later startup re-reported
    them as newly lost -- a permanent false alarm that trains people to ignore it."""
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("gone1", "x"))
    del s
    _drop_lines_containing(svn_checkout, "gone1")

    CognitionStorage(svn_checkout)
    assert _healed(svn_checkout)["healed_lines"] == 1
    CognitionStorage(svn_checkout)
    CognitionStorage(svn_checkout)
    assert _healed(svn_checkout)["healed_lines"] == 1, "the same repair was counted again"


def test_two_separate_repairs_before_anyone_reads_the_alert_are_both_reported(svn_checkout):
    """The flag is added to, not overwritten: a later, smaller event must not replace
    an unread earlier one."""
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("first", "x"))
    s.add_node(_node("second", "y"))
    del s
    _drop_lines_containing(svn_checkout, "first")
    CognitionStorage(svn_checkout)
    _drop_lines_containing(svn_checkout, "second")
    CognitionStorage(svn_checkout)

    assert _healed(svn_checkout)["healed_lines"] == 2


def test_a_deliberate_deletion_is_not_a_loss(svn_checkout):
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("doomed", "x"))
    del s
    s = CognitionStorage(svn_checkout)
    s.remove_node("doomed", removed_by={"name": "A", "email": "a@example.com"})
    del s

    CognitionStorage(svn_checkout)
    assert _flag(svn_checkout) is None


def test_nodes_created_mid_session_are_covered(svn_checkout):
    """The startup snapshot alone would miss everything recorded AFTER it -- which is
    exactly the work a 'use theirs' resolution throws away."""
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("mid-session", "recorded after startup"))
    log = svn_checkout / "local" / KNOWN_IDS_LOG_FILENAME
    assert "mid-session" in log.read_text(encoding="utf-8")
    del s
    _drop_lines_containing(svn_checkout, "mid-session")

    store = CognitionStorage(svn_checkout)
    assert store.has_node("mid-session"), "work recorded mid-session was destroyed"
    assert _healed(svn_checkout)["healed_lines"] == 1


def test_a_first_session_has_no_baseline_and_says_nothing(svn_checkout):
    s = CognitionStorage(svn_checkout)
    assert check_between_sessions(s) is None
    assert (svn_checkout / "local" / KNOWN_IDS_FILENAME).exists()


def test_a_git_rollback_is_repaired_not_ignored(tmp_path, graph_identity):
    """WP-Append-Only-Replay: this used to assert git got NOTHING, on the reasoning
    that branch switches remove ids legitimately. That left the reported incident --
    `git checkout -- .cognition` on the SAME branch -- destroying live memories. A
    real branch switch is still left alone (see the test below); a rollback in place
    is now repaired."""
    root = tmp_path / "gitwc"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    cognition = root / ".cognition"
    s = CognitionStorage(cognition)
    s.add_node(_node("on-branch", "x"))
    del s
    _drop_lines_containing(cognition, "on-branch")

    store = CognitionStorage(cognition)
    assert store.has_node("on-branch")
    assert _healed(cognition)["healed_lines"] == 1
    assert (cognition / "local" / KNOWN_IDS_FILENAME).exists(), (
        "since 0.44.0 the id snapshot is kept on git too, so a teammate's rollback "
        "between sessions is noticed there as well"
    )


def test_a_git_branch_switch_is_left_alone(tmp_path, graph_identity):
    """The other half: moving to another branch legitimately brings another line of
    history, so the repair stands down instead of re-appending this branch's lines."""
    root = tmp_path / "gitwc2"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    cognition = root / ".cognition"
    s = CognitionStorage(cognition)
    s.add_node(_node("main-only", "x"))
    del s
    CognitionStorage(cognition)

    _drop_lines_containing(cognition, "main-only")
    (root / ".git" / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")

    CognitionStorage(cognition)
    assert _flag(cognition) is None, "a branch switch must not look like a rollback"


def _fake_wc_db(root, url_root, repos_path, revision):
    import sqlite3

    db = root / ".svn" / "wc.db"
    db.unlink(missing_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE repository (id INTEGER, root TEXT)")
    conn.execute(
        "CREATE TABLE nodes (local_relpath TEXT, op_depth INTEGER, repos_id INTEGER, "
        "repos_path TEXT, revision INTEGER)"
    )
    conn.execute("INSERT INTO repository VALUES (1, ?)", (url_root,))
    conn.execute(
        "INSERT INTO nodes VALUES ('.cognition/journal.jsonl', 0, 1, ?, ?)",
        (f"{repos_path}/.cognition/journal.jsonl", revision),
    )
    conn.commit()
    conn.close()


def test_the_journal_source_is_read_from_the_working_copy_database(svn_checkout):
    from vibe_cognition.cognition.journal_watch import journal_source

    svn_checkout.mkdir(parents=True, exist_ok=True)
    _fake_wc_db(svn_checkout.parent, "file:///repo/", "trunk", 7)
    assert journal_source(svn_checkout) == {
        "url": "file:///repo/trunk/.cognition/journal.jsonl", "revision": 7,
    }


def test_svn_switch_to_another_branch_is_not_a_loss(svn_checkout):
    """Review finding: a switch drops every id that lives only on the old branch,
    and the alert's recovery recipe would then re-import that branch's lines."""
    svn_checkout.mkdir(parents=True, exist_ok=True)
    _fake_wc_db(svn_checkout.parent, "file:///repo", "trunk", 5)
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("trunk-only", "x"))
    del s
    CognitionStorage(svn_checkout)

    _drop_lines_containing(svn_checkout, "trunk-only")
    _fake_wc_db(svn_checkout.parent, "file:///repo", "branches/feature", 6)
    CognitionStorage(svn_checkout)
    assert _flag(svn_checkout) is None


def test_updating_back_to_an_older_revision_is_not_a_loss(svn_checkout):
    svn_checkout.mkdir(parents=True, exist_ok=True)
    _fake_wc_db(svn_checkout.parent, "file:///repo", "trunk", 9)
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("newer", "x"))
    del s
    CognitionStorage(svn_checkout)

    _drop_lines_containing(svn_checkout, "newer")
    _fake_wc_db(svn_checkout.parent, "file:///repo", "trunk", 4)
    CognitionStorage(svn_checkout)
    assert _flag(svn_checkout) is None


def test_a_conflict_resolved_during_an_ordinary_update_still_alerts(svn_checkout):
    """The switch carve-out must not swallow the case this module exists for."""
    svn_checkout.mkdir(parents=True, exist_ok=True)
    _fake_wc_db(svn_checkout.parent, "file:///repo", "trunk", 5)
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("mine", "x"))
    del s
    CognitionStorage(svn_checkout)

    _drop_lines_containing(svn_checkout, "mine")
    _fake_wc_db(svn_checkout.parent, "file:///repo", "trunk", 8)
    store = CognitionStorage(svn_checkout)
    assert store.has_node("mine"), "a conflict resolution destroyed this checkout's work"
    assert _healed(svn_checkout)["healed_lines"] == 1


def test_a_project_inside_a_larger_svn_checkout_is_watched(tmp_path, graph_identity):
    """Review finding, reproduced on real SVN: since 1.7 `.svn` exists only at the
    checkout root, so checking out `trunk` with the project at `trunk/proj` left
    the loss check switched off and a 'use theirs' resolution went unreported."""
    wc = tmp_path / "trunk"
    (wc / ".svn").mkdir(parents=True)
    cognition = wc / "proj" / ".cognition"
    s = CognitionStorage(cognition)
    s.add_node(_node("nested-gone", "x"))
    del s
    _drop_lines_containing(cognition, "nested-gone")

    store = CognitionStorage(cognition)
    assert store.has_node("nested-gone")
    assert _healed(cognition)["healed_lines"] == 1


def test_the_nested_journal_source_is_found_relative_to_the_checkout_root(tmp_path, graph_identity):
    import sqlite3

    from vibe_cognition.cognition.journal_watch import journal_source

    wc = tmp_path / "trunk"
    (wc / ".svn").mkdir(parents=True)
    cognition = wc / "proj" / ".cognition"
    cognition.mkdir(parents=True)
    conn = sqlite3.connect(wc / ".svn" / "wc.db")
    conn.execute("CREATE TABLE repository (id INTEGER, root TEXT)")
    conn.execute(
        "CREATE TABLE nodes (local_relpath TEXT, op_depth INTEGER, repos_id INTEGER, "
        "repos_path TEXT, revision INTEGER)"
    )
    conn.execute("INSERT INTO repository VALUES (1, 'file:///repo')")
    conn.execute(
        "INSERT INTO nodes VALUES ('proj/.cognition/journal.jsonl', 0, 1, "
        "'trunk/proj/.cognition/journal.jsonl', 12)"
    )
    conn.commit()
    conn.close()
    assert journal_source(cognition) == {
        "url": "file:///repo/trunk/proj/.cognition/journal.jsonl", "revision": 12,
    }


def test_a_git_project_nested_in_an_svn_checkout_is_watched_like_any_other(tmp_path, graph_identity):
    """Used to assert the check stayed OFF here (it keyed on the outer .svn). Since
    0.44.0 every project is watched, and a deliberate move is recognised instead."""
    outer = tmp_path / "svnwc"
    (outer / ".svn").mkdir(parents=True)
    (outer / "gitproj" / ".git").mkdir(parents=True)
    cognition = outer / "gitproj" / ".cognition"
    CognitionStorage(cognition)
    assert (cognition / "local" / KNOWN_IDS_FILENAME).exists()

def test_an_unreadable_wc_db_does_not_forget_the_last_source(svn_checkout, monkeypatch):
    """Review finding: a startup that could not read wc.db recorded no source, so the
    NEXT startup's deliberate switch was not recognised and raised a false alarm."""
    from vibe_cognition.cognition import journal_watch

    svn_checkout.mkdir(parents=True, exist_ok=True)
    _fake_wc_db(svn_checkout.parent, "file:///repo", "trunk", 5)
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("trunk-only", "x"))
    del s
    CognitionStorage(svn_checkout)

    with monkeypatch.context() as m:
        m.setattr(journal_watch, "journal_source", lambda _d: None)
        CognitionStorage(svn_checkout)

    _drop_lines_containing(svn_checkout, "trunk-only")
    _fake_wc_db(svn_checkout.parent, "file:///repo", "branches/feature", 6)
    CognitionStorage(svn_checkout)
    assert _flag(svn_checkout) is None


def test_a_read_only_open_writes_nothing_into_the_project(svn_checkout):
    """Review finding: cognition_load_project is documented read-only, yet opening a
    sibling SVN project re-baselined ITS loss record -- masking a loss its own next
    session should have reported."""
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("gone1", "x"))
    del s
    _drop_lines_containing(svn_checkout, "gone1")
    local = svn_checkout / "local"
    before = {p.name: p.read_bytes() for p in local.iterdir()}

    CognitionStorage(svn_checkout, read_only=True)
    assert {p.name: p.read_bytes() for p in local.iterdir()} == before

    store = CognitionStorage(svn_checkout)
    assert store.has_node("gone1")
    assert _healed(svn_checkout)["healed_lines"] == 1


def test_a_snapshot_that_travelled_from_another_checkout_is_ignored(svn_checkout, tmp_path):
    """If .cognition/local is ever committed, a teammate's snapshot must not make this
    checkout report their uncommitted work as lost here."""
    other = tmp_path / "other" / ".cognition"
    (other.parent / ".svn").mkdir(parents=True)
    s = CognitionStorage(other)
    s.add_node(_node("theirs-only", "x"))
    del s
    CognitionStorage(other)

    (svn_checkout / "local").mkdir(parents=True, exist_ok=True)
    (svn_checkout / "local" / KNOWN_IDS_FILENAME).write_bytes(
        (other / "local" / KNOWN_IDS_FILENAME).read_bytes()
    )
    CognitionStorage(svn_checkout)
    assert _flag(svn_checkout) is None


def _personal(node_id, email):
    return CognitionNode(
        id=node_id, type=CognitionNodeType.CONSTRAINT, summary="private", detail="d",
        context=[], references=[], timestamp="2026-01-01T00:00:00+00:00", author="a",
        metadata={"scope": "personal", "recorded_by": {"name": "Alice", "email": email}},
    )


def test_a_teammates_hidden_constraint_is_never_named_in_a_loss_alert(svn_checkout, graph_identity):
    graph_identity.acting_as("Alice", "alice@corp.example")
    s = CognitionStorage(svn_checkout)
    s.add_node(_personal("alice-private", "alice@corp.example"))
    del s

    graph_identity.acting_as("Bob", "bob@corp.example")
    CognitionStorage(svn_checkout)
    assert _flag(svn_checkout) is None, "a hidden node that is still present is not lost"

    _drop_lines_containing(svn_checkout, "alice-private")
    CognitionStorage(svn_checkout)
    flag = _flag(svn_checkout)
    assert flag is None or "alice-private" not in json.dumps(flag)


def test_losing_your_own_personal_constraint_is_repaired_like_any_other_line(svn_checkout, graph_identity):
    """WP-Append-Only-Replay: used to assert the loss. A personal constraint is an
    ordinary line in its owner's shard, so it is repaired the same way -- and the
    repair notice never names it, keeping the owner-only visibility rule intact."""
    graph_identity.acting_as("Alice", "alice@corp.example")
    s = CognitionStorage(svn_checkout)
    s.add_node(_personal("alice-private", "alice@corp.example"))
    del s
    CognitionStorage(svn_checkout)
    _drop_lines_containing(svn_checkout, "alice-private")
    store = CognitionStorage(svn_checkout)
    assert store.has_node("alice-private")
    flag = _healed(svn_checkout)
    assert flag["healed_lines"] == 1
    assert "alice-private" not in json.dumps(flag), "the alert must not name a personal constraint"
