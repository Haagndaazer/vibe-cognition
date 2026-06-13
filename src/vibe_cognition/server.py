"""FastMCP server for Vibe Cognition — project knowledge graph."""

import logging
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from .cognition import CognitionNodeType, CognitionStorage
from .config import Settings, setup_logging
from .embeddings import ChromaDBStorage, EmbeddingGenerator
from .instructions import SERVER_INSTRUCTIONS
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

    # Documents are graph-inert and intentionally NOT embedded (their searchable
    # text lives in the sidecar, chunked separately in D1b). Skip them here too —
    # otherwise this cross-process sync would re-embed every document node on the
    # next server start, re-introducing them into semantic search (N1 class).
    missing = [
        n
        for n in all_nodes
        if n["id"] not in existing_ids and n.get("type") != CognitionNodeType.DOCUMENT.value
    ]

    if missing:
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
    else:
        logger.info("Cognition embeddings: all nodes already synced")

    # Always reconcile orphans (independent of the add-missing pass above).
    _reconcile_orphan_embeddings(cognition_storage, embedding_storage)


def _reconcile_orphan_embeddings(
    cognition_storage: CognitionStorage,
    embedding_storage: ChromaDBStorage,
) -> None:
    """N1 startup reclamation (§9 N1b): delete Chroma ids (incl. ``#chunk-*``) whose
    node is absent from the graph. A node deleted on another machine replays as a
    remove_node tombstone (graph-only) and is NEVER un-embedded (the sync only ADDS),
    so its vector lingers and would surface in search.

    Best-effort RECLAMATION only — the query-time has_node filter in cognition_search
    is the correctness guarantee (it never returns a ghost regardless of this sweep).
    Ordering-hardened (peer review): the graph snapshot is freshly caught-up
    (get_all_nodes -> _synced), orphans are computed against it, enumeration uses the
    no-arg get() (a get/delete with ids=[] RAISES on an empty list), and delete is
    skipped on an empty orphan set. A residual cross-process TOCTOU remains (a write
    landing between catch-up and delete) — accepted under the non-transactional model;
    it can cause only a transiently-late reclamation, never a wrong search result.
    """
    try:
        all_ids = embedding_storage._collection.get()["ids"]
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"Orphan-embedding sweep: enumerate failed: {e}")
        return
    if not all_ids:
        return
    graph_ids = {n["id"] for n in cognition_storage.get_all_nodes()}
    orphans = [cid for cid in all_ids if cid.split("#chunk-")[0] not in graph_ids]
    if not orphans:
        return
    try:
        embedding_storage._collection.delete(ids=orphans)
        logger.info(f"Orphan-embedding sweep: removed {len(orphans)} stale vector(s)")
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"Orphan-embedding sweep: delete failed: {e}")


def _load_embeddings_and_sync(config: Settings, context: dict[str, Any]) -> None:
    """Background thread: load embedding model, then sync embeddings + edges.

    This runs after the MCP handshake completes so the server starts fast.
    Semantic curation is NOT done here — it is the agent's job via the
    `/vibe-curate` skill. The only automatic edges are the deterministic
    `part_of` edges created from shared references.
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

    except Exception as e:
        logger.error(f"Background initialization failed: {e}")
        context["embedding_error"] = str(e)
        context["embedding_ready"].set()  # Signal so tools don't hang forever


@asynccontextmanager
async def lifespan(server: FastMCP):
    """Manage server lifecycle - initialize and cleanup resources."""
    # Load configuration — reads REPO_PATH from env (set by the plugin's
    # mcpServers block in plugin.json; config.py also falls back to
    # CLAUDE_PROJECT_DIR). There is no per-project .mcp.json.
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
        "embedding_ready": threading.Event(),
        "embedding_error": None,
    }

    # ── Background init (2-30s for model, then sync + curation) ────

    bg_thread = threading.Thread(
        target=_load_embeddings_and_sync,
        args=(config, context),
        daemon=True,
    )
    bg_thread.start()
    context["_bg_thread"] = bg_thread

    logger.info("Vibe Cognition ready (embedding model loading in background)")

    yield context

    # ── Cleanup ───────────────────────────────────────────────────

    logger.info("Shutting down Vibe Cognition...")

    # Stop dashboard server if running
    if context.get("dashboard"):
        try:
            from .dashboard.server import stop_dashboard
            stop_dashboard(context, join_timeout=3.0)
        except Exception as e:
            logger.warning(f"Dashboard shutdown error: {e}")

    # Give background thread a chance to finish
    bg_thread = context.get("_bg_thread")
    if bg_thread:
        bg_thread.join(timeout=5.0)

    cognition_embedding_storage.close()
    logger.info("Shutdown complete")


# Create the MCP server. SERVER_INSTRUCTIONS (single source of truth in instructions.py,
# also used by the post-compact re-injection hook) is surfaced to the agent every session
# via the MCP `initialize` handshake ("MCP Server Instructions").
mcp = FastMCP("Vibe Cognition", instructions=SERVER_INSTRUCTIONS, lifespan=lifespan)

# Register all tools
register_all_tools(mcp)


def main():
    """Entry point for the Vibe Cognition MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
