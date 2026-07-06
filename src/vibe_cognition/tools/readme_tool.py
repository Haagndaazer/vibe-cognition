"""MCP tool: serve the vibe-cognition orientation guide."""

from __future__ import annotations

from fastmcp import Context

from ..cognition.readme import COGNITION_GETTING_STARTED, COGNITION_GUIDE
from .dispatch import dispatch_tool


def cognition_readme_core() -> dict[str, str]:
    """Pure core for cognition_readme (no MCP context required)."""
    return {"guide": COGNITION_GUIDE, "getting_started": COGNITION_GETTING_STARTED}


def register_readme_tool(mcp) -> None:
    """Register the cognition_readme tool with the MCP server."""

    @dispatch_tool(mcp)
    def cognition_readme(ctx: Context) -> dict[str, str]:
        """Return the vibe-cognition orientation guide and getting-started procedure.

        Pull surface complementing the always-pushed instructions.py standing
        practices. Call this when dropped into an unfamiliar project, when the
        graph is empty and you need to explain vibe-cognition to the user, or
        any time you want a structured reference for the tool groups, node/edge
        types, and when-to-record triggers.

        No `project` arg: this tool serves orientation for THIS project's server.
        For a loaded foreign project, guidance comes from that project's own
        vibe-cognition server.

        Args:
            ctx: MCP context (injected automatically).

        Returns:
            {
              guide: Full markdown orientation -- what vibe-cognition is, the
                     record->curate core loop, all tool groups, node and edge
                     types, when-to-record triggers, and cross-project usage.
              getting_started: Short act-now procedure for a project with no
                               cognition history yet (safe to call on any project).
            }
        """
        return cognition_readme_core()
