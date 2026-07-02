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

        Returns cognition graph statistics, embedding model status, journal
        rehydrate-reset events, and cross-project registry state.

        Returns:
            {
              repo_name: str,
              repo_path: str,
              cognition_graph: {nodes, edges, <count per node type>,
                                edge_<count per edge type>, uncurated}
                               (or {"error": ...} if storage is not initialized),
              rehydrate_events: null when no journal rehydrate-reset has occurred
                                in this server process; otherwise
                                {count: int, last: {at, nodes_before, nodes_after,
                                nodes_lost, sample_missing_ids}}. A rehydrate-reset
                                means the journal shrank or was replaced under the
                                live server and the in-memory graph was rebuilt
                                from disk; nodes_lost is computed by NODE IDENTITY
                                (nodes present before the reset but absent after),
                                not by count — a replacement journal can have MORE
                                total nodes while still having dropped one of
                                ours. sample_missing_ids is up to 5 of the lost
                                node ids, for diagnosis,
              cognition_embeddings: {nodes, chunks, total}
                                    (or {"error": ...}; 0 if uninitialized),
              embedding_status: "ready" | "loading" | "error: <detail>",
              loaded_foreign_projects: int,   # count of loaded foreign projects
                                              # (use cognition_list_projects for
                                              # details; absent if no registry)
              curation: str,  # reminder: curation is agent-driven via /vibe-curate
            }
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

        # Journal rehydrate-reset events (WP-1 loss visibility): null when none
        # occurred in this process; otherwise the count plus the last event's
        # before/after node delta so a silent in-memory loss is diagnosable.
        if cognition_storage and cognition_storage.last_rehydrate is not None:
            result["rehydrate_events"] = {
                "count": cognition_storage.rehydrate_count,
                "last": cognition_storage.last_rehydrate,
            }
        else:
            result["rehydrate_events"] = None

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
