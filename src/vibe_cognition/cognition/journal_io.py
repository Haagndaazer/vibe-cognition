"""Atomic, cross-process append for the cognition journal (audit C-1 / H-2).

STDLIB-ONLY BY CONTRACT. A test pins this — do NOT import anything outside the
standard library here. This is forward-compat posture: the server
(storage._append_journal) is the only caller today, running inside the full
venv, but the module stays dependency-free so any future path-loaded caller
(a git hook, a script run against a bare venv) can reuse it without pulling in
third-party deps.

Atomicity (C-1): each append goes to an O_APPEND fd while an exclusive lock is
held, and the record is written in a loop-to-completion that is interleave-safe
ONLY because the lock is held. A single os.write to an O_APPEND fd is atomic in
practice for small records on a local filesystem, but a short write (signal /
disk pressure) would otherwise leave a truncated record that the next appender
concatenates onto — exactly audit C-2 (a torn tail eats the next entry). The
lock closes that. SCOPE: a local filesystem. O_APPEND offset semantics and
advisory locks are unreliable on live network mounts (NFS/SMB); not supported.

Lock fallback residual window: the fallback is reachable ONLY on Windows. On
POSIX, fcntl.flock(LOCK_EX) blocks until granted, so acquisition cannot "fail";
on Windows, msvcrt byte-range locking times out (~10s per attempt). We retry a
bounded number of times, then fall back to a single os.write + a loud log —
never blocking forever, never dropping the entry. HONEST residual: on Windows,
CRT O_APPEND is a non-atomic seek-to-EOF-then-write, so a fallback write racing
the still-locked holder can OVERWRITE a whole record (not merely truncate it).
This is gated on sustained (> retries × ~10s) contention first, so it is rare
and bounded — but it is whole-record loss, not a torn tail.

POSIX has NO acquire timeout (fcntl.flock blocks until granted). A wedged-but-
alive lock holder would therefore hang every session's appends on POSIX (Windows
gets the ~10s timeout; POSIX gets none). Acceptable because the critical section
is a single bounded os.write — no legitimate holder keeps the lock long — but
stated so the asymmetry is visible.

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

# How many times to try acquiring the lock before the Windows-only fallback. Each
# Windows attempt is ~10s (msvcrt timeout); POSIX flock blocks until granted so it
# never reaches a second attempt.
_LOCK_ATTEMPTS = 2


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
        if any(_acquire(fd) for _ in range(_LOCK_ATTEMPTS)):
            try:
                _write_all(fd, blob)
            finally:
                _release(fd)
        else:
            # Windows-only (POSIX flock never fails to acquire). See the module's
            # "fallback residual window": under sustained contention this single
            # write can OVERWRITE a record (non-atomic CRT O_APPEND). Loud log;
            # never block forever, never drop the entry.
            logger.warning(
                "journal append: lock unavailable after %d attempts, single-write "
                "fallback — record may be lost under contention (path=%s)",
                _LOCK_ATTEMPTS, path,
            )
            os.write(fd, blob)
    finally:
        os.close(fd)


def snapshot_journal(src, dst) -> None:
    """Copy the journal to dst while holding the append lock — so the copy can
    never capture a torn mid-append tail.

    The manager flush reads the LIVE journal; the append lock guards APPENDERS
    only, so without this a copy could capture a half-written final line and, once
    committed, every clone that pulls it parks before that line forever and the
    next local append concatenates onto it — audit C-2 via the read path. Taking
    the same exclusive lock here excludes appenders for the duration of the copy.
    Lock acquisition is best-effort (Windows may time out under contention); the
    caller's last-byte-is-newline check is the backstop in that rare case.
    """
    lock_fd = os.open(
        os.fspath(src), os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_BINARY, 0o644
    )
    try:
        any(_acquire(lock_fd) for _ in range(_LOCK_ATTEMPTS))
        try:
            with open(os.fspath(src), "rb") as rf, open(os.fspath(dst), "wb") as wf:
                wf.write(rf.read())
        finally:
            _release(lock_fd)
    finally:
        os.close(lock_fd)
