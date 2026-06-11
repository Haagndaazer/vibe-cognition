"""Atomic, cross-process append for the cognition journal (audit C-1 / H-2).

STDLIB-ONLY BY CONTRACT. This module is imported by the post-commit git hook,
which path-loads it and must run against a possibly-bare venv (no third-party
deps available). A test pins this — do NOT import anything outside the standard
library here. Both the server (storage._append_journal) and the hook route their
journal writes through append_journal_line(), so the format lives in ONE place
(closes H-2, where the hook used to fork the journal-line format).

Atomicity (C-1): each append goes to an O_APPEND fd while an exclusive lock is
held, and the record is written in a loop-to-completion that is interleave-safe
ONLY because the lock is held. A single os.write to an O_APPEND fd is atomic in
practice for small records on a local filesystem, but a short write (signal /
disk pressure) would otherwise leave a truncated record that the next appender
concatenates onto — exactly audit C-2 (a torn tail eats the next entry). The
lock closes that. SCOPE: a local filesystem. O_APPEND offset semantics and
advisory locks are unreliable on live network mounts (NFS/SMB); not supported.

Lock fallback residual window: if the lock cannot be acquired (Windows byte-range
contention times out after ~10s), we fall back to a SINGLE un-looped os.write of
the whole record plus a loud log — never blocking the caller, never dropping the
entry. That single write is interleave-safe for the common case; its only
residual is the rare short-write truncation the lock would have prevented.

Line endings: append_journal_line reproduces text-mode's per-platform terminator
as raw bytes (\\r\\n on Windows, \\n on POSIX), so moving from buffered text-mode
to os.write does NOT change the journal's on-disk bytes — the .gitattributes
-text byte-determinism and the byte-offset replay in storage._catch_up depend on
that. LF-normalization was considered and deferred: normalizing the live CRLF
journal is itself a whole-file rewrite under byte-offset replay (ledger 5).
"""

import logging
import os
import sys

# Direct `sys.platform == "win32"` checks (here and in _acquire/_release) let the
# type checker narrow per-platform: on Windows it ignores the unreachable fcntl
# branch (fcntl doesn't exist there) and vice-versa.
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)

# Reproduce what text-mode write() emitted per platform, as raw bytes.
_NEWLINE = b"\r\n" if os.linesep == "\r\n" else b"\n"

# O_BINARY (Windows) makes os.write emit raw bytes — WITHOUT it, os.open defaults
# to text mode on Windows and would translate our \n into \r\n, doubling the
# terminator (\r\r\n). 0 on POSIX (no text-mode translation there).
_O_BINARY = getattr(os, "O_BINARY", 0)

# Windows byte-range lock target: a fixed sentinel byte FAR past any real journal
# size, used purely as a cross-process mutex. Locking real data (e.g. byte 0)
# would let a mandatory Windows lock block another process's catch-up read of
# that byte; a sentinel past EOF is never read or written by normal operations,
# so the lock serializes appends WITHOUT touching the data region.
_WIN_LOCK_OFFSET = 1 << 40


def _acquire(fd: int) -> bool:
    """Take an exclusive lock on fd. Return True if held, False if unavailable.

    POSIX: whole-file advisory flock (blocks until acquired; reads are unaffected
    because it is advisory). Windows: a mandatory byte-range lock on a sentinel
    byte past EOF (msvcrt retries ~10x/~10s then raises OSError -> False).
    """
    try:
        if sys.platform == "win32":
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        return True
    except OSError:
        return False


def _release(fd: int) -> None:
    try:
        if sys.platform == "win32":
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _write_all(fd: int, blob: bytes) -> None:
    """Write every byte of blob. Safe to loop ONLY while the lock is held."""
    mv = memoryview(blob)
    while mv:
        mv = mv[os.write(fd, mv):]


def append_journal_line(path, line: str) -> None:
    """Append one JSON record (no trailing newline) to the journal, atomically.

    Args:
        path: Journal file path (str or os.PathLike).
        line: The single-line JSON record WITHOUT a trailing newline; the
              platform-correct terminator is added here.
    """
    blob = line.encode("utf-8") + _NEWLINE
    # mode 0o644 is POSIX-only; Windows ignores it (default ACLs).
    fd = os.open(os.fspath(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_BINARY, 0o644)
    try:
        if _acquire(fd):
            try:
                _write_all(fd, blob)
            finally:
                _release(fd)
        else:
            # Lock unavailable (Windows contention timeout). A single un-looped
            # write is interleave-safe for the common case; see the module's
            # "fallback residual window" note. Log loudly; never drop the entry.
            logger.warning(
                "journal append: lock unavailable, single-write fallback (path=%s)", path
            )
            os.write(fd, blob)
    finally:
        os.close(fd)
