"""Console entry point for journal_io.snapshot_journal (WP-9, 4350a42fc4e5).

snapshot_journal takes the append lock so a manager's flush copy can never
capture a torn mid-append tail — but it had zero production callers; the
real shared-checkout worktree-flush protocol used a plain, unprotected copy.
This CLI is THE consumer-reachable way to invoke it (see docs/topology-guide.md's
shared-checkout flush section for the full worktree procedure this fits into).

Since per-person journal shards, the graph lives in several files. Pointing the CLI
at a `.cognition/` directory copies every append-only file -- the legacy journal,
each shard under journal/, and each people/ file -- each under its own lock. The
files are not frozen together at one instant; replay is order-free and idempotent,
so a set copied a moment apart still hydrates correctly.

Kept deliberately minimal and stdlib-only, mirroring migrate_mcp/prime's profile.
"""

import argparse
import sys
from pathlib import Path

from .journal_io import snapshot_journal

_APPEND_ONLY_GLOBS = ("journal.jsonl", "journal/*.jsonl", "people/*.jsonl")


def snapshot_directory(src: Path, dst: Path) -> list[Path]:
    """Copy every append-only file under a `.cognition/` directory; return the copies."""
    copied = []
    for pattern in _APPEND_ONLY_GLOBS:
        for path in sorted(src.glob(pattern)):
            if not path.is_file():
                continue
            target = dst / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            snapshot_journal(path, target)
            copied.append(target)
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="vibe-cognition-snapshot",
        description=(
            "Torn-tail-safe copy of live journal files — takes the same append lock a "
            "writer would, so a copy can never land mid-line. Give it one journal file, "
            "or a .cognition/ directory to copy the legacy journal, every per-person "
            "shard and every people/ file. Use this instead of a plain `cp`/`copy` "
            "when flushing while a server may still be appending."
        ),
    )
    parser.add_argument("src", type=Path, help="A journal file, or a .cognition/ directory")
    parser.add_argument("dst", type=Path, help="Where to copy the file, or the directory, TO")
    args = parser.parse_args()

    if not args.src.exists():
        print(f"error: source does not exist: {args.src}", file=sys.stderr)
        return 2

    if args.src.is_dir():
        copied = snapshot_directory(args.src, args.dst)
        print(f"snapshotted {len(copied)} file(s) from {args.src} -> {args.dst}")
        return 0

    snapshot_journal(args.src, args.dst)
    print(f"snapshotted {args.src} -> {args.dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
