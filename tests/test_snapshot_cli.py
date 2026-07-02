"""Tests for the vibe-cognition-snapshot console entry point (WP-9, 4350a42fc4e5).

No subprocess: invokes main() directly with monkeypatched sys.argv, per the
project's standing test constraint (no real subprocess/socket in new tests).
"""

import sys

from vibe_cognition.cognition.snapshot_cli import main


def test_snapshot_copies_journal_contents(tmp_path, monkeypatch, capsys):
    """Fails-before: no CLI existed to invoke snapshot_journal at all."""
    src = tmp_path / "journal.jsonl"
    src.write_text('{"id":"n1"}\n{"id":"n2"}\n', encoding="utf-8")
    dst = tmp_path / "out" / "journal.jsonl"
    dst.parent.mkdir()

    monkeypatch.setattr(sys, "argv", ["vibe-cognition-snapshot", str(src), str(dst)])
    rc = main()

    assert rc == 0
    assert dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")
    assert "snapshotted" in capsys.readouterr().out


def test_snapshot_missing_source_returns_error(tmp_path, monkeypatch, capsys):
    """A nonexistent source journal must fail loudly, not write an empty dst."""
    src = tmp_path / "does_not_exist.jsonl"
    dst = tmp_path / "out.jsonl"

    monkeypatch.setattr(sys, "argv", ["vibe-cognition-snapshot", str(src), str(dst)])
    rc = main()

    assert rc != 0
    assert not dst.exists()
    assert "does not exist" in capsys.readouterr().err
