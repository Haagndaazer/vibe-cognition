"""Shared catch-up engine for append-only per-person JSONL directories.

Extracted from PeopleFactsRegistry so the profile registry can reuse it rather
than carry a second copy. The scar tissue here was expensive: the racy-mtime
guard was found by Windows CI, and writing it twice means two chances to get the
window or the abs() clamp wrong. Two registries over one directory would also
double the stat sweep per cycle.

Subclasses supply only what differs — the action vocabulary and the fold — via
``_fold_entry`` and ``_drop_contribution``. Everything about WHEN to re-read,
how to survive a torn append, and how to detect a divergent rewrite lives here.

NOT thread-safe on its own: callers hold CognitionStorage._lock, mirroring how
the graph and _reference_index are guarded.
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# A dir mtime observed within this window of now may share its coarse tick with
# a same-tick creation, so equality cannot be trusted until it is safely past.
DIR_MTIME_RACY_WINDOW_NS = 2_000_000_000  # 2s

#: Suffixes claimed by specific registries sharing a directory. The generic
#: ".jsonl" owner must exclude them, because "x.profile.jsonl".endswith(".jsonl")
#: is True and it would otherwise read, hash and track its sibling's files.
#:
#: Declared here as a LITERAL, deliberately, rather than self-registered by each
#: subclass: registration only happens once that subclass's module is imported,
#: so a caller importing people_facts WITHOUT profiles would silently get the
#: un-excluded behaviour back. Verified — that hazard is real, not theoretical.
#: One greppable place, correct regardless of import order. ADD NEW PER-PERSON
#: FILE SUFFIXES HERE.
_RESERVED_SUFFIXES: frozenset[str] = frozenset({".profile.jsonl"})


@dataclass
class FileState:
    """Per-file replay state — the main journal's offset/hasher/mtime trio."""

    offset: int = 0
    hasher: "hashlib._Hash" = field(default_factory=hashlib.sha256)
    mtime_ns: int | None = None
    # Keys whose folded state came from this file. Folding trusts the LINES,
    # not the filename — defensive against a hand-edited or mis-slugged file.
    keys: set[str] = field(default_factory=set)

    def reset(self) -> None:
        self.offset = 0
        self.hasher = hashlib.sha256()
        self.keys = set()


class JsonlDirRegistry:
    """Folds append-only JSONL files in one directory, re-reading only deltas."""

    #: Only files with this suffix are folded, so two registries can share a
    #: directory without seeing each other's records.
    FILE_SUFFIX = ".jsonl"

    def __init__(self, directory: Path):
        self._dir = Path(directory)
        self._files: dict[str, FileState] = {}
        self._dir_mtime_ns: int | None = None
        self._dir_mtime_racy = False

    # ── Subclass hooks ──────────────────────────────────────────────────

    def _fold_entry(self, fs: FileState, entry: dict[str, Any]) -> None:
        """Apply one parsed record. Record any key touched in ``fs.keys``."""
        raise NotImplementedError

    def _drop_contribution(self, fs: FileState) -> None:
        """Undo everything ``fs`` contributed, before it is re-folded or dropped."""
        raise NotImplementedError

    def _clear_all(self) -> None:
        """Drop all folded state (the directory vanished)."""
        raise NotImplementedError

    # ── Engine ──────────────────────────────────────────────────────────

    def catch_up(self) -> int:
        """Fold records appended since the last pass; return folded line count.

        Cost contract: one dir stat gates the listdir; each known file is
        stat-gated before any read, so a quiescent pass costs 1 + N stats and
        zero reads. While the dir was modified within the racy window every
        pass re-lists (cheap, active-write periods only). A truncation or
        divergent rewrite re-folds THAT file only, never a global reset.

        Residual, disclosed not fixed (shared with the journal's _catch_up): a
        rewrite coincidentally matching both a file's size and st_mtime_ns
        evades its cheap path until the next real change.
        """
        try:
            dir_stat = self._dir.stat()
        except OSError:
            if self._files:
                self._clear_all()
                self._files.clear()
                self._dir_mtime_ns = None
                self._dir_mtime_racy = False
            return 0

        if dir_stat.st_mtime_ns != self._dir_mtime_ns or self._dir_mtime_racy:
            self._dir_mtime_ns = dir_stat.st_mtime_ns
            # Trust equality on FUTURE passes only once this observation is
            # safely in the past. abs() so far-future clock skew degrades to
            # the cheap gate instead of pinning racy forever.
            self._dir_mtime_racy = (
                abs(time.time_ns() - dir_stat.st_mtime_ns) < DIR_MTIME_RACY_WINDOW_NS
            )
            try:
                names = {p.name for p in self._dir.iterdir() if self._owns(p.name)}
            except OSError:
                return 0
            for gone in set(self._files) - names:
                self.drop_file(gone)
            for new in names - set(self._files):
                self._files[new] = FileState()

        folded = 0
        for name in list(self._files):
            folded += self._catch_up_file(name)
        return folded

    def _owns(self, name: str) -> bool:
        """Files this registry folds.

        A generic-suffix registry must not swallow a more specific sibling's
        files: ".jsonl" matches "x.profile.jsonl" too. Specific registries claim
        their suffix on subclass creation, so this stays correct as new
        per-person file types are added.
        """
        if not name.endswith(self.FILE_SUFFIX):
            return False
        return not any(
            name.endswith(reserved)
            for reserved in _RESERVED_SUFFIXES
            if reserved != self.FILE_SUFFIX
        )

    def drop_file(self, name: str) -> None:
        fs = self._files.pop(name, None)
        if fs is not None:
            self._drop_contribution(fs)

    def register_own_write(self, name: str) -> None:
        """Track a file this process just created.

        Dir-mtime discovery is timestamp-racy: a creation in the same coarse
        tick as a cached dir stat is numerically invisible (the Windows CI
        catch), so a writer registers its own file rather than waiting to be
        noticed.
        """
        self._files.setdefault(name, FileState())

    def _catch_up_file(self, name: str) -> int:
        fs = self._files[name]
        path = self._dir / name
        try:
            st = path.stat()
        except OSError:
            self.drop_file(name)
            return 0

        if st.st_size == fs.offset and st.st_mtime_ns == fs.mtime_ns:
            return 0
        fs.mtime_ns = st.st_mtime_ns

        try:
            data = path.read_bytes()
        except OSError:
            return 0

        if st.st_size < fs.offset or (
            fs.offset > 0
            and hashlib.sha256(data[: fs.offset]).digest() != fs.hasher.digest()
        ):
            # Truncated or divergently rewritten (e.g. a merge) — re-fold THIS
            # file only, from the top.
            self._drop_contribution(fs)
            fs.reset()

        raw = data[fs.offset:]
        last_nl = raw.rfind(b"\n")
        if last_nl == -1:
            return 0  # torn tail — park before it, re-read once complete
        complete = raw[: last_nl + 1]
        fs.offset += len(complete)
        fs.hasher.update(complete)

        count = 0
        for line in complete.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self._fold_entry(fs, json.loads(line))
                count += 1
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
                logger.warning("Skipping malformed line in %s: %s", name, e)
        return count
