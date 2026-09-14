"""Notice memories that vanished from the journal BETWEEN sessions.

A live server already detects a journal replaced under it and raises the loud
journal-loss alert. What it cannot see is loss that happens while no session is
running -- and on Subversion that is the ordinary routine: close the editor,
`svn update`, resolve a conflict, reopen. SVN has no union merge, so a journal
conflict is real, and the two buttons every SVN client puts first -- "resolve using
mine" and "resolve using theirs" -- each permanently delete one side's memories.
Verified in the SVN abuse lab: both lost nodes silently, with no warning anywhere.

So each checkout keeps a machine-local record of the node ids it has seen. At
startup, any remembered id that is gone WITHOUT a deletion tombstone in the journal
was lost, and the existing alert fires. Ids, not bytes, so a line-ending conversion
cannot raise a false alarm.

SCOPED TO SVN WORKING COPIES, deliberately. On git, `merge=union` makes this loss
mode rare, while switching branches legitimately removes every id that lives only
on the other branch -- an alert there would fire on every checkout and train people
to ignore it.

`svn switch` and `svn update -r <older>` are the same shape on SVN, and an alert
there would hand an agent a recovery recipe that re-imports another branch's lines.
So the snapshot also records where the journal came from (repository URL and base
revision, read from `.svn/wc.db` so no `svn` CLI is needed). A changed URL or a
revision that went backwards is a deliberate move: re-baseline, say nothing.

Known limit: ids a teammate's commit delivered DURING a session are recorded only at
the next startup, so their loss in between is not detected here. The teammate's own
checkout still holds them and detects its own loss.
"""

import contextlib
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .checkout_binding import binding_mismatch, current_binding, is_bound
from .journal_io import append_journal_line
from .local_paths import read_path, write_path
from .svn_identity import is_svn_working_copy

logger = logging.getLogger(__name__)

KNOWN_IDS_FILENAME = "known-node-ids.json"
KNOWN_IDS_LOG_FILENAME = "known-node-ids.log"

#: Written into the rehydrate flag so prime picks the between-session wording.
LOSS_KIND_BETWEEN_SESSIONS = "between_sessions"


def _wc_root(cognition_dir: Path) -> Path | None:
    """The SVN working-copy root holding this project. Since SVN 1.7 `.svn` exists
    only there, so a project checked out as a subfolder has none of its own."""
    start = Path(cognition_dir).parent
    for candidate in (start, *start.parents):
        if is_svn_working_copy(candidate):
            return candidate
        if (candidate / ".git").exists():
            return None
    return None


def applies_to(cognition_dir: Path) -> bool:
    return _wc_root(cognition_dir) is not None


def journal_source(cognition_dir: Path) -> dict[str, Any] | None:
    """Where the checked-out journal came from: `{"url", "revision"}`, or None.

    Read-only query of the working copy database. The nearest recorded ancestor
    is used when the journal itself is not yet committed.
    """
    root = _wc_root(cognition_dir)
    if root is None:
        return None
    db = root / ".svn" / "wc.db"
    if not db.is_file():
        return None
    try:
        rel_cognition = Path(cognition_dir).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    relpath = f"{rel_cognition}/journal.jsonl"
    query = (
        "SELECT r.root, n.repos_path, n.revision FROM nodes n "
        "JOIN repository r ON r.id = n.repos_id "
        "WHERE n.local_relpath = ? AND n.op_depth = 0"
    )
    try:
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    except (sqlite3.Error, ValueError):
        return None
    try:
        project = rel_cognition.rpartition("/")[0]
        candidates = list(dict.fromkeys([relpath, rel_cognition, project, ""]))
        for candidate in candidates:
            row = conn.execute(query, (candidate,)).fetchone()
            if row and row[0] is not None:
                return {
                    "url": f"{str(row[0]).rstrip('/')}/{row[1]}",
                    "revision": int(row[2]) if candidate == relpath and row[2] is not None else None,
                }
        return None
    except (sqlite3.Error, ValueError, TypeError):
        return None
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def _moved_deliberately(recorded: Any, current: dict[str, Any] | None) -> bool:
    """A switch to another URL, or an update back to an older revision."""
    if not isinstance(recorded, dict) or current is None:
        return False
    if recorded.get("url") != current["url"]:
        return True
    before, now = recorded.get("revision"), current["revision"]
    return isinstance(before, int) and isinstance(now, int) and now < before


def _read_known(cognition_dir: Path) -> tuple[set[str], Any] | None:
    """Ids this checkout last recorded, and where its journal came from then.

    None when there is no usable record. A record written for another machine,
    account or folder is ignored rather than trusted: if `.cognition/local` ever
    travels through version control, comparing against a teammate's snapshot would
    report their uncommitted work as "lost" here.
    """
    try:
        data = json.loads(read_path(cognition_dir, KNOWN_IDS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not is_bound(data):
        return None
    if binding_mismatch(data, cognition_dir) is not None:
        return None
    ids = data.get("ids")
    if not isinstance(ids, list):
        return None
    known = {str(i) for i in ids}
    try:
        log = read_path(cognition_dir, KNOWN_IDS_LOG_FILENAME).read_text(encoding="utf-8")
    except OSError:
        log = ""
    known.update(line.strip() for line in log.splitlines() if line.strip())
    return known, data.get("source")


def _write_snapshot(
    cognition_dir: Path, ids: set[str], accounted: set[str], source: dict[str, Any] | None,
) -> None:
    payload = {
        **current_binding(cognition_dir),
        "at": datetime.now(UTC).isoformat(),
        "source": source,
        "ids": sorted(ids),
    }
    snapshot = write_path(cognition_dir, KNOWN_IDS_FILENAME)
    tmp = snapshot.with_name(f"{KNOWN_IDS_FILENAME}.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(snapshot)
    # Drop every log line this check already accounted for -- present ones are in
    # the snapshot, missing ones were just reported -- and keep only lines appended
    # AFTER the log was read, by another process on this checkout.
    #
    # NOT "lines the snapshot does not cover": a LOST id is exactly such a line, so
    # that rule kept it in the log forever and re-reported it as newly lost on every
    # later startup -- a permanent false alarm, which trains people to ignore it.
    log = write_path(cognition_dir, KNOWN_IDS_LOG_FILENAME)
    try:
        pending = [
            line for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip() and line.strip() not in accounted and line.strip() not in ids
        ]
    except OSError:
        pending = []
    log.write_text("".join(f"{line}\n" for line in pending), encoding="utf-8")


def check_between_sessions(storage: Any) -> dict[str, Any] | None:
    """Startup check. Returns the loss report when memories vanished, else None.

    Must run AFTER the initial catch-up, so the graph and the tombstone set both
    reflect the whole journal. Never raises.
    """
    cognition_dir = Path(storage.cognition_dir)
    try:
        if not applies_to(cognition_dir):
            return None
        current = set(storage.graph.nodes)
        source = journal_source(cognition_dir)
        record = _read_known(cognition_dir)
        known, recorded_source = record if record else (set(), None)
        report = None
        if known and not _moved_deliberately(recorded_source, source):
            tombstoned = set(getattr(storage, "_removed_node_ids", set()))
            missing = sorted(known - current - tombstoned)
            if missing:
                report = {
                    "kind": LOSS_KIND_BETWEEN_SESSIONS,
                    "at": datetime.now(UTC).isoformat(),
                    "nodes_before": len(known),
                    "nodes_after": len(current),
                    "nodes_lost": len(missing),
                    "sample_missing_ids": missing[:5],
                }
        # An unreadable wc.db this time must not erase the last known source, or
        # the next deliberate switch would go unrecognised.
        _write_snapshot(cognition_dir, current, known, source or recorded_source)
        return report
    except Exception as exc:  # noqa: BLE001
        logger.debug("journal-watch: startup check failed (swallowed): %s", exc)
        return None


def note_created(cognition_dir: Path, node_id: str) -> None:
    """Record a node this session created, so losing it before the next startup is
    noticed. One small append per node. Never raises."""
    try:
        if not applies_to(cognition_dir):
            return
        append_journal_line(write_path(Path(cognition_dir), KNOWN_IDS_LOG_FILENAME), node_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("journal-watch: could not note %s (swallowed): %s", node_id, exc)
