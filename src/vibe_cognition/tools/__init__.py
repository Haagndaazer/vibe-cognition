"""MCP tools for the Vibe Cognition server."""

from .cognition_tools import register_cognition_tools
from .dashboard_tool import register_dashboard_tool
from .service_tools import register_service_tools


def register_all_tools(mcp) -> None:
    """Register all MCP tools with the server.

    Args:
        mcp: FastMCP server instance
    """
    register_cognition_tools(mcp)
    register_service_tools(mcp)
    register_dashboard_tool(mcp)


__all__ = [
    "register_all_tools",
    "register_cognition_tools",
    "register_dashboard_tool",
    "register_service_tools",
]
