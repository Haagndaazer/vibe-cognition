"""HTTP API handlers for the dashboard.

Handlers are sync `def` so Starlette runs them in a threadpool — this
matches CognitionStorage's RLock-based threading model.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.responses import JSONResponse

from ..cognition import delete_cognition_node

logger = logging.getLogger(__name__)


def _ctx(request) -> dict[str, Any]:
    return request.app.state.lifespan_ctx


def _embedding_status(lc: dict[str, Any]) -> tuple[bool, str | None]:
    """Return (ready, error_or_loading_status)."""
    error = lc.get("embedding_error")
    if error:
        return False, "error"
    event = lc.get("embedding_ready")
    if event and event.is_set() and lc.get("embedding_generator") is not None:
        return True, None
    return False, "loading"


def get_graph(request):
    """Return all nodes + edges, shaped for Cytoscape.

    Excludes the `detail` field per node to keep payloads small;
    fetch full detail via /api/node/{id} on click.
    """
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    # snapshot() catches up on the journal and returns nodes + edges together
    # under the lock — a consistent, converged view (no raw graph/_lock reach-in).
    snap = storage.snapshot()

    nodes_out = [
        {
            "data": {
                "id": n["id"],
                "label": (n.get("summary") or n["id"])[:80],
                "type": n.get("type", ""),
                "summary": n.get("summary", ""),
                "timestamp": n.get("timestamp", ""),
                "context": n.get("context", []),
                "severity": n.get("severity"),
            }
        }
        for n in snap["nodes"]
    ]

    edges_out = [
        {
            "data": {
                "id": f"{source_id}__{key}__{target_id}",
                "source": source_id,
                "target": target_id,
                "type": edge_data.get("type", key),
            }
        }
        for source_id, target_id, key, edge_data in snap["edges"]
    ]

    return JSONResponse({"nodes": nodes_out, "edges": edges_out})


def get_node(request):
    """Return full node data + neighbors."""
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    node_id = request.path_params["node_id"]

    node_data = storage.get_node(node_id)
    if node_data is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    successors = [
        {"id": tid, "type": ed.get("type", ""), "edge_type": ed.get("type", "")}
        for tid, ed in storage.get_successors(node_id)
    ]
    predecessors = [
        {"id": sid, "type": ed.get("type", ""), "edge_type": ed.get("type", "")}
        for sid, ed in storage.get_predecessors(node_id)
    ]

    return JSONResponse({
        "id": node_id,
        **node_data,
        "successors": successors,
        "predecessors": predecessors,
    })


def delete_node(request):
    """Remove a node from the graph and ChromaDB."""
    lc = _ctx(request)
    node_id = request.path_params["node_id"]

    result = delete_cognition_node(
        lc["cognition_storage"], lc["cognition_embedding_storage"], node_id
    )
    if result is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    return JSONResponse({"deleted": True, "id": node_id})


async def search(request):
    """Semantic search via embeddings.

    Async because we need request.json(); we then offload the blocking
    embedding+vector work via run_in_threadpool.
    """
    from starlette.concurrency import run_in_threadpool

    lc = _ctx(request)
    body = await request.json()
    query = body.get("query", "").strip()
    limit = int(body.get("limit", 20))
    entity_type = body.get("entity_type")

    if not query:
        return JSONResponse({"error": "missing query"}, status_code=400)

    ready, status = _embedding_status(lc)
    if not ready:
        return JSONResponse(
            {
                "embedding_status": status,
                "error": lc.get("embedding_error") or "embedding model still loading",
            },
            status_code=503,
        )

    generator = lc["embedding_generator"]
    embed_storage = lc["cognition_embedding_storage"]
    cognition_storage = lc["cognition_storage"]

    def _do_search():
        vector = generator.generate_query_embedding(query)
        hits = embed_storage.vector_search(
            query_embedding=vector,
            limit=limit,
            entity_type=entity_type,
        )
        # N1 ghost-search fix (WP-D2): drop hits whose node was deleted cross-process
        # but never un-embedded — D2 makes documents searchable, so an un-filtered
        # dashboard would serve verbatim deleted client-document chunk text. Same
        # shared predicate the MCP search uses; raw {_id, **metadata, score} shape
        # preserved (the dashboard JS consumes it, unlike the MCP formatter).
        return [h for h in hits if cognition_storage.search_hit_is_live(h.get("_id") or "")]

    results = await run_in_threadpool(_do_search)
    return JSONResponse({"results": results})


def get_stats(request):
    """Graph stats + embedding readiness."""
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    embed_storage = lc["cognition_embedding_storage"]

    ready, status = _embedding_status(lc)
    try:
        embedding_count = embed_storage.count_documents()
    except Exception as e:
        embedding_count = 0
        logger.warning(f"count_documents failed: {e}")

    return JSONResponse({
        "graph": storage.get_statistics(),
        "embeddings": embedding_count,
        "embedding_ready": ready,
        "embedding_status": status,
        "embedding_error": lc.get("embedding_error"),
        "embedding_generator_loaded": lc.get("embedding_generator") is not None,
    })
