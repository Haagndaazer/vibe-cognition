"""WP-13 (4aaef22e25ea): argparse + --help correctness + --days for
vibe-cognition-backfill (previously zero test coverage, --help silently
swallowed and the full report ran instead).

Never invokes real git subprocess -- monkeypatches _get_recent_commits/
_get_changed_files (the two functions that wrap subprocess.run) directly.
"""

import pytest

from vibe_cognition.cognition.backfill import main
from vibe_cognition.cognition.storage import CognitionStorage


def test_help_exits_zero_and_never_runs_the_report(tmp_path, monkeypatch, capsys):
    """Fails-before: no argparse at all, so --help was silently swallowed and
    the full report executed instead of printing usage."""
    calls = {"n": 0}
    monkeypatch.setattr(
        "vibe_cognition.cognition.backfill._get_recent_commits",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or [],
    )
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert calls["n"] == 0, "--help must not run the report"
    assert "usage" in capsys.readouterr().out.lower()


def test_rejects_unknown_flag(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(
        "vibe_cognition.cognition.backfill._get_recent_commits",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or [],
    )
    with pytest.raises(SystemExit) as exc:
        main(["--bogus"])
    assert exc.value.code == 2
    assert calls["n"] == 0


def test_rejects_non_positive_days(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(["--days", "0"])
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc2:
        main(["--days", "-5"])
    assert exc2.value.code == 2


def test_days_flag_reaches_get_recent_commits(tmp_path, monkeypatch):
    """WP-13/H-6(b) (21232d2acaea): --days N must actually change the window
    passed to _get_recent_commits, not just exist as a no-op flag."""
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    CognitionStorage(tmp_path / ".cognition")  # creates .cognition/ so main() proceeds
    seen = {}

    def _fake(repo_path, days=30):
        seen["days"] = days
        return []

    monkeypatch.setattr("vibe_cognition.cognition.backfill._get_recent_commits", _fake)
    with pytest.raises(SystemExit):
        main(["--days", "90"])
    assert seen["days"] == 90


def test_default_days_is_30_when_flag_omitted(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    CognitionStorage(tmp_path / ".cognition")
    seen = {}

    def _fake(repo_path, days=30):
        seen["days"] = days
        return []

    monkeypatch.setattr("vibe_cognition.cognition.backfill._get_recent_commits", _fake)
    with pytest.raises(SystemExit):
        main([])
    assert seen["days"] == 30


def test_no_cognition_dir_exits_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPO_PATH", str(tmp_path))  # no .cognition/ created
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 1
    assert "No .cognition/ directory found" in capsys.readouterr().out


def test_no_commits_message_mentions_the_actual_days_value(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    CognitionStorage(tmp_path / ".cognition")
    monkeypatch.setattr(
        "vibe_cognition.cognition.backfill._get_recent_commits", lambda *a, **k: []
    )
    with pytest.raises(SystemExit) as exc:
        main(["--days", "7"])
    assert exc.value.code == 0
    assert "last 7 days" in capsys.readouterr().out


def test_all_tracked_message_mentions_the_actual_days_value(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    storage = CognitionStorage(tmp_path / ".cognition")
    from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType

    storage.add_node(CognitionNode(
        id="ep1", type=CognitionNodeType.EPISODE, summary="s", detail="d",
        context=[], references=["commit:abc123"], timestamp="2026-06-23T00:00:00+00:00",
        author="t",
    ))
    monkeypatch.setattr(
        "vibe_cognition.cognition.backfill._get_recent_commits",
        lambda *a, **k: [{"hash": "abc123", "message": "m", "author": "a", "date": "d"}],
    )
    with pytest.raises(SystemExit) as exc:
        main(["--days", "14"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "last 14 days" in out
    assert "already tracked" in out


def test_untracked_report_mentions_the_actual_days_value(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    CognitionStorage(tmp_path / ".cognition")
    monkeypatch.setattr(
        "vibe_cognition.cognition.backfill._get_recent_commits",
        lambda *a, **k: [{"hash": "abc123def", "message": "m", "author": "a", "date": "d"}],
    )
    monkeypatch.setattr(
        "vibe_cognition.cognition.backfill._get_changed_files", lambda *a, **k: []
    )
    main(["--days", "45"])  # falls through to the report's natural end, no sys.exit here
    out = capsys.readouterr().out
    assert "last 45 days" in out
    assert "Untracked Commits" in out
