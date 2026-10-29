"""One VCS detector for the whole plugin (WP-VCS-Isolation)."""

import os

import pytest

from vibe_cognition.cognition.vcs import VCS_GIT, VCS_NONE, VCS_SVN, detect_vcs, git_root, svn_root


def _svn(root):
    (root / ".svn").mkdir(parents=True, exist_ok=True)
    (root / ".svn" / "wc.db").write_bytes(b"")
    return root


def test_a_plain_folder_is_no_vcs(tmp_path):
    assert detect_vcs(tmp_path) == VCS_NONE
    assert svn_root(tmp_path) is None
    assert git_root(tmp_path) is None


def test_a_git_directory_is_git(tmp_path):
    (tmp_path / ".git").mkdir()
    assert detect_vcs(tmp_path) == VCS_GIT
    assert git_root(tmp_path) == tmp_path.resolve()


def test_a_gitlink_file_is_git_too(tmp_path):
    """Worktrees and submodules carry a `.git` FILE, not a directory."""
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
    assert detect_vcs(tmp_path) == VCS_GIT


def test_a_working_copy_database_is_svn(tmp_path):
    _svn(tmp_path)
    assert detect_vcs(tmp_path) == VCS_SVN
    assert svn_root(tmp_path) == tmp_path.resolve()


def test_a_bare_svn_directory_without_a_database_is_nothing(tmp_path):
    (tmp_path / ".svn").mkdir()
    assert detect_vcs(tmp_path) == VCS_NONE
    assert svn_root(tmp_path) is None


def test_a_project_below_the_working_copy_root_is_svn(tmp_path):
    """Since SVN 1.7 `.svn` exists only at the checkout root."""
    _svn(tmp_path)
    project = tmp_path / "trunk" / "proj"
    project.mkdir(parents=True)
    assert detect_vcs(project) == VCS_SVN
    assert svn_root(project) == tmp_path.resolve()


def test_a_project_below_a_git_root_is_git(tmp_path):
    (tmp_path / ".git").mkdir()
    project = tmp_path / "packages" / "proj"
    project.mkdir(parents=True)
    assert detect_vcs(project) == VCS_GIT
    assert git_root(project) == tmp_path.resolve()


def test_the_nearest_marker_wins_git_inside_svn(tmp_path):
    _svn(tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    assert detect_vcs(project) == VCS_GIT
    assert svn_root(project) is None


def test_the_nearest_marker_wins_svn_inside_git(tmp_path):
    (tmp_path / ".git").mkdir()
    project = _svn(tmp_path / "vendor" / "wc")
    assert detect_vcs(project) == VCS_SVN
    assert git_root(project) is None


def test_svn_wins_when_both_markers_share_a_level(tmp_path):
    """Colton ruling Q4 (2026-10-28): a git-svn bridge keeps the SVN pass."""
    _svn(tmp_path)
    (tmp_path / ".git").mkdir()
    assert detect_vcs(tmp_path) == VCS_SVN
    assert svn_root(tmp_path) == tmp_path.resolve()
    assert git_root(tmp_path) is None


def test_a_relative_path_is_resolved_before_walking(tmp_path, monkeypatch):
    _svn(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    assert detect_vcs(os.curdir) == VCS_SVN


@pytest.mark.parametrize("bad", ["", "\0nope"])
def test_an_unusable_path_never_raises(bad):
    assert detect_vcs(bad) in (VCS_GIT, VCS_SVN, VCS_NONE)
