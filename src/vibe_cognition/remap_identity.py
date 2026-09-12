"""Remap graph attribution from one email address to another.

For history recorded under an address the person no longer uses -- a personal
account before a work one was configured, or an SVN login that differs from the
git identity. Attribution is corrected going forward by `cognition_set_identity`;
this CLI is what repairs what was already written.

NOTHING IS INFERRED. The mapping is supplied explicitly on the command line by a
human who knows both addresses, matching the standing no-auto-stamp ruling
(decision 833e9f67de4d): roster and blame matches are suggestion generators, and
only a human-confirmed value is ever written.

DRY RUN BY DEFAULT. Without `--apply` it reports what would change and writes
nothing.

STOP LIVE SESSIONS BEFORE --apply. Each node is rewritten read-modify-write, and
storage's lock is in-process only: a concurrent MCP session appending a real
change to the same node between the read and the write has that change silently
overwritten by the stale copy. The window is small but the loss is not limited to
identity fields -- any unrelated metadata change on that node goes with it.

KNOWN GAP: environment facts in .cognition/people/<slug>.jsonl are keyed by email
and are NOT remapped -- the old address's file is left in place. Re-record those
facts under the new identity if you rely on them.

History is never rewritten. Each change is an ordinary `update_node` event
appended to the journal, so replay stays append-only and idempotent and the
original attribution remains visible in the journal's history.

CLI: python -m vibe_cognition.remap_identity <project-path>
     --from old@example.com --to new@example.com [--apply]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .cognition.storage import CognitionStorage

# Metadata keys holding an identity dict of {"name", "email"}.
_IDENTITY_KEYS = ("recorded_by", "created_by", "claimed_by")
# Metadata lists whose entries carry a nested identity dict under "by".
_IDENTITY_LIST_KEYS = ("transitions", "assignments", "profile_history")


def _casefold(email: str) -> str:
    return (email or "").strip().casefold()


def _identity_matches(value: Any, old: str) -> bool:
    return isinstance(value, dict) and _casefold(str(value.get("email", ""))) == old


def plan_remap(storage: CognitionStorage, old: str, new: str) -> list[dict[str, Any]]:
    """Every node whose attribution would change, with the reason per node.

    Covers the metadata identity keys and each task status transition's ``by``.
    """
    old, new = _casefold(old), _casefold(new)
    planned: list[dict[str, Any]] = []
    for node in storage.get_all_nodes():
        meta = node.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        hits = [k for k in _IDENTITY_KEYS if _identity_matches(meta.get(k), old)]
        transition_hits = 0
        for list_key in _IDENTITY_LIST_KEYS:
            entries = meta.get(list_key)
            if isinstance(entries, list):
                transition_hits += sum(
                    1 for e in entries if isinstance(e, dict) and _identity_matches(e.get("by"), old)
                )
        if hits or transition_hits:
            planned.append({
                "id": node["id"],
                "type": node.get("type"),
                "summary": (node.get("summary") or "")[:70],
                "keys": hits,
                "transitions": transition_hits,
            })
    return planned


def _remap_identity_dict(value: dict[str, Any], new: str, name: str | None) -> dict[str, Any]:
    out = dict(value)
    out["email"] = new
    if name:
        out["name"] = name
    out["remapped_from"] = value.get("email", "")
    return out


def apply_remap(
    storage: CognitionStorage, old: str, new: str, name: str | None
) -> int:
    """Write the remap. Re-checks each node immediately before writing (narrows,
    never closes, the get_node/update_node gap). Returns nodes actually changed."""
    old, new = _casefold(old), _casefold(new)
    changed = 0
    for planned in plan_remap(storage, old, new):
        current = storage.get_node(planned["id"])
        if current is None:
            continue
        meta = dict(current.get("metadata") or {})
        touched = False
        for key in _IDENTITY_KEYS:
            if _identity_matches(meta.get(key), old):
                meta[key] = _remap_identity_dict(meta[key], new, name)
                touched = True
        for list_key in _IDENTITY_LIST_KEYS:
            entries = meta.get(list_key)
            if not isinstance(entries, list):
                continue
            rebuilt, changed_list = [], False
            for e in entries:
                if isinstance(e, dict) and _identity_matches(e.get("by"), old):
                    e = {**e, "by": _remap_identity_dict(e["by"], new, name)}
                    changed_list = True
                rebuilt.append(e)
            if changed_list:
                meta[list_key] = rebuilt
                touched = True
        # Plain email string, not an identity dict.
        if _casefold(str(meta.get("assigned_to") or "")) == old:
            meta["assigned_to"] = new
            touched = True
        if touched:
            storage.update_node(planned["id"], metadata=meta)
            changed += 1
    return changed


def _report(planned: list[dict[str, Any]], old: str, new: str) -> str:
    if not planned:
        return f"No nodes attributed to {old}. Nothing to remap."
    by_type: dict[str, int] = {}
    transitions = 0
    for p in planned:
        by_type[str(p["type"])] = by_type.get(str(p["type"]), 0) + 1
        transitions += p["transitions"]
    lines = [
        f"Remap {old} -> {new}",
        f"Nodes affected: {len(planned)}",
        f"Status transitions affected: {transitions}",
        "",
        "By node type:",
    ]
    lines += [f"  {t:<12} {c}" for t, c in sorted(by_type.items(), key=lambda kv: -kv[1])]
    lines += ["", "Sample:"]
    lines += [f"  {p['id']}  {p['type']:<10} {p['summary']}" for p in planned[:10]]
    if len(planned) > 10:
        lines.append(f"  ... and {len(planned) - 10} more")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vibe-cognition-remap-identity",
        description="Remap graph attribution from one email to another (dry run unless --apply).",
    )
    parser.add_argument("project_path", help="Project root containing .cognition/")
    parser.add_argument("--from", dest="old", required=True, help="Email currently attributed")
    parser.add_argument("--to", dest="new", required=True, help="Email to attribute to instead")
    parser.add_argument("--name", help="Also correct the display name on remapped entries")
    parser.add_argument("--apply", action="store_true", help="Actually write (default: dry run)")
    args = parser.parse_args(argv)

    old, new = _casefold(args.old), _casefold(args.new)
    if not old or not new:
        print("--from and --to must both be non-empty", file=sys.stderr)
        return 2
    if old == new:
        print("--from and --to are the same address; nothing to do", file=sys.stderr)
        return 2

    cognition_dir = Path(args.project_path) / ".cognition"
    if not cognition_dir.is_dir():
        print(f"No .cognition/ directory under {args.project_path}", file=sys.stderr)
        return 2

    storage = CognitionStorage(cognition_dir)
    planned = plan_remap(storage, old, new)
    print(_report(planned, old, new))

    if not planned:
        return 0
    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply to write.")
        return 0

    # Same concurrency guard as backfill_identity: a live session appending
    # between the plan and the write would make the plan stale.
    journal_path = cognition_dir / "journal.jsonl"
    mtime_before = journal_path.stat().st_mtime if journal_path.exists() else None
    changed = apply_remap(storage, old, new, args.name)
    if journal_path.exists() and journal_path.stat().st_mtime != mtime_before and changed == 0:
        print("Journal changed during the run and nothing was written; re-run.", file=sys.stderr)
        return 1
    print(f"\nRemapped {changed} node(s). Original addresses preserved as remapped_from.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
