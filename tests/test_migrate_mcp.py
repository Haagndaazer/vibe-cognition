"""Tests for the surgical per-project .mcp.json migration.

The migration must remove ONLY the ``vibe-cognition`` entry and never harm any
other MCP server, any other top-level key, or the file itself.
"""

import json
from pathlib import Path

from vibe_cognition.migrate_mcp import remove_server_entry

OUR_ENTRY = {
    "command": "uv",
    "args": ["run", "--directory", "/cache/x/0.5.1", "python", "-m", "vibe_cognition.server"],
    "env": {"REPO_PATH": "/some/project"},
}
OTHER_ENTRY = {
    "command": "node",
    "args": ["server.js"],
    "env": {"TOKEN": "abc"},
}


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


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

    status = remove_server_entry(str(mcp))

    assert status == "removed"
    result = json.loads(mcp.read_text(encoding="utf-8"))
    assert "vibe-cognition" not in result["mcpServers"]
    # Sibling server and unrelated top-level key are byte-for-byte equivalent.
    assert result["mcpServers"]["some-other-server"] == OTHER_ENTRY
    assert result["$schema"] == "https://example.com/schema.json"


def test_reduces_to_empty_object_but_keeps_file(tmp_path):
    """A file that held only our entry collapses to {} and is NOT deleted."""
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"vibe-cognition": OUR_ENTRY}})

    status = remove_server_entry(str(mcp))

    assert status == "removed"
    assert mcp.exists()  # never deleted
    assert json.loads(mcp.read_text(encoding="utf-8")) == {}


def test_keeps_other_top_level_keys_when_servers_empties(tmp_path):
    """Empty mcpServers is dropped, but unrelated top-level keys remain."""
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"mcpServers": {"vibe-cognition": OUR_ENTRY}, "other": {"k": 1}})

    status = remove_server_entry(str(mcp))

    assert status == "removed"
    result = json.loads(mcp.read_text(encoding="utf-8"))
    assert "mcpServers" not in result
    assert result == {"other": {"k": 1}}


def test_missing_file_is_noop(tmp_path):
    mcp = tmp_path / ".mcp.json"
    assert remove_server_entry(str(mcp)) == "missing"
    assert not mcp.exists()


def test_malformed_json_is_left_untouched(tmp_path):
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{not valid json", encoding="utf-8")

    assert remove_server_entry(str(mcp)) == "skip"
    # Content is preserved exactly — we never rewrite a file we can't parse.
    assert mcp.read_text(encoding="utf-8") == "{not valid json"


def test_non_object_json_is_left_untouched(tmp_path):
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("[1, 2, 3]", encoding="utf-8")

    assert remove_server_entry(str(mcp)) == "skip"
    assert mcp.read_text(encoding="utf-8") == "[1, 2, 3]"


def test_absent_entry_is_noop(tmp_path):
    """File without our entry is unchanged and reported absent."""
    mcp = tmp_path / ".mcp.json"
    original = {"mcpServers": {"some-other-server": OTHER_ENTRY}}
    _write(mcp, original)
    before = mcp.read_text(encoding="utf-8")

    assert remove_server_entry(str(mcp)) == "absent"
    assert mcp.read_text(encoding="utf-8") == before


def test_no_mcpservers_key_is_noop(tmp_path):
    mcp = tmp_path / ".mcp.json"
    _write(mcp, {"somethingElse": True})
    before = mcp.read_text(encoding="utf-8")

    assert remove_server_entry(str(mcp)) == "absent"
    assert mcp.read_text(encoding="utf-8") == before
