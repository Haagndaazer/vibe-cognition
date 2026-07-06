"""WP-Lifecycle (P1, docs/wp-lifecycle-plan.md rev 3): standalone launcher for
WPL-AC1/AC2/AC3's subprocess-real integration tests.

NOT collected directly by pytest (no test_ prefix) -- same convention as
tests/wp2_mode_a_forensics.py. Run via a REAL `uv run ... python <this
path>` invocation (test_wp_lifecycle_integration.py drives this), matching
plugin.json's launch shape exactly: disposable ancestor -> uv -> this
script -- so the ancestor-watch is exercised against the real client->uv->
python topology (spawning python directly would make WPL-AC1/AC2
vacuous-by-topology, the rev-1 BLOCKER the brief calls out).

Env vars (all optional, all test-only -- read here in test-support code,
never in src/vibe_cognition/ itself):
  VIBE_LIFECYCLE_TEST_PIDFILE    -- write this process's own pid here before
                                    calling main(), so the test can identify
                                    the real leaf process regardless of the
                                    uv intermediary's own pid (uv's pid !=
                                    the python process pid on Windows, since
                                    there's no exec).
  VIBE_LIFECYCLE_TEST_WEDGE_BG   -- if set, monkeypatch EmbeddingGenerator.
                                    from_config to block forever on a never-
                                    set threading.Event before calling
                                    main() -- WPL-AC2's bg-thread-wedge
                                    scenario, same monkeypatch shape
                                    test_wp_wedge2.py uses in-process,
                                    replicated here for a real subprocess.
  VIBE_LIFECYCLE_TEST_BUSY_LOOP  -- if set, permanently freeze the event
                                    loop (a plain sync while-loop with no
                                    await -- not a cooperative sleep(0)
                                    spin) once both watch threads have
                                    confirmed armed -- WPL-AC3: proves the
                                    loop-riding stdin-EOF graceful path
                                    cannot be what passed the test.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

pidfile = os.environ.get("VIBE_LIFECYCLE_TEST_PIDFILE")
if pidfile:
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))

if os.environ.get("VIBE_LIFECYCLE_TEST_WEDGE_BG"):
    from types import SimpleNamespace

    from vibe_cognition import server as _server

    _release_never_set = threading.Event()

    def _hung_from_config(cfg):
        _release_never_set.wait()
        return SimpleNamespace()

    _server.EmbeddingGenerator.from_config = staticmethod(_hung_from_config)

_watches_armed = threading.Event()

if os.environ.get("VIBE_LIFECYCLE_TEST_BUSY_LOOP"):
    from vibe_cognition import lifecycle as _lifecycle

    _armed_count = {"n": 0}
    _armed_lock = threading.Lock()
    _real_arm_ancestor = _lifecycle.arm_ancestor_watch
    _real_arm_stdin = _lifecycle.arm_stdin_watch

    def _mark_armed():
        with _armed_lock:
            _armed_count["n"] += 1
            if _armed_count["n"] >= 2:
                _watches_armed.set()

    def _wrapped_arm_ancestor(*args, **kwargs):
        result = _real_arm_ancestor(*args, **kwargs)
        _mark_armed()
        return result

    def _wrapped_arm_stdin(*args, **kwargs):
        result = _real_arm_stdin(*args, **kwargs)
        _mark_armed()
        return result

    _lifecycle.arm_ancestor_watch = _wrapped_arm_ancestor
    _lifecycle.arm_stdin_watch = _wrapped_arm_stdin


from vibe_cognition.server import mcp  # noqa: E402


async def _amain() -> None:
    import anyio

    async with anyio.create_task_group() as tg:
        if os.environ.get("VIBE_LIFECYCLE_TEST_BUSY_LOOP"):

            async def _busy_spin() -> None:
                # Wait for both watches to confirm armed WITHOUT blocking the
                # loop (the wait itself runs on an executor thread) -- only
                # once armed does this task switch to a genuinely-freezing,
                # never-yielding sync loop. Freezing the loop before the
                # watches exist would make the test vacuous (nothing to
                # prove survives the freeze).
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _watches_armed.wait)
                sys.stderr.write("LAUNCHER_BUSY_LOOP_ENGAGED\n")
                sys.stderr.flush()
                while True:
                    time.sleep(0.05)

            tg.start_soon(_busy_spin)

        await mcp.run_async()


if __name__ == "__main__":
    asyncio.run(_amain())
