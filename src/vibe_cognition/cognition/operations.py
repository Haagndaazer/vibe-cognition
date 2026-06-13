"""Shared cognition operations used by more than one surface (MCP tools + dashboard).

Kept HTTP/MCP-agnostic: functions here return plain result dicts (or ``None`` for
not-found) and never raise for control flow, so each caller maps the outcome to its
own error shape (a Starlette 404 vs. an MCP ``{"error": ...}``).
"""

from __future__ import annotations

import logging
from typing import Any

from .documents import doc_ref, remove_text_sidecar
from .models import CognitionNodeType
from .storage import CognitionStorage

logger = logging.getLogger(__name__)


def _other_document_has_sha(storage: CognitionStorage, ref: str, sha: str) -> bool:
    """True if a remaining DOCUMENT node carries ``sha`` (a force_new twin sharing
    the content-addressed sidecar). Entity/episode nodes that merely cite the
    doc: ref do NOT count — only another document keeps the sidecar alive."""
    for nid in storage.find_nodes_by_ref(ref):
        node = storage.get_node(nid)
        if (node
                and node.get("type") == CognitionNodeType.DOCUMENT.value
                and node.get("metadata", {}).get("sha256") == sha):
            return True
    return False


def delete_cognition_node(
    storage: CognitionStorage,
    embed_storage: Any,
    node_id: str,
) -> dict[str, Any] | None:
    """Delete a node, its incident edges, and its embedding.

    ``storage.remove_node`` cascades edge removal in-memory (NetworkX) and journals a
    single ``remove_node`` tombstone, so concurrent sessions converge on the deletion
    via their per-op journal catch-up. The ChromaDB vector is purged best-effort —
    a failure there is logged, not fatal, mirroring the long-standing dashboard path.

    Args:
        storage: The cognition graph store.
        embed_storage: The ChromaDB embedding store (duck-typed: needs
            ``delete_embedding(node_id)``).
        node_id: ID of the node to delete.

    Returns:
        A result dict ``{"id", "removed_edges", "edges_removed"}`` on success, or
        ``None`` if the node does not exist (caller maps to its own not-found shape).
    """
    if not storage.has_node(node_id):
        return None

    # Capture the document's sha BEFORE removal so we can purge its text sidecar
    # after — but only if no twin still references it (force_new can mint two
    # document nodes over identical bytes; deleting one must not orphan the
    # other's sidecar, since the sidecar is content-addressed by sha).
    doc_sha: str | None = None
    pre = storage.get_node(node_id)
    if pre is not None and pre.get("type") == CognitionNodeType.DOCUMENT.value:
        doc_sha = pre.get("metadata", {}).get("sha256")

    # Capture incident edges before deletion so callers can report what was orphaned.
    removed_edges: list[dict[str, Any]] = [
        {"from": node_id, "to": target_id, "type": edata.get("type")}
        for target_id, edata in storage.get_successors(node_id)
    ] + [
        {"from": source_id, "to": node_id, "type": edata.get("type")}
        for source_id, edata in storage.get_predecessors(node_id)
    ]

    if not storage.remove_node(node_id):
        # Lost a race with a concurrent deletion — treat as not found.
        return None

    # delete_embedding returns True even when nothing was actually deleted (ChromaDB
    # doesn't report it), so we don't surface its boolean — just purge best-effort.
    try:
        embed_storage.delete_embedding(node_id)
    except Exception as e:
        logger.warning(f"ChromaDB delete failed for {node_id}: {e}")

    # Purge the text sidecar (a managed artifact) iff no OTHER document node still
    # carries the same content. A "twin" is specifically another DOCUMENT node with
    # the same sha256 (force_new can mint these) — NOT any node merely citing the
    # doc: ref: per DESIGN §1/§9 S4 the intended pattern is descriptor ENTITIES
    # citing doc:<hash> in their references, and counting those as twins would leak
    # the sidecar forever (no document node remains to ever re-trigger the purge).
    # This mirrors the dedup filter in _store_document (type==document AND sha
    # match); the just-deleted id returns None from get_node, so it self-excludes.
    # NEVER touches the referenced original file — deletion reclaims only what the
    # server itself wrote.
    if doc_sha and not _other_document_has_sha(storage, doc_ref(doc_sha), doc_sha):
        try:
            remove_text_sidecar(storage.cognition_dir, doc_sha)
        except OSError as e:
            logger.warning(f"Text sidecar delete failed for {node_id} ({doc_sha[:12]}): {e}")

    return {
        "id": node_id,
        "removed_edges": removed_edges,
        "edges_removed": len(removed_edges),
    }
