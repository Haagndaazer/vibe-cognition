"""The supervisor stops an idle sidecar and brings it back on the next
request (WP-Memory-Lifecycle Stage C). Real subprocesses via the test stub;
the only process ever ended is the supervisor's own child."""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_wp_sidecar import _child_pids, _make_settings, _make_stub_spawn
from vibe_cognition.embeddings.sidecar_client import (
    SidecarError,
    SidecarRestartingError,
    SidecarSupervisor,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the sidecar stub harness is Windows-only")

_IDLE = 0.6


def _context():
    return {"embedding_ready": threading.Event(), "embedding_error": None, "embedding_generator": None}


def _wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _alive(pid: int) -> bool:
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


class _Spawns:
    """Counts spawns and can hold a restart's load open."""

    def __init__(self, monkeypatch, load_delay=0.0, fail_from=None):
        self.count = 0
        self._fail_from = fail_from
        normal = _make_stub_spawn(load_delay=load_delay)
        wedged = _make_stub_spawn(mode="wedge_forever")
        outer = self

        def _spawn(supervisor):
            outer.count += 1
            if outer._fail_from is not None and outer.count >= outer._fail_from:
                return wedged(supervisor)
            return normal(supervisor)

        monkeypatch.setattr(SidecarSupervisor, "_spawn", _spawn)


def _supervisor(tmp_path, **overrides):
    settings = {"sidecar_idle_timeout": _IDLE, "sidecar_idle_restart_wait": 20.0}
    settings.update(overrides)
    context = _context()
    return SidecarSupervisor(_make_settings(tmp_path, **settings), context), context


def test_idle_sidecar_is_stopped_launcher_and_interpreter_and_status_reads_idle(tmp_path, monkeypatch):
    _Spawns(monkeypatch)
    supervisor, context = _supervisor(tmp_path)
    try:
        supervisor.ensure_ready()
        assert supervisor.status() == "ready"
        launcher_pid = supervisor._process._proc.pid
        # sys.executable is a venv launcher whose child is the real
        # interpreter; both must go, and only the launcher is ours to end.
        assert _wait_for(lambda: _child_pids(launcher_pid), timeout=5.0), "no interpreter under the launcher"
        tree = [launcher_pid, *_child_pids(launcher_pid)]
        assert len(tree) >= 2 and all(_alive(pid) for pid in tree)

        assert _wait_for(lambda: supervisor.status() == "idle"), supervisor.status()
        assert supervisor._process is None
        assert context["embedding_ready"].is_set() and context["embedding_error"] is None
        assert _wait_for(lambda: not any(_alive(pid) for pid in tree), timeout=10.0), [
            pid for pid in tree if _alive(pid)
        ]
    finally:
        supervisor.shutdown()


def test_next_request_restarts_the_sidecar_and_returns_vectors(tmp_path, monkeypatch):
    spawns = _Spawns(monkeypatch)
    supervisor, context = _supervisor(tmp_path)
    try:
        supervisor.ensure_ready()
        assert _wait_for(lambda: supervisor.status() == "idle")

        assert supervisor.generate(["hello"], is_query=False) == [[5.0, 0.0, 0.0]]
        assert supervisor.status() == "ready"
        assert spawns.count == 2
        assert context["embedding_error"] is None
        assert supervisor._degraded is False
    finally:
        supervisor.shutdown()


def test_use_keeps_the_sidecar_alive_past_the_idle_timeout(tmp_path, monkeypatch):
    spawns = _Spawns(monkeypatch)
    supervisor, _ = _supervisor(tmp_path)
    try:
        supervisor.ensure_ready()
        until = time.monotonic() + _IDLE * 4
        while time.monotonic() < until:
            supervisor.generate(["x"], is_query=True)
            time.sleep(_IDLE / 6)
        assert supervisor.status() == "ready"
        assert spawns.count == 1
    finally:
        supervisor.shutdown()


def test_a_request_in_flight_is_never_stopped_under(tmp_path, monkeypatch):
    """The idle check runs while a request is counted in flight: it must
    leave the process alone however old the last-used stamp is."""
    _Spawns(monkeypatch)
    supervisor, _ = _supervisor(tmp_path, sidecar_idle_timeout=3600.0)
    try:
        supervisor.ensure_ready()
        proc = supervisor._checkout()
        try:
            monkeypatch.setattr(supervisor._config, "sidecar_idle_timeout", 0.01)
            supervisor._last_used = time.monotonic() - 100
            supervisor._stop_if_idle()
            assert supervisor.status() == "ready"
            assert supervisor._generate_on(proc, ["ab"], False) == [[2.0, 0.0, 0.0]]
        finally:
            with supervisor._state_lock:
                supervisor._in_flight -= 1
        supervisor._stop_if_idle()
        assert supervisor.status() == "idle"
    finally:
        supervisor.shutdown()


def test_concurrent_requests_after_an_idle_stop_spawn_one_sidecar_and_all_succeed(tmp_path, monkeypatch):
    spawns = _Spawns(monkeypatch, load_delay=1.0)
    supervisor, _ = _supervisor(tmp_path)
    try:
        supervisor.ensure_ready()
        assert _wait_for(lambda: supervisor.status() == "idle")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: supervisor.generate(["a" * n], is_query=False), range(1, 9)))
        assert results == [[[float(n), 0.0, 0.0]] for n in range(1, 9)]
        assert spawns.count == 2
        assert supervisor._degraded is False
    finally:
        supervisor.shutdown()


def test_a_slow_restart_fails_the_waiter_cleanly_and_does_not_degrade(tmp_path, monkeypatch):
    """The wait is bounded by sidecar_idle_restart_wait, not the request
    timeout, and running out of it is not a sidecar failure."""
    _Spawns(monkeypatch, load_delay=3.0)
    supervisor, context = _supervisor(tmp_path, sidecar_idle_restart_wait=0.3)
    try:
        supervisor.ensure_ready()
        assert _wait_for(lambda: supervisor.status() == "idle", timeout=20.0)
        started = time.monotonic()
        with pytest.raises(SidecarRestartingError, match="restarting after an idle stop"):
            supervisor.generate(["hello"], is_query=False)
        assert time.monotonic() - started < 2.0
        assert supervisor._degraded is False and context["embedding_error"] is None
        monkeypatch.setattr(supervisor._config, "sidecar_idle_restart_wait", 20.0)
        monkeypatch.setattr(supervisor._config, "sidecar_idle_timeout", 3600.0)
        assert supervisor.generate(["hello"], is_query=False) == [[5.0, 0.0, 0.0]]
    finally:
        supervisor.shutdown()


def test_a_failed_restart_degrades_and_tells_the_waiter_why(tmp_path, monkeypatch):
    _Spawns(monkeypatch, fail_from=2)
    supervisor, context = _supervisor(tmp_path, sidecar_load_timeout=1.0)
    try:
        supervisor.ensure_ready()
        assert _wait_for(lambda: supervisor.status() == "idle")
        with pytest.raises(SidecarError, match="failed to restart after an idle stop"):
            supervisor.generate(["hello"], is_query=False)
        assert supervisor._degraded is True
        assert "idle stop" in context["embedding_error"]
        assert supervisor.status().startswith("error:")
    finally:
        supervisor.shutdown()


def test_timeout_zero_disables_the_idle_stop(tmp_path, monkeypatch):
    spawns = _Spawns(monkeypatch)
    supervisor, _ = _supervisor(tmp_path, sidecar_idle_timeout=0.0, sidecar_periodic_retry_interval=0.1)
    try:
        supervisor.ensure_ready()
        time.sleep(1.0)
        assert supervisor.status() == "ready"
        assert supervisor._tick_seconds() == 0.1
        assert supervisor.generate(["hello"], is_query=False) == [[5.0, 0.0, 0.0]]
        assert spawns.count == 1
    finally:
        supervisor.shutdown()


def test_degraded_retries_keep_their_own_interval_despite_the_shorter_tick(tmp_path, monkeypatch):
    """The loop now wakes every idle tick; a degraded supervisor must still
    retry only once per sidecar_periodic_retry_interval unless poked."""
    attempts = []
    supervisor, _ = _supervisor(tmp_path, sidecar_idle_timeout=0.2, sidecar_periodic_retry_interval=30.0)
    monkeypatch.setattr(supervisor, "_attempt_load", lambda: attempts.append(time.monotonic()) or False)
    try:
        supervisor.ensure_ready()
        assert supervisor._degraded is True
        baseline = len(attempts)
        time.sleep(1.0)
        assert len(attempts) == baseline
        supervisor.notify_demand()
        assert _wait_for(lambda: len(attempts) == baseline + 1, timeout=5.0)
    finally:
        supervisor.shutdown()


def test_a_failed_restart_waits_a_full_interval_before_the_next_retry(tmp_path, monkeypatch):
    spawns = _Spawns(monkeypatch, fail_from=2)
    supervisor, _ = _supervisor(tmp_path, sidecar_load_timeout=1.0, sidecar_periodic_retry_interval=30.0)
    try:
        supervisor.ensure_ready()
        assert _wait_for(lambda: supervisor.status() == "idle")
        with pytest.raises(SidecarError):
            supervisor.generate(["hello"], is_query=False)
        after_failure = spawns.count
        time.sleep(1.5)
        assert spawns.count == after_failure
    finally:
        supervisor.shutdown()


def test_shutdown_releases_a_caller_waiting_on_a_restart(tmp_path, monkeypatch):
    _Spawns(monkeypatch, load_delay=5.0)
    supervisor, _ = _supervisor(tmp_path, sidecar_idle_restart_wait=60.0)
    supervisor.ensure_ready()
    assert _wait_for(lambda: supervisor.status() == "idle", timeout=20.0)
    outcome: list[BaseException | None] = []

    def _call():
        try:
            supervisor.generate(["hello"], is_query=False)
            outcome.append(None)
        except SidecarError as e:
            outcome.append(e)

    caller = threading.Thread(target=_call, daemon=True)
    caller.start()
    time.sleep(0.5)
    supervisor.shutdown()
    caller.join(timeout=5.0)
    assert not caller.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], SidecarError)
    # The restart that was under way must not leave its process behind.
    assert _wait_for(lambda: supervisor._process is None and not supervisor._recovery_thread.is_alive(), timeout=20.0)


def test_a_tick_that_raises_while_callers_wait_degrades_and_releases_them(tmp_path, monkeypatch):
    """The recovery thread is what brings an idle sidecar back; it must
    survive a bad tick and tell the waiters."""
    _Spawns(monkeypatch)
    supervisor, context = _supervisor(tmp_path)
    try:
        supervisor.ensure_ready()
        assert _wait_for(lambda: supervisor.status() == "idle")

        def _boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(supervisor, "_restart_from_idle", _boom)
        started = time.monotonic()
        with pytest.raises(SidecarError, match="supervisor error after an idle stop"):
            supervisor.generate(["hello"], is_query=False)
        assert time.monotonic() - started < 5.0
        assert supervisor._degraded is True and "boom" in context["embedding_error"]
        assert supervisor._recovery_thread.is_alive()
    finally:
        supervisor.shutdown()


def test_a_restart_wait_timeout_blames_no_node_in_the_sync(monkeypatch):
    from vibe_cognition import server

    monkeypatch.setattr(server, "_model_stoppers", {})
    monkeypatch.setattr(server, "_model_suspects", set())
    failures = server._EmbedFailures(generator=None)  # type: ignore[arg-type]
    failures.answered = True
    with pytest.raises(server._EmbedderUnavailableError):
        failures.failed("node-1", SidecarRestartingError("still restarting"))
    assert server._model_stoppers == {} and server._model_suspects == set()
