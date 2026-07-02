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
from .tools.cognition_tools import (
    _embed_document,
    _embed_entity_node,
    _embed_workflow,
    _node_from_dict,
)
from .tools.project_registry import build_registry, compute_model_guard

logger = logging.getLogger(__name__)


def _missing_deterministic_edge(cognition_storage: CognitionStorage, node_id: str, references: list) -> bool:
    """True if ``node_id`` shares a reference with some OTHER node it has no
    edge to/from yet — the exact condition ``create_deterministic_edges``
    would try to fill.

    WP-5 (7c1899fe59ed): diffs against the reference index rather than
    replicating the six-pair type-matching truth table
    (``_deterministic_edge_for_pair``) — a false positive here just costs one
    harmless, idempotent ``create_deterministic_edges`` call (it independently
    re-checks the real rules and no-ops if no edge is actually warranted for
    that pair), so this only needs to be a cheap, conservative OVER-approximation,
    not a byte-exact replay of the matching logic.
    """
    connected = {t for t, _ in cognition_storage.get_successors(node_id)}
    connected |= {s for s, _ in cognition_storage.get_predecessors(node_id)}
    for ref in references:
        for other_id in cognition_storage.find_nodes_by_ref(ref):
            if other_id != node_id and other_id not in connected:
                return True
    return False


def _create_deterministic_edges_for_edgeless(
    cognition_storage: CognitionStorage,
) -> None:
    """Run deterministic part_of matching for nodes missing an edge their
    references warrant.

    This catches nodes created by paths that bypass cognition_record (and
    thus skip deterministic matching), AND (WP-5, 7c1899fe59ed) nodes that
    already have SOME edge but are still missing a link to a reference-
    sharing peer that appeared later (e.g. a teammate's episode for the same
    commit, merged in after this node already had an unrelated edge) — the
    old has-ANY-edge predicate skipped those permanently, since this sweep
    only runs once per server startup and there's no other repair path.
    """
    all_nodes = cognition_storage.get_all_nodes()
    if not all_nodes:
        return

    total_created = 0
    for node_data in all_nodes:
        node_id = node_data["id"]
        references = node_data.get("references", [])
        # Skip nodes with no references (can't match)
        if not references:
            continue
        if not _missing_deterministic_edge(cognition_storage, node_id, references):
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
) -> dict[str, int]:
    """Sync cognition nodes from JSONL into ChromaDB if missing.

    This handles the case where a teammate pulled new JSONL entries via Git
    but the local ChromaDB doesn't have their embeddings yet.

    WP-4 (3e82d4ebc004): routes through the SAME shared embed paths
    _record_node uses (_embed_entity_node / _embed_workflow / _embed_document)
    instead of a drifted inline copy. The old inline non-doc branch omitted
    task status/owner from the embed text/metadata, and workflow nodes never
    got chunked here at all (no _embed_workflow call) — a workflow embedded
    ONLY by this reconciler (e.g. created in the 2-30s model-load window
    where _record_node defers) stayed permanently un-chunked with no repair
    path (in-place workflow edits are refused; versioning is by supersession).
    """
    all_nodes = cognition_storage.get_all_nodes()
    if not all_nodes:
        return {"nodes": 0, "workflows": 0, "documents": 0}

    # Get existing IDs + metadata (WP-4, 41ced8d1fa63: metadata carries each
    # node's chunk_count for the completeness check below) in one call.
    existing_ids: set[str] = set()
    existing_meta: dict[str, Any] = {}
    try:
        results = embedding_storage._collection.get(
            ids=[n["id"] for n in all_nodes], include=["metadatas"]
        )
        existing_ids = set(results["ids"])
        existing_meta = dict(zip(results["ids"], results["metadatas"] or [], strict=False))
    except Exception:
        pass  # If collection is empty or IDs not found, treat all as missing

    doc_type = CognitionNodeType.DOCUMENT.value
    wf_type = CognitionNodeType.WORKFLOW.value
    cognition_dir = cognition_storage.cognition_dir
    doc_nodes = [n for n in all_nodes if n.get("type") == doc_type]
    wf_nodes = [n for n in all_nodes if n.get("type") == wf_type]

    # WP-4 (41ced8d1fa63): verify the FULL chunk set via each node's stored
    # chunk_count metadata, not just chunk-0 presence — a crash mid-write-loop
    # used to read as "fully synced" the instant chunk-0 landed, permanently
    # hiding chunks 1..N. One batched probe across ALL doc+workflow nodes'
    # expected chunk ids (same cost shape as the old chunk-0-only probe).
    expected_chunk_ids: list[str] = []
    for n in doc_nodes + wf_nodes:
        if n["id"] not in existing_ids:
            continue
        count = int((existing_meta.get(n["id"]) or {}).get("chunk_count") or 0)
        expected_chunk_ids.extend(f"{n['id']}#chunk-{i}" for i in range(count))
    chunk_present: set[str] = set()
    if expected_chunk_ids:
        try:
            probe = embedding_storage._collection.get(ids=expected_chunk_ids)
            chunk_present = set(probe["ids"])
        except Exception:
            pass

    def _chunks_complete(node_id: str) -> bool:
        meta = existing_meta.get(node_id) or {}
        if "chunk_count" not in meta:
            # Legacy vector (written before this WP started stamping
            # chunk_count unconditionally, len(chunks) incl. explicit 0):
            # chunk state is UNKNOWN, not "zero expected" — key-absent must
            # NOT be conflated with an explicit 0 (redirect from Vince's
            # gate review) or a legacy text-bearing doc/workflow with a
            # missing/incomplete chunk set reads as permanently complete.
            # Force one re-embed; it stamps chunk_count and converges.
            return False
        count = int(meta.get("chunk_count") or 0)
        if count == 0:
            return True  # explicitly zero expected (e.g. empty sidecar) -> complete
        return all(f"{node_id}#chunk-{i}" in chunk_present for i in range(count))

    # Plain (non-document, non-workflow) nodes: embed node-level if missing.
    non_doc_missing = [
        n for n in all_nodes
        if n.get("type") not in (doc_type, wf_type) and n["id"] not in existing_ids
    ]

    # Workflows: missing entirely, OR present but with an incomplete chunk set
    # (WP-4, 41ced8d1fa63 — previously any presence at all was "synced").
    wf_to_embed = [
        n for n in wf_nodes
        if n["id"] not in existing_ids or not _chunks_complete(n["id"])
    ]

    # Documents (WP-D2): a document is fully synced iff its NODE vector exists AND
    # (its sidecar is empty/absent OR its FULL chunk set is present). The "empty
    # sidecar" branch is load-bearing — without it a text-less document looks
    # "missing" forever and re-embeds every boot. This replaces D1a's blanket
    # document-skip and backfills documents created in the D1a/D1b interim
    # (deliberately never embedded then).
    docs_to_embed: list[tuple[dict[str, Any], str]] = []
    for n in doc_nodes:
        node_present = n["id"] in existing_ids
        sha = n.get("metadata", {}).get("sha256")
        sidecar = read_text_sidecar(cognition_dir, sha) if sha else None
        has_text = bool(sidecar and sidecar.strip())
        complete = node_present and (not has_text or _chunks_complete(n["id"]))
        if not complete:
            docs_to_embed.append((n, sidecar or ""))

    if non_doc_missing:
        logger.info(f"Syncing {len(non_doc_missing)} cognition nodes to ChromaDB...")
        for n in non_doc_missing:
            _embed_entity_node(embedding_storage, generator, _node_from_dict(n["id"], n))

    if wf_to_embed:
        logger.info(f"Syncing {len(wf_to_embed)} workflow node(s) to ChromaDB...")
        for n in wf_to_embed:
            _embed_workflow(embedding_storage, generator, _node_from_dict(n["id"], n))

    # Documents: node vector + sidecar chunks via the shared _embed_document (the same
    # delete-then-write path store-time uses — no chunk-contract drift). A sidecar-less
    # reference doc (teammate pulled the journal but not the sidecar) embeds the node
    # only (sidecar_text == "" -> zero chunks); not an error.
    for node, sidecar_text in docs_to_embed:
        _embed_document(
            embedding_storage, generator, node["id"],
            node.get("summary", ""), node.get("detail", ""), sidecar_text,
        )

    if non_doc_missing or wf_to_embed or docs_to_embed:
        logger.info(
            f"Cognition embedding sync: {len(non_doc_missing)} nodes + "
            f"{len(wf_to_embed)} workflows + {len(docs_to_embed)} documents (re)embedded"
        )
    else:
        logger.info("Cognition embeddings: all nodes already synced")

    # Always reconcile orphans (independent of the add passes above).
    _reconcile_orphan_embeddings(cognition_storage, embedding_storage)

    # WP-4 item 3 (5340ae677931, code half): coarse progress counts for
    # get_status's "syncing" state — how much this pass actually (re)embedded.
    return {
        "nodes": len(non_doc_missing),
        "workflows": len(wf_to_embed),
        "documents": len(docs_to_embed),
    }


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
            # live_embed_scheme() (WP-3, b35e15766c6b), NOT self._collection.metadata:
            # a process-cached handle can go stale if another process already
            # recreated the collection, which used to let two racing startups
            # both decide "needs migration" and double-delete-recreate.
            # recreate_collection() itself is file-locked against that race too.
            if cognition_embedding_storage.live_embed_scheme() != "doc-prefix-v1":
                logger.info("E-3 migration: recreating collection with doc-prefix stamp")
                cognition_embedding_storage.recreate_collection()

            logger.info("Syncing cognition embeddings...")
            context["embedding_sync_progress"] = _sync_cognition_embeddings(
                cognition_storage, cognition_embedding_storage, embedding_generator
            )

        # WP-4 item 3 (5340ae677931, code half): embedding_ready.set() above
        # fires BEFORE this backfill sync — deliberately unchanged (known-
        # intentional) — so a teammate joining an existing graph could see
        # embedding_status "ready" while historical nodes were still
        # un-embedded and search silently incomplete. embedding_sync_done is
        # a SEPARATE signal get_status uses to report "syncing" in that
        # window instead of a falsely-confident "ready"; it never gates tool
        # availability.
        context["embedding_sync_done"].set()

    except Exception as e:
        logger.error(f"Background initialization failed: {e}")
        context["embedding_error"] = str(e)
        context["embedding_ready"].set()  # Signal so tools don't hang forever
        context["embedding_sync_done"].set()  # Sync phase is over either way


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
        # WP-4 item 3 (5340ae677931, code half): set once the historical
        # backfill sync finishes (success or error) — independent of
        # embedding_ready, which fires earlier and is frozen (known-
        # intentional). get_status derives "syncing" from ready-but-not-done.
        "embedding_sync_done": threading.Event(),
        "embedding_sync_progress": None,
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
