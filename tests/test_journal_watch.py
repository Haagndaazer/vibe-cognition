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
    LOSS_KIND_BETWEEN_SESSIONS,
    check_between_sessions,
)
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
    path = cognition / "local" / REHYDRATE_FLAG_FILENAME
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _lost(cognition) -> dict:
    flag = _flag(cognition)
    assert flag is not None, "the loss went unnoticed"
    return flag


def test_losing_your_own_work_between_sessions_raises_the_alert(svn_checkout):
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("keep1", "stays"))
    s.add_node(_node("gone1", "lost to use-theirs"))
    s.add_node(_node("gone2", "also lost"))
    del s  # session ends

    _drop_lines_containing(svn_checkout, "gone1", "gone2")

    CognitionStorage(svn_checkout)  # next session start
    flag = _flag(svn_checkout)
    assert flag is not None, "the loss went unnoticed"
    assert flag["kind"] == LOSS_KIND_BETWEEN_SESSIONS
    assert flag["nodes_lost"] == 2
    assert set(flag["sample_missing_ids"]) == {"gone1", "gone2"}


def test_the_alert_names_the_cause_and_the_recovery_commands(svn_checkout):
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("gone1", "x"))
    del s
    _drop_lines_containing(svn_checkout, "gone1")
    CognitionStorage(svn_checkout)

    message = _consume_rehydrate_flag(svn_checkout)
    assert "between sessions" in message
    assert "use mine" in message and "use theirs" in message
    assert "svn log .cognition/journal .cognition/journal.jsonl" in message
    assert "svn cat" in message
    # A recovery recipe handed to an agent must not be run on its own say-so.
    assert "without their go-ahead" in message


def test_a_lost_id_is_reported_once_not_on_every_later_startup(svn_checkout):
    """A first draft kept lost ids in the log, so every later startup re-reported
    them as newly lost -- a permanent false alarm that trains people to ignore it."""
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("gone1", "x"))
    del s
    _drop_lines_containing(svn_checkout, "gone1")

    CognitionStorage(svn_checkout)
    assert _lost(svn_checkout)["nodes_lost"] == 1
    CognitionStorage(svn_checkout)
    CognitionStorage(svn_checkout)
    assert _lost(svn_checkout)["nodes_lost"] == 1, "the same loss was counted again"


def test_two_separate_losses_before_anyone_reads_the_alert_are_both_reported(svn_checkout):
    """The flag is added to, not overwritten: a later, smaller loss must not replace
    an unread earlier one."""
    s = CognitionStorage(svn_checkout)
    s.add_node(_node("first", "x"))
    s.add_node(_node("second", "y"))
    del s
    _drop_lines_containing(svn_checkout, "first")
    CognitionStorage(svn_checkout)
    _drop_lines_containing(svn_checkout, "second")
    CognitionStorage(svn_checkout)

    assert _lost(svn_checkout)["nodes_lost"] == 2


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

    CognitionStorage(svn_checkout)
    assert _lost(svn_checkout)["sample_missing_ids"] == ["mid-session"]


def test_a_first_session_has_no_baseline_and_says_nothing(svn_checkout):
    s = CognitionStorage(svn_checkout)
    assert check_between_sessions(s) is None
    assert (svn_checkout / "local" / KNOWN_IDS_FILENAME).exists()


def test_git_checkouts_are_left_alone(tmp_path, graph_identity):
    """On git, switching branches legitimately removes ids that live only on the other
    branch; alerting there would fire on every checkout."""
    root = tmp_path / "gitwc"
    (root / ".git").mkdir(parents=True)
    cognition = root / ".cognition"
    s = CognitionStorage(cognition)
    s.add_node(_node("on-branch", "x"))
    del s
    _drop_lines_containing(cognition, "on-branch")

    CognitionStorage(cognition)
    assert _flag(cognition) is None
    assert not (cognition / "local" / KNOWN_IDS_FILENAME).exists()


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
    CognitionStorage(svn_checkout)
    assert _lost(svn_checkout)["sample_missing_ids"] == ["mine"]


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

    CognitionStorage(cognition)
    assert _lost(cognition)["sample_missing_ids"] == ["nested-gone"]


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


def test_a_git_project_nested_in_an_svn_checkout_is_left_alone(tmp_path, graph_identity):
    outer = tmp_path / "svnwc"
    (outer / ".svn").mkdir(parents=True)
    (outer / "gitproj" / ".git").mkdir(parents=True)
    cognition = outer / "gitproj" / ".cognition"
    CognitionStorage(cognition)
    assert not (cognition / "local" / KNOWN_IDS_FILENAME).exists()


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

    CognitionStorage(svn_checkout)
    assert _lost(svn_checkout)["sample_missing_ids"] == ["gone1"]


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


def test_losing_your_own_personal_constraint_is_still_reported(svn_checkout, graph_identity):
    graph_identity.acting_as("Alice", "alice@corp.example")
    s = CognitionStorage(svn_checkout)
    s.add_node(_personal("alice-private", "alice@corp.example"))
    del s
    CognitionStorage(svn_checkout)
    _drop_lines_containing(svn_checkout, "alice-private")
    CognitionStorage(svn_checkout)
    assert _lost(svn_checkout)["sample_missing_ids"] == ["alice-private"]
