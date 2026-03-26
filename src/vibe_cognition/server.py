"""FastMCP server for Vibe Cognition — project knowledge graph."""

import logging
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from .cognition import CognitionStorage
from .config import Settings, setup_logging
from .embeddings import ChromaDBStorage, EmbeddingGenerator
from .tools import register_all_tools

logger = logging.getLogger(__name__)


def _create_deterministic_edges_for_edgeless(
    cognition_storage: CognitionStorage,
) -> None:
    """Run deterministic part_of matching for nodes with no edges.

    This catches nodes created by the post-commit hook or other paths
    that bypass cognition_record (and thus skip deterministic matching).
    """
    all_nodes = cognition_storage.get_all_nodes()
    if not all_nodes:
        return

    total_created = 0
    for node_data in all_nodes:
        node_id = node_data["id"]
        # Skip nodes that already have edges
        if (cognition_storage.get_successors(node_id) or
                cognition_storage.get_predecessors(node_id)):
            continue
        # Skip nodes with no references (can't match)
        if not node_data.get("references"):
            continue
        total_created += cognition_storage.create_deterministic_edges(node_id)

    if total_created:
        logger.info(
            f"Startup deterministic matching: created {total_created} part_of edge(s)"
        )


def _sync_cognition_embeddings(
    cognition_storage: CognitionStorage,
    embedding_storage: ChromaDBStorage,
    generator: EmbeddingGenerator,
) -> None:
    """Sync cognition nodes from JSONL into ChromaDB if missing.

    This handles the case where a teammate pulled new JSONL entries via Git
    but the local ChromaDB doesn't have their embeddings yet.
    """
    all_nodes = cognition_storage.get_all_nodes()
    if not all_nodes:
        return

    # Get existing IDs from ChromaDB in one call
    existing_ids = set()
    try:
        results = embedding_storage._collection.get(ids=[n["id"] for n in all_nodes])
        existing_ids = set(results["ids"])
    except Exception:
        pass  # If collection is empty or IDs not found, treat all as missing

    missing = [n for n in all_nodes if n["id"] not in existing_ids]

    if not missing:
        logger.info("Cognition embeddings: all nodes already synced")
        return

    logger.info(f"Syncing {len(missing)} cognition nodes to ChromaDB...")
    for node in missing:
        embed_text = f"{node.get('type', '')}: {node.get('summary', '')}\n{node.get('detail', '')}"
        embedding = generator.generate_query_embedding(embed_text)
        metadata = {
            "entity_type": node.get("type", ""),
            "summary": node.get("summary", ""),
            "author": node.get("author", ""),
            "timestamp": node.get("timestamp", ""),
            "context": ",".join(node.get("context", [])),
        }
        if node.get("severity"):
            metadata["severity"] = node["severity"]
        if node.get("references"):
            metadata["references"] = ",".join(node["references"])
        embedding_storage.upsert_embedding(node["id"], embedding, metadata)

    logger.info(f"Cognition embedding sync complete: {len(missing)} nodes added")


def _load_embeddings_and_curate(config: Settings, context: dict[str, Any]) -> None:
    """Background thread: load embedding model, init curator, sync embeddings.

    This runs after the MCP handshake completes so the server starts fast.
    """
    import time

    try:
        # Load embedding model (the bottleneck: 2-30s)
        t_start = time.monotonic()
        logger.info(f"Background: loading embedding model ({config.embedding_backend})...")
        embedding_generator = EmbeddingGenerator.from_config(config)
        t_model = time.monotonic()
        logger.info(f"Background: embedding model loaded in {t_model - t_start:.1f}s")

        # Populate context
        context["embedding_generator"] = embedding_generator

        # Init curator (depends on embedding_generator)
        cognition_curator = None
        if config.curator_enabled:
            from .cognition.curator import CognitionCurator

            cognition_curator = CognitionCurator(
                storage=context["cognition_storage"],
                embedding_storage=context["cognition_embedding_storage"],
                embedding_generator=embedding_generator,
                ollama_base_url=config.ollama_base_url,
                model=config.curator_model,
                max_candidates=config.curator_max_candidates,
            )
            context["cognition_curator"] = cognition_curator
            logger.info(f"Cognition curator initialized (model: {config.curator_model})")

        # Signal that embedding-dependent tools are ready
        context["embedding_ready"].set()
        logger.info("All tools now available")

        # Run deterministic part_of matching for edgeless nodes
        # (catches hook-created episodes that bypassed cognition_record)
        cognition_storage = context.get("cognition_storage")
        if cognition_storage:
            _create_deterministic_edges_for_edgeless(cognition_storage)

        # Sync cognition embeddings (backfill any missing from JSONL)
        cognition_embedding_storage = context.get("cognition_embedding_storage")

        if cognition_storage and cognition_embedding_storage and embedding_generator:
            logger.info("Syncing cognition embeddings...")
            _sync_cognition_embeddings(
                cognition_storage, cognition_embedding_storage, embedding_generator
            )

        # Curate uncurated nodes
        if cognition_curator is not None:
            if cognition_curator.ensure_model():
                count = cognition_curator.curate_uncurated_nodes()
                if count:
                    logger.info(f"Curator: enqueued {count} uncurated node(s)")
            else:
                logger.warning("Curator model not available — skipping startup curation")

    except Exception as e:
        logger.error(f"Background initialization failed: {e}")
        context["embedding_error"] = str(e)
        context["embedding_ready"].set()  # Signal so tools don't hang forever


@asynccontextmanager
async def lifespan(server: FastMCP):
    """Manage server lifecycle - initialize and cleanup resources."""
    # Load configuration
    try:
        config = Settings()
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        raise

    setup_logging(config.log_level)
    logger.info(f"Starting Vibe Cognition for repository: {config.effective_repo_name}")
    logger.info(f"Repository path: {config.repo_path}")

    # ── Fast init (blocking, <500ms) ───────────────────────────────

    # Initialize cognition graph
    logger.info(f"Initializing cognition graph at {config.cognition_dir}...")
    cognition_storage = CognitionStorage(config.cognition_dir)

    # Initialize cognition ChromaDB
    logger.info(f"Initializing cognition ChromaDB at {config.cognition_chromadb_path}...")
    cognition_embedding_storage = ChromaDBStorage(
        persist_directory=config.cognition_chromadb_path,
        collection_name="cognition_embeddings",
    )

    # Build context for tools
    context: dict[str, Any] = {
        "config": config,
        "cognition_storage": cognition_storage,
        "cognition_embedding_storage": cognition_embedding_storage,
        "embedding_generator": None,  # Set by background thread
        "cognition_curator": None,  # Set by background thread
        "embedding_ready": threading.Event(),
        "embedding_error": None,
    }

    # ── Background init (2-30s for model, then sync + curation) ────

    bg_thread = threading.Thread(
        target=_load_embeddings_and_curate,
        args=(config, context),
        daemon=True,
    )
    bg_thread.start()
    context["_bg_thread"] = bg_thread

    logger.info("Vibe Cognition ready (embedding model loading in background)")

    yield context

    # ── Cleanup ───────────────────────────────────────────────────

    logger.info("Shutting down Vibe Cognition...")

    # Give background thread a chance to finish
    bg_thread = context.get("_bg_thread")
    if bg_thread:
        bg_thread.join(timeout=5.0)

    cognition_embedding_storage.close()
    logger.info("Shutdown complete")


# Create the MCP server
mcp = FastMCP("Vibe Cognition", lifespan=lifespan)

# Register all tools
register_all_tools(mcp)


def main():
    """Entry point for the Vibe Cognition MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
