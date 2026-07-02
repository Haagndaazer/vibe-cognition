"""FastMCP server for Vibe Cognition — project knowledge graph."""

import logging
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from .cognition import CognitionNodeType, CognitionStorage
from .cognition.documents import read_text_sidecar
from .config import Settings, setup_logging
from .embeddings import ChromaDBStorage, EmbeddingGenerator
from .instructions import SERVER_INSTRUCTIONS
from .tools import register_all_tools
from .tools.cognition_tools import _embed_document
from .tools.project_registry import build_registry, compute_model_guard

logger = logging.getLogger(__name__)


def _create_deterministic_edges_for_edgeless(
    cognition_storage: CognitionStorage,
) -> None:
    """Run deterministic part_of matching for nodes with no edges.

    This catches nodes created by paths that bypass cognition_record (and thus
    skip deterministic matching).
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

    doc_type = CognitionNodeType.DOCUMENT.value
    cognition_dir = cognition_storage.cognition_dir
    doc_nodes = [n for n in all_nodes if n.get("type") == doc_type]

    # Probe chunk-0 presence for document nodes (un-chunked detection, one batch get).
    chunk0_present: set[str] = set()
    if doc_nodes:
        try:
            probe = embedding_storage._collection.get(ids=[f"{n['id']}#chunk-0" for n in doc_nodes])
            chunk0_present = set(probe["ids"])
        except Exception:
            pass

    # Non-document nodes: embed node-level if missing (unchanged path).
    non_doc_missing = [
        n for n in all_nodes
        if n.get("type") != doc_type and n["id"] not in existing_ids
    ]

    # Documents (WP-D2): a document is fully synced iff its NODE vector exists AND
    # (its sidecar is empty/absent OR its chunk-0 exists). The "empty sidecar" branch
    # is load-bearing — without it a text-less document looks "missing" forever and
    # re-embeds every boot. This replaces D1a's blanket document-skip and backfills
    # documents created in the D1a/D1b interim (deliberately never embedded then).
    docs_to_embed: list[tuple[dict[str, Any], str]] = []
    for n in doc_nodes:
        node_present = n["id"] in existing_ids
        sha = n.get("metadata", {}).get("sha256")
        sidecar = read_text_sidecar(cognition_dir, sha) if sha else None
        has_text = bool(sidecar and sidecar.strip())
        chunked = f"{n['id']}#chunk-0" in chunk0_present
        if not (node_present and (not has_text or chunked)):
            docs_to_embed.append((n, sidecar or ""))

    if non_doc_missing:
        logger.info(f"Syncing {len(non_doc_missing)} cognition nodes to ChromaDB...")
        for node in non_doc_missing:
            embed_text = f"{node.get('type', '')}: {node.get('summary', '')}\n{node.get('detail', '')}"
            embedding = generator.generate(embed_text, input_type="document")
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

    # Documents: node vector + sidecar chunks via the shared _embed_document (the same
    # delete-then-write path store-time uses — no chunk-contract drift). A sidecar-less
    # reference doc (teammate pulled the journal but not the sidecar) embeds the node
    # only (sidecar_text == "" -> zero chunks); not an error.
    for node, sidecar_text in docs_to_embed:
        _embed_document(
            embedding_storage, generator, node["id"],
            node.get("summary", ""), node.get("detail", ""), sidecar_text,
        )

    if non_doc_missing or docs_to_embed:
        logger.info(
            f"Cognition embedding sync: {len(non_doc_missing)} nodes + "
            f"{len(docs_to_embed)} documents (re)embedded"
        )
    else:
        logger.info("Cognition embeddings: all nodes already synced")

    # Always reconcile orphans (independent of the add passes above).
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
        # Home model/dim drift guard (WP-2): a cheap metadata comparison (no
        # model load needed), run FIRST so it can't add to embedding_ready
        # latency below (that stays gated on model load only, unchanged —
        # known-intentional) and so get_status/cognition_search see the guard
        # state as early as possible. Reuses the SAME check the foreign-attach
        # path uses (compute_model_guard) — do not invent a second guard.
        # Unlike the foreign path, home's embeddings handle is never closed on
        # a mismatch: it's the actively-written index and new writes must
        # still land regardless of a stale stamp.
        home_chroma = context.get("cognition_embedding_storage")
        if home_chroma is not None:
            guard, guard_warning, _ = compute_model_guard(
                home_chroma, config.embedding_model, config.embedding_dimensions,
                config.effective_repo_name,
            )
            loaded_projects = context.get("loaded_projects")
            if loaded_projects is not None:
                home_entry = loaded_projects.get(config.repo_path)
                if home_entry is not None:
                    home_entry.model_guard = guard
            context["home_model_guard"] = guard
            context["home_model_guard_warning"] = guard_warning
            if guard in ("dim-mismatch", "model-mismatch"):
                logger.warning(f"Home embedding collection drift: {guard_warning}")

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
            # E-3 one-time migration: if the collection lacks the doc-prefix-v1 marker,
            # drop and recreate it so the sync below rebuilds all vectors document-prefixed.
            # Crash mid-rebuild self-heals: the additive sync re-adds only what's missing.
            col_meta = cognition_embedding_storage._collection.metadata or {}
            if col_meta.get("embed_scheme") != "doc-prefix-v1":
                logger.info("E-3 migration: recreating collection with doc-prefix stamp")
                cognition_embedding_storage.recreate_collection()

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
        embedding_model=config.embedding_model,
        embedding_dimensions=config.embedding_dimensions,
    )

    # Build project registry — home is always pinned
    loaded_projects = build_registry(
        home_path=config.repo_path,
        home_tag=config.effective_repo_name,
        home_storage=cognition_storage,
        home_embeddings=cognition_embedding_storage,
    )

    # Build context for tools
    context: dict[str, Any] = {
        "config": config,
        "cognition_storage": cognition_storage,
        "cognition_embedding_storage": cognition_embedding_storage,
        "loaded_projects": loaded_projects,
        "embedding_generator": None,  # Set by background thread
        "embedding_ready": threading.Event(),
        "embedding_error": None,
        # Optimistic default until the background thread's cheap drift check
        # runs (WP-2); matches add_home's default and is never search-visible
        # earlier than this, since search is gated behind embedding_ready
        # which the background thread only sets AFTER the drift check.
        "home_model_guard": "match",
        "home_model_guard_warning": None,
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
