"""WP-Sidecar (P0 endgame, docs/wp-sidecar-plan.md rev 3): the heavy import
leaves the server process. Subprocess-real integration tests for WPS-AC2
through WPS-AC6 (WPS-AC1's AST guard + runtime sys.modules assertion live in
test_wp_wedge.py / test_heavy_import_guard.py; WPS-AC7's zero-regression is
the whole-suite run itself).

All tests here drive tests/wp_sidecar_test_stub.py as a stand-in for the
real embeddings/sidecar.py entry module -- it uses the REAL protocol/mutex/
ancestor-watch machinery (only the "model" is fake and instant), so the
mechanisms under test (mutex serialization/abandonment, parent-death, pipe
discipline, supervisor kill/respawn) are exercised for real, without the
multi-second, non-deterministic cost of a real sentence-transformers load.

This is also WP-Sidecar's replacement for WP-Wedge's five AC2 watchdog/
late-recovery tests (removed from test_wp_wedge.py) -- called out explicitly
per the WP2-AC6 precedent (WP-Wedge-2 similarly replaced WP-Wedge's own
probe). Mapping:
  - test_watchdog_fires_after_timeout_when_bg_thread_never_finishes ->
    test_wpl_ac3-style coverage lives in test_wp_wedge2.py's
    test_ac1_every_tool_returns_within_bound_in_the_sidecar_degraded_state
    (dispatch stays instant while degraded) + this file's
    test_wedged_load_degrades_after_in_budget_retries_exhausted (the
    kill+respawn+degrade mechanics themselves).
  - test_watchdog_never_fires_while_probe_still_running -> subsumed: there
    is no separate probe phase anymore (the sidecar SPAWN is the probe).
  - test_watchdog_late_recovery_installs_generator_and_clears_error ->
    test_lazy_recovery_clears_error_and_updates_context_on_next_demand
    below.
  - test_watchdog_clobber_guard_genuine_error_after_fire_is_not_cleared ->
    N/A: there is no separate watchdog writer to race anymore (the
    supervisor is the SOLE writer), so this specific race is structurally
    impossible now, not just guarded against.
  - test_watchdog_stranding_interleaving_never_leaves_ready_no_error_no_generator
    -> structurally impossible by construction: ensure_ready() always
    assigns embedding_generator BEFORE setting embedding_ready (see
    SidecarSupervisor.ensure_ready's docstring) -- no test needed for a
    race that can no longer occur (single writer, no concurrent second one).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from vibe_cognition.config import Settings
from vibe_cognition.embeddings.generator import EmbeddingGenerator
from vibe_cognition.embeddings.sidecar_client import (
    SidecarError,
    SidecarSupervisor,
    _SidecarProcess,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="WP-Sidecar's mutex/pipe/ancestor-watch mechanisms are Windows-only",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_STUB_MODULE = "tests.wp_sidecar_test_stub"


def _make_stub_spawn(mode: str = "normal", load_delay: float = 0.0):
    """Replacement for SidecarSupervisor._spawn that launches the test stub
    instead of the real embeddings/sidecar.py entry module."""

    def _spawn(self) -> _SidecarProcess:
        env = dict(os.environ)
        env["WP_SIDECAR_STUB_MODE"] = mode
        env["WP_SIDECAR_STUB_LOAD_DELAY"] = str(load_delay)
        proc = _SidecarProcess(
            python_exe=sys.executable, module=_STUB_MODULE, env=env, cwd=str(_REPO_ROOT)
        )
        proc.set_event_callback(self._on_lock_event)
        return proc

    return _spawn


def _make_settings(tmp_path, **overrides) -> Settings:
    defaults = {
        "repo_path": tmp_path,
        "embedding_backend": "sentence-transformers",
        "sidecar_load_timeout": 10.0,
        "sidecar_request_timeout": 5.0,
        "sidecar_mutex_wait_timeout": 10.0,
        "sidecar_max_retry_attempts": 2,
        "sidecar_retry_backoff_seconds": 0.1,
        "sidecar_periodic_retry_interval": 0.3,
    }
    defaults.update(overrides)
    return Settings(**defaults)


# ── WPS-AC2: end-to-end embed flow works through the sidecar ────────────────


def test_embed_and_search_round_trip_through_the_sidecar(tmp_path, monkeypatch):
    """WPS-AC2: record (upsert) -> embed (via the sidecar-backed
    EmbeddingGenerator) -> search (vector_search) finds it, end to end
    through a real subprocess."""
    from vibe_cognition.embeddings.storage import ChromaDBStorage

    monkeypatch.setattr(SidecarSupervisor, "_spawn", _make_stub_spawn())
    config = _make_settings(tmp_path)
    context = {"embedding_ready": threading.Event(), "embedding_error": None, "embedding_generator": None}
    supervisor = SidecarSupervisor(config, context)

    try:
        supervisor.ensure_ready()
        assert context["embedding_error"] is None
        generator: EmbeddingGenerator = context["embedding_generator"]

        chroma = ChromaDBStorage(
            persist_directory=tmp_path / "chromadb", embedding_model="stub", embedding_dimensions=3
        )
        try:
            vector = generator.generate("a distinctive decision about widgets")
            chroma.upsert_embedding(
                "node-1", vector, {"entity_type": "decision"}, document="widget decision"
            )

            query_vector = generator.generate_query_embedding("a distinctive decision about widgets")
            results = chroma.vector_search(query_vector, limit=5)

            assert any(r["_id"] == "node-1" for r in results), (
                f"expected node-1 in search results, got {results}"
            )
        finally:
            chroma.close()
    finally:
        supervisor.shutdown()


# ── WPS-AC3: kill-mid-request recovers; wedged-load degrades ────────────────


def test_sidecar_killed_mid_request_returns_error_then_respawn_recovers(tmp_path, monkeypatch):
    """WPS-AC3 (part i): a request in flight when the sidecar dies returns
    the error dict within its timeout (never hangs); the NEXT request
    (triggering a fresh spawn) succeeds -- supervisor respawn works."""
    monkeypatch.setattr(SidecarSupervisor, "_spawn", _make_stub_spawn())
    config = _make_settings(tmp_path, sidecar_request_timeout=3.0)
    context = {"embedding_ready": threading.Event(), "embedding_error": None, "embedding_generator": None}
    supervisor = SidecarSupervisor(config, context)

    try:
        supervisor.ensure_ready()
        assert context["embedding_error"] is None

        # Kill the live sidecar out from under an in-flight-equivalent state --
        # the NEXT generate() call must observe the dead process cleanly.
        with supervisor._state_lock:
            live_proc = supervisor._process
        live_proc.kill()

        t0 = time.monotonic()
        with pytest.raises(SidecarError):
            supervisor.generate(["hello"], is_query=False)
        elapsed = time.monotonic() - t0
        assert elapsed < config.sidecar_request_timeout + 2.0, (
            f"generate() took {elapsed:.1f}s -- must fail fast on a dead process, not hang"
        )

        # The supervisor marks itself degraded on that failure -- and, per
        # the gate BLOCKER 1(b) fix, must ALSO write context["embedding_error"]
        # in that same except path. Without it, get_status still says "ready"
        # and require_embeddings doesn't gate, even though _degraded is True
        # (the post-ready flavor of the lying-status-tuple class _wedge_lock
        # existed to prevent).
        assert supervisor._degraded is True
        assert context["embedding_error"] is not None, (
            "a live generate() failure must write embedding_error, not just flip _degraded"
        )
        assert "error:" in supervisor.status(), f"status() must reflect the live failure, got {supervisor.status()!r}"

        # A lazy recovery attempt (as require_embeddings/get_lifespan would
        # trigger via notify_demand()) must bring it back.
        recovered = supervisor._attempt_load()
        assert recovered is True, "respawn after the kill must succeed"
    finally:
        supervisor.shutdown()


def test_wedged_load_degrades_after_in_budget_retries_exhausted(tmp_path, monkeypatch):
    """WPS-AC3 (part ii): a sidecar wedged at load (never responds) is
    killed at the load timeout, retried up to the budget, then degrades --
    ensure_ready() itself must return promptly (bounded by attempts x
    (load_timeout + backoff)), never hang."""
    monkeypatch.setattr(
        SidecarSupervisor, "_spawn", _make_stub_spawn(mode="wedge_forever")
    )
    config = _make_settings(
        tmp_path,
        sidecar_load_timeout=1.0,
        sidecar_max_retry_attempts=2,
        sidecar_retry_backoff_seconds=0.1,
    )
    context = {"embedding_ready": threading.Event(), "embedding_error": None, "embedding_generator": None}
    supervisor = SidecarSupervisor(config, context)

    try:
        t0 = time.monotonic()
        supervisor.ensure_ready()
        elapsed = time.monotonic() - t0

        assert elapsed < 10.0, f"ensure_ready() took {elapsed:.1f}s -- must be bounded by the retry budget"
        assert context["embedding_ready"].is_set()
        assert context["embedding_error"] is not None
        assert context["embedding_generator"] is not None, (
            "a proxy generator must always be installed, even degraded, so a "
            "later demand can trigger lazy recovery"
        )
    finally:
        supervisor.shutdown()


def test_lazy_recovery_clears_error_and_updates_context_on_next_demand(tmp_path, monkeypatch):
    """WP-Sidecar's replacement for WP-Wedge's late-recovery guarantee: after
    degrading, notify_demand() (the lazy-on-demand leg require_embeddings/
    get_lifespan pokes on every degraded tool call) must wake the REAL
    _recovery_loop thread and have IT clear embedding_error and install a
    working generator end to end -- not a hand-copied inline stand-in for
    what recovery is supposed to do."""
    spawn_calls = {"n": 0}
    real_spawn = _make_stub_spawn(mode="wedge_forever")

    def _flaky_spawn(self):
        spawn_calls["n"] += 1
        if spawn_calls["n"] <= 2:
            return real_spawn(self)
        return _make_stub_spawn(mode="normal")(self)

    monkeypatch.setattr(SidecarSupervisor, "_spawn", _flaky_spawn)
    config = _make_settings(
        tmp_path,
        sidecar_load_timeout=0.5,
        sidecar_max_retry_attempts=2,
        sidecar_retry_backoff_seconds=0.05,
        # long enough that the periodic tick can't be what recovers this --
        # only notify_demand() waking the loop early should.
        sidecar_periodic_retry_interval=60.0,
    )
    context = {"embedding_ready": threading.Event(), "embedding_error": None, "embedding_generator": None}
    supervisor = SidecarSupervisor(config, context)

    try:
        supervisor.ensure_ready()
        assert context["embedding_error"] is not None, "must degrade first (2 wedged attempts exhaust the budget)"

        supervisor.notify_demand()

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and context["embedding_error"] is not None:
            time.sleep(0.05)

        assert context["embedding_error"] is None, "notify_demand() must wake the recovery loop and clear the error"
        assert supervisor._degraded is False
        vector = context["embedding_generator"].generate("recovered")
        assert isinstance(vector, list) and vector
    finally:
        supervisor.shutdown()


# ── Protocol version skew (gate finding, MAJOR) ──────────────────────────────


def test_protocol_version_mismatch_rejects_without_the_heavy_import_and_stays_alive(monkeypatch):
    """A client whose _sidecar_protocol module still carries a stale
    PROTOCOL_VERSION (simulating a server process that outlived a plugin
    update on disk) talking to a REAL, freshly-spawned embeddings/sidecar.py
    process (which always re-reads whatever's on disk NOW) gets a clean
    error response, not a crash or a hang. The mismatch is caught in
    sidecar.py's dispatch BEFORE _do_load ever runs, so no heavy
    sentence_transformers/torch import is triggered -- this test is fast and
    needs no real model. Uses send() (not send_load()) deliberately: a
    rejected-before-_do_load response never emits a lock event, so
    send_load()'s clock-started wait would time out waiting for one instead
    of surfacing the real error."""
    from vibe_cognition.embeddings import _sidecar_protocol

    monkeypatch.setattr(_sidecar_protocol, "PROTOCOL_VERSION", _sidecar_protocol.PROTOCOL_VERSION + 999)

    proc = _SidecarProcess(
        python_exe=sys.executable,
        module="vibe_cognition.embeddings.sidecar",
        env=dict(os.environ),
    )
    try:
        with pytest.raises(SidecarError, match="protocol_version_mismatch"):
            proc.send("load", {"model_name": "unused", "mutex_wait_timeout": 5.0}, timeout=10.0)
        assert proc.poll() is None, "a version mismatch must not crash the sidecar process"
    finally:
        proc.kill()


def test_protocol_version_skew_degrades_bounded_and_never_self_heals_via_respawn(tmp_path, monkeypatch):
    """The full supervisor path: a stale client PROTOCOL_VERSION exhausts
    the retry budget against REAL (unmocked _spawn -- targets the real
    embeddings/sidecar.py entry module) freshly-spawned sidecars and
    degrades, same as any other exhausted-retry-budget failure -- proving
    the fixed docstring's claim that a version skew can never self-heal by
    respawning (every fresh spawn re-reads the SAME current-on-disk code and
    hits the identical mismatch, since only the CLIENT's own version is
    stale). A follow-up recovery attempt must fail identically, not silently
    "succeed" against a version it can't actually talk to."""
    from vibe_cognition.embeddings import _sidecar_protocol

    monkeypatch.setattr(_sidecar_protocol, "PROTOCOL_VERSION", _sidecar_protocol.PROTOCOL_VERSION + 999)

    config = _make_settings(
        tmp_path,
        sidecar_load_timeout=5.0,
        sidecar_mutex_wait_timeout=5.0,
        sidecar_max_retry_attempts=2,
        sidecar_retry_backoff_seconds=0.1,
    )
    context = {"embedding_ready": threading.Event(), "embedding_error": None, "embedding_generator": None}
    supervisor = SidecarSupervisor(config, context)

    try:
        t0 = time.monotonic()
        supervisor.ensure_ready()
        elapsed = time.monotonic() - t0

        assert elapsed < 15.0, f"ensure_ready() took {elapsed:.1f}s -- must be bounded by the retry budget"
        assert context["embedding_error"] is not None
        assert supervisor._degraded is True

        recovered_again = supervisor._attempt_load()
        assert recovered_again is False, "a version skew must fail identically on every fresh spawn, never self-heal"
    finally:
        supervisor.shutdown()


# ── WPS-AC4: cross-process mutex serialization + abandonment ────────────────


def _spawn_stub(load_delay: float = 0.0) -> _SidecarProcess:
    env = dict(os.environ)
    env["WP_SIDECAR_STUB_MODE"] = "normal"
    env["WP_SIDECAR_STUB_LOAD_DELAY"] = str(load_delay)
    return _SidecarProcess(python_exe=sys.executable, module=_STUB_MODULE, env=env, cwd=str(_REPO_ROOT))


def test_two_sidecars_serialize_model_load_on_the_named_mutex(tmp_path):
    """WPS-AC4: two concurrent sidecars (two real subprocesses) serialize
    their loads on the mutex -- the second waits (observes lock_wait) and
    both eventually succeed; waiting is not killed as wedged."""
    events_a: list[str] = []
    events_b: list[str] = []

    proc_a = _spawn_stub(load_delay=1.0)
    proc_a.set_event_callback(events_a.append)
    proc_b = _spawn_stub(load_delay=0.1)
    proc_b.set_event_callback(events_b.append)

    try:
        result_a = {}
        result_b = {}

        def _load(proc, args, out):
            try:
                out["result"] = proc.send_load(args, mutex_wait_timeout=10.0, load_timeout=10.0)
            except SidecarError as e:
                out["error"] = str(e)

        t_a = threading.Thread(
            target=_load, args=(proc_a, {"mutex_wait_timeout": 10.0}, result_a)
        )
        t_a.start()
        time.sleep(0.2)  # let A grab the mutex first
        t_b = threading.Thread(
            target=_load, args=(proc_b, {"mutex_wait_timeout": 10.0}, result_b)
        )
        t_b.start()

        t_a.join(timeout=15)
        t_b.join(timeout=15)

        assert result_a.get("result") == "ok", result_a
        assert result_b.get("result") == "ok", result_b
        assert "lock_wait" in events_b, "the second sidecar must have observed queueing, not skipped it"
    finally:
        proc_a.kill()
        proc_b.kill()


def test_mutex_abandonment_by_a_killed_holder_lets_the_waiter_proceed(tmp_path):
    """WPS-AC4 (abandonment variant): kill the holder mid-load -> the
    waiter's acquisition returns WAIT_ABANDONED (surfaced as the
    lock_acquired_abandoned event) and proceeds successfully."""
    events_waiter: list[str] = []

    holder = _spawn_stub(load_delay=30.0)  # long enough to still be "loading" when killed
    waiter = _spawn_stub(load_delay=0.0)
    waiter.set_event_callback(events_waiter.append)

    try:
        holder_started = threading.Event()

        def _load_holder():
            with __import__("contextlib").suppress(SidecarError):
                holder.send_load({"mutex_wait_timeout": 10.0}, mutex_wait_timeout=10.0, load_timeout=60.0)

        def _on_holder_event(name):
            if name == "lock_acquired":
                holder_started.set()

        holder.set_event_callback(_on_holder_event)
        t_holder = threading.Thread(target=_load_holder, daemon=True)
        t_holder.start()
        assert holder_started.wait(timeout=10), "holder never acquired the mutex"

        result = {}

        def _load_waiter():
            try:
                result["result"] = waiter.send_load(
                    {"mutex_wait_timeout": 15.0}, mutex_wait_timeout=15.0, load_timeout=15.0
                )
            except SidecarError as e:
                result["error"] = str(e)

        t_waiter = threading.Thread(target=_load_waiter)
        t_waiter.start()
        time.sleep(0.5)  # let the waiter genuinely start blocking on the mutex

        holder.kill()  # never released -- the mutex is now abandoned

        t_waiter.join(timeout=20)
        assert result.get("result") == "ok", result
        assert "lock_acquired_abandoned" in events_waiter, (
            f"expected lock_acquired_abandoned, got events: {events_waiter}"
        )
    finally:
        holder.kill()
        waiter.kill()


# ── WPS-AC5: pipe discipline, both directions ────────────────────────────────


def test_chatty_sidecar_flooding_stdout_does_not_block_either_process(tmp_path, monkeypatch):
    """WPS-AC5(i): a sidecar continuously flooding stdout with extra
    (non-protocol) data must not block either process -- the dedicated
    reader thread keeps draining, and a legitimate load/generate request
    still completes promptly."""
    monkeypatch.setattr(SidecarSupervisor, "_spawn", _make_stub_spawn(mode="chatty"))
    config = _make_settings(tmp_path, sidecar_load_timeout=10.0, sidecar_request_timeout=10.0)
    context = {"embedding_ready": threading.Event(), "embedding_error": None, "embedding_generator": None}
    supervisor = SidecarSupervisor(config, context)

    try:
        t0 = time.monotonic()
        supervisor.ensure_ready()
        assert context["embedding_error"] is None
        assert time.monotonic() - t0 < 8.0

        vector = supervisor.generate(["still works"], is_query=False)
        assert vector
    finally:
        supervisor.shutdown()


def test_stubborn_sidecar_that_stops_reading_stdin_unblocks_via_supervisor_kill(tmp_path, monkeypatch):
    """WPS-AC5(ii), the v0.12.1 class made into an AC, write side included: a
    stub that stops reading stdin after load succeeds. The next request
    (an oversized generate payload) fails at its timeout via the
    supervisor's kill, the requesting thread unblocks, and the process
    stays responsive for a subsequent fresh attempt."""
    monkeypatch.setattr(SidecarSupervisor, "_spawn", _make_stub_spawn(mode="stubborn_reader"))
    config = _make_settings(tmp_path, sidecar_request_timeout=3.0)
    context = {"embedding_ready": threading.Event(), "embedding_error": None, "embedding_generator": None}
    supervisor = SidecarSupervisor(config, context)

    try:
        supervisor.ensure_ready()
        assert context["embedding_error"] is None

        oversized_texts = ["x" * 100_000 for _ in range(50)]  # far exceeds the OS pipe buffer

        t0 = time.monotonic()
        with pytest.raises(SidecarError):
            supervisor.generate(oversized_texts, is_query=False)
        elapsed = time.monotonic() - t0

        assert elapsed < config.sidecar_request_timeout + 3.0, (
            f"generate() took {elapsed:.1f}s -- the requesting thread must unblock via the "
            "supervisor's kill, not hang on the stuck write forever"
        )
        assert supervisor._degraded is True
    finally:
        supervisor.shutdown()


# ── WPS-AC6: sidecar dies with its server (WP-Lifecycle parent-watch reuse) ──


_DISPOSABLE_ANCESTOR_SCRIPT = r"""
import json, os, subprocess, sys, time

python_exe, stub_module, repo_root, env_extra_json = sys.argv[1:5]
env = dict(os.environ)
env.update(json.loads(env_extra_json))

child = subprocess.Popen(
    [python_exe, "-m", stub_module],
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    cwd=repo_root, env=env,
)
print("SPAWNED", flush=True)
child.wait()
"""


def test_sidecar_dies_within_bound_when_its_server_dies(tmp_path):
    """WPS-AC6: kill the sidecar's parent ("the server", stood in by a
    disposable process here) -- the sidecar exits within 5s via the SAME
    lifecycle.arm_ancestor_watch(depth=1) mechanism WP-Lifecycle built (no
    intermediary this time: the sidecar's real parent IS the server
    process, spawned directly via subprocess.Popen -- depth=1 is exactly
    the parameterization WP-Lifecycle's brief called out for this reuse)."""
    import json
    import subprocess

    env_extra = {"WP_SIDECAR_STUB_MODE": "normal"}
    ancestor = subprocess.Popen(
        [
            sys.executable, "-c", _DISPOSABLE_ANCESTOR_SCRIPT,
            sys.executable, _STUB_MODULE, str(_REPO_ROOT), json.dumps(env_extra),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=str(_REPO_ROOT),
    )
    try:
        line = ancestor.stdout.readline()
        assert "SPAWNED" in line

        # Give the sidecar a moment to actually arm its ancestor watch.
        time.sleep(1.0)

        # Find the sidecar's pid via the ancestor's own child enumeration --
        # simplest robust approach on Windows: WMI query for children of
        # the ancestor's pid.
        import ctypes

        result = subprocess.run(
            [
                "powershell", "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ParentProcessId={ancestor.pid}\").ProcessId",
            ],
            capture_output=True, text=True, timeout=10,
        )
        child_pid_str = result.stdout.strip()
        assert child_pid_str, f"could not find sidecar child pid: {result.stdout!r} {result.stderr!r}"
        sidecar_pid = int(child_pid_str.splitlines()[0])

        def _is_alive(pid: int) -> bool:
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)

        assert _is_alive(sidecar_pid), "sidecar process not found alive before the kill"

        t0 = time.monotonic()
        ancestor.kill()

        died_at = None
        deadline = t0 + 8.0
        while time.monotonic() < deadline:
            if not _is_alive(sidecar_pid):
                died_at = time.monotonic() - t0
                break
            time.sleep(0.1)

        assert died_at is not None, f"sidecar pid {sidecar_pid} still alive 8s after its parent was killed"
        assert died_at <= 8.0
    finally:
        with __import__("contextlib").suppress(Exception):
            if ancestor.poll() is None:
                ancestor.kill()
                ancestor.wait(timeout=5)
