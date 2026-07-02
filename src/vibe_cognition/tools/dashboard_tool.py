"""MCP tool: launch the cognition dashboard."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import Context

from ..dashboard.server import DEFAULT_PORT, start_dashboard
from .utils import get_lifespan

logger = logging.getLogger(__name__)


def register_dashboard_tool(mcp) -> None:
    """Register the cognition_dashboard tool with the MCP server."""

    @mcp.tool()
    def cognition_dashboard(
        ctx: Context,
        port: int = DEFAULT_PORT,
        open_browser: bool = True,
    ) -> dict[str, Any]:
        """Launch the local cognition graph dashboard in a web browser.

        The dashboard shows an interactive graph of all nodes and edges, plus
        a semantic search bar backed by the embedding index. It runs on
        127.0.0.1 with a token-protected URL — calling this tool again returns
        the same URL instead of starting a second server.

        Args:
            port: Preferred port. Falls back to nearby ports
                  or an OS-assigned ephemeral port if busy.
            open_browser: If true, attempt to open the URL in the default browser.

        Returns:
            Dict with `url`, `status`, `embedding_ready` (bool), and
            `embedding_error` (string or null). `status` is "running" (first
            call) or "already_running" (a subsequent call, same session —
            `url` is the SAME URL as before). On a bind failure (e.g. no free
            port found), `status` is "failed" instead: `url` is None and an
            `error` key (string) is added explaining what happened.
        """
        lc = get_lifespan(ctx)
        return start_dashboard(lc, port=port, open_browser=open_browser)
