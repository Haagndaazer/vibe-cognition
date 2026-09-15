"""`vibe-cognition-journal`: see and switch a project's per-person journal files.

Projects switch on their own: the first write after upgrading creates the writer's
shard. This command is for looking (`status`) and for switching before anyone writes
(`adopt`). It never moves data and never rewrites the legacy journal.

    python -m vibe_cognition.cognition.journal_cli status [project]
    python -m vibe_cognition.cognition.journal_cli adopt [project]
"""

import argparse
import json
import sys
from pathlib import Path

from ..config import resolve_repo_path_env
from .storage import CognitionStorage, JournalWriterUnavailableError


def _status_text(project: Path, status: dict) -> str:
    lines = [f"Journal files for {project}"]
    legacy = status["legacy_journal_bytes"]
    lines.append(
        f"  legacy .cognition/journal.jsonl: {legacy} bytes (read, never written)"
        if legacy is not None else "  legacy .cognition/journal.jsonl: none"
    )
    if status["shards"]:
        lines.append(f"  per-person shards ({len(status['shards'])}):")
        lines += [f"    journal/{s['file']}: {s['bytes']} bytes" for s in status["shards"]]
    else:
        lines.append("  per-person shards: none yet (the first write creates one)")
    lines.append(f"  this checkout writes to: {status['writing_to'] or 'nothing -- no confirmed identity'}")
    lines.append(f"  switched to shards: {status['adopted_at'] or 'not yet'}")
    stragglers = status["stragglers"]
    if stragglers:
        who = ", ".join(stragglers["authors"]) or "unknown"
        lines.append(
            f"  WARNING: {stragglers['entries_after_adoption']} entr"
            f"{'y' if stragglers['entries_after_adoption'] == 1 else 'ies'} written to the "
            f"legacy journal after switching (latest {stragglers['latest_at'][:19]}, by {who}) "
            "-- someone is on an older plugin version"
        )
    for key, label in (
        ("unresolved_entries", "entries whose target is in no journal file"),
        ("id_collisions", "node id collisions (earlier node kept)"),
        ("glued_lines", "lines holding several entries (all read; repair the file)"),
    ):
        if status[key]:
            lines.append(f"  NOTE: {status[key]} {label}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vibe-cognition-journal",
        description="Show or switch a project's per-person journal files. Never moves data.",
    )
    parser.add_argument("command", choices=["status", "adopt"])
    parser.add_argument("project", nargs="?", help="project root (default: REPO_PATH or cwd)")
    parser.add_argument("--json", action="store_true", help="status as JSON")
    args = parser.parse_args(argv)

    project = Path(args.project) if args.project else resolve_repo_path_env(default=Path.cwd())
    cognition_dir = project / ".cognition"
    if not cognition_dir.is_dir():
        print(f"error: no .cognition/ directory at {project}", file=sys.stderr)
        return 2

    storage = CognitionStorage(cognition_dir, read_only=args.command == "status")
    if args.command == "adopt":
        try:
            path = storage.adopt_shards()
        except JournalWriterUnavailableError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
        print(f"This checkout now writes to {path.relative_to(project)}. Commit it.")
        return 0

    status = storage.journal_status()
    print(json.dumps(status, indent=2) if args.json else _status_text(project, status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
