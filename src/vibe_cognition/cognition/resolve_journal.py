"""Resolve SVN conflicts on .cognition's append-only JSONL files by keeping BOTH sides.

SVN has no union merge. When two people append to journal.jsonl (or a people/*.jsonl
file) between syncs, `svn update` leaves `<file>.mine`, `<file>.r<OLD>` and
`<file>.r<NEW>` beside a marker-filled working file. Every line is an independent
operation, so the correct result is every line from both sides, each exactly once.

Duplicates are dropped by IDENTICAL LINE, never by node id: `update_node` lines reuse
the id of the node they update and edge lines carry no id, so deduplicating by id
deletes the other side's updates and edges.

Run it yourself when prompted; it never runs during a checkout:

    python -m vibe_cognition.cognition.resolve_journal <project> [--dry-run]
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from ..config import resolve_repo_path_env
from . import svn_hygiene

_THEIRS_RE = re.compile(r"\.r(\d+)$")


def resolve_journal_command(project: Path) -> str:
    """The exact command to run it, with the interpreter that has this package --
    the console script lives inside the plugin's own environment, not on PATH."""
    return f'"{sys.executable}" -m vibe_cognition.cognition.resolve_journal "{project}"'


def find_conflicts(cognition_dir: Path) -> list[Path]:
    """Every .jsonl under .cognition/ left in conflict: what SVN reports as conflicted,
    plus any file with a `.mine` beside it. SVN's own status matters because a
    `.mine` can be missing (deleted by hand, or a client that does not write one)
    while the file is still conflicted -- the session start would name a conflict the
    resolver could not see."""
    cognition_dir = Path(cognition_dir)
    found: set[Path] = set()
    for mine in cognition_dir.rglob("*.jsonl.mine"):
        found.add(mine.with_name(mine.name[: -len(".mine")]))
    root = svn_hygiene.svn_root(cognition_dir.parent)
    svn = svn_hygiene._svn_command() if root is not None else None
    if root is not None and svn is not None:
        entries = svn_hygiene._status(svn, root, svn_hygiene._rel(root, cognition_dir)) or {}
        for rel, entry in entries.items():
            if entry["item"] == "conflicted" and rel.endswith(".jsonl"):
                found.add(root / rel)
    return sorted(
        t.resolve() for t in found
        if t.exists() and "local" not in t.resolve().relative_to(cognition_dir.resolve()).parts
    )


def _theirs(target: Path) -> Path | None:
    numbered = []
    for candidate in target.parent.glob(f"{target.name}.r*"):
        match = _THEIRS_RE.search(candidate.name)
        if match and candidate.name == f"{target.name}.r{match.group(1)}":
            numbered.append((int(match.group(1)), candidate))
    return max(numbered)[1] if numbered else None


def _entries(path: Path) -> tuple[list[str], int]:
    """The valid JSON-object lines of a file, and how many other lines were dropped."""
    kept, dropped = [], 0
    for raw in path.read_bytes().decode("utf-8", "replace").splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        try:
            ok = isinstance(json.loads(line), dict)
        except ValueError:
            ok = False
        if ok:
            kept.append(line)
        else:
            dropped += 1
    return kept, dropped


def merge_sides(mine: list[str], theirs: list[str]) -> list[str]:
    """Your lines, then theirs that you do not already have; identical lines once."""
    seen: set[str] = set()
    merged = []
    for line in [*mine, *theirs]:
        if line not in seen:
            seen.add(line)
            merged.append(line)
    return merged


def resolve(target: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Rebuild `target` from both sides. Without a `.mine`, the conflicted file
    itself is used as your side: its markers wrap every line of both sides, and
    the marker lines are not JSON, so they are dropped."""
    mine_path = target.with_name(f"{target.name}.mine")
    theirs_path = _theirs(target)
    mine_source = mine_path if mine_path.exists() else target
    try:
        mine, mine_bad = _entries(mine_source)
        theirs, theirs_bad = _entries(theirs_path) if theirs_path is not None else ([], 0)
    except OSError as exc:
        return {"file": str(target), "error": f"could not read a side of the conflict: {exc}"}
    if theirs_path is None and mine_source == mine_path:
        return {"file": str(target), "error": "no .r<N> file from the other side was found"}
    merged = merge_sides(mine, theirs)
    report: dict[str, Any] = {
        "file": str(target),
        "yours": len(mine),
        "theirs": len(theirs),
        "result": len(merged),
        "only_theirs_added": len(merged) - len(mine),
        "unreadable_lines_skipped": mine_bad + theirs_bad,
        "rebuilt_from_conflicted_file": mine_source == target,
        "resolved": False,
    }
    if dry_run:
        return report
    try:
        target.write_bytes(("\n".join(merged) + "\n").encode("utf-8"))
    except OSError as exc:
        return {**report, "error": f"could not write the resolved file: {exc}"}
    root = svn_hygiene.svn_root(target.parent)
    svn = svn_hygiene._svn_command()
    if root is None or svn is None:
        report["next_step"] = f"svn resolve --accept working {target}"
        return report
    result = svn_hygiene._with_targets(
        svn, ["resolve", "--accept", "working"], root, [svn_hygiene._rel(root, target)],
    )
    report["resolved"] = result is not None and result[0] == 0
    if not report["resolved"]:
        report["next_step"] = f"svn resolve --accept working {target}"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vibe-cognition-resolve-journal",
        description=(
            "Resolve SVN conflicts on .cognition/*.jsonl files by keeping both sides. "
            "Never commits."
        ),
    )
    parser.add_argument("project", nargs="?", help="project root (default: REPO_PATH or cwd)")
    parser.add_argument("--dry-run", action="store_true", help="report counts, change nothing")
    args = parser.parse_args(argv)

    project = Path(args.project) if args.project else resolve_repo_path_env(default=Path.cwd())
    cognition_dir = project / ".cognition"
    conflicts = find_conflicts(cognition_dir)
    if not conflicts:
        print(f"No conflicted .cognition files under {project}.")
        return 0
    failed = False
    for target in conflicts:
        report = resolve(target, dry_run=args.dry_run)
        if "error" in report:
            failed = True
            print(f"{report['file']}: {report['error']}")
            continue
        verb = "would keep" if args.dry_run else "kept"
        print(
            f"{report['file']}: {verb} {report['result']} lines "
            f"({report['yours']} yours, {report['only_theirs_added']} only on the other side)"
            + (f", skipped {report['unreadable_lines_skipped']} unreadable" if report["unreadable_lines_skipped"] else "")
        )
        if report.get("next_step"):
            print(f"  then run: {report['next_step']}")
    if not args.dry_run:
        print("Reload the graph (cognition_reload, or start a new session), then commit.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
