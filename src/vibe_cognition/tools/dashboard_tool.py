"""MCP tool: launch the cognition dashboard."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import Context

from ..dashboard.server import start_dashboard

logger = logging.getLogger(__name__)


def register_dashboard_tool(mcp) -> None:
    """Register the cognition_dashboard tool with the MCP server."""

    @mcp.tool()
    def cognition_dashboard(
        ctx: Context,
        port: int = 7842,
        open_browser: bool = True,
    ) -> dict[str, Any]:
        """Launch the local cognition graph dashboard in a web browser.

        The dashboard shows an interactive graph of all nodes and edges, plus
        a semantic search bar backed by the embedding index. It runs on
        127.0.0.1 with a token-protected URL — calling this tool again returns
        the same URL instead of starting a second server.

        Args:
            port: Preferred port (default 7842). Falls back to nearby ports
                  or an OS-assigned ephemeral port if busy.
            open_browser: If true, attempt to open the URL in the default browser.

        Returns:
            Dict with `url`, `status` ("running" or "already_running"),
            `embedding_ready` (bool), and `embedding_error` (string or null).
        """
        lc = ctx.request_context.lifespan_context
        return start_dashboard(lc, port=port, open_browser=open_browser)
