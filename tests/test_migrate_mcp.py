"""Tests for the surgical per-project .mcp.json migration.

The migration must remove ONLY the ``vibe-cognition`` entry, never harm any
other MCP server, any other top-level key, or the file itself. ``dry_run``
must report the same outcome while writing absolutely nothing.
"""

import json
import os
from pathlib import Path

from vibe_cognition.migrate_mcp import main, remove_server_entry

OUR_ENTRY = {
    "command": "uv",
    "args": ["run", "--directory", "/cache/x/0.5.1", "python", "-m", "vibe_cognition.server"],
    "env": {"REPO_PATH": "/some/project"},
}
OTHER_ENTRY = {"command": "node", "args": ["server.js"], "env": {"TOKEN": "abc"}}
OTHER_ENTRY_2 = {"command": "python", "args": ["other.py"]}


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _all_keys(result: dict) -> bool:
    return set(result) == {"status", "removed", "preserved", "dry_run"}


# ── Real-run removal ────────────────────────────────────────────────────

def test_removes_only_our_entry_preserving_siblings(tmp_path):
    """Only vibe-cognition is removed; other servers + top-level keys survive."""
    mcp = tmp_path / ".mcp.json"
    _write(
        mcp,
        {
            "$schema": "https://example.com/schema.json",
            "mcpServers": {
                "vibe-cognition": OUR_ENTRY,
                "some-other-server": OTHER_ENTRY,
            },
        },
    )

    result = remove_server_entry(str(mcp))

    assert _all_keys(result)
    assert result["status"] == "removed"
    assert result["removed"] == ["vibe-cognition"]
    assert result["preserved"] == ["some-other-server"]
    assert result["dry_run"] is False

    on_disk = json.loads(mcp.read_text(encoding="utf-8"))
    assert "vibe-cognition" not in on_disk["mcpServers"]
    assert on_disk["mcpServers"]["some-other-server"] == OTHER_ENTRY
    assert on_disk["$schema"] == "https://example.com/schema.json"


def test_preserved_reports_other_servers_in_insertion_order(tmp_path):
    """preserved lists the surviving servers in file order, never our key."""
    mcp = tmp_path / ".mcp.json"
    _write(
        mcp,
        {
            "mcpServers": {
                "zeta": OTHER_ENTRY,
                "vibe-cognition": OUR_ENTRY,
                "alpha": OTHER_ENTRY_2,
            }
        },
    )

    result = remove_server_entry(str(mcp))

    # Insertion order preserved (NOT sorted); removed key excluded.
    assert result["preserved"] == ["zeta", "alpha"]
    on_disk = json.loads(mcp.read_text(encoding="utf-8"))
    assert list(on_disk["mcpServers"]) == ["zeta", "alpha"]


def test_emptied_config_becomes_valid_empty_record_not_bare_object(tmp_path):
    """A file that held only our entry becomes {"mcpServers": {}}, never {}.

    Bare {} is rejected by Claude Code ("mcpServers: expected record, received
    undefined"); the emptied config must stay a valid (empty) record.
    """
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"vibe-cognition": OUR_ENTRY}})

    result = remove_server_entry(str(mcp))

    assert result["status"] == "removed"
    assert result["preserved"] == []
    assert mcp.exists()  # never deleted
    assert json.loads(mcp.read_text(encoding="utf-8")) == {"mcpServers": {}}


def test_keeps_other_top_level_keys_and_empty_record_when_servers_empties(tmp_path):
    """mcpServers stays present (empty) and unrelated top-level keys remain."""
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"vibe-cognition": OUR_ENTRY}, "other": {"k": 1}})

    result = remove_server_entry(str(mcp))

    assert result["status"] == "removed"
    on_disk = json.loads(mcp.read_text(encoding="utf-8"))
    assert on_disk == {"mcpServers": {}, "other": {"k": 1}}


# ── Self-repair of contentless/invalid shapes left by older versions ────

def test_repairs_bare_empty_object(tmp_path):
    """An already-damaged `{}` (our entry absent) is repaired to a valid record."""
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{}", encoding="utf-8")

    result = remove_server_entry(str(mcp))

    assert result["status"] == "repaired"
    assert mcp.exists()  # never deleted
    assert json.loads(mcp.read_text(encoding="utf-8")) == {"mcpServers": {}}


def test_repairs_null_mcpservers(tmp_path):
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": None})

    result = remove_server_entry(str(mcp))

    assert result["status"] == "repaired"
    assert json.loads(mcp.read_text(encoding="utf-8")) == {"mcpServers": {}}


def test_repair_is_dry_runnable(tmp_path):
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{}", encoding="utf-8")

    result = remove_server_entry(str(mcp), dry_run=True)

    assert result["status"] == "repaired"
    assert mcp.read_text(encoding="utf-8") == "{}"  # untouched
    assert not (tmp_path / ".mcp.json.tmp").exists()


def test_already_valid_empty_record_is_noop(tmp_path):
    """A valid {"mcpServers": {}} with no other keys is left exactly as-is."""
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {}})
    before = mcp.read_text(encoding="utf-8")

    result = remove_server_entry(str(mcp))

    assert result["status"] == "absent"
    assert mcp.read_text(encoding="utf-8") == before


def test_other_content_without_mcpservers_is_left_alone(tmp_path):
    """A file with other content but no mcpServers is NOT ours to repair."""
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"foo": 1})
    before = mcp.read_text(encoding="utf-8")

    result = remove_server_entry(str(mcp))

    assert result["status"] == "absent"
    assert mcp.read_text(encoding="utf-8") == before


def test_whitespace_only_file_is_skipped(tmp_path):
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("   \n", encoding="utf-8")

    result = remove_server_entry(str(mcp))

    assert result["status"] == "skip"
    assert mcp.read_text(encoding="utf-8") == "   \n"


# ── Dry-run: report, write nothing ──────────────────────────────────────

def test_dry_run_reports_but_writes_nothing(tmp_path):
    """dry_run computes the same outcome but does not touch the file or .tmp."""
    mcp = tmp_path / ".mcp.json"
    original_doc = {
        "mcpServers": {"vibe-cognition": OUR_ENTRY, "some-other-server": OTHER_ENTRY}
    }
    _write(mcp, original_doc)
    before = mcp.read_text(encoding="utf-8")

    result = remove_server_entry(str(mcp), dry_run=True)

    assert result["status"] == "removed"
    assert result["removed"] == ["vibe-cognition"]
    assert result["preserved"] == ["some-other-server"]
    assert result["dry_run"] is True
    # Nothing written: file byte-identical AND no .tmp sibling created.
    assert mcp.read_text(encoding="utf-8") == before
    assert not (tmp_path / ".mcp.json.tmp").exists()


def test_dry_run_on_collapse_case_writes_nothing(tmp_path):
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"vibe-cognition": OUR_ENTRY}})
    before = mcp.read_text(encoding="utf-8")

    result = remove_server_entry(str(mcp), dry_run=True)

    assert result["status"] == "removed"
    assert result["preserved"] == []
    assert mcp.read_text(encoding="utf-8") == before
    assert not (tmp_path / ".mcp.json.tmp").exists()


# ── No-op statuses ──────────────────────────────────────────────────────

def test_missing_file_is_noop(tmp_path):
    mcp = tmp_path / ".mcp.json"
    result = remove_server_entry(str(mcp))
    assert _all_keys(result)
    assert result["status"] == "missing"
    assert result["removed"] == [] and result["preserved"] == []
    assert not mcp.exists()


def test_malformed_json_is_left_untouched(tmp_path):
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{not valid json", encoding="utf-8")

    result = remove_server_entry(str(mcp))

    assert result["status"] == "skip"
    assert result["preserved"] == []
    assert mcp.read_text(encoding="utf-8") == "{not valid json"


def test_non_object_json_is_left_untouched(tmp_path):
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("[1, 2, 3]", encoding="utf-8")

    result = remove_server_entry(str(mcp))
    assert result["status"] == "skip"
    assert mcp.read_text(encoding="utf-8") == "[1, 2, 3]"


def test_absent_entry_lists_other_servers(tmp_path):
    """File without our entry is unchanged; preserved lists the others."""
    mcp = tmp_path / ".mcp.json"
    original = {"mcpServers": {"some-other-server": OTHER_ENTRY, "alpha": OTHER_ENTRY_2}}
    _write(mcp, original)
    before = mcp.read_text(encoding="utf-8")

    result = remove_server_entry(str(mcp))

    assert result["status"] == "absent"
    assert result["removed"] == []
    assert result["preserved"] == ["some-other-server", "alpha"]
    assert mcp.read_text(encoding="utf-8") == before


def test_no_mcpservers_key_is_noop(tmp_path):
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"somethingElse": True})
    before = mcp.read_text(encoding="utf-8")

    result = remove_server_entry(str(mcp))
    assert result["status"] == "absent"
    assert result["preserved"] == []
    assert mcp.read_text(encoding="utf-8") == before


# ── CLI (main) ──────────────────────────────────────────────────────────

def test_main_real_run_prints_note_and_writes(tmp_path, capsys):
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"vibe-cognition": OUR_ENTRY, "some-other-server": OTHER_ENTRY}})

    rc = main([str(mcp)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "removed" in out.lower()
    assert "some-other-server" in out  # preserved list surfaced
    assert "vibe-cognition" not in json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]


def test_main_real_run_silent_when_absent(tmp_path, capsys):
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"some-other-server": OTHER_ENTRY}})

    rc = main([str(mcp)])
    out = capsys.readouterr().out

    assert rc == 0
    assert out == ""  # nothing changed -> hook surfaces nothing


def test_main_dry_run_previews_without_writing(tmp_path, capsys):
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"vibe-cognition": OUR_ENTRY, "some-other-server": OTHER_ENTRY}})
    before = mcp.read_text(encoding="utf-8")

    rc = main([str(mcp), "--dry-run"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "dry-run" in out.lower()
    assert "some-other-server" in out
    assert mcp.read_text(encoding="utf-8") == before
    assert not (tmp_path / ".mcp.json.tmp").exists()


def test_main_requires_a_path():
    assert main([]) == 2


def test_main_rejects_unknown_flag(tmp_path):
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {}})
    assert main([str(mcp), "--bogus"]) == 2


# ── Write-path failure (WP-11, 8e207087b093) ─────────────────────────────────


def test_write_failure_on_removal_reports_structured_status(tmp_path, monkeypatch):
    """A locked/read-only target must not raise -- remove_server_entry returns a
    structured "write-failed" status instead of propagating OSError.

    Fails-before: _atomic_write was unguarded, so this raised straight out of
    remove_server_entry as an unhandled exception.
    """
    import vibe_cognition.migrate_mcp as mm

    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"vibe-cognition": OUR_ENTRY, "some-other-server": OTHER_ENTRY}})
    before = mcp.read_text(encoding="utf-8")

    def _boom(path, data):
        raise PermissionError("simulated lock")

    monkeypatch.setattr(mm, "_atomic_write", _boom)
    result = mm.remove_server_entry(str(mcp))

    assert result["status"] == "write-failed"
    assert "simulated lock" in result["error"]
    assert mcp.read_text(encoding="utf-8") == before  # original untouched


def test_write_failure_on_repair_reports_structured_status(tmp_path, monkeypatch):
    """Same guard on the repair branch's _atomic_write call site."""
    import vibe_cognition.migrate_mcp as mm

    mcp = tmp_path / ".mcp.json"
    _write(mcp, {})  # triggers the repair path
    before = mcp.read_text(encoding="utf-8")

    def _boom(path, data):
        raise OSError("simulated AV lock")

    monkeypatch.setattr(mm, "_atomic_write", _boom)
    result = mm.remove_server_entry(str(mcp))

    assert result["status"] == "write-failed"
    assert "simulated AV lock" in result["error"]
    assert mcp.read_text(encoding="utf-8") == before


def test_main_write_failure_prints_clean_stderr_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    """main() on a write failure: clean one-line stderr (no traceback), exit 1.

    Fails-before: an unguarded _atomic_write raised out of main() entirely,
    printing a full Python traceback and relying on the default unhandled-
    exception exit code rather than an intentional one.
    """
    import vibe_cognition.migrate_mcp as mm

    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"vibe-cognition": OUR_ENTRY}})

    monkeypatch.setattr(mm, "_atomic_write", lambda path, data: (_ for _ in ()).throw(OSError("locked")))
    rc = mm.main([str(mcp)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "Traceback" not in captured.err
    assert "locked" in captured.err
    assert captured.out == ""  # no misleading success note on stdout


def test_write_failure_real_readonly_file_on_windows(tmp_path):
    """Real (non-monkeypatched) Windows read-only file: os.replace onto it raises
    PermissionError -- verifies the guard against the actual OS behavior, not
    just a simulated exception."""
    import stat
    import sys

    if sys.platform != "win32":
        return  # this OS-specific behavior only reproduces on Windows

    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"vibe-cognition": OUR_ENTRY}})
    os.chmod(mcp, stat.S_IREAD)
    try:
        result = remove_server_entry(str(mcp))
        assert result["status"] == "write-failed"
        assert result["error"]
    finally:
        os.chmod(mcp, stat.S_IWRITE | stat.S_IREAD)  # let tmp_path cleanup delete it
