"""WP-Lifecycle (P1, docs/wp-lifecycle-plan.md rev 3) §L-c/WPL-AC4: pins that
BOTH watch threads are armed before the bg import thread starts -- a wedge
cannot prevent the watchers' own existence, since their OS threads must
exist BEFORE any loader-lock wedge can block further thread creation (same
reasoning as WP-Wedge's pre-yield warm spawn and WP-Wedge-2's dispatch-
executor prewarm).

Platform-independent (pure ordering assertion via monkeypatched fakes, no
real ctypes/Win32 calls) -- runs on any OS, unlike test_lifecycle.py's
primitive-level Windows-only coverage and test_wp_lifecycle_integration.py's
Windows-only subprocess-real WPL-AC1/AC2/AC3.
"""

from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace

import pytest

from vibe_cognition import lifecycle
from vibe_cognition.server import _load_embeddings_and_sync, lifespan


@pytest.mark.asyncio
async def test_both_watches_armed_before_bg_thread_starts(tmp_path, monkeypatch):
    """WPL-AC4: extends the WP-Wedge-2 §W2-b ordering pattern
    (test_wp_wedge.py::test_prespawn_happens_before_bg_thread_starts) to also
    pin arm_ancestor_watch/arm_stdin_watch -- both must run strictly before
    bg_thread.start().

    Fails-before: without this, a refactor could reorder the two `lifecycle.
    arm_*` calls to after `bg_thread.start()` (e.g. accidentally moved past a
    later insertion point) and nothing would catch it -- the watches would
    then race a loader-lock wedge for their own thread creation, the exact
    failure mode WP-Wedge's warm-spawn precedent exists to avoid."""

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")

    order: list[str] = []

    async def _fake_prespawn(count):
        order.append("prespawn")

    async def _fake_dispatch_prewarm(count=None):
        order.append("dispatch_prewarm")

    def _fake_arm_ancestor(*args, **kwargs):
        order.append("ancestor_watch_armed")
        return None

    def _fake_arm_stdin(*args, **kwargs):
        order.append("stdin_watch_armed")
        return None

    monkeypatch.setattr("vibe_cognition.server._warm_worker_batch", _fake_prespawn)
    monkeypatch.setattr(
        "vibe_cognition.server.prewarm_dispatch_executor", _fake_dispatch_prewarm
    )
    monkeypatch.setattr("vibe_cognition.lifecycle.arm_ancestor_watch", _fake_arm_ancestor)
    monkeypatch.setattr("vibe_cognition.lifecycle.arm_stdin_watch", _fake_arm_stdin)

    real_start = threading.Thread.start

    def _tracking_start(self):
        if self._target is _load_embeddings_and_sync:
            order.append("bg_thread_start")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", _tracking_start)

    def _fast_generator(cfg):
        return SimpleNamespace()

    monkeypatch.setattr("vibe_cognition.server.EmbeddingGenerator.from_config", _fast_generator)

    async with lifespan(None) as context:  # type: ignore[arg-type]
        for _ in range(500):
            if context["embedding_ready"].is_set():
                break
            await asyncio.sleep(0.02)

    assert order == [
        "prespawn",
        "dispatch_prewarm",
        "ancestor_watch_armed",
        "stdin_watch_armed",
        "bg_thread_start",
    ], f"wrong order: {order}"


def test_arm_ancestor_watch_posix_detects_reparenting(monkeypatch):
    """§L-a POSIX fallback: os.getppid() changes the instant the real parent
    dies (reparented to the reaper) -- a slow poll is sufficient; this is
    deliberately NOT over-engineered relative to the Windows path.

    Moved here from test_lifecycle.py (gate finding, CHEAP): that file's
    module-level pytestmark skips the whole file on non-Windows, silently
    contradicting this test's own claim to run regardless -- since
    arm_ancestor_watch_posix is pure Python (no ctypes), it belongs in this
    platform-independent file instead, where it genuinely runs everywhere."""
    exits = []
    ppids = iter([100, 100, 999])  # unchanged, unchanged, then changed

    monkeypatch.setattr(os, "getppid", lambda: next(ppids))
    monkeypatch.setattr(lifecycle, "_POLL_INTERVAL_SECONDS", 0.02)

    thread = lifecycle.arm_ancestor_watch_posix(exit_fn=lambda r, d="": exits.append((r, d)))
    thread.join(timeout=2.0)
    assert len(exits) == 1
    assert exits[0][0] == "parent_death_exit"


@pytest.mark.asyncio
async def test_watch_arming_failure_degrades_startup_instead_of_crashing_it(tmp_path, monkeypatch):
    """Gate item 6 (defensive degrade-don't-abort): an unexpected exception
    arming either watch (e.g. thread creation itself failing) must not crash
    the entire server startup over an optional safety net -- lifespan() must
    still reach handshake_yield and start the bg thread.

    Fails-before: without the try/except in server.py, this raises out of
    lifespan() and the `async with lifespan(None)` block below never even
    enters -- the whole server fails to start over a non-essential watch."""
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated: can't start new thread")

    monkeypatch.setattr("vibe_cognition.lifecycle.arm_ancestor_watch", _boom)
    monkeypatch.setattr("vibe_cognition.lifecycle.arm_stdin_watch", _boom)

    def _fast_generator(cfg):
        return SimpleNamespace()

    monkeypatch.setattr("vibe_cognition.server.EmbeddingGenerator.from_config", _fast_generator)

    async with lifespan(None) as context:  # type: ignore[arg-type]
        for _ in range(500):
            if context["embedding_ready"].is_set():
                break
            await asyncio.sleep(0.02)
        assert context["embedding_ready"].is_set(), "startup must still complete despite the watch failures"
