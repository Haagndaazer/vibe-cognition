"""Automatic SVN setup against the REAL svn client. Local only: `uv run pytest -m svn`.

Deselected by default (pyproject addopts) and skipped where svn is not installed.
CI has no svn, so a green CI run says nothing about this module -- run this before
any release that touches svn_hygiene or resolve_journal.
"""

import json
import shutil
import subprocess

import pytest

from vibe_cognition.cognition.resolve_journal import find_conflicts, resolve
from vibe_cognition.cognition.svn_hygiene import (
    STATUS_OK,
    STATUS_PARENT_UNVERSIONED,
    add_new_files,
    add_new_files_in_background,
    ensure_svn_hygiene,
    svn_root,
)

pytestmark = [
    pytest.mark.svn,
    pytest.mark.timeout(300),
    pytest.mark.skipif(
        not (shutil.which("svn") and shutil.which("svnadmin")),
        reason="svn and svnadmin command-line clients are required",
    ),
]


class Lab:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.config = tmp_path / "svnconfig"
        self.repo = tmp_path / "repo"
        subprocess.run(["svnadmin", "create", str(self.repo)], check=True, capture_output=True)
        self.url = self.repo.resolve().as_uri()
        self.svn("mkdir", "-m", "init", f"{self.url}/trunk")

    def svn(self, *args, cwd=None, check=True):
        return subprocess.run(
            ["svn", args[0], "--non-interactive", "--config-dir", str(self.config), *args[1:]],
            cwd=cwd, check=check, capture_output=True, text=True,
        )

    def checkout(self, name, path="trunk"):
        wc = self.tmp / name
        self.svn("checkout", f"{self.url}/{path}", str(wc))
        return wc

    def status(self, wc):
        return self.svn("status", "--no-ignore", cwd=wc).stdout


def _seed(cognition):
    (cognition / "local").mkdir(parents=True)
    (cognition / "local" / "identity.json").write_text("{}", encoding="utf-8")
    (cognition / "people").mkdir()
    (cognition / "people" / "alice%40x.com.profile.jsonl").write_text("{}\n", encoding="utf-8")
    (cognition / "journal.jsonl").write_text('{"action": "add_node", "data": {"id": "a"}}\n', encoding="utf-8")
    (cognition / "journal.jsonl.lock").write_text("", encoding="utf-8")
    (cognition / ".gitignore").write_text("# managed\nlocal/\n*.lock\nchromadb/\n", encoding="utf-8")
    docs = cognition / "documents"
    (docs / "ab").mkdir(parents=True)
    (docs / "ab" / "shared.md").write_text("shared", encoding="utf-8")
    (docs / "cd").mkdir()
    (docs / "cd" / "private.pdf").write_bytes(b"%PDF secret")
    (docs / ".gitignore").write_text(".gitignore\ncd/private.pdf\n", encoding="utf-8")


def _status_of(lab, wc, rel):
    for line in lab.status(wc).splitlines():
        if line[8:].replace("\\", "/").strip() == rel:
            return line[:7]
    return None


@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


def test_first_session_versions_cognition_without_any_machine_local_or_private_file(lab):
    wc = lab.checkout("alice")
    cognition = wc / ".cognition"
    _seed(cognition)

    report = ensure_svn_hygiene(cognition)
    assert report is not None and report.status == STATUS_OK, report
    assert report.property_set_now
    assert report.property_pending

    ignores = lab.svn("propget", "svn:global-ignores", ".cognition", cwd=wc).stdout.split()
    assert {"local", "*.lock", "chromadb"} <= set(ignores)

    for committed in (".cognition/journal.jsonl", ".cognition/people/alice%40x.com.profile.jsonl",
                      ".cognition/.gitignore", ".cognition/documents/ab/shared.md"):
        assert (_status_of(lab, wc, committed) or "")[0] == "A", (committed, lab.status(wc))
    assert (_status_of(lab, wc, ".cognition/local") or "")[0] == "I"
    assert (_status_of(lab, wc, ".cognition/journal.jsonl.lock") or "")[0] == "I"
    for private in (".cognition/documents/cd/private.pdf", ".cognition/documents/.gitignore",
                    ".cognition/documents/cd"):
        assert (_status_of(lab, wc, private) or "?")[0] != "A", (private, lab.status(wc))

    lab.svn("commit", "-m", "setup", cwd=wc)
    listed = lab.svn("ls", "-R", f"{lab.url}/trunk/.cognition").stdout
    assert "identity.json" not in listed and "private.pdf" not in listed
    assert "journal.jsonl" in listed and "alice%40x.com.profile.jsonl" in listed


def test_adding_never_happens_before_the_ignore_property_exists(lab):
    """Found in the live end-to-end run: a profile written in a project's FIRST
    session triggered a bare add before any session-start pass had set the
    property, and local/identity.json was committed."""
    wc = lab.checkout("alice")
    cognition = wc / ".cognition"
    _seed(cognition)

    assert add_new_files(shutil.which("svn"), svn_root(wc), cognition) == []
    assert _status_of(lab, wc, ".cognition") in (None, "?      ")

    lab.svn("add", "--depth", "empty", ".cognition", cwd=wc)
    assert add_new_files(shutil.which("svn"), svn_root(wc), cognition) == [], (
        "versioned but no ignore property yet: still nothing may be added"
    )


def test_the_background_add_in_a_first_session_sets_the_property_first(lab):
    wc = lab.checkout("alice")
    cognition = wc / ".cognition"
    _seed(cognition)

    thread = add_new_files_in_background(cognition)
    assert thread is not None
    thread.join(timeout=120)

    assert (_status_of(lab, wc, ".cognition/people/alice%40x.com.profile.jsonl") or "")[0] == "A"
    assert (_status_of(lab, wc, ".cognition/local") or "")[0] == "I", lab.status(wc)
    lab.svn("commit", "-m", "first session", cwd=wc)
    assert "identity.json" not in lab.svn("ls", "-R", f"{lab.url}/trunk/.cognition").stdout


def test_a_second_session_changes_nothing(lab):
    wc = lab.checkout("alice")
    _seed(wc / ".cognition")
    ensure_svn_hygiene(wc / ".cognition")
    lab.svn("commit", "-m", "setup", cwd=wc)

    report = ensure_svn_hygiene(wc / ".cognition")
    assert report.status == STATUS_OK
    assert not report.property_set_now and report.added_now == []
    assert report.pending_adds == 0 and not report.property_pending


def test_svn_revert_of_the_property_is_noticed_and_repaired(lab):
    """A persisted "done" flag would keep believing the property exists."""
    wc = lab.checkout("alice")
    _seed(wc / ".cognition")
    ensure_svn_hygiene(wc / ".cognition")
    lab.svn("commit", "-m", "setup", cwd=wc)
    lab.svn("propdel", "svn:global-ignores", ".cognition", cwd=wc)

    report = ensure_svn_hygiene(wc / ".cognition")
    assert report.property_set_now
    assert "local" in lab.svn("propget", "svn:global-ignores", ".cognition", cwd=wc).stdout


def test_an_existing_ignore_property_is_merged_not_replaced(lab):
    wc = lab.checkout("alice")
    cognition = wc / ".cognition"
    _seed(cognition)
    lab.svn("add", "--depth", "empty", ".cognition", cwd=wc)
    (lab.tmp / "team.txt").write_text("team-scratch\n", encoding="utf-8")
    lab.svn("propset", "svn:global-ignores", "-F", str(lab.tmp / "team.txt"), ".cognition", cwd=wc)

    ensure_svn_hygiene(cognition)
    values = lab.svn("propget", "svn:global-ignores", ".cognition", cwd=wc).stdout.split()
    assert values[0] == "team-scratch"
    assert "local" in values


def test_a_new_teammates_profile_is_added_before_their_first_commit(lab):
    """`svn commit` never includes new files and succeeds silently without them."""
    alice = lab.checkout("alice")
    _seed(alice / ".cognition")
    ensure_svn_hygiene(alice / ".cognition")
    lab.svn("commit", "-m", "setup", cwd=alice)

    bob = lab.checkout("bob")
    profile = bob / ".cognition" / "people" / "bob@odd%40x.com.profile.jsonl"
    profile.write_text("{}\n", encoding="utf-8")
    root = svn_root(bob)
    added = add_new_files(shutil.which("svn"), root, bob / ".cognition")
    assert ".cognition/people/bob@odd%40x.com.profile.jsonl" in added
    lab.svn("commit", "-m", "bob joins", cwd=bob)
    assert "bob@odd%40x.com.profile.jsonl" in lab.svn("ls", f"{lab.url}/trunk/.cognition/people").stdout


def test_leftover_conflict_files_without_a_live_conflict_are_never_added(lab):
    """A copy of .mine / .rN kept after resolving by hand must not ride into a commit."""
    wc = lab.checkout("alice")
    _seed(wc / ".cognition")
    ensure_svn_hygiene(wc / ".cognition")
    lab.svn("commit", "-m", "setup", cwd=wc)
    for leftover in ("journal.jsonl.mine", "journal.jsonl.r7", "journal.jsonl.merge-left.r2"):
        (wc / ".cognition" / leftover).write_text("{}\n", encoding="utf-8")

    report = ensure_svn_hygiene(wc / ".cognition")
    assert report.added_now == []
    assert (_status_of(lab, wc, ".cognition/journal.jsonl.mine") or "")[0] == "?"


def test_an_identity_file_stranded_at_the_legacy_path_is_never_added(lab):
    """Review finding, reproduced: a relocation deferred by a locked file left
    identity.json directly under .cognition/, and the add scheduled it."""
    wc = lab.checkout("alice")
    cognition = wc / ".cognition"
    _seed(cognition)
    (cognition / "identity.json").write_text('{"name": "Alice"}', encoding="utf-8")
    (cognition / "last-seen.json.tmp").write_text("{}", encoding="utf-8")

    report = ensure_svn_hygiene(cognition)
    assert report.status == STATUS_OK
    assert (_status_of(lab, wc, ".cognition/identity.json") or "")[0] == "I", lab.status(wc)
    assert (_status_of(lab, wc, ".cognition/last-seen.json.tmp") or "")[0] == "I"


def test_a_conflict_without_a_mine_file_is_still_found_and_resolved(lab):
    """Review finding, reproduced: with .mine deleted, svn still reports the file
    conflicted and the session start names it, but the resolver saw nothing."""
    alice = lab.checkout("alice")
    _seed(alice / ".cognition")
    ensure_svn_hygiene(alice / ".cognition")
    lab.svn("commit", "-m", "setup", cwd=alice)
    bob = lab.checkout("bob")
    line_a = json.dumps({"action": "add_node", "data": {"id": "from-alice"}})
    line_b = json.dumps({"action": "add_node", "data": {"id": "from-bob"}})
    with (alice / ".cognition" / "journal.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line_a + "\n")
    lab.svn("commit", "-m", "a", cwd=alice)
    with (bob / ".cognition" / "journal.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line_b + "\n")
    lab.svn("update", "--accept", "postpone", cwd=bob, check=False)
    (bob / ".cognition" / "journal.jsonl.mine").unlink()

    [target] = find_conflicts(bob / ".cognition")
    result = resolve(target)
    assert result["resolved"], result
    lines = target.read_text(encoding="utf-8").splitlines()
    assert line_a in lines and line_b in lines


def test_a_locked_working_copy_fails_fast_with_the_real_reason(lab):
    """Review finding: svn waited ~14s on a locked wc.db, stalling session start,
    and every failure read 'busy or locked?' whatever the cause."""
    import sqlite3
    import time

    wc = lab.checkout("alice")
    _seed(wc / ".cognition")
    conn = sqlite3.connect(wc / ".svn" / "wc.db", timeout=1)
    conn.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        report = ensure_svn_hygiene(wc / ".cognition")
        elapsed = time.monotonic() - started
    finally:
        conn.rollback()
        conn.close()
    assert report.status == "failed"
    assert "busy" in report.detail and "E200033" in report.detail
    assert elapsed < 8, f"session start blocked {elapsed:.1f}s on a locked working copy"


def test_a_project_folder_not_yet_in_svn_is_reported_not_forced_in(lab):
    wc = lab.checkout("alice")
    project = wc / "proj"
    cognition = project / ".cognition"
    _seed(cognition)
    report = ensure_svn_hygiene(cognition)
    assert report.status == STATUS_PARENT_UNVERSIONED
    assert "?" in (_status_of(lab, wc, "proj") or "?")


def test_a_project_inside_a_larger_checkout_is_set_up(lab):
    lab.svn("mkdir", "-m", "proj", f"{lab.url}/trunk/proj")
    wc = lab.checkout("alice")
    cognition = wc / "proj" / ".cognition"
    _seed(cognition)
    report = ensure_svn_hygiene(cognition)
    assert report.status == STATUS_OK and report.property_set_now
    assert (_status_of(lab, wc, "proj/.cognition/journal.jsonl") or "")[0] == "A"


def test_a_real_journal_conflict_is_reported_and_resolved_keeping_both_sides(lab):
    alice = lab.checkout("alice")
    _seed(alice / ".cognition")
    ensure_svn_hygiene(alice / ".cognition")
    lab.svn("commit", "-m", "setup", cwd=alice)
    bob = lab.checkout("bob")

    edit_a = json.dumps({"action": "update_node", "data": {"id": "a", "summary": "alice"}})
    edit_b = json.dumps({"action": "update_node", "data": {"id": "a", "summary": "bob"}})
    edge_b = json.dumps({"action": "add_edge", "data": {"from_id": "a", "to_id": "b"}})
    with (alice / ".cognition" / "journal.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(edit_a + "\n")
    lab.svn("commit", "-m", "alice edits", cwd=alice)
    with (bob / ".cognition" / "journal.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(edit_b + "\n" + edge_b + "\n")
    lab.svn("update", "--accept", "postpone", cwd=bob, check=False)

    report = ensure_svn_hygiene(bob / ".cognition")
    assert report.conflicted == [".cognition/journal.jsonl"]
    # The .mine / .rN leftovers are unversioned; a blind add would commit them.
    assert report.added_now == []
    assert (_status_of(lab, bob, ".cognition/journal.jsonl.mine") or "")[0] == "?"

    new_profile = bob / ".cognition" / "people" / "carol%40x.com.profile.jsonl"
    new_profile.write_text("{}\n", encoding="utf-8")
    during = ensure_svn_hygiene(bob / ".cognition")
    assert during.added_now == [], "nothing is added while a conflict is unresolved"

    [target] = find_conflicts(bob / ".cognition")
    result = resolve(target)
    assert result["resolved"], result
    lines = target.read_text(encoding="utf-8").splitlines()
    assert edit_a in lines and edit_b in lines and edge_b in lines
    assert "C" not in (_status_of(lab, bob, ".cognition/journal.jsonl") or "")
    lab.svn("commit", "-m", "bob resolves", cwd=bob)
