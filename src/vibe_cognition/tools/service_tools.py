"""MCP service tools for Vibe Cognition server status."""

import logging
from typing import Any

from fastmcp import Context

from .utils import get_lifespan

logger = logging.getLogger(__name__)


def register_service_tools(mcp) -> None:
    """Register service tools with the MCP server.

    Args:
        mcp: FastMCP server instance
    """

    @mcp.tool()
    def get_status(ctx: Context) -> dict[str, Any]:
        """Get the current status of the Vibe Cognition server.

        Returns cognition graph statistics and embedding model status.

        Returns:
            Server status including graph stats and embedding readiness
        """
        lc = get_lifespan(ctx)
        config = lc.get("config")
        cognition_storage = lc.get("cognition_storage")
        cognition_embedding_storage = lc.get("cognition_embedding_storage")
        from .project_registry import LoadedProjects
        registry: LoadedProjects | None = lc.get("loaded_projects")

        result: dict[str, Any] = {
            "repo_name": config.effective_repo_name if config else "unknown",
            "repo_path": str(config.repo_path) if config else "unknown",
        }

        # Cognition graph stats
        if cognition_storage:
            result["cognition_graph"] = cognition_storage.get_statistics()
        else:
            result["cognition_graph"] = {"error": "not initialized"}

        # Cognition embedding count — split node vectors vs document chunk vectors
        # (WP-D2: chunks carry is_chunk=True; don't let them silently inflate the
        # single count). The public param is filter=, not where= (it maps to the
        # internal _collection.get(where=)).
        if cognition_embedding_storage:
            try:
                total = cognition_embedding_storage.count_documents()
                chunks = cognition_embedding_storage.count_documents(filter={"is_chunk": True})
                result["cognition_embeddings"] = {
                    "nodes": total - chunks,
                    "chunks": chunks,
                    "total": total,
                }
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

        # Foreign project count (XP1)
        if registry is not None:
            result["loaded_foreign_projects"] = registry.foreign_count()

        # Curation is agent-driven: only deterministic part_of edges are automatic;
        # semantic edges are created by the agent via the /vibe-curate skill.
        result["curation"] = "agent-driven via /vibe-curate (no background curator)"

        return result
