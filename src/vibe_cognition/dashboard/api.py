"""HTTP API handlers for the dashboard.

Handlers are sync `def` so Starlette runs them in a threadpool — this
matches CognitionStorage's RLock-based threading model.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse

from ..cognition import CognitionNodeType, delete_cognition_node
from ..cognition.documents import documents_dir

logger = logging.getLogger(__name__)


def _ctx(request) -> dict[str, Any]:
    return request.app.state.lifespan_ctx


def _document_has_blob(node: dict[str, Any]) -> bool:
    """Whether a document node has a stored content-addressed blob (copy mode). THE
    single source for the has-blob decision — both the list endpoint and the download
    endpoint use it (ledger 11) so they can't disagree on what's downloadable."""
    return (node.get("metadata") or {}).get("mode") == "copy"


def _document_blob_path(cognition_dir: Path, node: dict[str, Any]) -> Path | None:
    """Resolve a copy-mode document's on-disk blob path, VALIDATED to live under the
    documents dir — None for reference mode, a missing blob_path, or any path that
    resolves outside documents_dir (path-safety defense even though the path is
    server-derived from the node's stored, sanitized metadata, never client input)."""
    if not _document_has_blob(node):
        return None
    rel = (node.get("metadata") or {}).get("blob_path")
    if not rel:
        return None
    docs = documents_dir(cognition_dir).resolve()
    candidate = (docs / rel).resolve()
    if not candidate.is_relative_to(docs):
        return None
    return candidate


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
        # Over-query so the many chunks of one document don't crowd out other nodes
        # before dedupe (mirrors the MCP search rationale).
        hits = embed_storage.vector_search(
            query_embedding=vector,
            limit=limit * 5,
            entity_type=entity_type,
        )
        # N1 ghost-search SAFETY (WP-D2): drop hits whose node was deleted cross-process
        # but never un-embedded (shared search_hit_is_live predicate, ledger 11) — else
        # the dashboard would serve verbatim deleted client-document chunk text.
        # D-6 NAVIGATION (WP-D4): dedupe document chunk hits (<node>#chunk-N) to the best
        # (first, score-desc) hit PER NODE, rewrite _id to the navigable NODE id, and
        # hydrate `summary` from the graph (chunk metadata has none). The raw
        # {_id, **metadata, score} shape is preserved — entity_type stays as the hit
        # carries it (NOT overwritten with the graph node's `type`), so renderSearchResults
        # navigates and labels correctly with no JS change.
        out: list[dict] = []
        seen: set[str] = set()
        for h in hits:
            raw_id = h.get("_id") or ""
            if not cognition_storage.search_hit_is_live(raw_id):
                continue
            node_id = raw_id.split("#chunk-")[0]
            if node_id in seen:
                continue
            seen.add(node_id)
            row = dict(h)
            row["_id"] = node_id
            node = cognition_storage.get_node(node_id)
            if node:
                row["summary"] = node.get("summary") or row.get("summary")
            matched = h.get("matched_text")
            if matched:
                row["matched_excerpt"] = matched[:500]
            out.append(row)
            if len(out) >= limit:
                break
        return out

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


def list_documents(request):
    """List stored document nodes (metadata only — never the text or blob bytes)."""
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    out = []
    for n in storage.get_nodes_by_type(CognitionNodeType.DOCUMENT):
        meta = n.get("metadata") or {}
        refs = n.get("references") or []
        out.append({
            "node_id": n["id"],
            "doc_ref": refs[0] if refs else None,
            "summary": n.get("summary", ""),  # for documents the title IS the summary
            "mode": meta.get("mode", "reference"),
            "size": meta.get("size"),
            "mime": meta.get("mime", ""),
            "filename": meta.get("filename", ""),
            "indexed_text_chars": meta.get("indexed_text_chars"),
            "timestamp": n.get("timestamp", ""),
            "has_blob": _document_has_blob(n),
        })
    out.sort(key=lambda d: d["timestamp"], reverse=True)  # newest first
    return JSONResponse({"documents": out})
