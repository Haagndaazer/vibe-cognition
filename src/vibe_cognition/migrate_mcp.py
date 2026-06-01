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

A ``--dry-run`` mode reports exactly what *would* change (what is removed and
what is preserved) without writing, so the safety guarantee is demonstrable on
the real file rather than only asserted by tests.
"""

import json
import os
import sys

SERVER_NAME = "vibe-cognition"


def remove_server_entry(
    mcp_path: str,
    server_name: str = SERVER_NAME,
    dry_run: bool = False,
) -> dict:
    """Remove a single MCP server entry from a project ``.mcp.json``.

    Args:
        mcp_path: Path to the project ``.mcp.json``.
        server_name: The single ``mcpServers`` key to remove.
        dry_run: If True, compute the outcome but write nothing to disk.

    Returns:
        A dict with stable keys for every outcome::

            {
              "status": "missing" | "skip" | "absent" | "removed",
              "removed": [server_name] if it was (or would be) removed else [],
              "preserved": other server names that remain (insertion order),
              "dry_run": bool,
            }

        ``status`` meanings:
          ``"missing"`` — file does not exist (nothing to do)
          ``"skip"``    — file exists but is not a valid JSON object (untouched)
          ``"absent"``  — file valid but had no such entry (nothing changed)
          ``"removed"`` — the entry was removed (or, under dry_run, would be)

        ``preserved`` is only meaningful when the file parsed as a JSON object
        (``absent``/``removed``); it is ``[]`` for ``missing``/``skip``.
    """
    result = {"status": "", "removed": [], "preserved": [], "dry_run": dry_run}

    try:
        with open(mcp_path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        result["status"] = "missing"
        return result
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # Malformed or unreadable — never risk corrupting a file we don't own.
        result["status"] = "skip"
        return result

    if not isinstance(data, dict):
        result["status"] = "skip"
        return result

    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or server_name not in servers:
        # No entry of ours. Report any other servers present for transparency.
        result["status"] = "absent"
        if isinstance(servers, dict):
            result["preserved"] = [k for k in servers if k != server_name]
        return result

    # Our entry is present. Snapshot what survives BEFORE mutating, in the
    # file's own insertion order; the removed key is never in preserved.
    result["status"] = "removed"
    result["removed"] = [server_name]
    result["preserved"] = [k for k in servers if k != server_name]

    if dry_run:
        # Report only — touch nothing on disk (not even the .tmp sibling).
        return result

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
    return result


def _format_summary(result: dict) -> str:
    """Build a one-line human/agent summary for a migration result.

    Returns an empty string when there is nothing worth surfacing
    (``missing``/``skip``/``absent`` on a real run — no change happened).
    For ``--dry-run`` every status produces a preview line.
    """
    status = result["status"]
    dry = result["dry_run"]
    preserved = result["preserved"]
    pres = ", ".join(preserved) if preserved else "none"

    if status == "removed":
        if dry:
            return (
                f"[dry-run] Would remove the stale `{SERVER_NAME}` entry from "
                f".mcp.json (would preserve: {pres}). No changes written."
            )
        return (
            f"Vibe Cognition removed a stale `{SERVER_NAME}` entry from this "
            f"project's .mcp.json (preserved: {pres}). The plugin now provides "
            f"the MCP server; no action needed."
        )

    if not dry:
        # Real run, nothing changed — stay silent so the hook surfaces nothing.
        return ""

    # Dry-run previews for the no-op cases.
    if status == "absent":
        return (
            f"[dry-run] No `{SERVER_NAME}` entry present in .mcp.json; nothing "
            f"to remove (other servers: {pres})."
        )
    if status == "missing":
        return "[dry-run] No .mcp.json found; nothing to remove."
    return "[dry-run] .mcp.json is not valid JSON; left untouched."


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m vibe_cognition.migrate_mcp <path> [--dry-run]``.

    Default (real run): performs the removal and prints a one-line note when an
    entry was removed (empty otherwise) — the SessionStart hook captures this
    and surfaces it. ``--dry-run`` writes nothing and prints a preview line.
    """
    args = sys.argv[1:] if argv is None else list(argv)

    dry_run = False
    positional: list[str] = []
    for arg in args:
        if arg == "--dry-run":
            dry_run = True
        elif arg.startswith("-"):
            print(f"unknown option: {arg}", file=sys.stderr)
            return 2
        else:
            positional.append(arg)

    if len(positional) != 1:
        print(
            "usage: python -m vibe_cognition.migrate_mcp <path-to-.mcp.json> "
            "[--dry-run]",
            file=sys.stderr,
        )
        return 2

    result = remove_server_entry(positional[0], dry_run=dry_run)
    summary = _format_summary(result)
    if summary:
        print(summary, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
