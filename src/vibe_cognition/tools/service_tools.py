"""MCP service tools for Vibe Cognition server status."""

import logging
from typing import Any

from fastmcp import Context

from .dispatch import dispatch_tool
from .project_registry import LoadedProjects
from .utils import get_lifespan

logger = logging.getLogger(__name__)


def register_service_tools(mcp) -> None:
    """Register service tools with the MCP server.

    Args:
        mcp: FastMCP server instance
    """

    @dispatch_tool(mcp)
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
              embedding_status: "spawning" | "loading" | "waiting-for-load-lock" |
                                "syncing" | "ready" | "error: <detail>". The first
                                three (WP-Sidecar) apply to the non-ollama backend
                                only: "spawning" while the sidecar subprocess is
                                starting, "waiting-for-load-lock" while it queues
                                behind another session's model load on the cross-
                                process mutex, "loading" once it holds the lock and
                                is actually loading. ollama (no sidecar) only ever
                                reports "loading" pre-ready, unchanged.
                                "syncing" (WP-4, 5340ae677931) means the embedding
                                model is loaded and tools are usable, but the
                                historical backfill sync (teammate-pulled nodes
                                not yet embedded) hasn't finished — search may be
                                silently incomplete during this window. embedding_
                                ready (tool availability) still fires BEFORE sync
                                starts, unchanged; "syncing" is purely a visibility
                                signal, not a new gate,
              embedding_sync_progress: null before the sync PASS finishes (covers
                                both "still loading" and "still syncing"); once it
                                finishes, {nodes, workflows, documents} counts of
                                what THAT pass (re)embedded (0/0/0 if nothing was
                                missing),
              home_model_drift: null when the home embedding collection's stored
                                model/dims match this process's configured
                                embedding_model/embedding_dimensions (the common
                                case); otherwise {state: "dim-mismatch" |
                                "model-mismatch" | "unknown", warning: str}.
                                A non-null state means cognition_search on the
                                home project (project=None) is degraded or
                                unavailable — see its docstring. "unknown" means
                                degraded-confidence (pre-stamp collection, search
                                still runs); the mismatch states mean search
                                short-circuits to an honest empty result. May be
                                stale for a few seconds after startup (the check
                                runs in the background init thread, same as
                                embedding model load, before this field settles),
              loaded_foreign_projects: int,   # count of loaded foreign projects
                                              # (use cognition_list_projects for
                                              # details; absent if no registry)
              curation: str,  # reminder: /vibe-curate launches the background
                              # curate-orchestrator agent that does the linking
            }
        """
        lc = get_lifespan(ctx)
        config = lc.get("config")
        cognition_storage = lc.get("cognition_storage")
        cognition_embedding_storage = lc.get("cognition_embedding_storage")
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

        # Home embedding model/dim drift (WP-2): null when clean (the common
        # case), matching rehydrate_events' null-when-clean shape.
        home_guard = lc.get("home_model_guard")
        if home_guard is not None and home_guard != "match":
            result["home_model_drift"] = {
                "state": home_guard,
                "warning": lc.get("home_model_guard_warning"),
            }
        else:
            result["home_model_drift"] = None

        # Embedding model status (WP-4 item 3: "syncing" is a THIRD state
        # between "loading" and "ready" — embedding_ready fires before the
        # historical backfill sync starts, unchanged, so without this a
        # teammate joining an existing graph saw a falsely-confident "ready"
        # while search was silently incomplete).
        embedding_ready = lc.get("embedding_ready")
        embedding_error = lc.get("embedding_error")
        embedding_sync_done = lc.get("embedding_sync_done")
        if embedding_error:
            result["embedding_status"] = f"error: {embedding_error}"
        elif embedding_ready and embedding_ready.is_set():
            if embedding_sync_done is not None and not embedding_sync_done.is_set():
                result["embedding_status"] = "syncing"
            else:
                result["embedding_status"] = "ready"
        else:
            # WP-Sidecar §S-d: while not yet ready, the sidecar supervisor
            # (non-ollama backend only) knows a more granular state than the
            # generic "loading" -- spawning the subprocess, waiting on the
            # cross-process load mutex, or actually loading the model.
            # ollama (no supervisor) keeps its unchanged, simpler "loading".
            supervisor = lc.get("_sidecar_supervisor")
            result["embedding_status"] = supervisor.status() if supervisor is not None else "loading"

        result["embedding_sync_progress"] = lc.get("embedding_sync_progress")

        # Foreign project count (XP1)
        if registry is not None:
            result["loaded_foreign_projects"] = registry.foreign_count()

        # Curation runs via a background curate-orchestrator agent: only deterministic
        # part_of edges are automatic; semantic edges are created by that agent,
        # launched via the /vibe-curate skill — never by the main instance directly.
        result["curation"] = "background curate-orchestrator agent, launched via /vibe-curate"

        return result
