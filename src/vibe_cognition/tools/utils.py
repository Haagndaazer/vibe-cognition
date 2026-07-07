"""Shared utilities for MCP tools."""

from typing import Any

from fastmcp import Context

from .. import _heavy_import_guard, _startup_timing


def get_lifespan(ctx: Context) -> dict[str, Any]:
    """Return the request's lifespan context, narrowing fastmcp's Optional typing
    (it types ``request_context`` as ``... | None``; inside a live tool call it is
    never None). Use at tool entry instead of ``ctx.request_context.lifespan_context``.

    WP-Wedge-2 §W2-e: every tool calls this first, making it the natural choke
    point for the ``tool_served_degraded`` breadcrumb -- first occurrence per
    process, when dispatch reaches a tool while the session is in a degraded
    state (``embedding_error`` set). Reaching this line already proves
    dispatch itself isn't hung, which is exactly the "degraded but serving"
    signal fleet logs need to distinguish from a real hang. ``stamp_once``
    never touches disk (worker-thread-context safe); it rides the next
    bg-thread flush.

    WP-Sidecar §S-c: same choke point doubles as the "first tool call"
    moment for the runtime heavy-import invariant check (first occurrence
    per process -- a sys.modules scan is cheap, but there is no reason to
    repeat it on every single dispatch).
    """
    rc = ctx.request_context
    if rc is None:  # pragma: no cover - defensive; not reachable inside a tool call
        raise RuntimeError("no request context")
    lc = rc.lifespan_context
    if lc.get("embedding_error"):
        _startup_timing.stamp_once("tool_served_degraded")
        supervisor = lc.get("_sidecar_supervisor")
        if supervisor is not None:
            # WP-Sidecar: dispatch reaching a tool call while degraded IS an
            # embedding demand arriving -- poke the recovery loop's lazy-
            # on-demand leg rather than waiting for its next periodic tick.
            supervisor.notify_demand()
    if _startup_timing.first_occurrence("heavy_import_guard_checked_first_tool_call"):
        _heavy_import_guard.check_and_log("first_tool_call")
    return lc


def require_embeddings(ctx: Context) -> dict[str, Any] | None:
    """Check if the embedding model is loaded. Returns error dict if not ready, None if ready."""
    lc = get_lifespan(ctx)
    event = lc.get("embedding_ready")
    if event is None or not event.is_set():
        return {
            "error": "Embedding model is still loading. Graph and cognition history "
                     "tools are available now. Try again in a few seconds.",
            "status": "loading_embeddings",
        }
    error = lc.get("embedding_error")
    if error:
        # notify_demand() already fired inside get_lifespan() above (the
        # universal per-tool-call choke point) -- no need to repeat it here.
        return {"error": f"Embedding model failed to load: {error}", "status": "embedding_error"}
    if lc.get("embedding_generator") is None:
        # WP-Wedge state contract (AC4): ready set + no error but the generator not
        # yet installed is the watchdog-fired-but-not-yet-late-recovered tuple — read
        # as not-ready, never as a green light with nothing to embed with.
        return {
            "error": "Embedding model is still loading. Graph and cognition history "
                     "tools are available now. Try again in a few seconds.",
            "status": "loading_embeddings",
        }
    return None
