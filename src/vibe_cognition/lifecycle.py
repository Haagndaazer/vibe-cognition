"""WP-Lifecycle (P1, docs/wp-lifecycle-plan.md rev 3) §L-a/§L-b: orphan
servers must die with their parent.

Two independent, server-side, belt-and-suspenders exit guarantees. The
client's own reap (killing the process tree on disconnect) is demonstrably
unreliable mid-session (evidence: docs/wp-lifecycle-plan.md §1) — the server
must guarantee its own exit without relying on it.

§L-a — ancestor-death watch (primary, works even mid-wedge):
Topology fact that drives this whole design: on Windows there is no exec, so
``plugin.json``'s ``uv run ... python -m vibe_cognition.server`` means our
DIRECT parent is uv, and uv waits on us — uv never dies first. Watching only
the direct parent would deadlock the pair forever (uv waits on python, python
waits on uv), which is exactly why orphans come in pairs. So this watches
BOTH uv (direct parent) and uv's own parent (the client, our grandparent) —
either dying forces ``os._exit(0)`` via a daemon thread blocked in
``WaitForMultipleObjects``, independent of the asyncio event loop (so it
fires even if the loop is frozen mid-import, Incident B's exact mode).

§L-b — pipe-closure watch (secondary, loop-independent by requirement):
The MCP-conventional stdin-EOF shutdown path rides the event loop (a
``to_thread`` readline's ``""`` must be processed ON the loop) — exactly the
path that never fires when the loop is frozen. A dedicated daemon thread
polls stdin via ``PeekNamedPipe`` (detects a broken pipe without consuming
data) and forces exit after a grace period, independent of the loop.

Windows-first (ctypes over pywin32 — no new runtime dependency); the fleet
is Windows. POSIX degrades to a slow ``os.getppid()`` poll for the ancestor
watch and skips the pipe watch entirely (breadcrumbed) — do not over-engineer
a platform we don't ship to.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from collections.abc import Callable

from . import _startup_timing

_IS_WINDOWS = sys.platform == "win32"

# Ancestor-walk depth: 1 = watch direct parent only (WP-Sidecar's reuse case,
# parent=server, no intermediary); 2 = watch direct parent + grandparent
# (this WP's uv-intermediary case). A parameter, not a constant, per the
# brief's explicit reuse requirement.
DEFAULT_ANCESTOR_DEPTH = 2

# §L-b: grace period between detecting a broken stdin pipe and forcing exit
# if graceful shutdown hasn't already completed on its own.
PIPE_CLOSE_GRACE_SECONDS = 5.0

# Fallback poll interval when a handle can't be waited on natively (ACCESS_
# DENIED case) or on POSIX (getppid has no blocking-wait equivalent).
_POLL_INTERVAL_SECONDS = 1.0
# Stdin-pipe poll interval (PeekNamedPipe is not blocking; this bounds how
# quickly a broken pipe is noticed).
_STDIN_POLL_INTERVAL_SECONDS = 0.5

if _IS_WINDOWS:
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _PROCESS_ACCESS = _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE

    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_TIMEOUT = 0x00000102

    _ERROR_ACCESS_DENIED = 5
    _ERROR_INVALID_PARAMETER = 87
    _ERROR_BROKEN_PIPE = 109

    _FILE_TYPE_PIPE = 3
    _FILE_TYPE_MASK = 0x0F  # low nibble; GetFileType can OR in FILE_TYPE_REMOTE
    _STD_INPUT_HANDLE = -10

    _STILL_ACTIVE = 259

    class _ProcessBasicInformation(ctypes.Structure):  # Win32 PROCESS_BASIC_INFORMATION
        _fields_ = [
            ("Reserved1", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("Reserved2", ctypes.c_void_p * 2),
            ("UniqueProcessId", ctypes.c_void_p),
            ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ]

    _NtQueryInformationProcess = _ntdll.NtQueryInformationProcess
    _NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    _NtQueryInformationProcess.restype = ctypes.c_long

    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.WaitForMultipleObjects.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    _kernel32.WaitForMultipleObjects.restype = wintypes.DWORD

    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL

    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    _kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    _kernel32.GetStdHandle.restype = wintypes.HANDLE

    _kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    _kernel32.GetFileType.restype = wintypes.DWORD

    _kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.PeekNamedPipe.restype = wintypes.BOOL

    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


def _filetime_to_int(ft) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _get_process_creation_time(handle) -> int | None:
    """GetProcessTimes creation time as a raw FILETIME int, or None on failure."""
    creation = wintypes.FILETIME()
    exit_t = wintypes.FILETIME()
    kernel_t = wintypes.FILETIME()
    user_t = wintypes.FILETIME()
    ok = _kernel32.GetProcessTimes(
        handle, ctypes.byref(creation), ctypes.byref(exit_t), ctypes.byref(kernel_t), ctypes.byref(user_t)
    )
    if not ok:
        return None
    return _filetime_to_int(creation)


def is_younger_than_self(handle) -> bool | None:
    """PID-reuse guard: True if the process behind `handle` was created AFTER
    our own process started (i.e. it cannot be our real ancestor -- a reused
    pid is necessarily younger than the process that opened it). Returns None
    if either creation time can't be read (fail open -- caller should treat
    an unreadable comparison as "can't validate, proceed with caution" rather
    than crash the watch)."""
    other_created = _get_process_creation_time(handle)
    if other_created is None:
        return None
    self_created = _get_process_creation_time(_kernel32.GetCurrentProcess())
    if self_created is None:
        return None
    return other_created > self_created


def _query_image_name(handle) -> str | None:
    buf_len = wintypes.DWORD(260)
    buf = ctypes.create_unicode_buffer(buf_len.value)
    ok = _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len))
    if not ok:
        return None
    return buf.value


def get_parent_pid_via_handle(handle) -> int | None:
    """The pid's parent pid, via NtQueryInformationProcess(ProcessBasicInformation)
    on an already-open handle to that pid. None on failure (process may have
    exited between OpenProcess and this call -- a real, expected race)."""
    info = _ProcessBasicInformation()
    return_length = ctypes.c_ulong()
    status = _NtQueryInformationProcess(
        handle, 0, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(return_length)
    )
    if status != 0:
        return None
    return int(info.InheritedFromUniqueProcessId or 0) or None


class _OpenResult:
    """Outcome of attempting to open a handle to a candidate ancestor pid."""

    def __init__(self, handle=None, pid_gone: bool = False, access_denied: bool = False):
        self.handle = handle
        self.pid_gone = pid_gone
        self.access_denied = access_denied


def _raw_open_process(rights: int, pid: int):
    """Thin wrapper around OpenProcess -- the seam tests monkeypatch to drive
    NULL/ACCESS_DENIED/success scenarios without a real second process."""
    return _kernel32.OpenProcess(rights, False, pid)


def _raw_last_error() -> int:
    return ctypes.get_last_error()


def _open_ancestor(pid: int) -> _OpenResult:
    handle = _raw_open_process(_PROCESS_ACCESS, pid)
    if handle:
        return _OpenResult(handle=handle)
    err = _raw_last_error()
    if err == _ERROR_ACCESS_DENIED:
        return _OpenResult(access_denied=True)
    # ERROR_INVALID_PARAMETER (pid slot no longer valid) and any other
    # failure are both treated as "pid is gone" -- OpenProcess has no other
    # legitimate failure mode for a PROCESS_QUERY_LIMITED_INFORMATION|
    # SYNCHRONIZE request against a plain pid.
    return _OpenResult(pid_gone=True)


def _pid_is_alive(pid: int) -> bool:
    """Slow-poll fallback liveness check (ACCESS_DENIED case): re-attempt to
    open the pid and, if possible, confirm it's still running via
    GetExitCodeProcess. If we can't even open it anymore, it's gone."""
    handle = _raw_open_process(_PROCESS_QUERY_LIMITED_INFORMATION, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


def _exit_now(reason: str, detail: str = "") -> None:
    # A wedged bg import can hold locks that make any graceful path (joins,
    # atexit, lifespan cleanup, even the logging module's own internal lock)
    # unreliable -- write directly to the raw stderr fd and exit immediately.
    # This is deliberately NOT routed through _startup_timing.stamp_and_flush:
    # os._exit is the point, and a stamp attempt that itself blocked on a
    # wedged lock would defeat the whole guarantee.
    try:
        sys.stderr.write(f"[vibe-lifecycle] {reason}: {detail}\n")
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)


def arm_ancestor_watch(
    depth: int = DEFAULT_ANCESTOR_DEPTH,
    exit_fn: Callable[[str, str], None] = _exit_now,
) -> threading.Thread | None:
    """§L-a: start a daemon thread that forces `exit_fn` when a watched
    ancestor dies. Returns the thread, or None on non-Windows (POSIX
    fallback is a separate, simpler function -- see `arm_ancestor_watch_posix`).

    `depth` follows the brief's reuse requirement: 1 watches only the direct
    parent (WP-Sidecar's parent=server, no intermediary); 2 (default) also
    resolves and watches the grandparent (this WP's uv intermediary case).
    """
    if not _IS_WINDOWS:
        return arm_ancestor_watch_posix(exit_fn=exit_fn)

    own_pid = os.getpid()
    own_parent_pid = get_parent_pid_via_handle(_kernel32.GetCurrentProcess())
    chain_breadcrumb = [f"self={own_pid}"]

    watched_handles: list[int] = []
    degraded = False

    if own_parent_pid is None:
        # Should not happen (querying our own process never races an exit),
        # but fail safe: nothing to watch, arm nothing, breadcrumb the surprise.
        chain_breadcrumb.append("parent=UNRESOLVED")
        degraded = True
    else:
        chain_breadcrumb.append(f"parent={own_parent_pid}")
        parent_result = _open_ancestor(own_parent_pid)
        if parent_result.pid_gone:
            # Direct parent (uv) already gone at arm time -- genuinely
            # orphaned before we ever got to watch anything. Exit now.
            exit_fn("parent_death_exit", f"direct parent {own_parent_pid} already gone at arm time")
            return None
        elif parent_result.access_denied:
            # Can't get a wait handle, but the pid is alive -- degrade to
            # slow polling for this ancestor rather than failing the watch.
            watched_handles.append(("poll", own_parent_pid))
        else:
            handle = parent_result.handle
            if is_younger_than_self(handle) is True:
                # PID reuse: what we opened is not our real parent (a reused
                # pid is necessarily younger than us) -- treat as gone.
                _kernel32.CloseHandle(handle)
                exit_fn(
                    "parent_death_exit",
                    f"direct parent pid {own_parent_pid} reused by a younger process",
                )
                return None
            watched_handles.append(("wait", handle))

            if depth >= 2:
                grandparent_pid = get_parent_pid_via_handle(handle)
                if grandparent_pid is None:
                    chain_breadcrumb.append("grandparent=UNRESOLVED")
                    degraded = True
                else:
                    chain_breadcrumb.append(f"grandparent={grandparent_pid}")
                    gp_result = _open_ancestor(grandparent_pid)
                    if gp_result.pid_gone:
                        # Grandparent already gone at arm time is NOT fatal --
                        # a launch shim that legitimately exits right after
                        # spawning uv is a normal topology, not an orphan.
                        # Degrade to uv-watch + pipe-watch only.
                        chain_breadcrumb.append("grandparent=GONE(degraded)")
                        degraded = True
                    elif gp_result.access_denied:
                        watched_handles.append(("poll", grandparent_pid))
                    else:
                        gp_handle = gp_result.handle
                        image = _query_image_name(gp_handle)
                        if is_younger_than_self(gp_handle) is True:
                            _kernel32.CloseHandle(gp_handle)
                            chain_breadcrumb.append(
                                f"grandparent=REUSED(degraded) image={image or 'UNKNOWN'}"
                            )
                            degraded = True
                        else:
                            if image:
                                chain_breadcrumb.append(f"grandparent_image={image}")
                            watched_handles.append(("wait", gp_handle))

    def _watch() -> None:
        wait_handles = [h for kind, h in watched_handles if kind == "wait"]
        poll_pids = [pid for kind, pid in watched_handles if kind == "poll"]

        if not wait_handles and not poll_pids:
            return

        array_type = wintypes.HANDLE * len(wait_handles) if wait_handles else None
        handle_array = array_type(*wait_handles) if wait_handles else None

        while True:
            if wait_handles:
                timeout_ms = int(_POLL_INTERVAL_SECONDS * 1000) if poll_pids else 0xFFFFFFFF
                result = _kernel32.WaitForMultipleObjects(
                    len(wait_handles), handle_array, False, timeout_ms
                )
                if _WAIT_OBJECT_0 <= result < _WAIT_OBJECT_0 + len(wait_handles):
                    exit_fn("parent_death_exit", "watched ancestor handle signaled")
                    return
                # WAIT_TIMEOUT with poll_pids present -> fall through to poll.
            if poll_pids:
                for pid in poll_pids:
                    if not _pid_is_alive(pid):
                        exit_fn("parent_death_exit", f"polled ancestor pid {pid} no longer alive")
                        return
                if not wait_handles:
                    time.sleep(_POLL_INTERVAL_SECONDS)

    thread = threading.Thread(target=_watch, daemon=True, name="vibe-ancestor-watch")
    thread.start()

    if degraded:
        _startup_timing.stamp("parent_watch_armed_degraded")
        sys.stderr.write(
            f"[vibe-lifecycle] parent_watch_armed (degraded): {', '.join(chain_breadcrumb)}\n"
        )
    else:
        _startup_timing.stamp("parent_watch_armed")
    return thread


def arm_ancestor_watch_posix(
    exit_fn: Callable[[str, str], None] = _exit_now,
) -> threading.Thread:
    """POSIX fallback: no NtQueryInformationProcess/WaitForMultipleObjects
    equivalent needed -- os.getppid() reparents to pid 1 (or the reaper)
    the instant the real parent dies, so a slow poll suffices. Deliberately
    simple per the brief ("do not over-engineer" the platform we don't ship
    to)."""
    original_ppid = os.getppid()

    def _watch() -> None:
        while True:
            time.sleep(_POLL_INTERVAL_SECONDS)
            if os.getppid() != original_ppid:
                exit_fn("parent_death_exit", f"getppid() changed from {original_ppid}")
                return

    thread = threading.Thread(target=_watch, daemon=True, name="vibe-ancestor-watch-posix")
    thread.start()
    _startup_timing.stamp("parent_watch_armed")
    return thread


def arm_stdin_watch(
    grace_seconds: float = PIPE_CLOSE_GRACE_SECONDS,
    exit_fn: Callable[[str, str], None] = _exit_now,
) -> threading.Thread | None:
    """§L-b: loop-independent stdin-pipe-closure watch. Returns None (and
    breadcrumbs why) when there's nothing useful to watch: non-Windows, or a
    console/dev run where stdin isn't a pipe at all."""
    if not _IS_WINDOWS:
        _startup_timing.stamp("stdin_watch_skipped_posix")
        return None

    stdin_handle = _kernel32.GetStdHandle(_STD_INPUT_HANDLE)
    file_type = _kernel32.GetFileType(stdin_handle) & _FILE_TYPE_MASK
    if file_type != _FILE_TYPE_PIPE:
        # Console/dev run: PeekNamedPipe would error immediately on a
        # FILE_TYPE_CHAR handle. Skip the watch; never exit on it.
        _startup_timing.stamp("stdin_watch_skipped_console")
        return None

    def _watch() -> None:
        bytes_avail = wintypes.DWORD(0)
        while True:
            ok = _kernel32.PeekNamedPipe(stdin_handle, None, 0, None, ctypes.byref(bytes_avail), None)
            if not ok:
                err = ctypes.get_last_error()
                if err == _ERROR_BROKEN_PIPE:
                    # Give the loop-riding graceful EOF path its head start;
                    # if it finishes on its own the process is already gone
                    # and this line never runs.
                    time.sleep(grace_seconds)
                    exit_fn("stdin_pipe_closed_exit", f"broken pipe, {grace_seconds}s grace elapsed")
                    return
                # Any other PeekNamedPipe error is not a confirmed closed
                # pipe -- keep polling rather than risk a false-positive exit.
            time.sleep(_STDIN_POLL_INTERVAL_SECONDS)

    thread = threading.Thread(target=_watch, daemon=True, name="vibe-stdin-watch")
    thread.start()
    _startup_timing.stamp("stdin_watch_armed")
    return thread
