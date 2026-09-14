"""Which code is actually running, and a warning when it is not what was installed.

Observed on Colton's machine after updating to v0.38.3: the MCP server launched nine
seconds after the update finished but three seconds BEFORE the hook re-pointed the
shared venv, and served v0.38.2 for the whole session while every manifest and the
install record said v0.38.3. Nothing reported it.
"""

import json
from datetime import UTC, datetime, timedelta

from vibe_cognition import running_version
from vibe_cognition.running_version import (
    code_version,
    running_report,
    stale_server_warning,
    stamp_running_server,
)


def test_code_version_reads_the_running_code_not_the_venv_record():
    """The dist-info in a shared venv describes whatever was last synced, which is
    exactly what can disagree with the code on disk."""
    import tomllib
    from pathlib import Path

    here = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected = tomllib.loads(here.read_text(encoding="utf-8"))["project"]["version"]
    assert code_version() == expected


def test_running_report_flags_a_mismatch_with_the_installed_version(tmp_path, monkeypatch):
    root = tmp_path / "plugin-root"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"version": "9.9.9"}), encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_PLUGIN_ROOT", str(root))

    report = running_report()
    assert report["installed_version"] == "9.9.9"
    assert report["version"] == code_version()
    assert report["matches"] is False


def test_matches_is_unknown_rather_than_true_when_nothing_says_what_was_installed(monkeypatch):
    """A dev checkout or an older host passes no plugin root; claiming a match there
    would be exactly the false reassurance this exists to remove."""
    monkeypatch.delenv("VIBE_PLUGIN_ROOT", raising=False)
    assert running_report()["matches"] is None


def test_the_digest_warns_when_the_answering_server_is_older(tmp_path, monkeypatch):
    cognition = tmp_path / ".cognition"
    monkeypatch.setattr(running_version, "code_version", lambda: "0.38.2")
    stamp_running_server(cognition)

    warning = stale_server_warning(cognition, "0.38.3")
    assert "running v0.38.2" in warning
    assert "v0.38.3 is installed" in warning
    assert "RESTART" in warning


def test_no_warning_when_the_server_is_current(tmp_path):
    cognition = tmp_path / ".cognition"
    stamp_running_server(cognition)
    assert stale_server_warning(cognition, code_version()) == ""


def test_a_stamp_from_an_earlier_session_says_nothing_about_this_one(tmp_path, monkeypatch):
    """Yesterday's server ran yesterday's version; that is not today's problem."""
    cognition = tmp_path / ".cognition"
    monkeypatch.setattr(running_version, "code_version", lambda: "0.1.0")
    stamp_running_server(cognition)
    assert stale_server_warning(cognition, "0.38.3"), "stamp name drifted; test is vacuous"

    stamp = next((cognition / "local").glob("running-server*.json"))
    data = json.loads(stamp.read_text(encoding="utf-8"))
    data["started_at"] = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
    stamp.write_text(json.dumps(data), encoding="utf-8")
    assert stale_server_warning(cognition, "0.38.3") == ""


def test_a_corrupt_stamp_never_breaks_session_start(tmp_path):
    cognition = tmp_path / ".cognition"
    stamp_running_server(cognition)
    stamp = next((cognition / "local").glob("running-server*.json"))
    stamp.write_text("{not json", encoding="utf-8")
    assert stale_server_warning(cognition, "0.38.3") == ""


def test_one_harness_server_is_not_reported_to_the_other(tmp_path, monkeypatch):
    """Review finding: Claude Code and Codex are updated as separate steps, so an
    older Codex server must not tell a current Claude Code session to restart."""
    cognition = tmp_path / ".cognition"
    monkeypatch.setattr(running_version, "code_version", lambda: "0.38.2")
    monkeypatch.setenv("VIBE_HARNESS", "codex")
    stamp_running_server(cognition)
    assert stale_server_warning(cognition, "0.38.3")

    monkeypatch.setenv("VIBE_HARNESS", "claude-code")
    assert stale_server_warning(cognition, "0.38.3") == ""


def test_the_plugin_manifest_pins_the_server_to_the_installed_code():
    """The fix itself: without PYTHONPATH the server imports whatever the shared
    venv's editable pointer says, which the hook updates only after launch."""
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json")
        .read_text(encoding="utf-8")
    )
    env = manifest["mcpServers"]["vibe-cognition"]["env"]
    assert env["PYTHONPATH"] == "${CLAUDE_PLUGIN_ROOT}/src"
    assert env["VIBE_PLUGIN_ROOT"] == "${CLAUDE_PLUGIN_ROOT}"


def test_the_codex_registration_pins_it_too_and_forces_re_registration():
    """Codex has the same --no-sync launch, so the same race. Bumping the stamp is
    what makes existing Codex installs re-register with the pin."""
    from pathlib import Path

    hook = (
        Path(__file__).resolve().parents[1] / "adapters" / "codex" / "hooks" / "session-start.sh"
    ).read_text(encoding="utf-8")
    assert '--env "PYTHONPATH=${ROOT_NATIVE}/src"' in hook
    assert 'DESIRED="v2|' in hook
    assert "PYTHONPATH VIBE_PLUGIN_ROOT" in hook
