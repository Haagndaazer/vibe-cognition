"""Startup timing breadcrumbs (WP-A 1b, decision 9022f7de94e9).

Imported FIRST in server.py (before any other import) so importing THIS
module's own top-level ``stamp("server_module_import_start")`` call fires as
early as possible -- before the heavy transitive imports (chromadb,
sentence-transformers/torch) run. A later ``stamp("server_module_import_done")``
call brackets that cost, quantifying how much of first-connect latency is pure
module-import weight (Windows Defender scanning the torch DLL tree, etc.) vs.
the ChromaDB open or the handshake itself.

HEISENBUG GUARD: ``stamp()`` never touches disk -- monotonic time + an
immediate stderr print (already captured by Claude Code) only, so it is safe
to call from the synchronous pre-yield MCP handshake path (the suspect window
for the flake this hardening targets). Disk persistence is the separate,
explicit ``flush_to_disk()`` step, which server.py calls ONLY from the
background thread, never from that pre-yield path.
"""

import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path

PID = os.getpid()
breadcrumbs: list[tuple[str, float]] = []
_stamped_once: set[str] = set()


def stamp(label: str) -> float:
    """Record a startup breadcrumb: monotonic time + stderr print, no disk I/O."""
    t = time.monotonic()
    breadcrumbs.append((label, t))
    print(f"[vibe-cognition startup] pid={PID} {label} t={t:.3f}", file=sys.stderr, flush=True)
    return t


def first_occurrence(label: str) -> bool:
    """True on the first call for a given ``label`` this process, False on
    every call after. Shared first-occurrence primitive behind ``stamp_once``
    (WP-Wedge-2 §W2-e) and also used directly to gate a one-time action that
    ISN'T itself a breadcrumb stamp (§W2-f's dispatch-stall stack dump).

    Safe from ANY thread context: no disk I/O. A race between two threads on
    the same not-yet-seen label is benign (worst case: the gated action runs
    twice instead of once), so no lock guards the check.
    """
    if label in _stamped_once:
        return False
    _stamped_once.add(label)
    return True


def stamp_once(label: str) -> None:
    """``stamp()`` on only the FIRST call for a given ``label`` per process --
    for high-frequency call sites (e.g. every dispatched tool call) where a
    per-invocation stamp would be unbounded, but a first-occurrence-only signal
    is exactly what's needed (WP-Wedge-2 §W2-e's ``tool_served_degraded``).
    """
    if first_occurrence(label):
        stamp(label)


def stamp_and_flush(label: str) -> float:
    """``stamp()`` + ``flush_to_disk()`` in one call -- WP-Wedge-2 §W2-e: for
    BG-THREAD-CONTEXT call sites ONLY. A mid-wedge breadcrumb file that only
    flushes at a few checkpoints under-reports what actually happened
    (Incident B: the file ended at ``handshake_yield`` even though the stack
    proved ``import_probe_*``/``bg_model_load_start`` had already run) --
    flushing after every bg-thread stamp keeps the file a faithful forensic
    trail even when the thread wedges before reaching its next checkpoint.

    NEVER call this from the event loop or a tool-dispatch/worker thread --
    disk I/O there is a new freeze/latency source, the same class as WP-Wedge
    rev-1's PIPE defect. Loop-side and dispatch/worker-side stamps stay plain
    ``stamp()`` (stderr-only) and ride the next bg-thread flush.
    """
    t = stamp(label)
    flush_to_disk()
    return t


def flush_to_disk() -> None:
    """Persist accumulated breadcrumbs to a per-PID temp-dir log file.

    Call ONLY from a background thread or after the MCP handshake yield --
    never from the synchronous pre-yield path. Per-PID by construction, so N
    concurrent server processes each write their own file and never collide
    on a shared one. Best-effort: a failed diagnostic write must never break
    startup, so any OSError is swallowed.
    """
    try:
        log_dir = Path(tempfile.gettempdir()) / "vibe-cognition-startup"
        log_dir.mkdir(parents=True, exist_ok=True)
        lines = [f"{label} t={t:.3f}" for label, t in breadcrumbs]
        (log_dir / f"pid-{PID}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


_PRUNE_MAX_AGE_DAYS = 7
# WP-Lifecycle §L-d: bumped 50 -> 200. The orphan-churn incident this WP
# fixes produced startup counts far above what 50 assumed (a single session
# re-accumulated 4 wedged pairs within 50 minutes via pure hang->reconnect
# churn) -- each file is a few bytes of text, so a wider margin costs nothing
# and buys real forensic headroom against the next churn spiral, even though
# this WP's whole point is to make that spiral not recur.
_PRUNE_KEEP_RECENT = 200


def prune_old_logs(
    max_age_days: float = _PRUNE_MAX_AGE_DAYS,
    keep_recent: int = _PRUNE_KEEP_RECENT,
) -> None:
    """Bound the per-PID breadcrumb log directory so it never grows without
    bound (N concurrent agents x many sessions x every project, in a GLOBAL
    temp dir, with no cleanup otherwise).

    Call ONLY from the background thread (same constraint as flush_to_disk)
    -- never on the pre-yield path. Two rules, both keyed off each file's
    mtime (never filename/PID order, which has no relationship to recency):
    delete anything older than max_age_days, AND cap the total count at
    keep_recent (oldest-first eviction beyond the cap). A genuinely fresh
    file's mtime is always near "now", so it is never older than max_age_days
    and always ranks at the top of "most recent" -- both rules are safe for a
    concurrent server's just-written file BY CONSTRUCTION, not by a bolted-on
    exception.

    CONCURRENCY-SAFE: best-effort throughout. Two servers pruning the same
    directory at once may both target the same stale file -- deletes are
    idempotent, so a missing/already-gone file (FileNotFoundError) or a
    transient lock (PermissionError) is swallowed, never raised.
    """
    try:
        log_dir = Path(tempfile.gettempdir()) / "vibe-cognition-startup"
        if not log_dir.exists():
            return

        entries: list[tuple[float, Path]] = []
        for p in log_dir.glob("pid-*.log"):
            try:
                entries.append((p.stat().st_mtime, p))
            except OSError:
                continue  # gone already (another process's prune/cleanup) -- fine

        cutoff = time.time() - max_age_days * 86400
        entries.sort(key=lambda e: e[0], reverse=True)  # newest first
        to_delete = {p for mtime, p in entries if mtime < cutoff}  # age rule
        to_delete |= {p for _, p in entries[keep_recent:]}  # keep-N rule (oldest excess)

        for p in to_delete:
            # Idempotent / racing with another process's prune -- fine either way.
            with contextlib.suppress(FileNotFoundError, PermissionError, OSError):
                p.unlink()
    except OSError:
        pass


stamp("server_module_import_start")
