"""Every journal line THIS checkout wrote, kept machine-locally so a rolled-back
shard can be repaired (docs/wp-append-only-replay-plan.md §3.3).

A journal file is the durable record, but an ordinary `git checkout -- .cognition`,
`git restore`, `git stash` or `svn revert` can put an OLDER copy of it on disk. Replay
then read that shorter file as truth and reset the graph down to it: five recorded
incidents, up to 93 nodes, two unrecoverable.

Only two things are needed to repair that, and this file is one of them: the lines
this checkout appended itself, verbatim. They are ours to re-append -- we wrote them,
so putting them back is not a merge decision, and an append can never destroy what is
already on disk. A teammate's shard and the frozen legacy journal are never repaired
from here, only reported.

Written SYNCHRONOUSLY with the journal append, not on the next read. The reported
incident is "write, session ends, nothing reads that shard again" -- a ledger filled
in on the next catch-up would miss exactly the case it exists for.

Checkout-bound like every other machine-local record (checkout_binding): a ledger that
travelled to another machine, account or folder is ignored rather than trusted, so a
copied project cannot claim to have written someone else's lines.
"""

import json
import logging
from pathlib import Path

from .checkout_binding import binding_mismatch, current_binding, is_bound
from .journal_io import append_journal_line, journal_lock
from .local_paths import read_path, write_path

logger = logging.getLogger(__name__)

OWN_LEDGER_FILENAME = "own-appends.jsonl"


def _path(cognition_dir: Path, *, write: bool = False) -> Path:
    fn = write_path if write else read_path
    return fn(Path(cognition_dir), OWN_LEDGER_FILENAME)


def record(cognition_dir: Path, shard_name: str, line: str) -> None:
    """Remember one line this checkout just appended. Never raises."""
    try:
        path = _path(cognition_dir, write=True)
        if not path.exists():
            # Unlocked check-then-append: two processes creating the ledger at the same
            # instant can both write a header. Harmless -- every header on one checkout
            # carries the same binding, and the reader takes the last one it sees.
            append_journal_line(path, json.dumps({"binding": current_binding(cognition_dir)}))
        append_journal_line(path, json.dumps({"file": shard_name, "line": line}))
    except Exception as exc:  # noqa: BLE001 - a ledger failure must never fail a write
        logger.debug("own-ledger: could not record a line (swallowed): %s", exc)


def lines_for(cognition_dir: Path, shard_name: str) -> list[str]:
    """This checkout's own appended lines for one shard, oldest first.

    Empty when there is no ledger, or when the one on disk was written for another
    machine, account or folder.
    """
    try:
        raw = _path(cognition_dir).read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    bound: bool | None = None
    for row in raw.splitlines():
        row = row.strip()
        if not row:
            continue
        try:
            entry = json.loads(row)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if "binding" in entry:
            record_binding = entry["binding"]
            bound = (
                isinstance(record_binding, dict)
                and is_bound(record_binding)
                and binding_mismatch(record_binding, cognition_dir) is None
            )
            continue
        if entry.get("file") == shard_name and isinstance(entry.get("line"), str):
            out.append(entry["line"])
    if bound is not True:
        return []
    return out


def prune(cognition_dir: Path, keep: set[str]) -> None:
    """Keep only the recorded lines in `keep`, dropping the rest. Never raises.

    Called when a deliberate move re-baselines this checkout, and by `accept-disk` when
    the human accepts the shorter file as the truth. NOT called after a successful
    heal: the ledger has to still hold this checkout's lines the NEXT time a file is
    rolled back, so emptying it whenever disk currently agrees would leave nothing to
    repair from.

    Takes the same cross-process lock the journal append takes, so a live session
    appending to the ledger while a CLI prunes it cannot lose that line.
    """
    try:
        path = _path(cognition_dir, write=True)
        if not path.exists():
            return
        # Lock a SIBLING file, never the one being replaced: Windows refuses to
        # replace a path while any handle on it is open, so locking the ledger itself
        # made every prune fail silently (caught in review testing).
        with journal_lock(path.with_name(path.name + ".lock")):
            kept = [json.dumps({"binding": current_binding(cognition_dir)})]
            for row in path.read_text(encoding="utf-8").splitlines():
                row = row.strip()
                if not row:
                    continue
                try:
                    entry = json.loads(row)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("line") in keep:
                    kept.append(row)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("own-ledger: could not prune (swallowed): %s", exc)
