"""One-time migration: remove the plugin's stale per-project MCP entry.

Older versions of this plugin wrote a ``vibe-cognition`` entry into each
project's ``.mcp.json`` via the SessionStart hook. The server is now declared
by the plugin itself (``.claude-plugin/plugin.json``), so any stale
per-project entry must be removed — a project-scope ``.mcp.json`` OUTRANKS the
plugin-provided definition, so leaving it would pin users to the old
version-locked path.

The removal is deliberately surgical: it touches ONLY the named entry, leaving
every other MCP server and every other top-level key untouched, and never
deletes the file.
"""

import json
import os
import sys

SERVER_NAME = "vibe-cognition"


def remove_server_entry(mcp_path: str, server_name: str = SERVER_NAME) -> str:
    """Remove a single MCP server entry from a project ``.mcp.json``.

    Args:
        mcp_path: Path to the project ``.mcp.json``.
        server_name: The single ``mcpServers`` key to remove.

    Returns:
        One of:
          ``"missing"`` — file does not exist (nothing to do)
          ``"skip"``    — file exists but is not a valid JSON object (untouched)
          ``"absent"``  — file valid but had no such entry (nothing changed)
          ``"removed"`` — the entry was removed and the file rewritten
    """
    try:
        with open(mcp_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return "missing"
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # Malformed or unreadable — never risk corrupting a file we don't own.
        return "skip"

    if not isinstance(data, dict):
        return "skip"

    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or server_name not in servers:
        return "absent"

    # Remove ONLY our entry; every other server and top-level key is preserved
    # in its original insertion order (dicts keep order through load/dump).
    servers.pop(server_name, None)

    # Cosmetic: drop the mcpServers container only if it is now empty.
    # Never delete the file itself — if the doc reduces to {}, leave {} on disk.
    if not servers:
        data.pop("mcpServers", None)

    tmp = mcp_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, mcp_path)
    return "removed"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m vibe_cognition.migrate_mcp <path>``."""
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(
            "usage: python -m vibe_cognition.migrate_mcp <path-to-.mcp.json>",
            file=sys.stderr,
        )
        return 2
    status = remove_server_entry(args[0])
    print(status, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
