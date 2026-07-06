"""Shared utilities for MCP tools."""

from typing import Any

from fastmcp import Context

from .. import _startup_timing


def get_lifespan(ctx: Context) -> dict[str, Any]:
    """Return the request's lifespan context, narrowing fastmcp's Optional typing
    (it types ``request_context`` as ``... | None``; inside a live tool call it is
    never None). Use at tool entry instead of ``ctx.request_context.lifespan_context``.

    WP-Wedge-2 §W2-e: every tool calls this first, making it the natural choke
    point for the ``tool_served_degraded`` breadcrumb -- first occurrence per
    process, when dispatch reaches a tool while the session is in a degraded
    state (``embedding_error`` set, or the watchdog already fired). Reaching
    this line already proves dispatch itself isn't hung, which is exactly the
    "degraded but serving" signal fleet logs need to distinguish from a real
    hang. ``stamp_once`` never touches disk (worker-thread-context safe); it
    rides the next bg-thread flush.
    """
    rc = ctx.request_context
    if rc is None:  # pragma: no cover - defensive; not reachable inside a tool call
        raise RuntimeError("no request context")
    lc = rc.lifespan_context
    if lc.get("embedding_error") or lc.get("watchdog_fired"):
        _startup_timing.stamp_once("tool_served_degraded")
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
