"""MCP service tools for Vibe Cognition server status."""

import logging
from typing import Any

from fastmcp import Context

logger = logging.getLogger(__name__)


def register_service_tools(mcp) -> None:
    """Register service tools with the MCP server.

    Args:
        mcp: FastMCP server instance
    """

    @mcp.tool()
    def get_status(ctx: Context) -> dict[str, Any]:
        """Get the current status of the Vibe Cognition server.

        Returns cognition graph statistics, embedding model status,
        and curator status.

        Returns:
            Server status including graph stats, embedding readiness, and curator info
        """
        lc = ctx.request_context.lifespan_context
        config = lc.get("config")
        cognition_storage = lc.get("cognition_storage")
        cognition_embedding_storage = lc.get("cognition_embedding_storage")

        result: dict[str, Any] = {
            "repo_name": config.effective_repo_name if config else "unknown",
            "repo_path": str(config.repo_path) if config else "unknown",
        }

        # Cognition graph stats
        if cognition_storage:
            result["cognition_graph"] = cognition_storage.get_statistics()
        else:
            result["cognition_graph"] = {"error": "not initialized"}

        # Cognition embedding count
        if cognition_embedding_storage:
            try:
                result["cognition_embeddings"] = cognition_embedding_storage.count_documents()
            except Exception as e:
                result["cognition_embeddings"] = {"error": str(e)}
        else:
            result["cognition_embeddings"] = 0

        # Embedding model status
        embedding_ready = lc.get("embedding_ready")
        embedding_error = lc.get("embedding_error")
        if embedding_error:
            result["embedding_status"] = f"error: {embedding_error}"
        elif embedding_ready and embedding_ready.is_set():
            result["embedding_status"] = "ready"
        else:
            result["embedding_status"] = "loading"

        # Curator status
        curator = lc.get("cognition_curator")
        result["curator"] = {
            "enabled": config.curator_enabled if config else False,
            "model": config.curator_model if config else "unknown",
            "active": curator is not None,
        }

        return result
