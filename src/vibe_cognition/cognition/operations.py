"""Shared cognition operations used by more than one surface (MCP tools + dashboard).

Kept HTTP/MCP-agnostic: functions here return plain result dicts (or ``None`` for
not-found) and never raise for control flow, so each caller maps the outcome to its
own error shape (a Starlette 404 vs. an MCP ``{"error": ...}``).
"""

from __future__ import annotations

import logging
from typing import Any

from .documents import remove_blob_rel, remove_gitignore_entry, remove_text_sidecar
from .models import CognitionNodeType
from .storage import CognitionStorage

logger = logging.getLogger(__name__)


def delete_cognition_node(
    storage: CognitionStorage,
    embed_storage: Any,
    node_id: str,
    removed_by: dict[str, str] | str | None = None,
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
        removed_by: Acting author recorded in the journal tombstone (provenance):
            a resolved git identity dict (MCP path) or a surface tag like
            "dashboard". Optional — omitted from the tombstone when None.

    Returns:
        A result dict ``{"id", "removed_edges", "edges_removed"}`` on success, or
        ``None`` if the node does not exist (caller maps to its own not-found shape).
    """
    if not storage.has_node(node_id):
        return None

    # Capture the document's sha + blob path BEFORE removal so we can reclaim its
    # managed artifacts after — each only if no twin still references it (force_new
    # can mint two document nodes over identical bytes; deleting one must not orphan
    # the other's sidecar/blob, which are content-addressed).
    doc_sha: str | None = None
    doc_blob_rel: str | None = None
    pre = storage.get_node(node_id)
    if pre is not None and pre.get("type") == CognitionNodeType.DOCUMENT.value:
        pre_meta = pre.get("metadata", {})
        doc_sha = pre_meta.get("sha256")
        if pre_meta.get("mode") == "copy":
            doc_blob_rel = pre_meta.get("blob_path")

    # Capture incident edges before deletion so callers can report what was orphaned.
    removed_edges: list[dict[str, Any]] = [
        {"from": node_id, "to": target_id, "type": edata.get("type")}
        for target_id, edata in storage.get_successors(node_id)
    ] + [
        {"from": source_id, "to": node_id, "type": edata.get("type")}
        for source_id, edata in storage.get_predecessors(node_id)
    ]

    if not storage.remove_node(node_id, removed_by=removed_by):
        # Lost a race with a concurrent deletion — treat as not found.
        return None

    # delete_embedding returns True even when nothing was actually deleted (ChromaDB
    # doesn't report it), so we don't surface its boolean — just purge best-effort.
    # The node-level vector AND any chunk vectors (chunk-embedding is D2; this is a
    # forward-compatible no-op today, present so document deletes inherit it).
    try:
        embed_storage.delete_embedding(node_id)
        embed_storage.delete_by_node_id(node_id)
    except Exception as e:
        logger.warning(f"ChromaDB delete failed for {node_id}: {e}")

    unlinked: list[str] = []
    if doc_sha:
        # Sidecar reclaim: purge iff NO document with this sha remains (any mode) —
        # via the ONE shared identity predicate. Called AFTER remove_node, so the
        # just-deleted node self-excludes; a force_new twin keeps it; a mere doc:-ref
        # CITER (entity/episode) is NOT a document and so does not count (F1 fix, now
        # structural). NEVER touches the referenced original file.
        sha_cohort = storage.documents_with_sha(doc_sha)
        if not sha_cohort:
            try:
                if remove_text_sidecar(storage.cognition_dir, doc_sha):
                    unlinked.append(f"text/{doc_sha}.txt")
            except OSError as e:
                logger.warning(f"Text sidecar delete failed for {node_id} ({doc_sha[:12]}): {e}")

        # Blob reclaim (copy mode): unlink iff no OTHER copy-mode document owns this
        # EXACT blob file. Same sha cohort, refined caller-side to mode=="copy" AND
        # same blob_path — a reference twin has no blob stake; a same-sha diff-ext
        # twin owns a different file (so per-blob-path, not per-sha).
        if doc_blob_rel:
            blob_siblings = [
                n for n in sha_cohort
                if (sib := storage.get_node(n)) is not None
                and sib.get("metadata", {}).get("mode") == "copy"
                and sib.get("metadata", {}).get("blob_path") == doc_blob_rel
            ]
            if not blob_siblings:
                if remove_blob_rel(storage.cognition_dir, doc_blob_rel):
                    unlinked.append(doc_blob_rel)
                # Local_only blobs leave a .gitignore line; reclaim it at refcount-zero.
                remove_gitignore_entry(storage.cognition_dir, doc_blob_rel)

    result: dict[str, Any] = {
        "id": node_id,
        "removed_edges": removed_edges,
        "edges_removed": len(removed_edges),
    }
    if doc_sha is not None:
        # Privacy caveat (§9 N2/§4): a committed blob survives in git history and on
        # the remote after deletion — deleting the node does not un-publish it; other
        # clones retain managed artifacts until they pull the removal.
        result["unlinked_artifacts"] = unlinked
    return result
