"""Regression test for the post-commit hook's git-output decoding (WP-3, finding b).

Pins the contract that post-commit.py decodes git output as UTF-8 rather than the
system locale codepage. Before the fix (subprocess `text=True` with no encoding),
a non-ASCII commit summary is mangled on a non-UTF-8 locale (Windows cp1252):
"§" -> "Â§". The fix adds encoding="utf-8".

The red-before state is only observable on a non-UTF-8-locale machine (e.g. this
Windows dev box). On a UTF-8 CI runner, `text=True` already decodes correctly, so
this passes there both before and after — it still guards the contract everywhere.
Validate the fails-before locally by codepoint comparison (==), never by eyeballing
terminal output, which re-renders the mangle identically to the correct glyph.
"""

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

_HOOK_PATH = Path(__file__).resolve().parents[1] / "hooks" / "post-commit.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _load_hook():
    spec = importlib.util.spec_from_file_location("post_commit_hook", _HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )


def test_get_latest_commit_decodes_utf8_message(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    # Make commits work on any host/CI (no global identity, no signing).
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "i18n.commitEncoding", "utf-8")

    (repo / "f.txt").write_text("hi", encoding="utf-8")
    _git(repo, "add", "f.txt")
    summary = "§8.1: café déjà vu"  # section sign + accented Latin-1
    _git(repo, "commit", "-q", "-m", summary)

    hook = _load_hook()
    commit = hook._get_latest_commit(str(repo))

    assert commit is not None
    # Codepoint-exact: a locale-decoded mangle ("Â§8.1...") differs here even
    # though a terminal may render it identically to the correct string.
    assert commit["message"] == summary
