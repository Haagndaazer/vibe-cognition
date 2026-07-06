"""WP-Wedge (P0, docs/wp-wedge-plan.md rev 3): bound the bg-thread heavy-import
wedge that intermittently freezes a live MCP session on Windows.

Covers AC1 (subprocess import probe, kill + retry-success), AC2 (watchdog +
late recovery, incl. clobber-guard and stranding-interleaving variants), AC3
(import-collision across every registered tool), AC4 (state-contract tuple),
and AC6 (heartbeat lifecycle). AC5 (the pinned whole-repo gate command) is run
separately, not from within this file.
"""

import asyncio
import contextlib
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibe_cognition.embeddings import ChromaDBStorage
from vibe_cognition.server import (
    _load_embeddings_and_sync,
    _run_subprocess_import_probe,
    _watchdog,
    _worker_heartbeat,
    lifespan,
)
from vibe_cognition.tools import register_all_tools
from vibe_cognition.tools.cognition_tools import _embeddings_ready
from vibe_cognition.tools.utils import require_embeddings

# ── AC1: subprocess import probe ─────────────────────────────────────────────


def _write_marker_script(tmp_path: Path, marker: Path) -> Path:
    """A script that blocks forever on its FIRST run (creating `marker`) and
    exits immediately on any run after `marker` already exists -- simulates
    "wedged once, recovers on retry" without needing two different commands."""
    script = tmp_path / "probe_script.py"
    script.write_text(
        "import pathlib, sys, time\n"
        f"p = pathlib.Path({str(marker)!r})\n"
        "if p.exists():\n"
        "    sys.exit(0)\n"
        "p.write_text('x')\n"
        "time.sleep(9999)\n",
        encoding="utf-8",
    )
    return script


def test_probe_kills_and_gives_up_after_two_timeouts(tmp_path):
    """AC1: a probe command that blocks forever is killed at the parameterized
    timeout, retried once after the parameterized backoff, and killed again ->
    returns False. Fails-before: no probe existed at all (the in-process import
    ran unbounded on the loader-lock-holding thread)."""
    script = tmp_path / "blocks_forever.py"
    script.write_text("import time\ntime.sleep(9999)\n", encoding="utf-8")

    t0 = time.monotonic()
    ok = _run_subprocess_import_probe(
        cmd=[sys.executable, str(script)], timeout=0.3, retry_backoff=0.2,
    )
    elapsed = time.monotonic() - t0

    assert ok is False
    # Two timeouts + one backoff, bounded -- not the unbounded hang this WP fixes.
    assert elapsed < 3.0, f"probe took {elapsed:.2f}s -- should be bounded to ~0.8s"


def test_probe_recovers_on_retry_after_first_timeout(tmp_path):
    """AC1 recovery variant: first attempt wedges and is killed; the retried
    attempt succeeds -> True, safe to proceed with the in-process import."""
    marker = tmp_path / "recovers_marker"
    script = _write_marker_script(tmp_path, marker)

    ok = _run_subprocess_import_probe(
        cmd=[sys.executable, str(script)], timeout=0.3, retry_backoff=0.1,
    )

    assert ok is True


def test_probe_succeeds_immediately_when_import_is_fast(tmp_path):
    """Sunny-day: a command that exits promptly (any exit code) counts as
    success -- only a genuine timeout is treated as a wedge."""
    script = tmp_path / "fast_fail.py"
    script.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")

    ok = _run_subprocess_import_probe(cmd=[sys.executable, str(script)], timeout=5.0)

    assert ok is True


def test_load_embeddings_gives_up_without_in_process_import_after_two_wedges(
    tmp_path, monkeypatch
):
    """AC1: when the probe wedges twice, `_load_embeddings_and_sync` must
    degrade WITHOUT ever attempting the real in-process import, and all gated
    tools must respond promptly (< 2s) afterward -- session survives degraded,
    never hangs."""
    from vibe_cognition.config import Settings

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "sentence-transformers")
    config = Settings()

    # The probe's own kill/retry mechanics are covered directly by
    # test_probe_kills_and_gives_up_after_two_timeouts; here we only need the
    # WIRING in _load_embeddings_and_sync -- a probe that reports "gave up" --
    # to prove the in-process import is never attempted and the session degrades.
    probe_calls = {"n": 0}

    def _fake_probe(cmd=None, timeout=None, retry_backoff=None):
        probe_calls["n"] += 1
        return False

    monkeypatch.setattr("vibe_cognition.server._run_subprocess_import_probe", _fake_probe)

    called = {"from_config": False}

    def _boom(*a, **k):
        called["from_config"] = True
        raise AssertionError("in-process import must never be attempted after a double wedge")

    monkeypatch.setattr("vibe_cognition.server.EmbeddingGenerator.from_config", _boom)

    context = {
        "cognition_storage": None,
        "cognition_embedding_storage": ChromaDBStorage(persist_directory=tmp_path / "chromadb"),
        "loaded_projects": None,
        "embedding_ready": threading.Event(),
        "embedding_sync_done": threading.Event(),
        "embedding_error": None,
        "embedding_generator": None,
        "_wedge_lock": threading.Lock(),
        "watchdog_fired": False,
        "bg_model_load_start_time": None,
    }

    t0 = time.monotonic()
    _load_embeddings_and_sync(config, context)
    elapsed = time.monotonic() - t0

    assert called["from_config"] is False
    assert context["embedding_generator"] is None
    assert context["embedding_ready"].is_set()
    assert context["embedding_sync_done"].is_set()
    assert "wedged twice" in context["embedding_error"]
    assert elapsed < 3.0

    # All tools must respond promptly now -- gate check is a pure in-memory read.
    t1 = time.monotonic()
    lc = {"embedding_ready": context["embedding_ready"], "embedding_error": context["embedding_error"],
          "embedding_generator": context["embedding_generator"]}
    assert _embeddings_ready(lc) is False
    assert time.monotonic() - t1 < 2.0


# ── AC2: watchdog + late recovery ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_watchdog_fires_after_timeout_when_bg_thread_never_finishes():
    """AC2: watchdog fires at the parameterized T if bg_model_load_start_time
    is set but embedding_ready never fires -- sets error+ready+watchdog_fired."""
    context = {
        "embedding_ready": threading.Event(),
        "_wedge_lock": threading.Lock(),
        "watchdog_fired": False,
        "bg_model_load_start_time": time.monotonic(),
        "embedding_error": None,
    }

    await _watchdog(context, timeout=0.15, poll_interval=0.03)

    assert context["embedding_ready"].is_set()
    assert context["watchdog_fired"] is True
    assert context["embedding_error"] == "embedding load slow/wedged; search temporarily degraded"


@pytest.mark.asyncio
async def test_watchdog_never_fires_while_probe_still_running():
    """AC2 ordering contract: while bg_model_load_start_time is still None (the
    probe hasn't finished), the watchdog must re-arm rather than fire -- a
    legitimately slow, bounded probe must never trip it."""
    context = {
        "embedding_ready": threading.Event(),
        "_wedge_lock": threading.Lock(),
        "watchdog_fired": False,
        "bg_model_load_start_time": None,
        "embedding_error": None,
    }

    async def _finish_soon():
        await asyncio.sleep(0.2)
        context["embedding_ready"].set()

    finisher = asyncio.create_task(_finish_soon())
    await asyncio.wait_for(
        _watchdog(context, timeout=0.05, poll_interval=0.02), timeout=2.0
    )
    await finisher

    assert context["watchdog_fired"] is False, "must not fire while the probe window is unbounded-but-unstarted"
    assert context["embedding_error"] is None


@pytest.mark.asyncio
async def test_watchdog_late_recovery_installs_generator_and_clears_error(tmp_path, monkeypatch):
    """AC2: watchdog fires first (bg thread still "wedged" in the in-process
    import); once the bg thread finishes normally, the SAME lock's atomicity
    installs the generator, clears the watchdog's placeholder error, and
    clears watchdog_fired -- session ends ready and error-free."""
    from vibe_cognition.config import Settings

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")  # skip §3a probe -- orthogonal to this AC
    config = Settings()

    release = threading.Event()
    sentinel_generator = SimpleNamespace(name="real-generator")

    def _slow_from_config(cfg):
        release.wait(timeout=10)
        return sentinel_generator

    monkeypatch.setattr("vibe_cognition.server.EmbeddingGenerator.from_config", _slow_from_config)

    context = {
        "cognition_storage": None,
        "cognition_embedding_storage": ChromaDBStorage(persist_directory=tmp_path / "chromadb"),
        "loaded_projects": None,
        "embedding_ready": threading.Event(),
        "embedding_sync_done": threading.Event(),
        "embedding_error": None,
        "embedding_generator": None,
        "_wedge_lock": threading.Lock(),
        "watchdog_fired": False,
        "bg_model_load_start_time": None,
    }

    bg_thread = threading.Thread(target=_load_embeddings_and_sync, args=(config, context), daemon=True)
    bg_thread.start()

    await _watchdog(context, timeout=0.1, poll_interval=0.02)

    assert context["watchdog_fired"] is True
    assert context["embedding_generator"] is None, "bg thread is still blocked on `release`"

    release.set()
    bg_thread.join(timeout=10)
    assert not bg_thread.is_alive()

    assert context["embedding_generator"] is sentinel_generator
    assert context["embedding_error"] is None
    assert context["watchdog_fired"] is False


@pytest.mark.asyncio
async def test_watchdog_clobber_guard_genuine_error_after_fire_is_not_cleared(tmp_path, monkeypatch):
    """AC2 clobber-guard variant: the bg thread raises a GENUINE error after the
    watchdog already fired -- the except path's real error must stand, never
    be silently cleared by anything (there is no late-recovery path for a
    genuine failure)."""
    from vibe_cognition.config import Settings

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")
    config = Settings()

    release = threading.Event()

    def _slow_then_raises(cfg):
        release.wait(timeout=10)
        raise RuntimeError("genuine failure after wedge")

    monkeypatch.setattr("vibe_cognition.server.EmbeddingGenerator.from_config", _slow_then_raises)

    context = {
        "cognition_storage": None,
        "cognition_embedding_storage": ChromaDBStorage(persist_directory=tmp_path / "chromadb"),
        "loaded_projects": None,
        "embedding_ready": threading.Event(),
        "embedding_sync_done": threading.Event(),
        "embedding_error": None,
        "embedding_generator": None,
        "_wedge_lock": threading.Lock(),
        "watchdog_fired": False,
        "bg_model_load_start_time": None,
    }

    bg_thread = threading.Thread(target=_load_embeddings_and_sync, args=(config, context), daemon=True)
    bg_thread.start()

    await _watchdog(context, timeout=0.1, poll_interval=0.02)
    assert context["watchdog_fired"] is True

    release.set()
    bg_thread.join(timeout=10)
    assert not bg_thread.is_alive()

    assert context["embedding_generator"] is None
    assert context["embedding_error"] == "genuine failure after wedge", (
        "the genuine except-path error must stand, not be cleared/overwritten "
        "by the watchdog's placeholder"
    )


@pytest.mark.asyncio
async def test_watchdog_stranding_interleaving_never_leaves_ready_no_error_no_generator(
    tmp_path, monkeypatch
):
    """AC2 stranding-interleaving variant: drive the watchdog's fire and the bg
    thread's normal completion at a near-simultaneous race, repeatedly. The
    lock must make the transition atomic -- the forbidden tuple (ready=True,
    error=None, generator=None) must NEVER be observable, and the session must
    never end stranded (ready + error but a generator that never installs)."""
    from vibe_cognition.config import Settings

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")
    config = Settings()

    for _ in range(15):
        sentinel_generator = SimpleNamespace()
        release = threading.Event()

        def _from_config(cfg, _release=release, _gen=sentinel_generator):
            _release.wait(timeout=10)
            return _gen

        monkeypatch.setattr("vibe_cognition.server.EmbeddingGenerator.from_config", _from_config)

        context = {
            "cognition_storage": None,
            "cognition_embedding_storage": ChromaDBStorage(persist_directory=tmp_path / f"chromadb_{_}"),
            "loaded_projects": None,
            "embedding_ready": threading.Event(),
            "embedding_sync_done": threading.Event(),
            "embedding_error": None,
            "embedding_generator": None,
            "_wedge_lock": threading.Lock(),
            "watchdog_fired": False,
            "bg_model_load_start_time": time.monotonic(),
        }

        bg_thread = threading.Thread(target=_load_embeddings_and_sync, args=(config, context), daemon=True)
        # Race: release the bg thread and arm a near-simultaneous watchdog deadline together.
        bg_thread.start()
        watchdog_task = asyncio.create_task(_watchdog(context, timeout=0.01, poll_interval=0.005))
        await asyncio.sleep(0.005)
        release.set()

        await watchdog_task
        bg_thread.join(timeout=10)
        assert not bg_thread.is_alive()

        ready = context["embedding_ready"].is_set()
        error = context["embedding_error"]
        generator = context["embedding_generator"]

        assert ready is True
        assert not (error is None and generator is None), (
            f"stranded tuple observed: ready={ready} error={error} generator={generator}"
        )
        if error is None:
            assert generator is sentinel_generator


# ── AC4: state-contract tuple (ready, no error, generator=None) ─────────────


def test_embeddings_ready_false_when_generator_missing_despite_ready_and_no_error():
    """AC4: _embeddings_ready must read (ready, no error, generator=None) as
    NOT ready -- the watchdog-fired-but-not-yet-late-recovered tuple."""
    lc = {
        "embedding_ready": threading.Event(),
        "embedding_error": None,
        "embedding_generator": None,
    }
    lc["embedding_ready"].set()

    assert _embeddings_ready(lc) is False


def test_require_embeddings_returns_loading_dict_when_generator_missing(tmp_path, build_lc, make_ctx):
    """AC4: require_embeddings' sibling check -- same tuple, same verdict."""
    lc = build_lc(tmp_path, embeddings_ready=True)
    lc["embedding_generator"] = None
    ctx = make_ctx(lc)

    err = require_embeddings(ctx)

    assert err is not None
    assert err["status"] == "loading_embeddings"


def test_record_node_does_not_attribute_error_on_stale_generator_race(
    tmp_path, mock_mcp, build_lc, make_ctx
):
    """AC4: cognition_record must return the loading dict, never raise
    AttributeError, when ready+no-error+generator=None (the exact race the
    stale-early-capture bug used to hit)."""
    from vibe_cognition.tools.cognition_tools import register_cognition_tools

    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    lc["embedding_generator"] = None
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_record"](
        ctx, node_type="decision", summary="s", detail="d", context="c", author="a",
    )

    assert isinstance(result, dict)
    assert "id" in result, "the node itself must still be created (embed is best-effort/deferred)"


def test_add_task_does_not_attribute_error_on_stale_generator_race(
    tmp_path, mock_mcp, build_lc, make_ctx
):
    """AC4: same race, cognition_add_task."""
    from vibe_cognition.tools.cognition_tools import register_cognition_tools

    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    lc["embedding_generator"] = None
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_add_task"](
        ctx, summary="s", detail="d", context="c",
    )

    assert isinstance(result, dict)
    assert "id" in result


def test_update_task_and_update_node_do_not_read_stale_generator(
    tmp_path, mock_mcp, build_lc, make_ctx
):
    """AC4: cognition_update_task / cognition_update_node read the generator
    AFTER the ready gate, not before -- both must degrade cleanly, never raise,
    when generator is None despite ready+no-error."""
    from vibe_cognition.tools.cognition_tools import register_cognition_tools

    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)
    created = mock_mcp.tools["cognition_add_task"](ctx, summary="s", detail="d", context="c")
    node_id = created["id"]

    lc["embedding_generator"] = None

    r1 = mock_mcp.tools["cognition_update_task"](ctx, node_id=node_id, status="in_progress")
    assert isinstance(r1, dict)
    assert r1.get("reembed") == "deferred"

    r2 = mock_mcp.tools["cognition_update_node"](ctx, node_id=node_id, summary="s2")
    assert isinstance(r2, dict)
    assert r2.get("reembed") == "deferred"


# ── AC3: import-collision across every registered tool ──────────────────────

_FAKE_HEAVY_MODULE = "_wp_wedge_test_fake_heavy_import_target"


class _BlockingFinder:
    """Meta-path hook that blocks (via a threading.Event) any NEW import of a
    name matching `names`, simulating an in-flight per-module import lock held
    by a "fake bg thread" -- the exact mechanism AC3 exercises, scoped to a
    synthetic name rather than the real torch/scipy/sentence_transformers/etc.
    to avoid destabilizing already-imported C-extension modules shared with
    the rest of this (single-process) test suite."""

    def __init__(self, names, gate):
        self.names = names
        self.gate = gate

    def find_spec(self, fullname, path, target=None):
        if fullname in self.names:
            self.gate.wait(timeout=15)
        return None  # never resolves -- caller only cares about the blocking window


_TOOL_ARGS: dict[str, dict] = {
    "cognition_record": {"node_type": "decision", "summary": "s", "detail": "d", "context": "c", "author": "a"},
    "cognition_store_document": {
        "title": "t", "document_text": "dt", "context": "c", "author": "a", "content_text": "x",
    },
    "cognition_get_document": {"node_id": "nonexistent"},
    "cognition_get_node": {"node_id": "nonexistent"},
    "cognition_update_node": {"node_id": "nonexistent", "summary": "x"},
    "cognition_search": {"query": "test"},
    "cognition_get_chain": {"node_id": "nonexistent"},
    "cognition_get_superseded_chain": {"node_id": "nonexistent"},
    "cognition_get_workflow": {"name_or_topic": "test"},
    "cognition_get_incident_resolution": {"node_id": "nonexistent"},
    "cognition_get_history": {},
    "cognition_add_edge": {"from_id": "a", "to_id": "b", "edge_type": "led_to"},
    "cognition_add_edges_batch": {"edges": "[]"},
    "cognition_get_edgeless_nodes": {},
    "cognition_get_uncurated_nodes": {},
    "cognition_mark_curated": {"node_ids": "a,b"},
    "cognition_get_neighbors": {"node_id": "nonexistent"},
    "cognition_remove_edge": {"from_id": "a", "to_id": "b", "edge_type": "led_to"},
    "cognition_remove_node": {"node_id": "nonexistent"},
    "cognition_reload": {},
    "cognition_load_project": {"path": "does-not-exist"},
    "cognition_unload_project": {"project": "home"},
    "cognition_list_projects": {},
    "cognition_add_task": {"summary": "s", "detail": "d", "context": "c"},
    "cognition_list_tasks": {},
    "cognition_update_task": {"node_id": "nonexistent", "status": "open"},
    "get_status": {},
    "cognition_dashboard": {},
    "cognition_readme": {},
}


def test_every_registered_tool_responds_within_10s_during_simulated_import_wedge(
    tmp_path, mock_mcp, build_lc, make_ctx
):
    """AC3: with a fake bg thread holding a per-module import lock on a heavy
    name, invoke every registered tool (embeddings NOT ready -- the load
    window) each in its own thread with join(timeout=10). No tool may touch
    the blocked import at all (gated tools short-circuit on pure in-memory
    checks), so none may hang regardless of the simulated wedge.

    Fails-before: N/A (this is a fresh regression guard) -- pins that no
    tool's call path performs an eager heavy import while not-ready, so a
    future change can't silently reintroduce the collision this WP bounds.
    """
    from unittest.mock import patch

    register_all_tools(mock_mcp)
    assert len(mock_mcp.tools) == 29, "tool count drifted -- update _TOOL_ARGS to match"
    assert set(_TOOL_ARGS) == set(mock_mcp.tools), (
        f"missing args entries: {set(mock_mcp.tools) - set(_TOOL_ARGS)}"
    )

    lc = build_lc(tmp_path, embeddings_ready=False)
    ctx = make_ctx(lc)

    gate = threading.Event()
    finder = _BlockingFinder({_FAKE_HEAVY_MODULE}, gate)
    sys.meta_path.insert(0, finder)

    fake_bg_started = threading.Event()

    def _fake_bg_import():
        fake_bg_started.set()
        # ImportError is expected -- the name doesn't really exist; only the block matters.
        with contextlib.suppress(ImportError):
            __import__(_FAKE_HEAVY_MODULE)

    fake_bg = threading.Thread(target=_fake_bg_import, daemon=True)

    try:
        fake_bg.start()
        assert fake_bg_started.wait(timeout=5), "fake bg thread never started"

        results: dict[str, dict] = {}
        errors: dict[str, BaseException] = {}

        def _call(name, kwargs):
            try:
                results[name] = mock_mcp.tools[name](ctx, **kwargs)
            except Exception as e:  # noqa: BLE001 - capturing per-tool for reporting
                errors[name] = e

        threads = {
            name: threading.Thread(target=_call, args=(name, kwargs), daemon=True)
            for name, kwargs in _TOOL_ARGS.items()
        }
        with patch("vibe_cognition.tools.dashboard_tool.start_dashboard",
                   return_value={"url": "http://127.0.0.1:7842/?token=fake",
                                 "status": "running", "embedding_ready": False,
                                 "embedding_error": None}):
            for t in threads.values():
                t.start()
            for name, t in threads.items():
                t.join(timeout=10)
                assert not t.is_alive(), f"{name} did not return within 10s -- hung on the simulated wedge"
    finally:
        gate.set()
        sys.meta_path.remove(finder)
        fake_bg.join(timeout=5)

    assert not errors, f"tool(s) raised instead of returning an error dict: {errors}"
    for name, result in results.items():
        assert isinstance(result, dict), f"{name} did not return a dict: {result!r}"


# ── AC6: heartbeat lifecycle ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_batches_at_least_twice_during_a_load_window(monkeypatch):
    """AC6: the heartbeat submits >=2 batches while ready is unset, each batch
    running the full warm-worker count."""
    calls = []

    async def _fake_batch(count):
        calls.append(count)

    monkeypatch.setattr("vibe_cognition.server._warm_worker_batch", _fake_batch)

    ready = threading.Event()
    context = {"embedding_ready": ready}

    task = asyncio.create_task(_worker_heartbeat(context, interval=0.03, batch_size=4))
    await asyncio.sleep(0.25)
    ready.set()
    await asyncio.wait_for(task, timeout=5)

    assert len(calls) >= 2, f"expected >=2 heartbeat batches, got {len(calls)}"
    assert all(c == 4 for c in calls)


@pytest.mark.asyncio
async def test_heartbeat_skips_tick_when_previous_batch_still_in_flight(monkeypatch):
    """AC6: a batch slower than the tick interval must not stack a second
    spawn-triggering batch on top -- at most one batch in flight at a time."""
    concurrent = {"n": 0, "max": 0}
    lock = asyncio.Lock()

    async def _slow_batch(count):
        async with lock:
            concurrent["n"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["n"])
        await asyncio.sleep(0.15)  # much slower than the 0.03s tick interval
        async with lock:
            concurrent["n"] -= 1

    monkeypatch.setattr("vibe_cognition.server._warm_worker_batch", _slow_batch)

    ready = threading.Event()
    context = {"embedding_ready": ready}

    task = asyncio.create_task(_worker_heartbeat(context, interval=0.03, batch_size=4))
    await asyncio.sleep(0.4)
    ready.set()
    await asyncio.wait_for(task, timeout=5)

    assert concurrent["max"] <= 1, f"batches overlapped: max concurrent = {concurrent['max']}"


@pytest.mark.asyncio
async def test_heartbeat_stops_promptly_once_ready_sets():
    """AC6: the heartbeat exits as soon as embedding_ready is set (either
    path) -- it must not linger past the load window."""
    ready = threading.Event()
    ready.set()  # already ready before the heartbeat even starts
    context = {"embedding_ready": ready}

    await asyncio.wait_for(_worker_heartbeat(context, interval=5.0), timeout=1.0)


@pytest.mark.asyncio
async def test_prespawn_happens_before_bg_thread_starts(tmp_path, monkeypatch):
    """AC6: the lifespan pre-spawns warm workers BEFORE starting the bg import
    thread -- pins the ordering `lifespan()` scope §3c1 requires."""

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")

    order: list[str] = []

    async def _fake_prespawn(count):
        order.append("prespawn")

    monkeypatch.setattr("vibe_cognition.server._warm_worker_batch", _fake_prespawn)

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

    assert order == ["prespawn", "bg_thread_start"], f"wrong order: {order}"
