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
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, cast

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
    _WAIT_FAILED = 0xFFFFFFFF

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

    # WP-Lifecycle-2 Stage 1 (report-only): pipe-end pid resolution. Both
    # calls return the pid of the process that CREATED the pipe, recorded at
    # creation time -- for the anonymous CreatePipe pipes Claude Code uses for
    # MCP stdio, both ends resolve to the creator (empirically validated in
    # the rev-3 scoping against anonymous pipes, a pass-through uv chain, and
    # a Node/libuv creator).
    _kernel32.GetNamedPipeServerProcessId.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL

    _kernel32.GetNamedPipeClientProcessId.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
else:
    # POSIX: every wintypes use below sits on a Windows-only path that never
    # runs here — this binding exists solely so the name is never unbound for
    # static analysis (Any: its attributes must not be type errors either).
    wintypes = cast(Any, None)


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


def _raw_get_std_input_handle():
    """Seam: the process stdin handle (tests monkeypatch to drive pipe/console
    scenarios without rebinding real std handles)."""
    return _kernel32.GetStdHandle(_STD_INPUT_HANDLE)


def _raw_get_file_type(handle) -> int:
    return _kernel32.GetFileType(handle)


def _raw_get_pipe_end_pids(handle) -> tuple[int | None, int | None]:
    """Seam: (server_end_pid, client_end_pid) of a pipe handle, each None on
    API failure. Both return the pipe CREATOR's pid for anonymous pipes."""
    server_pid = wintypes.DWORD(0)
    client_pid = wintypes.DWORD(0)
    server_ok = _kernel32.GetNamedPipeServerProcessId(handle, ctypes.byref(server_pid))
    client_ok = _kernel32.GetNamedPipeClientProcessId(handle, ctypes.byref(client_pid))
    return (
        int(server_pid.value) if server_ok else None,
        int(client_pid.value) if client_ok else None,
    )


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


# ── WP-Lifecycle-2 Stage 1: report-only identification ──────────────────────
# Everything below in this section only OBSERVES and LOGS. No watch is armed,
# no exit behavior changes, no process other than ourselves is ever touched
# (read-only PROCESS_QUERY_LIMITED_INFORMATION opens, closed before return).
# Stage 2 arms these resolutions only after field logs prove them correct
# (docs/wp-lifecycle2-plan.md rev 4, two-stage rollout ruling 2026-07-29).

SUPERVISOR_PID_ENV = "VIBE_SUPERVISOR_PID"


def _log_identity(kind: str, result: dict) -> None:
    parts = " ".join(f"{k}={v}" for k, v in result.items() if v is not None)
    try:
        sys.stderr.write(f"[vibe-lifecycle] {kind}: {parts}\n")
        sys.stderr.flush()
    except Exception:
        pass
    # Keep the evidence in memory for the existing startup-log flush, too.
    # Server stderr depends on harness retention; sidecar stderr is DEVNULL.
    # No disk I/O here: server identification runs before handshake yield.
    try:
        evidence = {
            "kind": kind,
            "pid": os.getpid(),
            "harness": os.environ.get("VIBE_HARNESS", "unspecified"),
            "executable": sys.executable,
            "observed_at_unix": time.time(),
            **result,
        }
        _startup_timing.stamp("lifecycle_identity " + json.dumps(evidence, sort_keys=True))
    except Exception:
        pass  # Diagnostics must never change startup or shutdown behavior.


def resolve_stdin_pipe_peer() -> dict:
    """Identify (report-only) the process on the far end of our stdin pipe --
    the REAL client (claude.exe in production: uv passes stdio handles
    through, it does not re-pipe). Returns a verdict dict and breadcrumbs it;
    never raises, never keeps a handle, never affects any watch.

    Verdicts: ok | skipped_posix | skipped_console | api_failed |
    peer_is_self | ends_disagree | peer_inside_ancestor_set | peer_gone |
    peer_access_denied | peer_younger_than_self | unresolved.
    A verdict other than `ok` on a real session blocks Stage 2 until
    explained -- that is the whole point of this release."""
    result: dict = {"verdict": "unresolved", "peer_pid": None, "peer_image": None}
    try:
        if not _IS_WINDOWS:
            result["verdict"] = "skipped_posix"
            _startup_timing.stamp("pipe_peer_skipped_posix")
            return result

        stdin_handle = _raw_get_std_input_handle()
        if _raw_get_file_type(stdin_handle) & _FILE_TYPE_MASK != _FILE_TYPE_PIPE:
            # Console/dev run -- same rule as arm_stdin_watch: nothing to
            # resolve, never treat as a finding.
            result["verdict"] = "skipped_console"
            _startup_timing.stamp("pipe_peer_skipped_console")
            _log_identity("pipe_peer_resolved", result)
            return result

        server_end, client_end = _raw_get_pipe_end_pids(stdin_handle)
        result["server_end_pid"] = server_end
        result["client_end_pid"] = client_end
        if server_end is None and client_end is None:
            result["verdict"] = "api_failed"
            result["last_error"] = _raw_last_error()
            _startup_timing.stamp("pipe_peer_resolved")
            _log_identity("pipe_peer_resolved", result)
            return result

        own_pid = os.getpid()
        candidates = {p for p in (server_end, client_end) if p and p != own_pid}
        if not candidates:
            result["verdict"] = "peer_is_self"
            _startup_timing.stamp("pipe_peer_resolved")
            _log_identity("pipe_peer_resolved", result)
            return result
        if len(candidates) > 1:
            # Anonymous pipes report the creator on both ends; disagreement
            # means a topology we have not seen -- report it, resolve nothing.
            result["verdict"] = "ends_disagree"
            _startup_timing.stamp("pipe_peer_resolved")
            _log_identity("pipe_peer_resolved", result)
            return result
        peer_pid = candidates.pop()
        result["peer_pid"] = peer_pid

        # Spike criterion (rev 3, pinned): a peer inside the depth-<=2
        # ancestor set means an intermediary re-piped stdio -- Stage 2 would
        # engage the fallback walk, Stage 1 reports it.
        own_parent = get_parent_pid_via_handle(_kernel32.GetCurrentProcess())
        grandparent = None
        if own_parent is not None:
            parent_probe = _open_ancestor(own_parent)
            if parent_probe.handle:
                grandparent = get_parent_pid_via_handle(parent_probe.handle)
                _kernel32.CloseHandle(parent_probe.handle)
        if peer_pid in {own_parent, grandparent}:
            result["verdict"] = "peer_inside_ancestor_set"
            _startup_timing.stamp("pipe_peer_resolved")
            _log_identity("pipe_peer_resolved", result)
            return result

        peer_probe = _open_ancestor(peer_pid)
        if peer_probe.pid_gone:
            result["verdict"] = "peer_gone"
        elif peer_probe.access_denied:
            result["verdict"] = "peer_access_denied"
        else:
            try:
                if is_younger_than_self(peer_probe.handle) is True:
                    # Creation-time guard: the pipe creator predates us by
                    # construction, so a younger pid is a reused pid.
                    result["verdict"] = "peer_younger_than_self"
                else:
                    result["peer_image"] = _query_image_name(peer_probe.handle)
                    result["verdict"] = "ok"
            finally:
                _kernel32.CloseHandle(peer_probe.handle)
        _startup_timing.stamp("pipe_peer_resolved")
        _log_identity("pipe_peer_resolved", result)
        return result
    except Exception as e:  # pragma: no cover - defensive: log-only code must never break startup
        result["verdict"] = "unresolved"
        result["error"] = repr(e)
        _log_identity("pipe_peer_resolved", result)
        return result


def log_supervisor_identity() -> dict:
    """Sidecar-side (report-only): resolve the supervisor pid the server
    handed us via VIBE_SUPERVISOR_PID and breadcrumb what we find. Stage 1
    arms nothing on it; the existing depth-1 parent watch is unchanged.

    Verdicts: ok | env_absent | env_invalid | skipped_posix |
    supervisor_gone | access_denied | younger_than_self."""
    result: dict = {"verdict": "unresolved", "supervisor_pid": None, "supervisor_image": None}
    try:
        raw = os.environ.get(SUPERVISOR_PID_ENV)
        if not raw:
            # Expected when spawned by a pre-Stage-1 server or run directly.
            result["verdict"] = "env_absent"
            _startup_timing.stamp("supervisor_pid_env_absent")
            return result
        try:
            pid = int(raw)
        except ValueError:
            result["verdict"] = "env_invalid"
            result["raw"] = raw
            _log_identity("supervisor_pid_resolved", result)
            return result
        result["supervisor_pid"] = pid
        if not _IS_WINDOWS:
            result["verdict"] = "skipped_posix"
            return result

        probe = _open_ancestor(pid)
        if probe.pid_gone:
            # Report-only in Stage 1; Stage 2 pins this to exit-now (a dead
            # supervisor at arm time is unambiguous orphaning, the mirror of
            # F1's pre-dead-peer rule).
            result["verdict"] = "supervisor_gone"
        elif probe.access_denied:
            result["verdict"] = "access_denied"
        else:
            try:
                if is_younger_than_self(probe.handle) is True:
                    result["verdict"] = "younger_than_self"
                else:
                    result["supervisor_image"] = _query_image_name(probe.handle)
                    result["verdict"] = "ok"
            finally:
                _kernel32.CloseHandle(probe.handle)
        _startup_timing.stamp("supervisor_pid_resolved")
        _log_identity("supervisor_pid_resolved", result)
        return result
    except Exception as e:  # pragma: no cover - defensive: log-only code must never break startup
        result["verdict"] = "unresolved"
        result["error"] = repr(e)
        _log_identity("supervisor_pid_resolved", result)
        return result


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

    # Each entry is a mutable [kind ("wait"|"poll"), handle_or_None, pid] list —
    # see _watch below, which flips kinds in place on WAIT_FAILED.
    watched_handles: list[list[Any]] = []
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
            watched_handles.append(["poll", None, own_parent_pid])
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
            watched_handles.append(["wait", handle, own_parent_pid])
            parent_image = _query_image_name(handle)
            if parent_image:
                chain_breadcrumb.append(f"parent_image={parent_image}")

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
                        watched_handles.append(["poll", None, grandparent_pid])
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
                            watched_handles.append(["wait", gp_handle, grandparent_pid])

    def _watch() -> None:
        # Mutable per-entry [kind, handle_or_None, pid] lists (not tuples) --
        # a WAIT_FAILED result flips affected entries from "wait" to "poll"
        # in place, degrading the watch instead of silently disabling it.
        entries = [list(e) for e in watched_handles]

        if not entries:
            return

        while True:
            wait_entries = [e for e in entries if e[0] == "wait"]
            poll_entries = [e for e in entries if e[0] == "poll"]

            if wait_entries:
                handle_array = (wintypes.HANDLE * len(wait_entries))(*(e[1] for e in wait_entries))
                timeout_ms = int(_POLL_INTERVAL_SECONDS * 1000) if poll_entries else 0xFFFFFFFF
                result = _kernel32.WaitForMultipleObjects(
                    len(wait_entries), handle_array, False, timeout_ms
                )
                if _WAIT_OBJECT_0 <= result < _WAIT_OBJECT_0 + len(wait_entries):
                    exit_fn("parent_death_exit", "watched ancestor handle signaled")
                    return
                if result == _WAIT_FAILED:
                    # A WAIT_FAILED result returns near-instantly -- retrying
                    # the same WaitForMultipleObjects call would busy-loop
                    # forever with zero effective monitoring, silently
                    # disabling the primary guarantee. We can't tell WHICH
                    # handle failed, so degrade all of them to polling by
                    # their pid instead.
                    sys.stderr.write(
                        "[vibe-lifecycle] WaitForMultipleObjects failed "
                        f"(GetLastError={ctypes.get_last_error()}); degrading "
                        f"{len(wait_entries)} handle(s) to polling\n"
                    )
                    sys.stderr.flush()
                    for e in wait_entries:
                        e[0] = "poll"
                    continue
                # WAIT_TIMEOUT with poll_entries present -> fall through to poll.
            if poll_entries:
                for e in poll_entries:
                    if not _pid_is_alive(e[2]):
                        exit_fn("parent_death_exit", f"polled ancestor pid {e[2]} no longer alive")
                        return
                if not wait_entries:
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
        # WP-Lifecycle-2 Stage 1 (G4 fold): the HEALTHY path now states what
        # it is watching, with image names, on every startup. One line of
        # `grandparent_image=uv.exe` (instead of claude.exe) would have
        # exposed the vacuous depth-2 watch on day one.
        sys.stderr.write(
            f"[vibe-lifecycle] parent_watch_armed: {', '.join(chain_breadcrumb)}\n"
        )
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
