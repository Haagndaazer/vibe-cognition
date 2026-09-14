"""Automatic SVN setup: the parts that need no real svn binary.

The real-binary suite is tests/test_svn_lab.py (marker `svn`, local only).
"""

import json

import pytest

from vibe_cognition.cognition import svn_hygiene
from vibe_cognition.cognition.git_hygiene import vcs_hygiene_opted_out
from vibe_cognition.cognition.svn_hygiene import (
    STATUS_COGNITION_IGNORED,
    STATUS_FAILED,
    STATUS_NO_CLI,
    STATUS_OK,
    STATUS_PARENT_UNVERSIONED,
    SvnHygieneReport,
    ensure_svn_hygiene,
    format_svn_announce,
    ignore_globs,
    local_only_paths,
    parse_status_xml,
    svn_root,
)

RESOLVE = '"python" -m vibe_cognition.cognition.resolve_journal "proj"'


def _fake_wc(root):
    (root / ".svn").mkdir(parents=True)
    (root / ".svn" / "wc.db").write_bytes(b"")
    return root


def test_ignore_globs_strip_every_trailing_slash_and_keep_user_entries(tmp_path):
    """`local/` matches NOTHING in SVN; only bare `local` ignores the identity file."""
    cognition = tmp_path / ".cognition"
    cognition.mkdir()
    (cognition / ".gitignore").write_text(
        "# vibe-cognition managed\nlocal/\n*.lock\nchromadb/\nidentity.json*\nmy-scratch/\n\n",
        encoding="utf-8",
    )
    globs = ignore_globs(cognition)
    assert globs[:5] == ["local", "*.lock", "chromadb", "identity.json*", "my-scratch"]
    assert "last-seen.json*" in globs
    assert len(globs) == len(set(globs))
    assert not any(g.endswith("/") or g.startswith("#") for g in globs)


def test_ignore_globs_cover_the_managed_entries_even_without_a_gitignore(tmp_path):
    cognition = tmp_path / ".cognition"
    cognition.mkdir()
    assert {"local", "*.lock", "chromadb"} <= set(ignore_globs(cognition))


def test_machine_local_files_are_ignored_by_name_at_the_legacy_path_too(tmp_path):
    """Review finding: a deferred relocation leaves identity.json directly under
    .cognition/, where the `local` glob does not reach it."""
    from vibe_cognition.cognition.local_paths import RELOCATED_FILENAMES
    from vibe_cognition.cognition.svn_hygiene import matches_ignore

    cognition = tmp_path / ".cognition"
    cognition.mkdir()
    globs = ignore_globs(cognition)
    for name in RELOCATED_FILENAMES:
        assert matches_ignore(f".cognition/{name}", ".cognition", globs), name
        assert matches_ignore(f".cognition/{name}.tmp", ".cognition", globs), name


def test_svn_error_text_reaches_the_report_with_a_useful_hint():
    svn_hygiene._diagnostics.last_error = "svn: E155036: Please see the 'svn upgrade' command"
    report = svn_hygiene._failure("svn status failed")
    assert "E155036" in report.detail and "svn upgrade" in report.detail

    svn_hygiene._diagnostics.last_error = "svn: E200033: sqlite[S5]: database is locked"
    assert "busy" in svn_hygiene._failure("svn status failed").detail
    svn_hygiene._diagnostics.last_error = ""


def test_status_xml_is_parsed_into_posix_paths():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<status><target path=".cognition">
<entry path=".cognition\\journal.jsonl"><wc-status item="modified" props="none"/></entry>
<entry path=".cognition\\people\\sub"><wc-status item="unversioned" props="none"/></entry>
<entry path=".cognition"><wc-status item="normal" props="modified" tree-conflicted="true"/></entry>
</target></status>"""
    entries = parse_status_xml(xml)
    assert entries[".cognition/journal.jsonl"]["item"] == "modified"
    assert entries[".cognition/people/sub"]["item"] == "unversioned"
    assert entries[".cognition"]["props"] == "modified"
    assert entries[".cognition"]["tree_conflicted"] == "true"


def test_local_only_documents_are_never_added(tmp_path):
    """documents/.gitignore holds the per-machine local-only list, and SVN does not
    read it -- so without this, automatic adds would commit documents the user
    deliberately kept off the repository."""
    root = _fake_wc(tmp_path / "wc")
    cognition = root / ".cognition"
    docs = cognition / "documents"
    docs.mkdir(parents=True)
    (docs / ".gitignore").write_text(".gitignore\nab/abcdef.pdf\n", encoding="utf-8")
    excluded = local_only_paths(root.resolve(), cognition)
    assert ".cognition/documents/.gitignore" in excluded
    assert ".cognition/documents/ab/abcdef.pdf" in excluded


@pytest.mark.parametrize("name,artifact", [
    (".cognition/journal.jsonl.mine", True),
    (".cognition/journal.jsonl.r12", True),
    (".cognition/journal.jsonl.working", True),
    (".cognition/journal.jsonl.merge-left.r3", True),
    (".cognition/journal.jsonl.merge-right.r4", True),
    (".cognition/dir_conflicts.prej", True),
    (".cognition/journal.jsonl", False),
    (".cognition/people/a%40x.com.profile.jsonl", False),
    (".cognition/documents/ab/report.pdf", False),
])
def test_conflict_leftovers_are_recognised(name, artifact):
    from vibe_cognition.cognition.svn_hygiene import is_conflict_artifact

    assert is_conflict_artifact(name) is artifact


@pytest.mark.parametrize("path,ignored", [
    (".cognition/local/identity.json", True),
    (".cognition/local", True),
    (".cognition/journal.jsonl.lock", True),
    (".cognition/chromadb/x.bin", True),
    (".cognition/journal.jsonl", False),
    (".cognition/people/a%40x.com.profile.jsonl", False),
])
def test_a_path_matching_an_ignore_glob_is_never_added_whatever_the_property_says(path, ignored):
    from vibe_cognition.cognition.svn_hygiene import matches_ignore

    assert matches_ignore(path, ".cognition", ["local", "*.lock", "chromadb"]) is ignored


def test_svn_root_walks_up_and_needs_a_real_working_copy_database(tmp_path):
    root = _fake_wc(tmp_path / "trunk")
    project = root / "proj"
    project.mkdir()
    assert svn_root(project) == root.resolve()

    stray = tmp_path / "stray"
    (stray / ".svn").mkdir(parents=True)
    assert svn_root(stray) is None


def test_svn_root_stops_at_a_git_repository(tmp_path):
    root = _fake_wc(tmp_path / "svnwc")
    inner = root / "gitproj"
    (inner / ".git").mkdir(parents=True)
    assert svn_root(inner) is None


def test_the_pass_does_nothing_outside_svn(tmp_path):
    cognition = tmp_path / ".cognition"
    cognition.mkdir()
    assert ensure_svn_hygiene(cognition) is None


def test_no_svn_client_is_reported_not_raised(tmp_path):
    root = _fake_wc(tmp_path / "wc")
    cognition = root / ".cognition"
    cognition.mkdir()
    report = ensure_svn_hygiene(cognition)
    assert report is not None and report.status == STATUS_NO_CLI


@pytest.mark.parametrize("name", ["VIBE_COGNITION_NO_VCS_HYGIENE", "VIBE_COGNITION_NO_GIT_HYGIENE"])
def test_either_opt_out_name_suppresses_the_svn_pass(tmp_path, monkeypatch, name):
    monkeypatch.setenv(name, "1")
    assert vcs_hygiene_opted_out()
    root = _fake_wc(tmp_path / "wc")
    cognition = root / ".cognition"
    cognition.mkdir()
    assert ensure_svn_hygiene(cognition) is None


def test_a_crash_inside_the_pass_is_reported_not_raised(tmp_path, monkeypatch):
    root = _fake_wc(tmp_path / "wc")
    cognition = root / ".cognition"
    cognition.mkdir()
    monkeypatch.setattr(svn_hygiene, "_svn_command", lambda: "svn")

    def boom(*_a, **_k):
        raise RuntimeError("wc.db is busy")

    monkeypatch.setattr(svn_hygiene, "_run_pass", boom)
    report = ensure_svn_hygiene(cognition)
    assert report is not None and report.status == STATUS_FAILED
    assert "busy" in report.detail


@pytest.mark.parametrize("status", [
    STATUS_OK, STATUS_NO_CLI, STATUS_PARENT_UNVERSIONED, STATUS_COGNITION_IGNORED, STATUS_FAILED,
])
def test_every_announce_keeps_the_no_union_merge_warning_and_the_fix(status):
    """A reader who knows git would assume parity; this line must survive every branch."""
    line = format_svn_announce(SvnHygieneReport(status), RESOLVE)
    assert "SVN has no union merge" in line
    assert RESOLVE in line
    assert "use theirs" in line


def test_the_announce_says_what_it_changed_and_that_nothing_was_committed():
    line = format_svn_announce(SvnHygieneReport(
        STATUS_OK, property_set_now=True, added_now=["a", "b"], pending_adds=2,
        property_pending=True,
    ), RESOLVE)
    assert "changed your working copy" in line
    assert "scheduled 2 new item(s)" in line
    assert "Nothing was committed" in line
    assert "SECOND column" in line
    assert "commit" in line


def test_a_conflict_is_called_out_first_with_the_resolver():
    line = format_svn_announce(SvnHygieneReport(
        STATUS_OK, conflicted=[".cognition/journal.jsonl"],
    ), RESOLVE)
    assert line.startswith("CONFLICT in .cognition/journal.jsonl")
    assert "TELL THE USER" in line
    assert line.count(RESOLVE) == 1
    assert "SVN has no union merge" in line and "use theirs" in line


def test_an_excluded_journal_is_a_loud_warning():
    line = format_svn_announce(SvnHygieneReport(STATUS_OK, journal_excluded=True), RESOLVE)
    assert "never shared" in line and "TELL THE USER" in line


def test_a_quiet_checkout_gets_one_short_line():
    line = format_svn_announce(SvnHygieneReport(STATUS_OK), RESOLVE)
    assert "set up and nothing is pending" in line
    assert "changed your working copy" not in line


def test_session_start_on_an_svn_checkout_uses_the_svn_announce(tmp_path, monkeypatch, capsys):
    """The git line says local files are ignored; on SVN that is false until the
    property is set, so an SVN checkout must never be shown it."""
    from vibe_cognition.cognition import prime

    root = _fake_wc(tmp_path / "wc")
    cognition = root / ".cognition"
    cognition.mkdir()
    monkeypatch.setenv("REPO_PATH", str(root))
    prime.main([])
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "command-line client is not on PATH" in context
    assert "local-only files ignored" not in context
