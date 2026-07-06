"""WP-Wedge-2 (P0, docs/wp-wedge2-plan.md rev 4) §W2-b: INV-1's dispatch seam.

FastMCP's own sync-tool dispatch (``call_sync_fn_in_threadpool`` ->
``anyio.to_thread.run_sync``) spawns a fresh ``WorkerThread`` via
``Thread.start()`` SYNCHRONOUSLY ON THE EVENT-LOOP THREAD whenever anyio's
warm pool has no idle worker available. Under an in-process import wedge
holding the Windows loader lock, that ``Thread.start()`` blocks at
thread-attach and never returns -- freezing the loop itself (Incident B,
photographed via py-spy). WP-Wedge v1's warm-pool/heartbeat narrowed this
window but cannot close it (any instant the pool is saturated forces a
spawn).

The fix (first-party, no fastmcp fork, no monkeypatching of anyio/fastmcp/mcp
internals): route every registered tool through a DEDICATED, pre-started
``concurrent.futures.ThreadPoolExecutor`` instead of anyio's spawn-on-demand
pool. Register with ``@dispatch_tool(mcp)`` exactly where ``@mcp.tool()`` was
used -- it wraps the sync function as an ``async def`` dispatcher, preserving
the original signature/docstring (``functools.wraps``) so FastMCP's schema
introspection and Context dependency-injection are unaffected; FastMCP sees a
coroutine function and awaits it directly on the loop (no anyio threadpool
involvement at all for tool bodies once this is in place).

Why a dedicated TPE closes the wedge window that anyio's pool cannot: once
``prewarm_dispatch_executor()`` has forced exactly ``max_workers`` threads to
exist (called pre-``handshake_yield``, while spawning is still safe -- same
principle as WP-Wedge's existing warm-pool step), ``ThreadPoolExecutor``
NEVER reclaims idle workers (no ``MAX_IDLE_TIME`` unlike anyio's pool) and
``submit()``/``run_in_executor()`` beyond ``max_workers`` QUEUES the work item
against the existing threads rather than spawning a new one. So after
pre-warming, dispatch can NEVER call ``Thread.start()`` on the loop again for
the lifetime of the process, regardless of load -- WP2-AC3's zero-spawn
invariant.

Does NOT touch the stdio TRANSPORT's own ``to_thread`` usage (``mcp/server/
stdio.py`` wraps stdin/stdout via ``anyio.wrap_file``, whose ``AsyncFile``
routes every readline/write/flush through anyio's pool, independent of tool
dispatch) -- that path stays covered by WP-Wedge v1's UNCHANGED
``_worker_heartbeat``/``_warm_worker_batch`` (4 warm anyio workers, more than
the 2 the reader+writer need). Removing that machinery would be a regression
per the brief; it is intentionally left in place.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

_DISPATCH_POOL_SIZE = 4

# Module-level singleton, same lifetime as the module-level `mcp` server
# object itself (server.py) -- constructing a ThreadPoolExecutor does NOT
# eagerly spawn any OS thread (CPython creates worker threads lazily inside
# submit()), so this is safe at import time; prewarm_dispatch_executor()
# below is what actually forces all _DISPATCH_POOL_SIZE threads to exist.
dispatch_executor = ThreadPoolExecutor(
    max_workers=_DISPATCH_POOL_SIZE, thread_name_prefix="vibe-dispatch"
)

F = TypeVar("F", bound=Callable[..., Any])


async def prewarm_dispatch_executor(count: int = _DISPATCH_POOL_SIZE) -> None:
    """Force exactly ``count`` dispatch-executor threads to exist, all
    concurrently. A naive ``count`` no-op submissions is NOT reliable:
    ``ThreadPoolExecutor._adjust_thread_count`` skips spawning a new thread
    whenever an idle one is already available, and a near-instant no-op can
    free its thread before the next submission lands -- under-provisioning
    silently. A ``threading.Barrier(count)`` forces every submitted callable
    to block until all ``count`` have started, which keeps every thread
    "busy" (never idle) for the whole warm-up, guaranteeing the idle-thread
    fast path never fires and each submission spawns a genuinely new thread
    (mirrors WP-Wedge's ``_warm_worker_batch`` for anyio's pool, adapted for
    a real rendezvous since a TPE has no anyio-style concurrent-gather
    guarantee). Call pre-``handshake_yield``, while spawning is still
    known-safe.
    """
    barrier = threading.Barrier(count)

    def _hold() -> None:
        barrier.wait(timeout=10)

    loop = asyncio.get_running_loop()
    futures = [loop.run_in_executor(dispatch_executor, _hold) for _ in range(count)]
    await asyncio.gather(*futures)


def dispatch_tool(mcp) -> Callable[[F], F]:
    """Drop-in replacement for ``@mcp.tool()``: registers the SAME tool, but
    the decorated sync function is wrapped as an async dispatcher routing to
    ``dispatch_executor`` via ``run_in_executor`` instead of relying on
    FastMCP's anyio-backed ``call_sync_fn_in_threadpool``. See the module
    docstring for why this closes INV-1's wedge window."""
    tool_decorator = mcp.tool()

    def _decorator(fn: F) -> F:
        if asyncio.iscoroutinefunction(fn):
            # run_in_executor would call fn(*args, **kwargs) on a worker
            # thread, get back a coroutine object (never run), and hand THAT
            # to the executor's future as the "result" -- silently wrong,
            # not an error, and easy to miss in review. Fail loud at
            # registration time instead: every tool this decorator wraps
            # must be a plain sync function.
            raise TypeError(
                f"dispatch_tool cannot wrap {fn.__name__!r}: it is already an "
                "async def. dispatch_tool routes sync tool bodies to the "
                "dedicated executor via run_in_executor -- an async function "
                "would return an un-awaited coroutine as its 'result'. "
                "Register async tools with @mcp.tool() directly instead."
            )

        @functools.wraps(fn)
        async def _async_dispatch(*args: Any, **kwargs: Any) -> Any:
            loop = asyncio.get_running_loop()
            # FastMCP's Context.request_context reads a contextvars.ContextVar
            # set on the calling (event-loop) task. anyio.to_thread.run_sync
            # (what this replaces) propagates that context into its worker
            # thread automatically; a plain ThreadPoolExecutor.submit() does
            # NOT -- the tool would see request_context as None ("no request
            # context") on every call. Capture the context here (loop side,
            # correct value) and run the tool body through it on the worker
            # thread via Context.run, restoring the propagation anyio gave us
            # for free.
            call_context = contextvars.copy_context()
            return await loop.run_in_executor(
                dispatch_executor,
                functools.partial(call_context.run, fn, *args, **kwargs),
            )

        return tool_decorator(_async_dispatch)

    return _decorator
