"""The one session-start slot for journal-loss and journal-repair notices.

Three different things can need to tell the reader something about the journal:
a rollback that was repaired, a file this checkout may not repair, and a loss that
happened between sessions. They used to share one flag file by each writing the whole
file, so whichever ran last silently erased the others: a repair notice was wiped by an
ordinary replayed deletion, and a between-sessions loss could erase the repair notice
that named the file to commit (both found in review, both reproduced).

So the flag holds a LIST of notices, each keyed by `kind`, and every writer touches only
its own. A file written by an older version -- a single notice object -- still reads.
"""

import contextlib
import json
from pathlib import Path
from typing import Any

from .journal_io import journal_lock


def read_notices(flag: Path) -> list[dict[str, Any]]:
    """Every outstanding notice. Empty when there is no flag or it is unreadable."""
    try:
        raw = json.loads(flag.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(raw, dict):
        notices = raw.get("notices")
        if isinstance(notices, list):
            return [n for n in notices if isinstance(n, dict)]
        return [raw]  # a pre-0.44.0 single-notice flag
    return []


def write_notices(flag: Path, notices: list[dict[str, Any]]) -> None:
    """Replace the outstanding notices; an empty list removes the flag entirely."""
    if not notices:
        with contextlib.suppress(OSError):
            flag.unlink()
        return
    flag.write_text(json.dumps({"notices": notices}), encoding="utf-8")


def replace_kind(flag: Path, kinds: tuple[str, ...], notice: dict[str, Any] | None) -> None:
    """Set (or clear) this writer's notice, leaving every other writer's alone.

    Under the lock: a live server and a session-start prime run in different processes
    and both touch this file, so an unlocked read-modify-write silently drops whichever
    update lands first -- including, in the worst case, a brand-new repair notice nobody
    has read yet (peer review).
    """
    with notices_lock(flag):
        others = [n for n in read_notices(flag) if n.get("kind") not in kinds]
        write_notices(flag, [*others, notice] if notice is not None else others)


def notices_lock(flag: Path):
    """Hold the notices file's sibling lock for a read-modify-write.

    A SIBLING file, never the flag itself: Windows refuses to replace or unlink a path
    while a handle on it is open.
    """
    return journal_lock(flag.with_name(flag.name + ".lock"))
