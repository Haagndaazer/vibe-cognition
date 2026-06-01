"""Tests for the surgical per-project .mcp.json migration.

The migration must remove ONLY the ``vibe-cognition`` entry, never harm any
other MCP server, any other top-level key, or the file itself. ``dry_run``
must report the same outcome while writing absolutely nothing.
"""

import json
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


def test_reduces_to_empty_object_but_keeps_file(tmp_path):
    """A file that held only our entry collapses to {} and is NOT deleted."""
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"vibe-cognition": OUR_ENTRY}})

    result = remove_server_entry(str(mcp))

    assert result["status"] == "removed"
    assert result["preserved"] == []
    assert mcp.exists()  # never deleted
    assert json.loads(mcp.read_text(encoding="utf-8")) == {}


def test_keeps_other_top_level_keys_when_servers_empties(tmp_path):
    """Empty mcpServers is dropped, but unrelated top-level keys remain."""
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"vibe-cognition": OUR_ENTRY}, "other": {"k": 1}})

    result = remove_server_entry(str(mcp))

    assert result["status"] == "removed"
    on_disk = json.loads(mcp.read_text(encoding="utf-8"))
    assert "mcpServers" not in on_disk
    assert on_disk == {"other": {"k": 1}}


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
