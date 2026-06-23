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
import json
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


def test_generate_id_discriminates_by_commit_hash():
    """WP-ID: two DISTINCT commits with an identical message + identical timestamp must
    get DISTINCT episode ids — the commit hash discriminates. Fails-before (no
    discriminator): identical inputs → identical id → the second episode overwrites the
    first on replay (data loss)."""
    hook = _load_hook()
    ts = "2026-06-13T00:00:00+00:00"
    msg = "same message"
    id_a = hook._generate_id("episode", msg, ts, "a" * 40)
    id_b = hook._generate_id("episode", msg, ts, "b" * 40)
    assert id_a != id_b, "identical message+timestamp collided despite distinct commit hashes"
    # Same commit (same hash) is stable/idempotent.
    assert hook._generate_id("episode", msg, ts, "a" * 40) == id_a


# ── T-1b extensions ───────────────────────────────────────────────────────────


def _make_repo_with_commit(base_path: Path) -> tuple[Path, str]:
    """Create a minimal git repo with one commit; return (repo_path, commit_hash)."""
    repo = base_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "f.txt").write_text("hello", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "initial commit")
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%H"],
        capture_output=True, text=True, encoding="utf-8",
    )
    return repo, result.stdout.strip()


def test_main_non_bash_tool_emits_empty_json(tmp_path):
    """main(): non-Bash tool_name → {} stdout, no exception.

    Fails-before: if main() crashed or printed non-JSON when tool_name was not
    'Bash' (the hook must be a no-op for all non-commit tool calls).
    """
    import io
    hook = _load_hook()
    hook_input = json.dumps({"tool_name": "Read", "tool_input": {"command": ""}})
    buf = io.StringIO()

    import sys
    old_stdin, old_stdout = sys.stdin, sys.stdout
    try:
        sys.stdin = io.StringIO(hook_input)
        sys.stdout = buf
        hook.main()
    finally:
        sys.stdin, sys.stdout = old_stdin, old_stdout

    assert json.loads(buf.getvalue()) == {}


def test_main_bash_non_commit_emits_empty_json(tmp_path):
    """main(): Bash with no 'git commit' in command → {} stdout.

    Fails-before: if the hook triggered on any Bash command and tried to call
    git log unconditionally (slow + spurious journal entries on every read).
    """
    import io
    import sys
    hook = _load_hook()
    hook_input = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
    buf = io.StringIO()
    old_stdin, old_stdout = sys.stdin, sys.stdout
    try:
        sys.stdin = io.StringIO(hook_input)
        sys.stdout = buf
        hook.main()
    finally:
        sys.stdin, sys.stdout = old_stdin, old_stdout

    assert json.loads(buf.getvalue()) == {}


def test_main_records_episode_shape_and_idempotent(tmp_path):
    """main(): git commit command → episode node appended; second call → {} (idempotent).

    Pins the episode journal-record shape:
      {action:'add_node', data:{id, type:'episode', summary(<=250), detail,
       context:[files], references:['commit:<hash>'], severity, timestamp, author}}

    Fails-before: if the hook wrote a different action name, dropped 'commit:' prefix,
    truncated summary beyond 250 chars, or allowed a duplicate episode on a second call.
    """
    import io
    import sys
    hook = _load_hook()
    repo, commit_hash = _make_repo_with_commit(tmp_path)
    cognition_dir = repo / ".cognition"
    cognition_dir.mkdir()
    journal = cognition_dir / "journal.jsonl"

    hook_input = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m 'initial commit'"},
    })

    def _run_main():
        buf = io.StringIO()
        old_stdin, old_stdout = sys.stdin, sys.stdout
        try:
            sys.stdin = io.StringIO(hook_input)
            sys.stdout = buf
            import os
            old_repo = os.environ.get("REPO_PATH")
            os.environ["REPO_PATH"] = str(repo)
            try:
                hook.main()
            finally:
                if old_repo is None:
                    os.environ.pop("REPO_PATH", None)
                else:
                    os.environ["REPO_PATH"] = old_repo
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout
        return json.loads(buf.getvalue())

    # First call: episode must be appended.
    out1 = _run_main()
    assert out1 == {}
    assert journal.exists()
    lines = [ln for ln in journal.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert record["action"] == "add_node"
    data = record["data"]
    assert data["type"] == "episode"
    assert len(data["summary"]) <= 250
    assert isinstance(data["context"], list)
    assert any(r.startswith("commit:") for r in data["references"]), (
        "references must contain 'commit:<hash>' so the episode links to the commit"
    )
    assert data["severity"] is None
    assert "timestamp" in data
    assert "author" in data
    assert "id" in data

    # Second call with same commit: idempotent → no new line appended.
    _run_main()
    lines2 = [ln for ln in journal.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines2) == 1, "idempotency broken: second call must not append a duplicate episode"


def test_main_missing_cognition_dir_emits_empty_json(tmp_path):
    """main(): .cognition/ absent → {} stdout (no crash, no journal created).

    Fails-before: if the hook tried to open journal.jsonl without checking that
    .cognition/ exists, raising FileNotFoundError that breaks the Bash call.
    """
    import io
    import os
    import sys
    hook = _load_hook()
    repo, _ = _make_repo_with_commit(tmp_path)
    # Do NOT create .cognition/ — the hook must bail gracefully.

    hook_input = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m 'test'"},
    })
    buf = io.StringIO()
    old_stdin, old_stdout = sys.stdin, sys.stdout
    old_repo = os.environ.get("REPO_PATH")
    try:
        sys.stdin = io.StringIO(hook_input)
        sys.stdout = buf
        os.environ["REPO_PATH"] = str(repo)
        hook.main()
    finally:
        sys.stdin, sys.stdout = old_stdin, old_stdout
        if old_repo is None:
            os.environ.pop("REPO_PATH", None)
        else:
            os.environ["REPO_PATH"] = old_repo

    assert json.loads(buf.getvalue()) == {}
