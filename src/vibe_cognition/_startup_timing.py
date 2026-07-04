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

import os
import sys
import tempfile
import time
from pathlib import Path

PID = os.getpid()
breadcrumbs: list[tuple[str, float]] = []


def stamp(label: str) -> float:
    """Record a startup breadcrumb: monotonic time + stderr print, no disk I/O."""
    t = time.monotonic()
    breadcrumbs.append((label, t))
    print(f"[vibe-cognition startup] pid={PID} {label} t={t:.3f}", file=sys.stderr, flush=True)
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


stamp("server_module_import_start")
