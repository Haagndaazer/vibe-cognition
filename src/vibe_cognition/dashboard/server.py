"""Dashboard HTTP server — Starlette + uvicorn."""

from __future__ import annotations

import logging
import secrets
import socket
import threading
from contextlib import ExitStack
from importlib import resources
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .api import (
    delete_node,
    get_graph,
    get_node,
    get_stats,
    search,
)
from .middleware import build_middleware

logger = logging.getLogger(__name__)


def _resolve_static_dir(stack: ExitStack) -> Path:
    """Resolve the packaged static directory to a real filesystem path.

    Uses ExitStack so the importlib.resources context stays alive
    for the lifetime of the caller (typically the whole server run).
    """
    traversable = resources.files("vibe_cognition.dashboard") / "static"
    return stack.enter_context(resources.as_file(traversable))


def build_app(lifespan_ctx: dict[str, Any], token: str) -> tuple[Starlette, ExitStack]:
    """Build the Starlette app for the dashboard.

    Returns the app plus an ExitStack that holds the resolved static-files
    context — the caller must keep it alive (call .close() at shutdown).
    """
    stack = ExitStack()
    static_dir = _resolve_static_dir(stack)

    def index(request):
        return FileResponse(static_dir / "index.html")

    routes = [
        Route("/", endpoint=index, methods=["GET"]),
        Route("/api/graph", endpoint=get_graph, methods=["GET"]),
        Route("/api/node/{node_id}", endpoint=get_node, methods=["GET"]),
        Route("/api/node/{node_id}", endpoint=delete_node, methods=["DELETE"]),
        Route("/api/search", endpoint=search, methods=["POST"]),
        Route("/api/stats", endpoint=get_stats, methods=["GET"]),
        Mount("/static", app=StaticFiles(directory=static_dir), name="static"),
    ]

    app = Starlette(
        routes=routes,
        middleware=build_middleware(token),
    )
    app.state.lifespan_ctx = lifespan_ctx
    app.state.token = token
    return app, stack


def _find_free_port(preferred: int, fallback_range: int = 10) -> int:
    """Try the preferred port, scan +1..+fallback_range, then ephemeral."""
    for port in range(preferred, preferred + fallback_range + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_url(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/?token={token}"


def run_dashboard_blocking(
    lifespan_ctx: dict[str, Any],
    port: int = 7842,
    open_browser: bool = True,
) -> None:
    """Run the dashboard in the foreground (CLI/dev path).

    Blocks until Ctrl-C. Signal handlers work normally because we run
    on the main thread.
    """
    import webbrowser

    token = secrets.token_urlsafe(32)
    chosen_port = _find_free_port(port)
    app, stack = build_app(lifespan_ctx, token)

    url = _make_url(chosen_port, token)
    logger.info(f"Dashboard listening at {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=chosen_port,
            log_level="warning",
            lifespan="off",
        )
    finally:
        stack.close()


def start_dashboard(
    lifespan_ctx: dict[str, Any],
    port: int = 7842,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Launch the dashboard in a background daemon thread (MCP path).

    Idempotent — repeat calls return the existing URL.
    Stores `{thread, server, url, token, stack}` in lifespan_ctx["dashboard"].
    """
    import webbrowser

    existing = lifespan_ctx.get("dashboard")
    if existing:
        return {
            "url": existing["url"],
            "status": "already_running",
            "embedding_ready": _embedding_ready(lifespan_ctx),
            "embedding_error": lifespan_ctx.get("embedding_error"),
        }

    token = secrets.token_urlsafe(32)
    chosen_port = _find_free_port(port)
    app, stack = build_app(lifespan_ctx, token)
    url = _make_url(chosen_port, token)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=chosen_port,
        log_level="warning",
        lifespan="off",
        log_config=None,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # signal.signal() crashes off-main-thread

    thread = threading.Thread(target=server.run, daemon=True, name="dashboard-uvicorn")
    thread.start()

    lifespan_ctx["dashboard"] = {
        "thread": thread,
        "server": server,
        "url": url,
        "token": token,
        "port": chosen_port,
        "stack": stack,
    }

    logger.info(f"Dashboard launched at {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    return {
        "url": url,
        "status": "running",
        "embedding_ready": _embedding_ready(lifespan_ctx),
        "embedding_error": lifespan_ctx.get("embedding_error"),
    }


def stop_dashboard(lifespan_ctx: dict[str, Any], join_timeout: float = 3.0) -> None:
    """Stop a running dashboard server (called from MCP lifespan cleanup)."""
    state = lifespan_ctx.pop("dashboard", None)
    if not state:
        return
    state["server"].should_exit = True
    state["thread"].join(timeout=join_timeout)
    state["stack"].close()


def _embedding_ready(lifespan_ctx: dict[str, Any]) -> bool:
    event = lifespan_ctx.get("embedding_ready")
    return bool(
        event
        and event.is_set()
        and lifespan_ctx.get("embedding_generator") is not None
        and not lifespan_ctx.get("embedding_error")
    )
