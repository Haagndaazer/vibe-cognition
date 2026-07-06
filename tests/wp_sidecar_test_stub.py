"""WP-Sidecar (P0 endgame): standalone stand-in sidecar entry point for
WPS-AC2..AC6's subprocess-real integration tests.

NOT collected directly by pytest (no test_ prefix) -- test-support only,
same convention as this repo's other subprocess-driven test harnesses. Uses
the REAL protocol/mutex/ancestor-watch machinery (_sidecar_protocol.py,
_load_mutex.py, lifecycle.arm_ancestor_watch) so the mechanisms under test
(mutex serialization/abandonment, parent-death, pipe discipline) are
exercised for real -- only the "model" itself is fake (instant, no
download), controlled via env vars, so tests are fast and deterministic
instead of depending on a real multi-second sentence-transformers load.

Env vars (all optional):
  WP_SIDECAR_STUB_MODE  -- "normal" (default) | "wedge_forever" | "chatty" |
                           "stubborn_reader"
  WP_SIDECAR_STUB_LOAD_DELAY -- seconds to sleep before "loading" completes
                           (simulates queueing/contention timing; default 0)
"""

from __future__ import annotations

import os
import sys
import threading
import time

from vibe_cognition import lifecycle
from vibe_cognition.embeddings import _load_mutex, _sidecar_protocol

_MODE = os.environ.get("WP_SIDECAR_STUB_MODE", "normal")
_LOAD_DELAY = float(os.environ.get("WP_SIDECAR_STUB_LOAD_DELAY", "0"))


def _write(obj: dict) -> None:
    sys.stdout.write(_sidecar_protocol.encode_line(obj))
    sys.stdout.flush()


def _respond(request_id, result=None, error=None) -> None:
    _write(_sidecar_protocol.make_response(request_id, result=result, error=error))


def _emit(event_name: str) -> None:
    _write(_sidecar_protocol.make_event(event_name))


def _do_load(args: dict) -> None:
    mutex_wait_timeout = float(args.get("mutex_wait_timeout", 300.0))
    handle = _load_mutex.create_mutex()
    _emit("lock_wait")
    outcome = _load_mutex.acquire(handle, timeout_seconds=mutex_wait_timeout)
    if outcome == _load_mutex.AcquireOutcome.ACQUIRED:
        _emit("lock_acquired")
    elif outcome == _load_mutex.AcquireOutcome.ACQUIRED_ABANDONED:
        _emit("lock_acquired_abandoned")
    else:
        _emit("lock_wait_expired")
        return  # never acquired -- nothing to release

    try:
        if _MODE == "wedge_forever":
            threading.Event().wait()  # never returns -- simulates a wedged load
        time.sleep(_LOAD_DELAY)
    finally:
        if outcome != _load_mutex.AcquireOutcome.TIMEOUT:
            _load_mutex.release(handle)
        _load_mutex.close(handle)


def _chatty_flood_loop() -> None:
    """AC5(i): flood stdout with a continuous stream of extra (non-protocol,
    silently-skipped-by-the-reader) lines, proving the dedicated reader
    thread draining fast enough prevents an OS-pipe-buffer write from ever
    blocking this process."""
    junk_line = "x" * 4096
    while True:
        sys.stdout.write(junk_line + "\n")
        sys.stdout.flush()
        time.sleep(0.001)


def main() -> None:
    lifecycle.arm_ancestor_watch(depth=1)

    if _MODE == "chatty":
        threading.Thread(target=_chatty_flood_loop, daemon=True).start()

    loaded = False
    stop_reading_after_load = _MODE == "stubborn_reader"

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = _sidecar_protocol.decode_line(line)
        except Exception:
            continue
        request_id = req.get("id")
        op = req.get("op")
        args = req.get("args") or {}

        if op == "load":
            _do_load(args)
            loaded = True
            _respond(request_id, result="ok")
            if stop_reading_after_load:
                # AC5(ii): stop reading stdin entirely -- the client's next
                # (oversized) generate payload will fill the OS pipe buffer
                # and block the WRITER thread on the client side, which only
                # the supervisor's kill (closing this process's handles) can
                # unblock. Sleeping forever without touching stdin again is
                # the simplest faithful way to simulate "stopped draining".
                threading.Event().wait()
        elif op == "generate":
            if not loaded:
                _respond(request_id, error="not_loaded")
                continue
            texts = args.get("texts", [])
            _respond(request_id, result=[[float(len(t)), 0.0, 0.0] for t in texts])
        elif op == "ping":
            _respond(request_id, result={"loaded": loaded})
        else:
            _respond(request_id, error=f"unknown_op: {op!r}")


if __name__ == "__main__":
    main()
