"""WP-Wedge (P0, docs/wp-wedge-plan.md rev 3): bound the bg-thread heavy-import
wedge that intermittently freezes a live MCP session on Windows.

WP-Sidecar (P0 endgame) subsumed AC1 (the subprocess import probe) and AC2
(the watchdog + late recovery) entirely -- the heavy import no longer
happens in this process at all, so there is nothing left to probe or watch
a timeout on here. Their coverage is replaced by
tests/test_wp_sidecar.py's supervisor-equivalent tests (kill+respawn+
backoff, degrade, lazy/periodic recovery), called out explicitly per the
WP2-AC6 precedent (WP-Wedge-2 replacing WP-Wedge's own probe similarly).

This file now covers: AC3 (import-collision across every registered tool,
static heavy-chain guard flipped to sidecar.py as the sole sanctioned site),
AC4 (state-contract tuple), and AC6 (heartbeat lifecycle). AC5 (the pinned
whole-repo gate command) is run separately, not from within this file.
"""

import ast
import asyncio
import contextlib
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibe_cognition.server import (
    _load_embeddings_and_sync,
    _worker_heartbeat,
    lifespan,
)
from vibe_cognition.tools import register_all_tools
from vibe_cognition.tools.cognition_tools import _embeddings_ready
from vibe_cognition.tools.utils import require_embeddings

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


# ── AC3: import-collision, static half + runtime half ───────────────────────
#
# Per Vince's gate review (HOLD on the first cut): a synthetic-name meta_path
# hook alone can't catch AC3's actual regression -- a future tool adding a
# real `import scipy` at module level runs before any finder installs, and a
# name-based hook that isn't watching the real names never sees it either way.
# AC3 is split into two complementary tests:
#   - static (authoring-time): AST-walk every module under src/vibe_cognition/
#     and assert no import of the real heavy names outside the one sanctioned
#     site. Catches a module-level OR unexercised-branch import regardless of
#     whether any test happens to invoke the code that triggers it.
#   - runtime (below): proves tool DISPATCH survives an unrelated import being
#     blocked mid-flight elsewhere in the process -- a real property, but a
#     narrower one than "no tool imports the heavy chain" (that's the static
#     test's job).

_HEAVY_IMPORT_RE = re.compile(r"^(torch|scipy|sentence_transformers|transformers|sklearn)(\.|$)")

# WP-Sidecar (P0 endgame) FLIPS this: the sanctioned site moves from
# embeddings/generator.py to embeddings/sidecar.py -- the server process may
# never import the heavy chain ANYWHERE now; only the sidecar entry module
# (a separate subprocess, never imported server-side) does. generator.py's
# SentenceTransformersBackend moved to sidecar.py verbatim; generator.py now
# only constructs a SidecarBackend proxy (no heavy import at all). server.py's
# _venv_guard.py's find_spec presence-check is NOT an actual Import/
# ImportFrom AST node (an importlib.util.find_spec call), so it never needs
# listing here -- an AST walk over Import/ImportFrom nodes naturally never
# sees it.
_SANCTIONED_HEAVY_IMPORT_FILES = {
    Path("src", "vibe_cognition", "embeddings", "sidecar.py"),
}


def _iter_heavy_imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _HEAVY_IMPORT_RE.match(alias.name):
                    yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and _HEAVY_IMPORT_RE.match(node.module):
            yield node.lineno, node.module


def test_no_module_imports_the_heavy_embedding_chain_outside_the_sanctioned_site():
    """AC3 (static half) / WPS-AC1: no module under src/vibe_cognition/ may
    `import` or `from ... import` torch|scipy|sentence_transformers|
    transformers|sklearn -- module-level OR inside a function body -- outside
    embeddings/sidecar.py (the ONLY process allowed to touch the heavy chain
    after WP-Sidecar). Catches what a runtime dispatch test can't: a module-
    level eager import (already executed before any test-installed hook
    exists) or an import hidden in a branch no test happens to exercise.

    Fails-before: N/A -- added per Vince's WP-Wedge gate review (the first AC3
    cut, a runtime-only synthetic-name hook, could never catch this class of
    regression at all).
    """
    repo_root = Path(__file__).resolve().parents[1]
    src_root = repo_root / "src" / "vibe_cognition"
    sanctioned = {repo_root / p for p in _SANCTIONED_HEAVY_IMPORT_FILES}

    violations = []
    for py_file in sorted(src_root.rglob("*.py")):
        if py_file in sanctioned:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for lineno, name in _iter_heavy_imports(tree):
            violations.append(f"{py_file.relative_to(repo_root)}:{lineno} imports {name!r}")

    assert not violations, (
        "heavy embedding-chain import found outside the sanctioned site "
        "(embeddings/sidecar.py) -- this reintroduces the WP-C wedge risk "
        "in the SERVER PROCESS, exactly what WP-Sidecar exists to prevent:\n"
        + "\n".join(violations)
    )


def test_sanctioned_file_actually_contains_the_expected_lazy_import():
    """Guard the guard: if embeddings/sidecar.py's lazy import is ever
    removed entirely, the exclusion above must not silently make this file's
    coverage vacuous."""
    repo_root = Path(__file__).resolve().parents[1]
    sidecar_py = repo_root / "src" / "vibe_cognition" / "embeddings" / "sidecar.py"
    tree = ast.parse(sidecar_py.read_text(encoding="utf-8"), filename=str(sidecar_py))

    assert list(_iter_heavy_imports(tree)), (
        "expected sentence_transformers to still be imported (lazily) in "
        "embeddings/sidecar.py -- if it moved, update "
        "_SANCTIONED_HEAVY_IMPORT_FILES to match"
    )


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
    "cognition_register_person": {
        "name": "n", "role": "r", "seniority": "mid", "email": "nonexistent@example.com",
    },
    "cognition_update_person": {"email_or_id": "nonexistent@example.com", "role": "x"},
    "cognition_get_person": {"email_or_id": "nonexistent@example.com"},
    "cognition_list_people": {},
    "get_status": {},
    "cognition_dashboard": {},
    "cognition_readme": {},
}


def test_tool_dispatch_completes_while_an_unrelated_import_is_blocked_midflight(
    tmp_path, mock_mcp, build_lc, make_ctx
):
    """AC3 (runtime half): with a fake bg thread holding a per-module import
    lock on a name (synthetic, not the real torch/scipy/sentence_transformers
    -- those are very likely already cached in sys.modules by other tests in
    this shared pytest process, and evicting+reimporting a real C-extension
    mid-suite risks destabilizing unrelated tests), invoke every registered
    tool (embeddings NOT ready -- the load window) each in its own thread with
    join(timeout=10).

    This proves tool DISPATCH itself never blocks on an unrelated in-flight
    import elsewhere in the process -- every tool bails via pure in-memory
    checks (embedding_ready/embedding_error reads), never by touching the
    blocked name. It does NOT prove "no tool imports the heavy chain" on its
    own (a synthetic name can't stand in for that) -- see
    test_no_module_imports_the_heavy_embedding_chain_outside_the_sanctioned_site
    above for that half; together the two cover AC3.
    """
    from unittest.mock import patch

    register_all_tools(mock_mcp)
    assert len(mock_mcp.tools) == 33, "tool count drifted -- update _TOOL_ARGS to match"
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
    """AC6 + WP2 §W2-b (INV-1): the lifespan pre-spawns warm anyio workers AND
    pre-warms the dedicated dispatch executor BEFORE starting the bg import
    thread -- pins the ordering both `lifespan()` scope §3c1 and INV-1 require.
    Fails-before (the gate's MAJOR finding): the original cut of this test only
    recorded _warm_worker_batch vs bg_thread_start -- prewarm_dispatch_executor
    ran in the right place by construction but nothing here would have caught
    a regression that moved or dropped it."""

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")

    order: list[str] = []

    async def _fake_prespawn(count):
        order.append("prespawn")

    async def _fake_dispatch_prewarm(count=None):
        order.append("dispatch_prewarm")

    monkeypatch.setattr("vibe_cognition.server._warm_worker_batch", _fake_prespawn)
    monkeypatch.setattr(
        "vibe_cognition.server.prewarm_dispatch_executor", _fake_dispatch_prewarm
    )

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

    assert order == ["prespawn", "dispatch_prewarm", "bg_thread_start"], f"wrong order: {order}"
