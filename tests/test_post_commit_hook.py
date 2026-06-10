"""Regression test for the post-commit hook's git-output decoding (WP-3, finding b).

Pins the contract that post-commit.py decodes git output as UTF-8 rather than the
system locale codepage. Before the fix (subprocess `text=True` with no encoding),
a non-ASCII commit summary is mangled on a non-UTF-8 locale (Windows cp1252):
"§" -> "Â§". The fix adds encoding="utf-8".

The red-before state is observable on any non-UTF-8-locale machine — both this
Windows dev box AND the CI `windows-latest` leg (cp1252 ACP, Python 3.11 with no
UTF-8 default), so the Windows CI leg genuinely gates a revert of the encoding fix.
The UTF-8 CI legs (ubuntu) decode correctly with or without the fix, so they don't
gate it — they still guard the contract. (A forced-locale Linux gate via a child
interpreter with `-X utf8=0` is available if the Windows leg is ever dropped or
moves to 3.15+/PEP 686, but is not needed today.)
Validate the fails-before by codepoint comparison (==), never by eyeballing terminal
output, which re-renders the mangle identically to the correct glyph.
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
    # Adversarial: a user whose global config would re-encode `git log` output to
    # latin1. The hook's `-c i18n.logOutputEncoding=utf-8` override must win, or the
    # utf-8 decode sees latin1 bytes and mangles (exercises WP-3 finding c).
    _git(repo, "config", "i18n.logOutputEncoding", "latin1")

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
