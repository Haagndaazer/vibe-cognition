"""Console entry point for journal_io.snapshot_journal (WP-9, 4350a42fc4e5).

snapshot_journal takes the append lock so a manager's flush copy can never
capture a torn mid-append tail — but it had zero production callers; the
real shared-checkout worktree-flush protocol used a plain, unprotected copy.
This CLI is THE consumer-reachable way to invoke it (see docs/topology-guide.md's
shared-checkout flush section for the full worktree procedure this fits into).

Kept deliberately minimal and stdlib-only, mirroring migrate_mcp/prime's profile.
"""

import argparse
import sys
from pathlib import Path

from .journal_io import snapshot_journal


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="vibe-cognition-snapshot",
        description=(
            "Torn-tail-safe copy of a live .cognition/journal.jsonl — takes the "
            "same append lock a writer would, so the copy can never land mid-line. "
            "Use this instead of a plain `cp`/`copy` when flushing a live journal "
            "(e.g. into a temp worktree) while a server may still be appending to it."
        ),
    )
    parser.add_argument("src", type=Path, help="Path to the live journal.jsonl to copy FROM")
    parser.add_argument("dst", type=Path, help="Path to copy the snapshot TO")
    args = parser.parse_args()

    if not args.src.exists():
        print(f"error: source journal does not exist: {args.src}", file=sys.stderr)
        return 2

    snapshot_journal(args.src, args.dst)
    print(f"snapshotted {args.src} -> {args.dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
