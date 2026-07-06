"""WP-Sidecar (P0 endgame) §2: cross-process model-load serialization.

Evidence driving this: N concurrent sessions each constructing their own
sentence-transformers backend at once stampede the disk, and that
concurrency is what turns a healthy ~27s load into a 24-minute wedge (see
docs/wp-sidecar-plan.md §2). A machine-wide named mutex around the heavy
import + model load makes N concurrent sidecars load ONE at a time instead.

``Local\\`` namespace, not ``Global\\``: every server on this machine already
runs as the same interactive user in the same session (that's what the
namespace spans) -- ``Global\\`` buys nothing here and invites ACL questions
on multi-user boxes.

``WAIT_ABANDONED`` IS successful acquisition, not an error: the supervisor
(sidecar_client.py) deliberately kills a wedged sidecar while it may be
holding this mutex -- abandonment is DESIGNED behavior, a certainty over the
process's lifetime, not an edge case. Callers must branch on
``AcquireOutcome.ACQUIRED_ABANDONED`` the same way they branch on
``ACQUIRED``, just breadcrumbed distinctly.

Windows-first (ctypes over pywin32, no new runtime dependency); the fleet is
Windows. POSIX has no cross-process named-mutex equivalent worth building
here (the fleet doesn't run there) -- ``acquire()`` degrades to a no-op
ACQUIRED on POSIX, same "do not over-engineer" philosophy as lifecycle.py's
POSIX fallback.
"""

from __future__ import annotations

import ctypes
import enum
import sys

_IS_WINDOWS = sys.platform == "win32"

MUTEX_NAME = r"Local\vibe-cognition-model-load"

if _IS_WINDOWS:
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_ABANDONED = 0x00000080
    _WAIT_TIMEOUT = 0x00000102
    _WAIT_FAILED = 0xFFFFFFFF

    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE

    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD

    _kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    _kernel32.ReleaseMutex.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


class AcquireOutcome(enum.Enum):
    ACQUIRED = "acquired"
    ACQUIRED_ABANDONED = "acquired_abandoned"
    TIMEOUT = "timeout"


def create_mutex():
    """Open (or create) the named mutex. Returns a HANDLE on Windows, or a
    sentinel object on POSIX (never actually waited on there)."""
    if not _IS_WINDOWS:
        return object()
    handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def acquire(handle, timeout_seconds: float) -> AcquireOutcome:
    """Wait up to ``timeout_seconds`` to acquire the mutex.

    ``ACQUIRED_ABANDONED`` is returned when a previous holder was killed
    while holding it (WAIT_ABANDONED) -- this IS successful acquisition, the
    caller now owns the mutex exactly as if it had returned ACQUIRED.
    """
    if not _IS_WINDOWS:
        return AcquireOutcome.ACQUIRED
    result = _kernel32.WaitForSingleObject(handle, int(timeout_seconds * 1000))
    if result == _WAIT_OBJECT_0:
        return AcquireOutcome.ACQUIRED
    if result == _WAIT_ABANDONED:
        return AcquireOutcome.ACQUIRED_ABANDONED
    if result == _WAIT_TIMEOUT:
        return AcquireOutcome.TIMEOUT
    raise ctypes.WinError(ctypes.get_last_error())  # WAIT_FAILED -- genuinely unexpected


def release(handle) -> None:
    """Release a mutex previously acquired (ACQUIRED or ACQUIRED_ABANDONED
    only -- never call this after a TIMEOUT outcome, which never acquired
    anything). Best-effort: a failed release just means the mutex will
    read as abandoned on the NEXT process's acquire, which is already a
    handled, expected outcome -- not a reason to raise here."""
    if not _IS_WINDOWS:
        return
    _kernel32.ReleaseMutex(handle)


def close(handle) -> None:
    if not _IS_WINDOWS:
        return
    _kernel32.CloseHandle(handle)
