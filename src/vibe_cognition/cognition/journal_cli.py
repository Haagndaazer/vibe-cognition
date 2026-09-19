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
from .identity import read_confirmed_identity
from .models import CognitionNode, CognitionNodeType, generate_node_id
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


def _accept_disk(storage: CognitionStorage, project: Path, *, assume_yes: bool) -> int:
    """Accept the journal files as they are, dropping what this checkout retained.

    This DISCARDS memories the plugin was holding because a journal file lost them.
    It is not gated technically -- an agent can run it, and a token echoed from the
    session-start text would only look like a gate, since the agent reads that text
    too (Colton ruling 2026-09-18). Instead every run leaves an audit record in the
    graph naming what was dropped, so silencing a data-loss warning is visible
    afterwards. An agent must ask the human before running this.
    """
    plan = storage.plan_accept_disk()
    print("accept-disk DISCARDS entries this checkout is holding that are not in the")
    print("journal files on disk. Use it only for a deliberate rollback.")
    print(f"  project: {project}")
    print(f"  entries that will be given up: {len(plan['dropped'])}")
    if plan["dropped"]:
        print("  " + ", ".join(plan["dropped"][:10]) + (" ..." if len(plan["dropped"]) > 10 else ""))
    if not assume_yes:
        try:
            answer = input("Type 'accept' to proceed: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "accept":
            print("Nothing was dropped.")
            return 1

    preview = storage.plan_accept_disk()
    try:
        _record_audit(storage, preview)
    except Exception as exc:  # noqa: BLE001
        # The audit IS the accountability for this command (ruling 2026-09-18: make it
        # undeniable rather than fake prevention), so a run that cannot be recorded does
        # not happen at all. Otherwise a silenced data-loss warning leaves no trace.
        print(f"error: nothing was dropped -- the audit record could not be written: {exc}",
              file=sys.stderr)
        return 4

    result = storage.accept_disk()
    dropped = result["dropped"]
    print(f"Dropped {len(dropped)} node(s); the graph now matches the files on disk.")
    if dropped:
        print("  " + ", ".join(dropped[:10]) + (" ..." if len(dropped) > 10 else ""))
    print("An audit record was written to the graph.")
    return 0


def _record_audit(storage: CognitionStorage, result: dict) -> None:
    """Record what accept-disk dropped, as an ordinary graph node."""
    who = read_confirmed_identity(storage.cognition_dir) or {}
    dropped = result["dropped"]
    summary = (
        f"accept-disk dropped {len(dropped)} retained journal entr"
        f"{'y' if len(dropped) == 1 else 'ies'} to match the files on disk"
    )
    storage.add_node(
        CognitionNode(
            id=generate_node_id(CognitionNodeType.INCIDENT.value, summary, result["at"]),
            type=CognitionNodeType.INCIDENT,
            summary=summary,
            detail=(
                f"Ran by {who.get('name') or 'unknown'} <{who.get('email') or 'unknown'}>. "
                f"Nodes held before: {result['nodes_before']}. "
                f"Dropped ids: {', '.join(dropped) or 'none'}. This is the deliberate-rollback "
                "path: the plugin was holding these entries because a journal file no longer "
                "had them, and this command accepted the file as the truth."
            ),
            context=["accept-disk", "journal", "rollback"],
            references=[],
            severity="high",
            timestamp=result["at"],
            author=who.get("name") or "unknown",
            metadata={"recorded_by": {"name": who.get("name") or "", "email": who.get("email") or ""}},
        ),
        mint_unique_id=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vibe-cognition-journal",
        description="Show or switch a project's per-person journal files. Never moves data.",
    )
    parser.add_argument("command", choices=["status", "adopt", "accept-disk"])
    parser.add_argument("project", nargs="?", help="project root (default: REPO_PATH or cwd)")
    parser.add_argument("--json", action="store_true", help="status as JSON")
    parser.add_argument(
        "--yes", action="store_true",
        help="accept-disk only: skip the confirmation prompt (still audited)",
    )
    args = parser.parse_args(argv)

    project = Path(args.project) if args.project else resolve_repo_path_env(default=Path.cwd())
    cognition_dir = project / ".cognition"
    if not cognition_dir.is_dir():
        print(f"error: no .cognition/ directory at {project}", file=sys.stderr)
        return 2

    if args.command == "accept-disk":
        # repair=False: constructing the store would otherwise put the rolled-back
        # lines back before the human's decision is applied, and accept-disk would
        # silently do nothing.
        return _accept_disk(
            CognitionStorage(cognition_dir, repair=False), project, assume_yes=args.yes,
        )
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
