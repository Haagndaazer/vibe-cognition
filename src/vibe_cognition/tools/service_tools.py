"""MCP service tools for Vibe Cognition server status."""

import logging
import threading
from pathlib import Path
from typing import Any

from fastmcp import Context

logger = logging.getLogger(__name__)


def _looks_like_plugin_cache(path: Path) -> bool:
    """Heuristic: does this path look like a Claude Code plugin cache dir?"""
    parts = path.parts
    return ".claude" in parts and ("plugins" in parts or "cache" in parts)


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

        # Warn if repo_path looks like plugin cache
        if config and _looks_like_plugin_cache(config.repo_path):
            result["warning"] = (
                "repo_path points to plugin cache — call cognition_set_project "
                "with the project directory"
            )

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

    @mcp.tool()
    def cognition_set_project(ctx: Context, project_dir: str) -> dict[str, Any]:
        """Set the project directory for cognition data storage.

        Must be called before using other cognition tools when running as a
        plugin, so that .cognition/ data is stored in the correct project
        directory instead of the plugin cache.

        Args:
            project_dir: Absolute path to the project directory

        Returns:
            Status with the new repo_path, repo_name, and cognition_dir
        """
        from ..cognition import CognitionStorage
        from ..cognition.curator import CognitionCurator
        from ..config import Settings
        from ..embeddings import ChromaDBStorage

        lc = ctx.request_context.lifespan_context
        config = lc.get("config")

        # Convert MSYS/Git Bash paths (/c/Users/...) to Windows (C:/Users/...)
        import re
        if len(project_dir) >= 3 and project_dir[0] == "/" and project_dir[2] == "/":
            project_dir = re.sub(
                r"^/([a-zA-Z])/", lambda m: m.group(1).upper() + ":/", project_dir
            )

        # Resolve and validate
        new_path = Path(project_dir).resolve()

        # Short-circuit if already pointing at this path
        if config and config.repo_path.resolve() == new_path:
            return {
                "status": "already_set",
                "repo_path": str(new_path),
                "repo_name": config.effective_repo_name,
                "cognition_dir": str(config.cognition_dir),
            }

        # Create new config (validates path exists + is dir)
        new_config = Settings(repo_path=new_path)

        # Initialize new storage at the project's .cognition/ dir
        logger.info(f"Switching project to: {new_path}")
        new_cognition_storage = CognitionStorage(new_config.cognition_dir)
        new_embedding_storage = ChromaDBStorage(
            persist_directory=new_config.cognition_chromadb_path,
            collection_name="cognition_embeddings",
        )

        # Swap context entries (all tools read from this dict)
        lc["config"] = new_config
        lc["cognition_storage"] = new_cognition_storage
        lc["cognition_embedding_storage"] = new_embedding_storage

        # Recreate curator if embedding model is ready
        embedding_ready = lc.get("embedding_ready")
        embedding_generator = lc.get("embedding_generator")

        if embedding_ready and embedding_ready.is_set() and embedding_generator:
            new_curator = CognitionCurator(
                storage=new_cognition_storage,
                embedding_storage=new_embedding_storage,
                embedding_generator=embedding_generator,
                ollama_base_url=new_config.ollama_base_url,
                model=new_config.curator_model,
                max_candidates=new_config.curator_max_candidates,
            )
            lc["cognition_curator"] = new_curator

            # Background sync for the new project
            def _bg_sync():
                # Lazy import to avoid circular dependency
                from ..server import (
                    _create_deterministic_edges_for_edgeless,
                    _sync_cognition_embeddings,
                )

                _create_deterministic_edges_for_edgeless(new_cognition_storage)
                _sync_cognition_embeddings(
                    new_cognition_storage, new_embedding_storage, embedding_generator
                )

            threading.Thread(target=_bg_sync, daemon=True).start()
        else:
            # Embedding model still loading — store override so background
            # thread picks up the new storage when it finishes
            lc["_project_override"] = {
                "config": new_config,
                "cognition_storage": new_cognition_storage,
                "cognition_embedding_storage": new_embedding_storage,
            }

        logger.info(f"Project set to: {new_config.effective_repo_name} ({new_path})")

        return {
            "status": "ok",
            "repo_path": str(new_path),
            "repo_name": new_config.effective_repo_name,
            "cognition_dir": str(new_config.cognition_dir),
        }
