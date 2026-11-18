"""Which version control system a project is under. Stdlib only, imports nothing
from the package, so every module can use it without a cycle.

One rule for everyone (WP-VCS-Isolation): walk up from the project root and report
the first level holding a marker. `.svn/wc.db` means Subversion (a bare `.svn`
directory without the database is not a working copy); `.git` means git, whether a
directory or the gitlink file of a worktree or submodule. When both sit at the same
level (a git-svn bridge) Subversion wins, matching the order the SVN setup pass and
the journal watch always used. Neither anywhere above means no VCS, which keeps the
pre-`git init` behaviour of a plain folder.
"""

import contextlib
from pathlib import Path

VCS_GIT = "git"
VCS_SVN = "svn"
VCS_NONE = "none"


def _resolved(start: Path | str) -> Path | None:
    try:
        return Path(start).resolve()
    except OSError:
        return None


def _is_svn_root(candidate: Path) -> bool:
    with contextlib.suppress(OSError):
        return (candidate / ".svn" / "wc.db").is_file()
    return False


def _is_git_root(candidate: Path) -> bool:
    with contextlib.suppress(OSError):
        return (candidate / ".git").exists()
    return False


def svn_root(start: Path | str) -> Path | None:
    """The working-copy root at or above `start`; None under git or outside SVN."""
    path = _resolved(start)
    if path is None:
        return None
    for candidate in (path, *path.parents):
        if _is_svn_root(candidate):
            return candidate
        if _is_git_root(candidate):
            return None
    return None


def git_root(start: Path | str) -> Path | None:
    """The directory holding `.git` at or above `start`; None under SVN or outside git."""
    path = _resolved(start)
    if path is None:
        return None
    for candidate in (path, *path.parents):
        if _is_svn_root(candidate):
            return None
        if _is_git_root(candidate):
            return candidate
    return None


def detect_vcs(repo_path: Path | str) -> str:
    """VCS_SVN, VCS_GIT or VCS_NONE for the project at `repo_path`. Never raises."""
    path = _resolved(repo_path)
    if path is None:
        return VCS_NONE
    for candidate in (path, *path.parents):
        if _is_svn_root(candidate):
            return VCS_SVN
        if _is_git_root(candidate):
            return VCS_GIT
    return VCS_NONE
