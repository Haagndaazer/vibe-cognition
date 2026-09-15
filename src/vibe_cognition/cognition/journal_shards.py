"""Per-user journal shards: layout, stamps, adoption and straggler detection.

The graph used to live in one `.cognition/journal.jsonl` that every teammate appended
to, so two people writing between syncs conflicted -- resolvable by union merge on
git, but not at all on Subversion. Each person now appends only to their own file
under `.cognition/journal/`, and the graph is replayed from the legacy journal plus
every shard (docs/wp-journal-shards-plan.md).

The legacy journal is never split or rewritten: 78% of its lines carry no identity,
so there is no lawful way to divide it. It is frozen -- this plugin never appends to
it again -- and stays a replay input forever.

Stamps. With several files there is no single byte order, and a running process picks
up whichever file changed first, so applying lines in arrival order would let two
processes settle on different values permanently. Every shard line therefore carries
`at` (its write time), and conflicting writes resolve by stamp, not arrival:

    shard line:  (1, at, file name, sha256 of the line)
    legacy line: (0, "", "", line index)

The line hash, not its byte offset, breaks ties, so a git merge that inserts lines
and shifts offsets cannot reorder any decision.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .people_facts import email_slug

LEGACY_JOURNAL_FILENAME = "journal.jsonl"
SHARD_DIRNAME = "journal"
SHARD_SUFFIX = ".jsonl"
SHARD_START_ACTION = "shard_start"

TIER_LEGACY = 0
TIER_SHARD = 1

Stamp = tuple[int, str, str, str]

_AT_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


def shard_dir(cognition_dir: Path) -> Path:
    return Path(cognition_dir) / SHARD_DIRNAME


def legacy_journal(cognition_dir: Path) -> Path:
    return Path(cognition_dir) / LEGACY_JOURNAL_FILENAME


def shard_filename(email: str) -> str:
    slug = email_slug(email)
    if not slug:
        raise ValueError("a shard needs a non-empty email")
    return f"{slug}{SHARD_SUFFIX}"


def shard_path(cognition_dir: Path, email: str) -> Path:
    return shard_dir(cognition_dir) / shard_filename(email)


def journal_files(cognition_dir: Path) -> list[Path]:
    """Every existing journal file, legacy first, then shards by name."""
    files = []
    legacy = legacy_journal(cognition_dir)
    if legacy.is_file():
        files.append(legacy)
    try:
        shards = sorted(
            p for p in shard_dir(cognition_dir).iterdir()
            if p.is_file() and p.name.endswith(SHARD_SUFFIX)
        )
    except OSError:
        shards = []
    return files + shards


def journal_signature(cognition_dir: Path) -> tuple[tuple[str, int, int], ...]:
    """(name, size, mtime) for every journal file -- changes when anything writes."""
    out = []
    for path in journal_files(cognition_dir):
        try:
            st = path.stat()
        except OSError:
            continue
        out.append((path.name, st.st_size, st.st_mtime_ns))
    return tuple(out)


def has_journal(cognition_dir: Path) -> bool:
    return bool(journal_files(cognition_dir))


def format_at(moment: datetime) -> str:
    """Always microseconds and +00:00, so stamps compare correctly as strings."""
    return moment.astimezone(UTC).strftime(_AT_FORMAT)


def next_at(floor: str | None = None, last: str | None = None) -> str:
    """A write time later than `floor` (the newest stamp this write must beat) and
    `last` (this process's previous write). A teammate with a fast clock cannot make
    a later edit silently lose -- the same rule profiles use."""
    now = format_at(datetime.now(UTC))
    for bound in (floor, last):
        if bound and now <= bound:
            now = format_at(datetime.strptime(bound, _AT_FORMAT).replace(tzinfo=UTC)
                            + timedelta(microseconds=1))
    return now


def line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def shard_stamp(at: str, file_name: str, line: str) -> Stamp:
    return (TIER_SHARD, at, file_name, line_hash(line))


def legacy_stamp(index: int) -> Stamp:
    return (TIER_LEGACY, "", "", f"{index:012d}")


def split_entries(line: str) -> tuple[list[dict[str, Any]], str]:
    """Every complete JSON object on one line, and any unreadable remainder.

    Two appends with no terminator between them land on one line; reading only the
    first object dropped the rest silently (a Northstar episode and 13 entries it
    anchored, lost for 25 days)."""
    decoder = json.JSONDecoder()
    entries: list[dict[str, Any]] = []
    pos = 0
    text = line.strip()
    while pos < len(text):
        try:
            obj, end = decoder.raw_decode(text, pos)
        except ValueError:
            return entries, text[pos:]
        if isinstance(obj, dict):
            entries.append(obj)
        pos = end
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return entries, ""


def encode_entry(action: str, data: dict[str, Any], at: str | None = None) -> str:
    record: dict[str, Any] = {"action": action, "data": data}
    if at is not None:
        record["at"] = at
    return json.dumps(record, ensure_ascii=False)


def shard_start_line(cognition_dir: Path, plugin_version: str | None, at: str) -> str:
    legacy = legacy_journal(cognition_dir)
    try:
        data = legacy.read_bytes()
    except OSError:
        data = b""
    return encode_entry(SHARD_START_ACTION, {
        "legacy_bytes": len(data),
        "legacy_sha256": hashlib.sha256(data).hexdigest(),
        "plugin_version": plugin_version,
    }, at)


def adoption(cognition_dir: Path) -> dict[str, Any] | None:
    """The earliest shard_start in the project: when it switched to shards, and how
    big the legacy journal was then. None when no shard exists."""
    earliest: dict[str, Any] | None = None
    for path in journal_files(cognition_dir):
        if path.name == LEGACY_JOURNAL_FILENAME:
            continue
        try:
            with path.open("rb") as fh:
                first = fh.readline().decode("utf-8", errors="replace")
        except OSError:
            continue
        entries, _ = split_entries(first)
        start = next((e for e in entries if e.get("action") == SHARD_START_ACTION), None)
        if not start or not isinstance(start.get("at"), str):
            continue
        if earliest is None or start["at"] < earliest["at"]:
            earliest = {
                "at": start["at"],
                "legacy_bytes": int(start.get("data", {}).get("legacy_bytes") or 0),
                "shard": path.name,
            }
    return earliest


def _entry_time(entry: dict[str, Any]) -> str | None:
    data = entry.get("data") or {}
    for key in ("timestamp", "removed_at", "at"):
        value = data.get(key) if key != "at" else entry.get("at")
        if isinstance(value, str) and value:
            return value
    return None


def _entry_author(entry: dict[str, Any]) -> str | None:
    data = entry.get("data") or {}
    meta = data.get("metadata") or {}
    for who in (meta.get("recorded_by"), meta.get("created_by"), data.get("removed_by")):
        if isinstance(who, dict) and who.get("email"):
            return str(who["email"])
    author = data.get("author")
    return str(author) if isinstance(author, str) and author else None


def straggler_report(cognition_dir: Path) -> dict[str, Any] | None:
    """Legacy-journal writes made AFTER the project switched to shards.

    Only a plugin that predates sharding still appends to the legacy journal. Lines
    past the adoption size whose own time is after adoption are such writes; lines
    without a time, or timed before adoption, are pre-upgrade work that merged in
    late and are not reported. None when there is nothing to report.
    """
    start = adoption(cognition_dir)
    if start is None:
        return None
    try:
        data = legacy_journal(cognition_dir).read_bytes()
    except OSError:
        return None
    tail = data[start["legacy_bytes"]:] if len(data) > start["legacy_bytes"] else b""
    if not tail:
        return None
    count, latest, authors = 0, "", set()
    for raw in tail.decode("utf-8", errors="replace").splitlines():
        entries, _ = split_entries(raw)
        for entry in entries:
            when = _entry_time(entry)
            if not when or when <= start["at"]:
                continue
            count += 1
            latest = max(latest, when)
            if author := _entry_author(entry):
                authors.add(author)
    if not count:
        return None
    return {
        "adopted_at": start["at"],
        "entries_after_adoption": count,
        "latest_at": latest,
        "authors": sorted(authors),
    }


def format_straggler_warning(report: dict[str, Any]) -> str:
    who = ", ".join(report["authors"]) if report["authors"] else "an unknown teammate"
    return (
        "## Teammate on an old plugin version\n"
        f"This project switched to per-person journal files on {report['adopted_at'][:10]}, "
        f"but {report['entries_after_adoption']} memor"
        f"{'y was' if report['entries_after_adoption'] == 1 else 'ies were'} written to the "
        f"old shared journal since then (latest {report['latest_at'][:19]}), by {who}. "
        "Those entries are still read, but they conflict like before and lose to any "
        "newer edit. Whoever wrote them should update vibe-cognition. TELL THE USER."
    )
