"""WP-Wedge-2 (P0, docs/wp-wedge2-plan.md rev 4): tool dispatch must survive an
in-process import wedge.

Covers WP2-AC1/AC3 (INV-1: spawn-free event loop, this file's §W2-b section)
and WP2-AC4 (INV-2: import-free tool surface, §W2-c section). The watchdog-
timeout wiring test (AC5) is in test_wp_wedge.py next to the rest of the
watchdog tests; the §W2-a repro script (AC2 part ii) and stall-forensics test
(AC2 part i) are wired in separately per docs/wp-wedge2-plan.md rev 4.
"""

import ast
import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastmcp import Client
from fastmcp.server.middleware import MiddlewareContext

from tests.test_wp_wedge import _TOOL_ARGS
from vibe_cognition import _startup_timing
from vibe_cognition.embeddings import ChromaDBStorage
from vibe_cognition.server import _DispatchStallForensics, lifespan, mcp
from vibe_cognition.tools.dispatch import (
    _DISPATCH_POOL_SIZE,
    dispatch_executor,
    dispatch_tool,
    prewarm_dispatch_executor,
)

# ── §W2-c: import-free tool surface (INV-2) ─────────────────────────────────
#
# A DIFFERENT guard from WP-Wedge (v1)'s heavy-chain AST test in
# test_wp_wedge.py (which only watches five package names, torch/scipy/
# sentence_transformers/transformers/sklearn): INV-2's rule is "the tool
# surface is import-free at runtime," full stop -- so this flags ANY
# function-body Import/ImportFrom, anywhere under src/vibe_cognition/.
#
# Three files are exempt, each for a documented reason -- NOT "not gotten to
# yet":
#   - embeddings/generator.py: the ONE sanctioned lazy-backend-construction
#     site (sentence_transformers AND ollama, WP-C's decision 9022f7de94e9)
#     -- runs on the bg thread via _load_embeddings_and_sync, never on the
#     tool-dispatch path. Same file WP-Wedge's heavy-chain guard already
#     sanctions, same underlying reason.
#   - dashboard/cli.py: a standalone console-script entry point
#     (`vibe-cognition-dashboard`, pyproject.toml [project.scripts]) -- never
#     imported by register_all_tools/server.py's import graph, so it never
#     executes inside the serving MCP process at all.
#   - instructions.py: `main()`'s lazy imports are for the SessionStart
#     `compact`-hook entry point, a SEPARATE process invocation -- server.py
#     only imports the SERVER_INSTRUCTIONS constant, never calls main().
#
# Known tradeoff (shared with the heavy-chain guard's own exclusion style): a
# whole-file exclusion means a NEW, unrelated function-body import added to
# one of these three files later would go unflagged. The "guard the guard"
# tests below at least catch the exclusion going silently vacuous (the
# sanctioned import removed entirely, e.g. by a refactor).
#
# A fourth, narrower exception style: a single line carrying the
# `wp2-import-free: sanctioned` marker (mirrors ruff's noqa comments --
# self-documenting, per-line, doesn't blanket-exempt the rest of that file).
# Used for
# server.py's shutdown-path `from .dashboard.server import stop_dashboard`:
# hoisting THAT one to module level creates a genuine circular import
# (dashboard.server -> dashboard.api -> tools.cognition_tools ->
# tools/__init__.py -> dashboard_tool.py -> dashboard.server, still
# mid-init) -- discovered when this guard's own repro-wiring test failed on
# it. It's provably safe as a function-body import: dashboard_tool.py's own
# top-level import already fully loads dashboard.server during server.py's
# normal module import (well before this shutdown code ever runs), so it's
# always a sys.modules cache hit, never a fresh import.

_IMPORT_FREE_SANCTIONED_FILES = {
    Path("src", "vibe_cognition", "embeddings", "generator.py"),
    Path("src", "vibe_cognition", "dashboard", "cli.py"),
    Path("src", "vibe_cognition", "instructions.py"),
}

_IMPORT_FREE_LINE_MARKER = "wp2-import-free: sanctioned"


def _iter_function_body_imports(tree: ast.AST):
    """Yield (lineno, end_lineno, name) for every Import/ImportFrom whose
    nearest enclosing scope is a function/async function. Module-level
    imports (including inside an `if TYPE_CHECKING:` block) are NOT
    function-body and are fine -- INV-2 only bans imports that execute at
    CALL time. end_lineno is returned alongside lineno because a formatter
    (ruff) can wrap a single-line import with a trailing marker comment onto
    a multi-line parenthesized form, moving the marker off line `lineno`."""

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.depth = 0
            self.found: list[tuple[int, int, str]] = []

        def visit_FunctionDef(self, node):
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815 - ast.NodeVisitor dispatch name, can't rename

        def visit_Import(self, node):
            if self.depth > 0:
                for alias in node.names:
                    self.found.append((node.lineno, node.end_lineno or node.lineno, alias.name))
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            if self.depth > 0:
                self.found.append((node.lineno, node.end_lineno or node.lineno, node.module or "?"))
            self.generic_visit(node)

    visitor = _Visitor()
    visitor.visit(tree)
    return visitor.found


def test_tool_surface_has_no_function_body_imports_outside_sanctioned_files():
    """WP2-AC4 (INV-2): after handshake_yield, nothing a tool handler or the
    dispatch path executes may trigger import machinery -- no module under
    src/vibe_cognition/ may import ANYTHING inside a function body, outside
    the three sanctioned files above.

    Fixes the four known §2.3 sites (service_tools.py:87's
    `from .project_registry import LoadedProjects`, cognition_tools.py's
    `import json as _json`, dashboard/api.py's `starlette.concurrency`
    import, dashboard/server.py's `webbrowser`/`time` imports) by hoisting
    each to module level; this test is the mechanical guard against
    regression -- it would have caught all four before they were hoisted.
    """
    repo_root = Path(__file__).resolve().parents[1]
    src_root = repo_root / "src" / "vibe_cognition"
    sanctioned = {repo_root / p for p in _IMPORT_FREE_SANCTIONED_FILES}

    violations = []
    for py_file in sorted(src_root.rglob("*.py")):
        if py_file in sanctioned:
            continue
        source = py_file.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(py_file))
        for lineno, end_lineno, name in _iter_function_body_imports(tree):
            span = lines[lineno - 1 : end_lineno]
            if any(_IMPORT_FREE_LINE_MARKER in line for line in span):
                continue
            violations.append(f"{py_file.relative_to(repo_root)}:{lineno} imports {name!r}")

    assert not violations, (
        "function-body import found outside the sanctioned files -- this "
        "reintroduces the mode-(a) import-lock-collision risk class INV-2 "
        "eliminates:\n" + "\n".join(violations)
    )


def test_sanctioned_files_still_actually_contain_a_function_body_import():
    """Guard the guard: if any sanctioned file's function-body import is ever
    removed entirely (e.g. a refactor hoists it), the blanket exclusion above
    would silently stop covering anything in that file -- catch the exclusion
    going vacuous, not just the positive case."""
    repo_root = Path(__file__).resolve().parents[1]
    for rel_path in _IMPORT_FREE_SANCTIONED_FILES:
        py_file = repo_root / rel_path
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        assert _iter_function_body_imports(tree), (
            f"{rel_path} is sanctioned but no longer contains a function-body "
            "import -- update _IMPORT_FREE_SANCTIONED_FILES to drop it"
        )


# ── §W2-c: chroma pre-exercise before handshake_yield ───────────────────────


@pytest.mark.asyncio
async def test_lifespan_pre_exercises_chroma_count_and_get_before_yield(tmp_path, monkeypatch):
    """§W2-c: lifespan() calls count_documents() (bare + is_chunk-filtered,
    the same two calls get_status makes in production) against the ALREADY-
    OPENED home collection before yielding -- by the time a caller is inside
    the `async with lifespan(...)` block, both calls must already have run."""
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")

    calls: list[dict | None] = []
    real_count_documents = ChromaDBStorage.count_documents

    def _tracking_count_documents(self, filter=None):  # noqa: A002 - match real signature
        calls.append(filter)
        return real_count_documents(self, filter=filter)

    monkeypatch.setattr(ChromaDBStorage, "count_documents", _tracking_count_documents)

    async with lifespan(None) as context:  # type: ignore[arg-type]
        assert None in calls, "bare count_documents() was not pre-exercised"
        assert {"is_chunk": True} in calls, "is_chunk-filtered count_documents() was not pre-exercised"
        assert context["embedding_ready"] is not None  # sanity: context is real


@pytest.mark.asyncio
async def test_lifespan_survives_chroma_pre_exercise_failure(tmp_path, monkeypatch, caplog):
    """Best-effort: a failure in the pre-exercise call must never break
    startup -- the tool surface degrades on its own gates, not because a
    warm-up probe raised."""
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")

    def _boom(self, filter=None):  # noqa: A002 - match real signature
        raise RuntimeError("simulated chroma failure")

    monkeypatch.setattr(ChromaDBStorage, "count_documents", _boom)

    async with lifespan(None) as context:  # type: ignore[arg-type]
        assert context is not None  # lifespan must still yield normally


# ── §W2-f: dispatch-stall self-forensics ────────────────────────────────────


def _mw_context(lc: dict, tool_name: str = "slow_tool") -> MiddlewareContext:
    return MiddlewareContext(
        message=SimpleNamespace(name=tool_name),
        fastmcp_context=SimpleNamespace(lifespan_context=lc),
    )


def _loading_lc(threshold: float) -> dict:
    return {
        "embedding_ready": threading.Event(),  # unset -- the load window
        "embedding_error": None,
        "watchdog_fired": False,
        "config": SimpleNamespace(dispatch_stall_threshold=threshold),
    }


@pytest.mark.asyncio
async def test_stall_forensics_dumps_stack_when_call_exceeds_threshold_during_load(
    monkeypatch, capsys
):
    """§W2-f/WP2-AC2(i): a tool call that runs past the threshold DURING the
    load window gets an all-thread stack dump to stderr, and the real result
    is still returned once the call actually finishes (never cancelled)."""
    monkeypatch.setattr(_startup_timing, "_stamped_once", set())
    lc = _loading_lc(threshold=0.05)

    async def _slow_call_next(context):
        await asyncio.sleep(0.3)
        return "the real result"

    mw = _DispatchStallForensics()
    result = await mw.on_call_tool(_mw_context(lc), _slow_call_next)

    assert result == "the real result"
    err = capsys.readouterr().err
    assert "DISPATCH STALL" in err
    assert "slow_tool" in err


@pytest.mark.asyncio
async def test_stall_forensics_silent_when_call_finishes_within_threshold(monkeypatch, capsys):
    """Fails-before contrast: a call that returns promptly, even during the
    load window, must not trigger a stack dump -- only a genuine stall does."""
    monkeypatch.setattr(_startup_timing, "_stamped_once", set())
    lc = _loading_lc(threshold=5.0)

    async def _fast_call_next(context):
        return "fast"

    mw = _DispatchStallForensics()
    result = await mw.on_call_tool(_mw_context(lc), _fast_call_next)

    assert result == "fast"
    assert "DISPATCH STALL" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_stall_forensics_does_not_engage_when_healthy(monkeypatch, capsys):
    """§W2-f is scoped to the load window or a degraded state ONLY -- a
    healthy session (ready, no error, watchdog never fired) must never wrap
    dispatch in the stall-race at all, even for a genuinely slow tool call."""
    monkeypatch.setattr(_startup_timing, "_stamped_once", set())
    ready = threading.Event()
    ready.set()
    lc = {
        "embedding_ready": ready,
        "embedding_error": None,
        "watchdog_fired": False,
        "config": SimpleNamespace(dispatch_stall_threshold=0.01),
    }

    async def _slow_call_next(context):
        await asyncio.sleep(0.2)
        return "slow but healthy"

    mw = _DispatchStallForensics()
    result = await mw.on_call_tool(_mw_context(lc), _slow_call_next)

    assert result == "slow but healthy"
    assert "DISPATCH STALL" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_stall_forensics_engages_on_degraded_state_even_when_ready(monkeypatch, capsys):
    """The OTHER half of the scoping condition: embedding_ready IS set, but
    embedding_error is set (or watchdog_fired) -- degraded, not loading --
    must still engage the stall race."""
    monkeypatch.setattr(_startup_timing, "_stamped_once", set())
    ready = threading.Event()
    ready.set()
    lc = {
        "embedding_ready": ready,
        "embedding_error": "wedged",
        "watchdog_fired": False,
        "config": SimpleNamespace(dispatch_stall_threshold=0.05),
    }

    async def _slow_call_next(context):
        await asyncio.sleep(0.3)
        return "degraded result"

    mw = _DispatchStallForensics()
    result = await mw.on_call_tool(_mw_context(lc), _slow_call_next)

    assert result == "degraded result"
    assert "DISPATCH STALL" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_stall_forensics_dumps_at_most_once_per_process(monkeypatch, capsys):
    """Once-per-process bound: two independent stalls in the same process
    must produce exactly one dump, not one per stalled call."""
    monkeypatch.setattr(_startup_timing, "_stamped_once", set())
    lc = _loading_lc(threshold=0.05)

    async def _slow_call_next(context):
        await asyncio.sleep(0.3)
        return "result"

    mw = _DispatchStallForensics()
    await mw.on_call_tool(_mw_context(lc, "tool_a"), _slow_call_next)
    await mw.on_call_tool(_mw_context(lc, "tool_b"), _slow_call_next)

    err = capsys.readouterr().err
    assert err.count("DISPATCH STALL") == 1


# ── §W2-a repro wired in as WP2-AC2(ii) regression coverage ─────────────────


def test_import_lock_class_repro_passes_on_current_main(tmp_path):
    """WP2-AC2(ii): the §W2-a repro script (subprocess-isolated, real dispatch,
    real module name) passes on current main for the four cleared sites. It
    guards the plain-import-lock CLASS going forward -- it does NOT claim to
    reproduce Incident A (see the script's own module docstring for why: the
    §W2-a forensics finding was negative, and the likely real mechanism, the
    Windows OS loader lock, can't be reproduced from pure Python either way).

    Slow (~10-20s: a real subprocess importing sentence_transformers/torch/
    scipy fresh) -- this is inherent to the fidelity requirement, not
    incidental.
    """
    script = Path(__file__).parent / "wp2_mode_a_forensics.py"
    repo_dir = tmp_path / "wp2_repro_repo"

    result = subprocess.run(
        [sys.executable, str(script), str(repo_dir)],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"import-lock-class repro failed (stdout below) -- a function-body "
        f"import was reintroduced that collides with a bg-thread import lock:\n"
        f"{result.stdout}\n---stderr---\n{result.stderr}"
    )
    payload = json.loads(result.stdout)
    assert all(r["ok"] for r in payload), payload


# ── §W2-b: INV-1 spawn-free event loop (WP2-AC1, WP2-AC3) ───────────────────
#
# Real dispatch (fastmcp.Client in-memory transport against the real module-
# level `mcp` singleton), not mock_mcp -- per the brief's AC fidelity note.
# `mcp` is a process-wide singleton shared across the whole test session (its
# dispatch_executor and middleware are registered once at import time), so
# these tests reuse it directly rather than building a fresh server instance.


async def _call_all_tools(client: Client, *, bound: float = 10.0):
    async def _call(name, kwargs):
        return await asyncio.wait_for(client.call_tool(name, kwargs), timeout=bound)

    return await asyncio.gather(
        *(_call(name, kwargs) for name, kwargs in _TOOL_ARGS.items()),
        return_exceptions=True,
    )


def _failures(results) -> list[tuple[str, BaseException]]:
    return [
        (name, r)
        for (name, _kwargs), r in zip(_TOOL_ARGS.items(), results, strict=True)
        if isinstance(r, BaseException)
    ]


@pytest.mark.asyncio
async def test_ac1_every_tool_returns_within_bound_during_the_load_window(
    tmp_path, monkeypatch
):
    """WP2-AC1: with the load window held open (bg thread blocked inside
    EmbeddingGenerator.from_config, mirroring _load_embeddings_and_sync),
    EVERY registered tool returns within 10s via real dispatch.
    cognition_dashboard is patched (mirrors the existing WP-Wedge dispatch
    test) so the bound can't fail for an unrelated reason (a real browser/
    port bind)."""
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")

    release = threading.Event()

    def _slow_from_config(cfg):
        release.wait(timeout=30)
        return SimpleNamespace()

    monkeypatch.setattr(
        "vibe_cognition.server.EmbeddingGenerator.from_config", _slow_from_config
    )

    with patch(
        "vibe_cognition.tools.dashboard_tool.start_dashboard",
        return_value={
            "url": "http://127.0.0.1:7842/?token=fake",
            "status": "running",
            "embedding_ready": False,
            "embedding_error": None,
        },
    ):
        async with Client(mcp) as client:
            assert not mcp._lifespan_result["embedding_ready"].is_set(), (
                "embeddings already ready -- the load window wasn't held open"
            )
            results = await _call_all_tools(client)
    release.set()

    failures = _failures(results)
    assert not failures, f"tool(s) failed/hung during the load window: {failures}"


@pytest.mark.asyncio
async def test_ac1_every_tool_returns_within_bound_in_the_watchdog_fired_state(
    tmp_path, monkeypatch
):
    """WP2-AC1, the OTHER required state: once the watchdog has fired
    (embedding_error set, watchdog_fired True -- production hung specifically
    in this post-fire window per Incident A), every tool STILL returns within
    10s via real dispatch -- the degraded-serving contract, not just the
    loading contract."""
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")
    monkeypatch.setenv("WEDGE_WATCHDOG_TIMEOUT", "0.05")

    release = threading.Event()

    def _slow_from_config(cfg):
        release.wait(timeout=30)
        return SimpleNamespace()

    monkeypatch.setattr(
        "vibe_cognition.server.EmbeddingGenerator.from_config", _slow_from_config
    )

    with patch(
        "vibe_cognition.tools.dashboard_tool.start_dashboard",
        return_value={
            "url": "http://127.0.0.1:7842/?token=fake",
            "status": "running",
            "embedding_ready": False,
            "embedding_error": None,
        },
    ):
        async with Client(mcp) as client:
            lc = mcp._lifespan_result
            for _ in range(500):
                if lc.get("watchdog_fired") or lc.get("embedding_error"):
                    break
                await asyncio.sleep(0.02)
            assert lc.get("watchdog_fired") or lc.get(
                "embedding_error"
            ), "watchdog never fired -- test setup invalid"

            results = await _call_all_tools(client)
    release.set()

    failures = _failures(results)
    assert not failures, f"tool(s) failed/hung in the watchdog-fired state: {failures}"


@pytest.mark.asyncio
async def test_ac3_dispatch_storm_causes_zero_thread_start_from_loop_thread(
    tmp_path, monkeypatch
):
    """WP2-AC3 (INV-1's core claim): once the dispatch executor is pre-warmed
    (happens during real Client connection, same as production lifespan),
    a storm of >= 2x pool-capacity concurrent tool calls causes ZERO
    Thread.start() calls from the event-loop thread, and every call
    completes. Instruments Thread.start() and records the CALLING thread
    (a bare thread census is contaminated by legitimate test-side threads --
    only loop-thread spawns matter for INV-1).

    Honest disclosure (gate finding, MINOR): ``Client(mcp)`` is FastMCP's
    IN-MEMORY ``FastMCPTransport`` (anyio memory streams) -- it never touches
    the real stdio transport's ``anyio.wrap_file``/``AsyncFile`` read/write
    path, so this test proves the DISPATCH half of INV-1's zero-spawn claim
    only. The transport-WRITE half (every MCP response write needing an
    anyio worker) has zero coverage in this harness; it stays covered ONLY by
    WP-Wedge v1's retained, unchanged warm-pool/heartbeat keeping anyio's
    pool warm, not by a zero-spawn proof here.
    """
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")

    loop_thread = threading.current_thread()
    spawned_from_loop: list[str] = []
    real_start = threading.Thread.start

    def _tracking_start(self):
        if threading.current_thread() is loop_thread:
            spawned_from_loop.append(self.name)
        return real_start(self)

    with patch(
        "vibe_cognition.tools.dashboard_tool.start_dashboard",
        return_value={
            "url": "http://127.0.0.1:7842/?token=fake",
            "status": "running",
            "embedding_ready": True,
            "embedding_error": None,
        },
    ):
        async with Client(mcp) as client:
            lc = mcp._lifespan_result
            for _ in range(500):
                if lc["embedding_ready"].is_set():
                    break
                await asyncio.sleep(0.02)
            assert lc["embedding_ready"].is_set(), "never became ready -- test setup invalid"

            # Pre-warming already happened during connection -- NOW install the
            # tracker, so only storm-time spawns count (matches AC3's framing:
            # the invariant holds post-prewarm, under load).
            monkeypatch.setattr(threading.Thread, "start", _tracking_start)

            pool_capacity = 4  # tools/dispatch.py: _DISPATCH_POOL_SIZE
            storm_size = pool_capacity * 3  # >= 2x capacity
            calls = [
                asyncio.wait_for(client.call_tool("get_status", {}), timeout=10)
                for _ in range(storm_size)
            ]
            results = await asyncio.gather(*calls, return_exceptions=True)

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"call(s) failed/hung during the storm: {failures}"
    assert spawned_from_loop == [], (
        f"{len(spawned_from_loop)} Thread.start() call(s) from the event-loop "
        f"thread during the storm: {spawned_from_loop}"
    )


# ── §W2-b: tools/dispatch.py primitives (unit-level, below the AC tests) ────


@pytest.mark.asyncio
async def test_prewarm_dispatch_executor_creates_exactly_pool_size_threads():
    """§W2-b: prewarm forces _DISPATCH_POOL_SIZE distinct threads to exist --
    the property INV-1's zero-spawn guarantee depends on. Uses the REAL
    module-level dispatch_executor (shared/reused across the whole test
    session, same as `mcp`); idempotent to call more than once, so running
    after other tests have already warmed it is fine."""
    await prewarm_dispatch_executor(_DISPATCH_POOL_SIZE)
    assert len(dispatch_executor._threads) == _DISPATCH_POOL_SIZE


@pytest.mark.asyncio
async def test_dispatch_beyond_capacity_after_prewarm_spawns_no_new_threads():
    """§W2-b/WP2-AC3 core mechanism, isolated from the fastmcp/Client
    machinery: once prewarmed, submitting MORE work than _DISPATCH_POOL_SIZE
    queues against the existing threads rather than spawning new ones."""
    await prewarm_dispatch_executor(_DISPATCH_POOL_SIZE)
    threads_before = set(dispatch_executor._threads)

    loop = asyncio.get_running_loop()
    await asyncio.gather(
        *(
            loop.run_in_executor(dispatch_executor, lambda: None)
            for _ in range(_DISPATCH_POOL_SIZE * 3)
        )
    )

    assert dispatch_executor._threads == threads_before


@pytest.mark.asyncio
async def test_dispatch_tool_propagates_contextvars_into_the_executor_thread():
    """§W2-b regression guard: FastMCP's Context.request_context reads a
    contextvars.ContextVar bound on the calling (event-loop) task. Plain
    ThreadPoolExecutor.submit() does NOT propagate contextvars to its worker
    threads (unlike anyio.to_thread.run_sync, which does) -- without the
    explicit contextvars.copy_context().run(...) in dispatch_tool, a sync
    tool body would see a DIFFERENT (default/empty) context value on the
    executor thread. Proven directly with a real ContextVar, not through the
    heavier fastmcp Context machinery.

    Fails-before: this exact bug was hit live (§W2-a repro's first run raised
    `RuntimeError: no request context`) before the copy_context().run() fix
    landed in dispatch_tool.
    """
    import contextvars

    from vibe_cognition.tools.dispatch import dispatch_executor as _executor

    probe_var: contextvars.ContextVar[str] = contextvars.ContextVar("probe_var")
    token = probe_var.set("expected-value")
    try:
        seen = {}

        def _sync_body():
            seen["value"] = probe_var.get(None)

        # Mirrors dispatch_tool's _async_dispatch body directly (not going
        # through mcp.tool() registration -- this isolates the propagation
        # mechanism from FastMCP's own schema/DI layer).
        call_context = contextvars.copy_context()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor, call_context.run, _sync_body)
    finally:
        probe_var.reset(token)

    assert seen["value"] == "expected-value"


def test_dispatch_tool_preserves_signature_and_docstring_for_schema_introspection():
    """§W2-b: dispatch_tool must be a transparent drop-in for @mcp.tool() --
    FastMCP builds its argument schema and Context dependency-injection from
    the function's signature at registration time, so functools.wraps
    preserving __wrapped__ (which inspect.signature follows by default) is
    load-bearing, not cosmetic."""
    import inspect

    class _RecordingMcp:
        """Captures whatever function mcp.tool() receives, without
        depending on real FastMCP internals (mirrors conftest's _MockMcp)."""

        def __init__(self):
            self.registered = None

        def tool(self):
            def decorator(fn):
                self.registered = fn
                return fn

            return decorator

    recorder = _RecordingMcp()

    def original(ctx, node_id: str, project: str | None = None) -> dict:
        """Original docstring."""
        return {}

    wrapped = dispatch_tool(recorder)(original)

    assert recorder.registered is wrapped
    assert inspect.signature(wrapped) == inspect.signature(original)
    assert wrapped.__doc__ == "Original docstring."
    assert wrapped.__name__ == "original"


def test_dispatch_tool_rejects_an_already_async_function():
    """§W2-b gate finding (MINOR): dispatch_tool must fail LOUD at
    registration time if handed an async def -- run_in_executor would call it
    on a worker thread, get back an un-awaited coroutine object, and hand
    THAT to the executor future as the tool's "result" silently. Every
    dispatch_tool-wrapped function must be a plain sync function."""

    class _RecordingMcp:
        def tool(self):
            def decorator(fn):
                return fn

            return decorator

    async def already_async(ctx):
        return {}

    with pytest.raises(TypeError, match="already an async def"):
        dispatch_tool(_RecordingMcp())(already_async)
