"""WP-Lifecycle-2 (F4): machine-wide stale-sibling sweep — LOG-ONLY.

Counts live `vibe_cognition.server` processes machine-wide, one per server
tree. A tree shows the server command line up to three times (uv, the venv
launcher, the real interpreter), so only python images are enumerated (uv
is never a row) and only the LEAF is counted: a row with the server marker
and no child row that also carries it. Where the interpreter lives does not
matter (uv-managed, Microsoft Store, python.org). This is the only
detection for the mode no death watch can catch: client alive but session
logically dead (/reload-plugins leaving the old pair running, a lingering
/exit'd claude.exe).

NEVER kills, signals, or otherwise acts on anything — enumeration + read-only
``PROCESS_QUERY_LIMITED_INFORMATION`` opens only, every handle closed before
return. Auto-kill needs an explicit ruling from Colton and is out of scope
(rev-3 pin, unchanged).

Command lines are read via ``NtQueryInformationProcess(
ProcessCommandLineInformation)`` (class 60, Win8.1+, needs only
QUERY_LIMITED rights) — pinned by the rev-4 review: NO cross-process PEB
walking (out of ctypes budget per rev 3) and no shelled-out WMI. A pid whose
command line can't be read is reported separately as *unverified*,
over-count accepted and labeled, never silently folded into the server
count.

Execution site (pinned, rev 3): the server's existing background init thread
(``_load_embeddings_and_sync``) — never pre-yield (enumeration costs
100ms-2s: handshake-latency regression), never a new post-yield thread
(WP-Wedge INV-1: no Thread.start after the load window opens).
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import time

from . import lifecycle

_IS_WINDOWS = sys.platform == "win32"

SERVER_CMDLINE_MARKER = "vibe_cognition.server"
# The module launch form only: a script or `-c` string that merely mentions
# the name is not a server.
_SERVER_CMDLINE_RE = re.compile(r"-m\s+" + re.escape(SERVER_CMDLINE_MARKER) + r"(?![\w.])")

# FILETIME (100ns ticks since 1601-01-01) to Unix-epoch conversion.
_FILETIME_EPOCH_DIFF_SECONDS = 11_644_473_600

if _IS_WINDOWS:
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    _STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
    _PROCESS_COMMAND_LINE_INFORMATION = 60

    class _ProcessEntry32W(ctypes.Structure):  # Win32 PROCESSENTRY32W
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class _UnicodeString(ctypes.Structure):  # Win32 UNICODE_STRING
        _fields_ = [
            ("Length", ctypes.c_ushort),
            ("MaximumLength", ctypes.c_ushort),
            ("Buffer", ctypes.c_void_p),
        ]

    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL


def is_python_image(exe_name: str) -> bool:
    """python.exe, pythonw.exe, python3.13.exe (the Store interpreter's name)."""
    name = exe_name.lower()
    return name.startswith("python") and name.endswith(".exe")


def _enumerate_python_procs() -> list[tuple[int, int]]:
    """Toolhelp snapshot: (pid, parent pid) of every python image."""
    procs: list[tuple[int, int]] = []
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE or not snapshot:
        return procs
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if is_python_image(entry.szExeFile):
                procs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        lifecycle._kernel32.CloseHandle(snapshot)
    return procs


def _read_command_line(handle) -> str | None:
    """Command line via NtQueryInformationProcess(ProcessCommandLineInformation).
    None on any failure (old Windows, gone process, odd rights) — callers
    report the row as unverified, never guess."""
    needed = ctypes.c_ulong(0)
    status = lifecycle._NtQueryInformationProcess(
        handle, _PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(needed)
    )
    if (status & 0xFFFFFFFF) != _STATUS_INFO_LENGTH_MISMATCH or needed.value == 0:
        return None
    buf = ctypes.create_string_buffer(needed.value)
    status = lifecycle._NtQueryInformationProcess(
        handle, _PROCESS_COMMAND_LINE_INFORMATION, buf, needed.value, ctypes.byref(needed)
    )
    if status != 0:
        return None
    us = ctypes.cast(buf, ctypes.POINTER(_UnicodeString)).contents
    if not us.Buffer or us.Length == 0:
        return None
    return ctypes.wstring_at(us.Buffer, us.Length // 2)


def _probe_pid(pid: int, parent_pid: int | None = None) -> dict | None:
    """Read-only probe: image path, command line, creation time. None when
    the pid can't even be opened (gone, or a protected process)."""
    handle = lifecycle._raw_open_process(lifecycle._PROCESS_QUERY_LIMITED_INFORMATION, pid)
    if not handle:
        return None
    try:
        return {
            "pid": pid,
            "parent_pid": parent_pid,
            "image_path": lifecycle._query_image_name(handle),
            "cmdline": _read_command_line(handle),
            "created_filetime": lifecycle._get_process_creation_time(handle),
        }
    finally:
        lifecycle._kernel32.CloseHandle(handle)


def classify_rows(rows: list[dict], now: float | None = None) -> dict:
    """Pure classification (unit-testable without processes). Rows are
    _probe_pid dicts of python images. A row is a SERVER when its command
    line carries the marker and no child row carries it too (the leaf of
    launcher -> interpreter). A child counts only if it was not created
    before its parent, which rules out a reused parent pid. Rows with an
    unreadable command line are counted separately as unverified — a possible
    over-count, labeled, never folded into the server count."""
    now = time.time() if now is None else now
    marked = [r for r in rows if _SERVER_CMDLINE_RE.search(r.get("cmdline") or "")]
    unverified = [r for r in rows if r.get("cmdline") is None]

    def _is_child_of(child: dict, parent: dict) -> bool:
        if child.get("parent_pid") != parent["pid"]:
            return False
        child_created = child.get("created_filetime")
        parent_created = parent.get("created_filetime")
        return not (child_created and parent_created and child_created < parent_created)

    verified = [row for row in marked if not any(_is_child_of(other, row) for other in marked)]

    def _oldest_age(pool: list[dict]) -> float | None:
        ages = [
            now - (ft / 1e7 - _FILETIME_EPOCH_DIFF_SECONDS)
            for ft in (r.get("created_filetime") for r in pool)
            if ft
        ]
        return round(max(ages), 1) if ages else None

    return {
        "server_count": len(verified),
        "server_pids": sorted(r["pid"] for r in verified),
        "unverified_interpreter_count": len(unverified),
        "oldest_server_age_seconds": _oldest_age(verified),
        "method": "cmdline" if not unverified else "cmdline+image-only-degraded",
    }


def run_stale_sweep() -> dict:
    """The sweep. Returns a labeled result dict; never raises and never
    touches any process (read-only opens, all closed)."""
    if not _IS_WINDOWS:
        return {"skipped": "posix"}
    try:
        rows = [
            row
            for pid, parent_pid in _enumerate_python_procs()
            if (row := _probe_pid(pid, parent_pid))
        ]
        result = classify_rows(rows)
        result["own_pid"] = os.getpid()
        result["scope"] = "machine-wide (all sessions/projects, includes this one); log-only, nothing is acted on"
        return result
    except Exception as e:  # pragma: no cover - defensive: telemetry must never break the bg thread
        return {"error": str(e)}
