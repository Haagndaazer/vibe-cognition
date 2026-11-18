"""WP-Lifecycle-2 Stage 1 (F4): machine-wide stale-sibling sweep — LOG-ONLY.

Counts live `vibe_cognition.server` processes machine-wide, deduped to REAL
interpreters only (image path under the uv-managed base-interpreter dir,
e.g. ``AppData\\Roaming\\uv\\python\\cpython-...\\python.exe``) — each server
tree otherwise triple-counts via uv + trampoline + real. This is the only
detection for the mode no death watch can catch: client alive but session
logically dead (/reload-plugins leaving the old pair running, a lingering
/exit'd claude.exe). It also doubles as leak-recurrence telemetry for the
Stage-2 go/no-go.

NEVER kills, signals, or otherwise acts on anything — enumeration + read-only
``PROCESS_QUERY_LIMITED_INFORMATION`` opens only, every handle closed before
return. Auto-kill needs an explicit ruling from Colton and is out of scope
(rev-3 pin, unchanged).

Command lines are read via ``NtQueryInformationProcess(
ProcessCommandLineInformation)`` (class 60, Win8.1+, needs only
QUERY_LIMITED rights) — pinned by the rev-4 review: NO cross-process PEB
walking (out of ctypes budget per rev 3) and no shelled-out WMI. A pid whose
command line can't be read degrades to image-path-only counting: it is
reported separately as *unverified*, over-count accepted and labeled, never
silently folded into the server count.

Execution site (pinned, rev 3): the server's existing background init thread
(``_load_embeddings_and_sync``) — never pre-yield (enumeration costs
100ms-2s: handshake-latency regression), never a new post-yield thread
(WP-Wedge INV-1: no Thread.start after the load window opens).
"""

from __future__ import annotations

import ctypes
import os
import sys
import time

from . import lifecycle

_IS_WINDOWS = sys.platform == "win32"

SERVER_CMDLINE_MARKER = "vibe_cognition.server"

# The uv-managed base-interpreter dir's stable path segment. The trampoline
# lives under `...\.venv\Scripts\python.exe` and uv.exe elsewhere entirely, so
# this segment uniquely selects the REAL interpreter of each tree. Hardcoded
# backslashes, NOT os.sep: the rows come from the Windows-only Toolhelp
# enumerator, so the paths are Windows paths regardless of the host analyzing
# them — an os.sep marker silently broke classify_rows' pure-function
# semantics on POSIX (caught by the ubuntu CI legs the first time they got
# past the month-red pyright gate).
_REAL_INTERPRETER_MARKER = "\\uv\\python\\"

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


def _enumerate_python_pids() -> list[int]:
    """Toolhelp snapshot: pids whose exe basename is python.exe/pythonw.exe."""
    pids: list[int] = []
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE or not snapshot:
        return pids
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in ("python.exe", "pythonw.exe"):
                pids.append(int(entry.th32ProcessID))
            ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        lifecycle._kernel32.CloseHandle(snapshot)
    return pids


def _read_command_line(handle) -> str | None:
    """Command line via NtQueryInformationProcess(ProcessCommandLineInformation).
    None on any failure (old Windows, gone process, odd rights) — callers
    degrade to image-only counting, never guess."""
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


def _probe_pid(pid: int) -> dict | None:
    """Read-only probe: image path, command line, creation time. None when
    the pid can't even be opened (gone, or a protected process)."""
    handle = lifecycle._raw_open_process(lifecycle._PROCESS_QUERY_LIMITED_INFORMATION, pid)
    if not handle:
        return None
    try:
        return {
            "pid": pid,
            "image_path": lifecycle._query_image_name(handle),
            "cmdline": _read_command_line(handle),
            "created_filetime": lifecycle._get_process_creation_time(handle),
        }
    finally:
        lifecycle._kernel32.CloseHandle(handle)


def classify_rows(rows: list[dict], now: float | None = None) -> dict:
    """Pure classification (unit-testable without processes). Rows are
    _probe_pid dicts. Only real interpreters (image path under the uv base
    dir) are considered; of those, a row counts as a SERVER only when its
    command line positively carries the marker. Unreadable command lines are
    counted separately as unverified — over-count accepted and labeled."""
    now = time.time() if now is None else now
    verified: list[dict] = []
    unverified: list[dict] = []
    for row in rows:
        image = (row.get("image_path") or "").lower()
        if _REAL_INTERPRETER_MARKER.lower() not in image:
            continue
        cmdline = row.get("cmdline")
        if cmdline is None:
            unverified.append(row)
        elif SERVER_CMDLINE_MARKER in cmdline:
            verified.append(row)

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
    """The Stage-1 sweep. Returns a labeled result dict; never raises and
    never touches any process (read-only opens, all closed)."""
    if not _IS_WINDOWS:
        return {"skipped": "posix"}
    try:
        rows = [row for pid in _enumerate_python_pids() if (row := _probe_pid(pid))]
        result = classify_rows(rows)
        result["own_pid"] = os.getpid()
        result["scope"] = "machine-wide (all sessions/projects, includes this one); log-only, nothing is acted on"
        return result
    except Exception as e:  # pragma: no cover - defensive: telemetry must never break the bg thread
        return {"error": str(e)}
