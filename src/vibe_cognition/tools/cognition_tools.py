"""MCP tools for the Cognition History Graph."""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastmcp import Context

from ..cognition import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    CognitionStorage,
    delete_cognition_node,
    generate_node_id,
    get_history_for_context,
    get_incident_resolution,
    get_reasoning_chain,
    get_superseded_chain,
    get_workflow_head,
    resolve_git_identity,
)
from ..cognition.chunking import chunk_text
from ..cognition.documents import (
    BLOB_REFUSE_BYTES,
    BLOB_WARN_BYTES,
    add_gitignore_entry,
    blob_path,
    blob_rel_path,
    cheap_staleness_signal,
    doc_ref,
    gitignore_has_entry,
    read_text_sidecar,
    remove_gitignore_entry,
    sanitize_extension,
    sha256_bytes,
    sha256_file,
    write_blob,
    write_text_sidecar,
)
from ..cognition.prime import SEVERITY_ORDER
from ..embeddings import ChromaDBStorage, EmbeddingGenerator, adaptive_vector_search
from .project_registry import (
    LoadedProjects,
    ProjectEntry,
    compute_model_guard,
    resolve_project,
    tag_results,
)
from .utils import get_lifespan, require_embeddings

logger = logging.getLogger(__name__)


def _embeddings_ready(lc: dict[str, Any]) -> bool:
    """True when the embedding model is loaded and didn't error — the boolean form
    of require_embeddings (which returns a tool error dict). Used by the internal
    record/update paths to decide whether to embed inline or defer to the sync."""
    event = lc.get("embedding_ready")
    return bool(event and event.is_set() and not lc.get("embedding_error"))


def _embed_entity_node(
    embedding_storage: "ChromaDBStorage",
    generator: "EmbeddingGenerator",
    node: CognitionNode,
) -> None:
    """THE single node-vector embed path (ledger 11) — used by both _record_node and
    cognition_update_node. Builds the embed text + metadata from the node and upserts
    under ``node.id``. Because upsert overwrites by id, calling this again after an
    edit REFRESHES the searchable vector (this is what makes update_node's re-embed
    correct — no stale vector, no ghost/duplicate). The caller must pass a node whose
    ``id`` is the FINAL (post-mint) id, else the vector lands under a stale id (A1).

    TASK lifecycle (B1): a ``task`` node carries ``status``/``owner`` in ``node.metadata``.
    Surface BOTH into the Chroma metadata dict AND the embed text (only when present) so
    metadata filters and semantic search both reflect lifecycle state. This is the SINGLE
    shared embed path, so create AND re-embed (cognition_update_task) pick it up once-and-
    done; for non-task nodes ``metadata`` is empty so this stays a no-op."""
    status = node.metadata.get("status")
    owner = node.metadata.get("owner")
    embed_text = f"{node.type.value}: {node.summary}\n{node.detail}"
    if status:
        embed_text += f"\nstatus: {status}"
    if owner:
        embed_text += f" owner: {owner}"
    embedding = generator.generate(embed_text, input_type="document")
    metadata: dict[str, Any] = {
        "entity_type": node.type.value,
        "summary": node.summary,
        "author": node.author,
        "timestamp": node.timestamp,
        "context": ",".join(node.context),
    }
    if node.severity:
        metadata["severity"] = node.severity
    if node.references:
        metadata["references"] = ",".join(node.references)
    if status:
        metadata["status"] = status
    if owner:
        metadata["owner"] = owner
    embedding_storage.upsert_embedding(node.id, embedding, metadata)


def _embed_workflow(
    embedding_storage: "ChromaDBStorage",
    generator: "EmbeddingGenerator",
    node: CognitionNode,
) -> int:
    """Embed a workflow node: ONE node-level vector + full body chunked into
    ``<node_id>#chunk-N`` vectors (each carrying ``is_chunk: True``). Mirrors the
    document chunking path so long procedures remain fully searchable.

    Delete-then-write (idempotent): purges this node's existing chunks BEFORE
    writing the fresh set so a re-chunk never orphans stale high-N chunks.
    The node-level vector's metadata carries ``chunk_count`` (WP-4,
    41ced8d1fa63) so a completeness check (e.g. the startup reconciler) can
    verify the FULL chunk set landed, not just chunk-0 — a crash mid-loop
    used to read as "fully synced" once chunk-0 existed, permanently hiding
    chunks 1..N. Returns the chunk count written."""
    wf_type = CognitionNodeType.WORKFLOW.value
    node_text = f"{wf_type}: {node.summary}\n{node.detail[:2000]}"
    chunks = chunk_text(node.detail)
    embedding_storage.upsert_embedding(
        node.id,
        generator.generate(node_text, input_type="document"),
        {
            "entity_type": wf_type,
            "summary": node.summary,
            "author": node.author,
            "timestamp": node.timestamp,
            "context": ",".join(node.context),
            "chunk_count": len(chunks),
        },
    )
    # Delete-then-write the chunk set (idempotent regardless of count change).
    embedding_storage.delete_by_node_id(node.id)
    for i, chunk in enumerate(chunks):
        embedding_storage.upsert_embedding(
            f"{node.id}#chunk-{i}",
            generator.generate(chunk, input_type="document"),
            {"node_id": node.id, "entity_type": wf_type, "is_chunk": True},
            document=chunk,
        )
    return len(chunks)


def _node_from_dict(node_id: str, data: dict[str, Any]) -> CognitionNode:
    """Reconstruct a CognitionNode from a storage node dict (CognitionStorage.
    get_node()'s return, which omits 'id'; or one row of get_all_nodes(),
    which includes it — either works since 'id' is never read FROM ``data``,
    only the explicit ``node_id`` param is used).

    THE single such reconstruction: cognition_update_node's re-embed,
    _reembed_replayed_nodes (WP-3), and _sync_cognition_embeddings (WP-4) all
    need it — kept in one place so the field mapping can't drift between
    them (the exact kind of drift 3e82d4ebc004 exists to close)."""
    return CognitionNode(
        id=node_id,
        type=CognitionNodeType(data["type"]),
        summary=data["summary"],
        detail=data["detail"],
        context=data.get("context", []),
        references=data.get("references", []),
        severity=data.get("severity"),
        timestamp=data["timestamp"],
        author=data["author"],
        metadata=data.get("metadata", {}),
    )


def _record_node(
    ctx: Context,
    node_type: CognitionNodeType,
    summary: str,
    detail: str,
    context: str,
    author: str,
    severity: str | None = None,
    references: str | None = None,
) -> dict[str, Any]:
    """Shared logic for cognition_record tool."""
    lc = get_lifespan(ctx)
    storage: CognitionStorage = lc["cognition_storage"]
    embedding_storage: ChromaDBStorage = lc["cognition_embedding_storage"]
    generator: EmbeddingGenerator = lc["embedding_generator"]

    # Parse comma-separated strings into lists
    context_list = [c.strip() for c in context.split(",") if c.strip()] if context else []
    references_list = [r.strip() for r in references.split(",") if r.strip()] if references else []

    timestamp = datetime.now(UTC).isoformat()
    node_id = generate_node_id(node_type.value, summary, timestamp)

    # WP-5 dedup-contract half (b36e4a79113a): two clones/agents can each
    # independently mint a distinct EPISODE for the SAME commit ref, and both
    # survive merges forever today (no automatic repair path — reconciliation is
    # supersedes, added manually by a curator; WP-14/7b9db5a8d675 retired
    # duplicate_of rather than building a repair path for it). Ref-lookup
    # BEFORE mint so this is visible instead of silent, WITHOUT changing
    # cognition_record's contract:
    # it still always creates what was asked (never silently substitutes/
    # reuses an existing node) — same philosophy as elsewhere in this
    # codebase (prefer a supersedes edge over deleting/replacing history). A
    # curator reconciles genuine duplicates manually. Scoped to episodes
    # (contrast: five different decisions legitimately sharing one commit ref
    # is normal, not a duplicate — episodes are the "one node per completed
    # unit of work" type where a shared ref really does suggest the same
    # work got recorded twice).
    possible_duplicates: list[str] = []
    if node_type == CognitionNodeType.EPISODE and references_list:
        seen: set[str] = set()
        for ref in references_list:
            for other_id in storage.find_nodes_by_ref(ref):
                if other_id in seen:
                    continue
                seen.add(other_id)
                other = storage.get_node(other_id)
                if other and other.get("type") == CognitionNodeType.EPISODE.value:
                    possible_duplicates.append(other_id)
        if possible_duplicates:
            logger.warning(
                "cognition_record: new episode shares a reference with "
                f"existing episode(s) {possible_duplicates} — possible "
                "duplicate (e.g. two clones independently minted an episode "
                "for the same commit); not auto-merged, consider a "
                "supersedes/duplicate note"
            )

    node = CognitionNode(
        id=node_id,
        type=node_type,
        summary=summary,
        detail=detail,
        context=context_list,
        references=references_list,
        severity=severity,
        timestamp=timestamp,
        author=author,
    )
    # WP-ID: mint a collision-free id under the lock (global fix). Rebind node_id to
    # the returned id BEFORE the embedding upsert + edges + result — else a salted node
    # lands in the graph under the minted id while its vector lands under the stale id,
    # leaving it silently unsearchable (A1). Carry the minted id into the node copy so
    # the shared embed path upserts under the FINAL id.
    node_id = storage.add_node(node, mint_unique_id=True)
    node = node.model_copy(update={"id": node_id})

    # Embed and upsert to ChromaDB (skip if model not loaded yet — startup sync catches it later).
    # Workflows chunk the full body for searchability; all other types use a single node vector.
    if _embeddings_ready(lc):
        if node_type == CognitionNodeType.WORKFLOW:
            _embed_workflow(embedding_storage, generator, node)
        else:
            _embed_entity_node(embedding_storage, generator, node)

    # Create deterministic part_of edges via reference matching. This is the ONLY
    # automatic edge creation — semantic curation (led_to, supersedes, contradicts,
    # etc.) is the agent's job via the /vibe-curate skill after recording.
    # Note: workflow-involving pairs are inert in the matcher (B1) so this is a no-op
    # for workflow nodes.
    det_edges = storage.create_deterministic_edges(node_id)

    result: dict[str, Any] = {
        "id": node_id,
        "type": node_type.value,
        "summary": summary,
        "timestamp": timestamp,
    }
    if det_edges:
        result["deterministic_edges_created"] = det_edges
    if possible_duplicates:
        result["possible_duplicate_of"] = possible_duplicates
    return result


def _parse_node_type(
    node_type: str | None,
) -> tuple[CognitionNodeType | None, dict[str, Any] | None]:
    """Parse an optional node_type string into the enum. THE single node_type parser
    (T-6): returns ``(enum, None)`` for a valid type, ``(None, None)`` for None, or
    ``(None, error_dict)`` for an invalid one — so every tool validates the same way
    and returns the same error shape (no bare raise, no silent empty-success)."""
    if node_type is None:
        return None, None
    try:
        return CognitionNodeType(node_type), None
    except ValueError:
        valid = [e.value for e in CognitionNodeType]
        return None, {"error": f"Invalid node_type '{node_type}'. Valid: {valid}"}


def _validate_direction(direction: str, allowed: tuple[str, ...]) -> dict[str, Any] | None:
    """Return an error dict for an unknown direction (T-6) instead of silently doing
    the wrong thing (treating it as incoming, or returning neither list)."""
    if direction not in allowed:
        return {"error": f"Invalid direction '{direction}'. Valid: {list(allowed)}"}
    return None


def _would_create_supersedes_cycle(storage: CognitionStorage, from_id: str, to_id: str) -> bool:
    """True if ``from_id`` is already reachable by following EXISTING ``supersedes``
    edges outward from ``to_id`` — i.e. adding ``from_id -> to_id`` would close a
    loop. Full traversal (not a single-path walk like get_superseded_chain's
    newest-first display) since a cycle can exist along ANY branch, not just the
    first one found. Mirrors cognition_update_task's re-parent cycle guard
    (``_task_ancestor_ids``): walk the graph BEFORE adding the edge."""
    visited: set[str] = set()
    stack = [to_id]
    while stack:
        current = stack.pop()
        if current == from_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        for succ_id, _ in storage.get_successors(current, CognitionEdgeType.SUPERSEDES):
            stack.append(succ_id)
    return False


def _validate_supersedes_shape(
    storage: CognitionStorage, from_id: str, to_id: str
) -> dict[str, Any] | None:
    """Enforce the WP-14 supersedes shape rule (decision 7b9db5a8d675, refined
    live against the real journal): a supersedes edge is legal iff (a) from_id
    and to_id are the SAME node type (the "newer version of the same thing"
    case), OR (b) the from-node is fail/incident AND the to-node is NOT workflow
    (the RETRACTION case — "this claim/decision turned out to be wrong").
    Everything else is rejected with an error naming both legal shapes.

    Same-type protects typed chain-resolution: workflow is the only type whose
    RETRIEVAL resolves via supersession head (get_workflow_head), so a workflow
    chain must never resolve to a non-workflow node — a wrong runbook gets
    retracted by superseding it with a corrected WORKFLOW (or a `contradicts`
    edge from the fail), never by a fail sitting at the head of the chain.

    Also enforces no-cycle via ``_would_create_supersedes_cycle``.

    Returns an error dict, or None if the edge is legal. Caller must confirm
    both nodes exist first (this assumes ``storage.get_node`` returns non-None)."""
    from_node = storage.get_node(from_id)
    to_node = storage.get_node(to_id)
    ft = from_node.get("type") if from_node else None
    tt = to_node.get("type") if to_node else None
    same_type = ft is not None and ft == tt
    is_retraction = (
        ft in (CognitionNodeType.FAIL.value, CognitionNodeType.INCIDENT.value)
        and tt != CognitionNodeType.WORKFLOW.value
    )
    if not (same_type or is_retraction):
        return {
            "error": (
                f"supersedes rejected: '{from_id}' ({ft}) -> '{to_id}' ({tt}) is neither "
                "same-type (newer version of the same thing) nor a fail/incident "
                "retracting a non-workflow node (the only two legal shapes)."
            )
        }
    if _would_create_supersedes_cycle(storage, from_id, to_id):
        return {
            "error": (
                f"supersedes rejected: '{from_id}' -> '{to_id}' would create a cycle "
                "(to_id already transitively supersedes from_id)"
            )
        }
    return None


def _add_edge_core(
    storage: CognitionStorage,
    from_id: str,
    to_id: str,
    edge_type: str,
    reason: str | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    """Validate + create one edge (testable core of cognition_add_edge)."""
    try:
        et = CognitionEdgeType(edge_type)
    except ValueError:
        valid = [e.value for e in CognitionEdgeType]
        return {"error": f"Invalid edge_type '{edge_type}'. Valid: {valid}"}
    if from_id == to_id:
        return {"error": "Self-referencing edges are not allowed"}
    if not storage.has_node(from_id):
        return {"error": f"Source node '{from_id}' does not exist"}
    if not storage.has_node(to_id):
        return {"error": f"Target node '{to_id}' does not exist"}
    if any(tid == to_id for tid, _ in storage.get_successors(from_id, et)):
        return {"error": f"Edge already exists: {from_id} -[{edge_type}]-> {to_id}"}
    if et == CognitionEdgeType.SUPERSEDES:
        shape_error = _validate_supersedes_shape(storage, from_id, to_id)
        if shape_error:
            return shape_error

    timestamp = datetime.now(UTC).isoformat()
    edge = CognitionEdge(
        from_id=from_id, to_id=to_id, edge_type=et, timestamp=timestamp,
        source=source, reason=reason,
    )
    # C-5: add_edge returns False if a node vanished between the has_node check and
    # the write (cross-process delete race) — surface it, don't report created:True.
    if not storage.add_edge(edge):
        return {"error": f"Edge not created: a node ('{from_id}' or '{to_id}') is missing"}
    if reason:
        logger.info(f"Edge created: {from_id} -[{edge_type}]-> {to_id} (reason: {reason})")
    return {
        "created": True,
        "from_id": from_id,
        "to_id": to_id,
        "edge_type": edge_type,
        "timestamp": timestamp,
    }


def _add_edges_batch_core(storage: CognitionStorage, edges: str) -> dict[str, Any]:
    """Validate + create a batch of edges (testable core of cognition_add_edges_batch).
    Every malformed input is skip-and-reported — no element can crash the batch after
    earlier edges were already committed (T-3)."""
    import json as _json
    try:
        edge_list = _json.loads(edges)
    except _json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}
    if not isinstance(edge_list, list):
        return {"error": "Expected a JSON array of edge objects"}
    if len(edge_list) > 500:
        return {"error": f"Max 500 edges per batch, got {len(edge_list)}"}

    created = 0
    skipped = 0
    errors: list[str] = []
    seen_triples: set[tuple[str, str, str]] = set()

    for i, e in enumerate(edge_list):
        # T-3: a non-dict element must be skipped-and-reported like every other
        # malformed input — NOT crash on e.get(...) after earlier edges were journaled.
        if not isinstance(e, dict):
            errors.append(f"[{i}] Not an edge object (expected a JSON object)")
            skipped += 1
            continue
        fid = e.get("from_id", "")
        tid = e.get("to_id", "")
        etype_str = e.get("edge_type", "")
        src = e.get("source", "batch")
        triple = (fid, tid, etype_str)

        try:
            et = CognitionEdgeType(etype_str)
        except ValueError:
            errors.append(f"[{i}] Invalid edge_type '{etype_str}'")
            skipped += 1
            continue
        if fid == tid:
            errors.append(f"[{i}] Self-reference: {fid}")
            skipped += 1
            continue
        if not storage.has_node(fid):
            errors.append(f"[{i}] Missing from_id: {fid}")
            skipped += 1
            continue
        if not storage.has_node(tid):
            errors.append(f"[{i}] Missing to_id: {tid}")
            skipped += 1
            continue
        if triple in seen_triples:
            errors.append(f"[{i}] Duplicate in batch: {fid} -[{etype_str}]-> {tid}")
            skipped += 1
            continue
        if any(t == tid for t, _ in storage.get_successors(fid, et)):
            errors.append(f"[{i}] Already exists: {fid} -[{etype_str}]-> {tid}")
            skipped += 1
            continue
        if et == CognitionEdgeType.SUPERSEDES:
            shape_error = _validate_supersedes_shape(storage, fid, tid)
            if shape_error:
                errors.append(f"[{i}] {shape_error['error']}")
                skipped += 1
                continue

        seen_triples.add(triple)
        timestamp = datetime.now(UTC).isoformat()
        edge = CognitionEdge(
            from_id=fid, to_id=tid, edge_type=et, timestamp=timestamp, source=src,
            reason=e.get("reason"),
        )
        if not storage.add_edge(edge):  # C-5: surface a failed add, don't count it created
            errors.append(f"[{i}] Not created: a node is missing ({fid} or {tid})")
            skipped += 1
            continue
        created += 1

    return {"created": created, "skipped": skipped, "errors": errors[:50]}


_MATCHED_EXCERPT_LEN = 500   # chars of chunk text returned as the match excerpt


def _format_search_results(
    results: list[dict[str, Any]], storage: CognitionStorage, limit: int
) -> list[dict[str, Any]]:
    """Dedupe over-queried hits to the BEST hit per node, dropping graph-absent
    nodes, and carry a ``matched_excerpt`` for chunk hits — the N1 fix + D2 dedupe.

    N1 (§9): a cross-process remove_node replays into the graph but never un-embeds,
    so Chroma serves hits for nodes deleted on another machine — escalated by
    documents to verbatim deleted client text. Dropping hits whose (chunk-stripped)
    node id is absent from the graph is the CORRECTNESS guarantee (it never deletes;
    the startup sweep is best-effort reclamation).

    D2 dedupe: a document yields many ``<node_id>#chunk-N`` hits; collapse them to one
    result keyed on the NODE id (results arrive score-desc from vector_search, so the
    FIRST hit per node is the best), carrying its chunk text as ``matched_excerpt``.

    WP-12 (db65f1568fa5): a DOCUMENT hit ALSO gets a cheap ``staleness`` key (stat-only,
    no file read — see ``cheap_staleness_signal``) when the cheap check finds something.
    This is a MUCH weaker signal than ``cognition_get_document``'s full re-hash
    ``freshness`` field (a same-size content edit isn't caught) — deliberately, so search
    cost never scales with re-reading every matched document's full referenced file.

    Returns at most ``limit`` deduped nodes."""
    formatted: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    for r in results:
        raw_id = r.get("_id") or ""
        if not storage.search_hit_is_live(raw_id):  # N1 drop (shared predicate)
            continue
        node_id = raw_id.split("#chunk-")[0]
        if node_id in seen_nodes:  # keep only the best (first) hit per node
            continue
        seen_nodes.add(node_id)
        node_type = r.get("entity_type")
        entry: dict[str, Any] = {
            "id": node_id,  # the NODE id, never the chunk id
            "node_type": node_type,
            "summary": r.get("summary") or r.get("name"),
            "author": r.get("author"),
            "timestamp": r.get("timestamp"),
            "severity": r.get("severity"),
            "context": r.get("context", ""),
            "score": r.get("score"),
        }
        if node_type == CognitionNodeType.DOCUMENT.value:
            doc_node = storage.get_node(node_id)  # cheap: in-memory graph lookup
            if doc_node:
                staleness = cheap_staleness_signal(doc_node.get("metadata", {}))
                if staleness:
                    entry["staleness"] = staleness
        matched = r.get("matched_text")
        if matched:
            entry["matched_excerpt"] = matched[:_MATCHED_EXCERPT_LEN]
        formatted.append(entry)
        if len(formatted) >= limit:
            break
    return formatted


def _reembed_replayed_nodes(
    storage: CognitionStorage,
    embedding_storage: ChromaDBStorage,
    generator: EmbeddingGenerator,
) -> int:
    """Close the node<->embedding structural drift for nodes that arrived via
    journal REPLAY — another process's write — rather than this process's own
    _record_node/cognition_update_task call (those already embed synchronously
    at write time). Without this, a teammate's node stays visible to
    get_history but invisible to cognition_search until a server restart
    (discovery 4b99fa9f44d5; WP-3 task 8606d59905a5).

    Called at the cognition_search boundary (see caller — home path only, see
    its comment for why) since that's exactly where the drift becomes user-
    visible; the common case (nothing replayed since the last search) costs
    one empty pop() and zero model calls.

    Routes through the SAME shared embed paths _record_node uses
    (_embed_entity_node / _embed_workflow) — not a third bespoke copy (the
    plan's explicit pin; WP-4 will also converge the startup reconciler onto
    these). DOCUMENT nodes are skipped: they need sidecar-text + chunk
    handling that only the existing startup _sync_cognition_embeddings path
    does today — left to that path, same as before this fix (no regression).

    Filters to ids ACTUALLY missing from Chroma (one batched existence check)
    before embedding anything, so redundant catch-up of this process's OWN
    just-written nodes doesn't cost a model call on every search. Never
    raises — a per-node failure is logged and skipped so it can't block the
    search that triggered this or take down the rest of the batch.
    """
    pending = storage.pop_replayed_node_ids()
    if not pending:
        return 0

    try:
        existing = set(embedding_storage._collection.get(ids=pending)["ids"])
    except Exception:
        existing = set()
    missing = [nid for nid in pending if nid not in existing]
    if not missing:
        return 0

    doc_type = CognitionNodeType.DOCUMENT.value
    embedded = 0
    for node_id in missing:
        data = storage.get_node(node_id)
        if data is None or data.get("type") == doc_type:
            continue
        try:
            node = _node_from_dict(node_id, data)
            if node.type == CognitionNodeType.WORKFLOW:
                _embed_workflow(embedding_storage, generator, node)
            else:
                _embed_entity_node(embedding_storage, generator, node)
            embedded += 1
        except Exception as e:
            logger.warning(f"re-embed-on-replay: failed for {node_id}: {e}")
    return embedded


def _search_with_embedding(
    storage: CognitionStorage,
    embedding_storage: ChromaDBStorage,
    query_embedding: list[float],
    node_type: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Inner search: takes a pre-computed embedding, returns formatted result list.

    Extracted so multi-project search can embed the query ONCE and fan over entries
    without re-embedding per project. ``storage`` must be the storage for THIS
    ``embedding_storage`` — the N1 ghost-filter (search_hit_is_live) in
    _format_search_results uses it to check node presence; passing mismatched
    storage silently drops all hits from the foreign graph (XP2 correctness trap).
    """
    return adaptive_vector_search(
        embedding_storage,
        query_embedding,
        entity_type=node_type,
        limit=limit,
        dedupe=lambda results, lim: _format_search_results(results, storage, lim),
    )


def _search_cognition(
    storage: CognitionStorage,
    embedding_storage: ChromaDBStorage,
    generator: EmbeddingGenerator,
    query: str,
    node_type: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Semantic search core (testable; cognition_search is the thin ctx wrapper).

    Embeds the query, ADAPTIVELY over-queries Chroma, then dedupes-to-best-hit-per-
    node + drops graph-absent ghosts (N1) via _format_search_results. Keeping
    vector_search + the filter together here lets the cross-process ghost test
    exercise the REAL search path end-to-end.

    Adaptive over-query (peer-review B3): a FIXED limit×k cannot guarantee `limit`
    distinct nodes — a single document can yield more than limit×k chunks and starve
    other live nodes. So widen n_results (doubling) until we have `limit` distinct
    live nodes, OR Chroma is exhausted (fewer hits than asked), OR a cap is hit.
    Doubling keeps round-trips logarithmic. The single-document-with->cap-chunks case
    is the only residual (capped), and only degrades recall (never serves a wrong or
    deleted node — the dedupe + N1 filter are exact)."""
    limit = min(limit, 50)
    query_embedding = generator.generate_query_embedding(query)
    formatted = _search_with_embedding(storage, embedding_storage, query_embedding, node_type, limit)
    return {"query": query, "results": formatted, "count": len(formatted)}


def _materialize_blob(
    cognition_dir: Path, sha: str, ext: str, blob_rel: str, size: int,
    local_only: bool | None, *, data: bytes | None, src: Path | None,
) -> tuple[bool, dict[str, Any], list[str]]:
    """Write the content-addressed blob (write-once) and reconcile its git policy.

    Returns ``(effective_local_only, status, warnings)``. Size policy §9 S1 (no
    hard cap): ≥50MB auto local_only + warn; ≥95MB default-commit refused (forced
    local_only). S3 dedup transitions: a blob that WAS local_only going to default
    is ``promoted`` (its .gitignore line removed); a blob already on the commit track
    going to local_only reports ``already_committed`` (git can't un-publish it).

    ``already_committed`` is a conservative PROXY: it fires when the blob already
    existed un-ignored (on the default-commit track), which is the case git may
    already hold — not proof of an actual commit. The warning is safe either way."""
    was_ignored = gitignore_has_entry(cognition_dir, blob_rel)
    blob_existed = blob_path(cognition_dir, sha, ext).exists()
    warnings: list[str] = []
    eff_local = bool(local_only)
    if size >= BLOB_REFUSE_BYTES and not local_only:
        eff_local = True
        warnings.append(f"size {size}B >= 95MB: default-commit refused; stored local_only")
    elif size >= BLOB_WARN_BYTES and not local_only:
        eff_local = True
        warnings.append(f"size {size}B >= 50MB: auto local_only (exceeds GitHub push limit)")

    status: dict[str, Any] = {}
    already_committed = eff_local and blob_existed and not was_ignored
    if eff_local and already_committed:
        # Already on the default-commit track. A .gitignore line here would be
        # INERT (git keeps tracking an already-tracked file), so don't write a
        # misleading one — just warn that local_only can't retroactively un-publish.
        status["already_committed"] = True
        warnings.append(
            "blob is on the default-commit track; local_only cannot retroactively "
            "un-publish a committed blob (git history retains it)"
        )
    elif eff_local:
        # WP-12 (07fdfe725e7f): gitignore BEFORE the blob write, not after — the
        # previous order left a brief window where a privacy-intended local_only
        # blob sat unignored on disk; a coincident `git add`/commit in that window
        # could have published it. was_ignored/blob_existed above were already
        # computed pre-write, so this reorder changes nothing else.
        add_gitignore_entry(cognition_dir, blob_rel)

    write_blob(cognition_dir, sha, ext, data=data, src_path=src)

    if not eff_local and remove_gitignore_entry(cognition_dir, blob_rel):
        status["promoted"] = True
    return eff_local, status, warnings


def _embed_document(
    embedding_storage: ChromaDBStorage,
    generator: EmbeddingGenerator,
    node_id: str,
    summary: str,
    detail: str,
    sidecar_text: str,
) -> int:
    """Embed a document: ONE node-level vector (so it shows in node search) + its
    sidecar text chunked into ``<node_id>#chunk-N`` vectors (each carrying its chunk
    text + ``is_chunk: True``). THE single chunk-write path — store-time (D2 c3) and
    re-sync (c4) both call it, so the chunk-metadata contract can't drift.

    Delete-then-write (peer-review A5): purge this node's existing chunks BEFORE
    writing the fresh set, so a re-chunk that yields FEWER chunks can't orphan stale
    high-N chunks under the live node_id (which would surface as ghost excerpts of
    deleted text). The node-level vector's metadata carries ``chunk_count`` (WP-4,
    41ced8d1fa63) so a completeness check can verify the FULL chunk set landed,
    not just chunk-0 — a crash mid-loop used to read as "fully synced" once
    chunk-0 existed, permanently hiding chunks 1..N. Returns the chunk count
    written."""
    doc_type = CognitionNodeType.DOCUMENT.value
    node_text = f"{doc_type}: {summary}\n{detail}"
    chunks = chunk_text(sidecar_text)
    embedding_storage.upsert_embedding(
        node_id,
        generator.generate(node_text, input_type="document"),
        {"entity_type": doc_type, "summary": summary, "chunk_count": len(chunks)},
    )
    # Delete-then-write the chunk set (idempotent regardless of count change).
    embedding_storage.delete_by_node_id(node_id)
    for i, chunk in enumerate(chunks):
        embedding_storage.upsert_embedding(
            f"{node_id}#chunk-{i}",
            generator.generate(chunk, input_type="document"),
            {"node_id": node_id, "entity_type": doc_type, "is_chunk": True},
            document=chunk,
        )
    return len(chunks)


def _store_document(
    storage: CognitionStorage,
    title: str,
    document_text: str,
    context: str,
    author: str,
    file_path: str | None = None,
    content_text: str | None = None,
    references: str | None = None,
    mime: str | None = None,
    force_new: bool = False,
    store_copy: bool = False,
    local_only: bool | None = None,
    embedding_storage: "ChromaDBStorage | None" = None,
    generator: "EmbeddingGenerator | None" = None,
) -> dict[str, Any]:
    """Document store, reference (default) or opt-in copy mode (testable core of
    cognition_store_document). ``store_copy`` copies the bytes into a content-
    addressed blob; ``local_only`` keeps that blob out of git (else committed,
    subject to the size policy).

    If ``embedding_storage`` AND ``generator`` are both provided, the new document is
    embedded (node vector + sidecar chunks) for search. Both default to None — the
    embedding is SKIPPED then (storage-only callers, or the model still loading), and
    the next ``_sync`` backfills it. Never blocks/fails the store on embedding."""
    cognition_dir = storage.cognition_dir

    if file_path and content_text is not None:
        return {"error": "provide file_path OR content_text, not both"}
    blob_data: bytes | None = None
    blob_src: Path | None = None
    if file_path:
        p = Path(file_path)
        if not p.is_file():
            return {"error": f"file_path not found: {file_path}"}
        sha = sha256_file(p)
        size = p.stat().st_size
        filename = p.name
        source_path: str | None = str(p.resolve())
        blob_src = p
    elif content_text is not None:
        data = content_text.encode("utf-8")
        sha = sha256_bytes(data)
        size = len(data)
        filename = title
        source_path = None
        blob_data = data
    else:
        return {"error": "provide file_path or content_text"}

    ref = doc_ref(sha)
    ext = sanitize_extension(Path(filename).suffix)
    blob_rel = blob_rel_path(sha, ext)

    # Dedup via the ONE shared document-identity predicate (storage.documents_with_sha
    # confirms type==document AND full sha — the same expression sidecar/blob reclaim
    # use, so dedup and delete cannot drift; F1 root cause).
    if not force_new:
        for nid in storage.documents_with_sha(sha):
            existing = storage.get_node(nid)
            meta = existing.get("metadata", {}) if existing else {}
            result: dict[str, Any] = {
                "node_id": nid,
                "doc_ref": ref,
                "mode": meta.get("mode", "reference"),
                "size": meta.get("size", size),
                "indexed_text_chars": meta.get("indexed_text_chars", 0),
                "already_stored": True,
            }
            # S3: store_copy on an existing node ensures the blob + reconciles git
            # policy (promote/demote). Node returned as-is otherwise (context NOT
            # merged — stated). Updates only the blob-policy metadata keys.
            if store_copy:
                eff_local, status, warnings = _materialize_blob(
                    cognition_dir, sha, ext, blob_rel, size, local_only,
                    data=blob_data, src=blob_src,
                )
                storage.update_node(nid, metadata={
                    **meta, "mode": "copy", "blob_path": blob_rel,
                    "local_only": eff_local, "blob_bytes": size,
                })
                result.update({
                    "mode": "copy", "blob_bytes": size, "blob_path": blob_rel,
                    "local_only": eff_local, **status,
                })
                if warnings:
                    result["warnings"] = warnings
            return result

    # WP-12 (db65f1568fa5): OFFER (never auto-create) a supersedes linkage when this
    # store is a re-store of a CHANGED file at a path some EXISTING document node
    # already references (same path, different sha — the dedup loop above already
    # returned early for the "same sha" case, so reaching here means either this path
    # is new or its content genuinely changed). Manual-only linking is the recorded
    # design (e752ff313ad7 §8d) — this only surfaces the prior node id + a note; the
    # caller decides whether to add the edge via cognition_add_edge.
    prior_version_id: str | None = None
    if source_path:
        for doc in storage.get_nodes_by_type(CognitionNodeType.DOCUMENT):
            dmeta = doc.get("metadata", {})
            if dmeta.get("path") == source_path and dmeta.get("sha256") != sha:
                prior_version_id = doc["id"]
                break

    indexed_chars = write_text_sidecar(cognition_dir, sha, document_text)

    # Copy mode (new node): materialize the blob + resolve git policy now, so the
    # node's metadata records the blob path/policy it owns.
    mode = "reference"
    blob_meta: dict[str, Any] = {}
    blob_result: dict[str, Any] = {}
    if store_copy:
        eff_local, status, warnings = _materialize_blob(
            cognition_dir, sha, ext, blob_rel, size, local_only,
            data=blob_data, src=blob_src,
        )
        mode = "copy"
        blob_meta = {"blob_path": blob_rel, "local_only": eff_local, "blob_bytes": size}
        blob_result = {"blob_bytes": size, "blob_path": blob_rel, "local_only": eff_local, **status}
        if warnings:
            blob_result["warnings"] = warnings

    context_list = [c.strip() for c in context.split(",") if c.strip()] if context else []
    # S4/N3: agent-supplied refs go to CONTEXT; the document node's OWN references
    # are restricted to its doc: key so old plugin versions can't link it on a
    # shared issue:/commit: ref and the matcher gates on doc:.
    if references:
        context_list += [r.strip() for r in references.split(",") if r.strip()]

    timestamp = datetime.now(UTC).isoformat()
    # WP-ID: id-collision minting is now unified into storage.add_node (mint_unique_id);
    # the document-scoped salt loop here is removed (one mechanism — ledger 11).
    node_id = generate_node_id(CognitionNodeType.DOCUMENT.value, title, timestamp)
    metadata: dict[str, Any] = {
        "filename": filename,
        "mime": mime or "",
        "size": size,
        "sha256": sha,
        "mode": mode,
        "indexed_text_chars": indexed_chars,
        **blob_meta,
    }
    if source_path:
        metadata["path"] = source_path

    node = CognitionNode(
        id=node_id,
        type=CognitionNodeType.DOCUMENT,
        summary=title,
        detail=document_text[:2000],  # bounded abstract; full text lives in the sidecar
        context=context_list,
        references=[ref],
        severity=None,
        timestamp=timestamp,
        author=author,
        metadata=metadata,
    )
    # WP-ID: mint a collision-free id under the lock. Rebind node_id to the return
    # BEFORE edges + embedding — else the doc node vector and every <id>#chunk-N vector
    # land under the stale id → unsearchable document + orphaned chunk vectors (A2).
    node_id = storage.add_node(node, mint_unique_id=True)
    storage.create_deterministic_edges(node_id)
    # WP-D2: embed the document (node vector + sidecar chunks) so it's searchable.
    # Skipped if embedding deps absent (storage-only caller, or model still loading) —
    # the next _sync backfills it. Never block/fail the store on embedding.
    if embedding_storage is not None and generator is not None:
        _embed_document(embedding_storage, generator, node_id, title,
                        document_text[:2000], document_text)

    result = {
        "node_id": node_id,
        "doc_ref": ref,
        "mode": mode,
        "size": size,
        "indexed_text_chars": indexed_chars,
        **blob_result,
    }
    if prior_version_id:
        result["prior_version_id"] = prior_version_id
        result["consider_supersedes"] = (
            f"a prior document node ({prior_version_id}) exists at this same path "
            "with different content — this was NOT auto-linked (manual-only by "
            f"design); if this is genuinely a new version, link it yourself: "
            f"cognition_add_edge(from_id='{node_id}', to_id='{prior_version_id}', "
            "edge_type='supersedes')"
        )
    return result


def _get_document(
    storage: CognitionStorage,
    node_id: str | None = None,
    doc_ref_arg: str | None = None,
) -> dict[str, Any]:
    """Retrieve a document + freshness (testable core of cognition_get_document)."""
    cognition_dir = storage.cognition_dir

    resolved_id: str | None = None
    if node_id:
        resolved_id = node_id
    elif doc_ref_arg:
        for cand in storage.find_nodes_by_ref(doc_ref_arg):
            cdata = storage.get_node(cand)
            if cdata and cdata.get("type") == CognitionNodeType.DOCUMENT.value:
                resolved_id = cand
                break
    else:
        return {"error": "provide node_id or doc_ref_arg"}

    node = storage.get_node(resolved_id) if resolved_id else None
    if not node or node.get("type") != CognitionNodeType.DOCUMENT.value:
        return {"error": "document not found"}

    meta = node.get("metadata", {})
    sha = meta.get("sha256", "")
    text = read_text_sidecar(cognition_dir, sha)

    # Freshness (reference mode): re-hash the referenced original. A missing /
    # unreadable path returns "missing" — never raises. (Re-hash reads the full
    # file; that cost scales with the referenced document's size.)
    freshness = "unchanged"
    path = meta.get("path")
    if path:
        fp = Path(path)
        if not fp.is_file():
            freshness = "missing"
        else:
            try:
                freshness = "unchanged" if sha256_file(fp) == sha else "modified"
            except OSError:
                freshness = "missing"

    return {
        "node_id": resolved_id,
        "doc_ref": doc_ref(sha) if sha else None,
        "metadata": meta,
        "text": text,
        "path": path,
        "freshness": freshness,
    }


def _get_node(storage: CognitionStorage, node_id: str) -> dict[str, Any]:
    """Read a single node's FULL narrative by id (testable core of cognition_get_node).

    ``storage.get_node`` returns the bare graph attributes (no ``id`` — that's the
    graph key), so re-attach ``id`` to give a self-describing dict. Returns an error
    dict for an absent node rather than raising or returning ``None``."""
    node = storage.get_node(node_id)
    if node is None:
        return {"error": f"Node '{node_id}' does not exist"}
    return {"id": node_id, **node}


def _update_node(
    storage: CognitionStorage,
    embedding_storage: "ChromaDBStorage",
    generator: "EmbeddingGenerator",
    *,
    node_id: str,
    embeddings_ready: bool,
    summary: str | None = None,
    detail: str | None = None,
    context: str | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    """Edit a node's narrative fields in place (testable core of cognition_update_node).

    WHITELIST: only summary/detail/context/severity. Structural fields (id, type,
    references, metadata, timestamp) are NOT editable here — editing them would
    corrupt invariants (a document's sha/mode/doc: ref, the part_of edge index, the
    minted id). RE-EMBEDS the node vector when summary or detail changed and the model
    is ready, so cognition_search reflects the edit instead of serving the stale vector
    (the silent-staleness failure mode); if the model isn't ready it reports
    reembed="deferred" (the vector stays stale until a future re-embed — rare, an edit
    needs a loaded model anyway)."""
    if storage.get_node(node_id) is None:
        return {"error": f"Node '{node_id}' does not exist"}

    # B5: block in-place edits on workflow nodes — they are versioned by supersession.
    existing = storage.get_node(node_id)
    if existing and existing.get("type") == CognitionNodeType.WORKFLOW.value:
        return {
            "error": (
                "Workflow nodes are versioned by supersession — record a new workflow "
                "node with the full updated procedure and add a `supersedes` edge to "
                "this one; do not edit in place."
            )
        }

    updates: dict[str, Any] = {}
    if summary is not None:
        updates["summary"] = summary
    if detail is not None:
        updates["detail"] = detail
    if context is not None:
        updates["context"] = [c.strip() for c in context.split(",") if c.strip()]
    if severity is not None:
        updates["severity"] = severity

    if not updates:
        return {"error": "No updatable fields provided (summary, detail, context, severity)"}

    storage.update_node(node_id, **updates)

    # Re-embed on ANY whitelisted change. summary/detail change the searchable VECTOR;
    # context/severity don't, but they ARE stored in the Chroma metadata that
    # _format_search_results surfaces in every hit — so a context/severity-only edit
    # would otherwise leave search results DISPLAYING the old values (the same silent
    # search-staleness this tool exists to kill). For such an edit _embed_entity_node
    # regenerates an identical vector (the embed text is unchanged) but refreshes the
    # metadata via the same upsert — negligible cost on a rare path. If the model isn't
    # ready, defer (the vector/metadata stay stale until a future re-embed — rare, an
    # edit needs a loaded model anyway).
    if embeddings_ready:
        post = storage.get_node(node_id)
        assert post is not None  # just updated it; cannot vanish under the lock
        cnode = _node_from_dict(node_id, post)
        _embed_entity_node(embedding_storage, generator, cnode)
        reembed = "done"
    else:
        reembed = "deferred"

    result = _get_node(storage, node_id)
    result["reembed"] = reembed
    return result


# ── Task node core logic (cognition_add_task / _list_tasks / _update_task) ───
#
# A ``task`` is trackable open work, server-attributed to the git user. Unlike
# ``workflow`` (inert + supersession-versioned), a task is concise (one entity
# vector), curatable, and EDITED IN PLACE via a mutable ``status`` carried in
# ``metadata``. The lifecycle constants below are the single source of truth shared
# by _update_task and its tests.

# Status vocabulary (locked clarifier 6). ``open`` is the seeded default.
_TASK_STATUSES: tuple[str, ...] = ("open", "in_progress", "blocked", "done", "cancelled")

# Priority vocabulary (WP-12, 4ae72cafb48c) — derived from SEVERITY_ORDER's keys (the
# dict priority is stored under, via `severity=priority`) so the two can't drift apart.
# Previously unvalidated: a typo like "urgent"/"P0" silently fell into the "normal" sort
# band (SEVERITY_ORDER.get(..., 2) fallback) and evaded priority= filters silently.
_TASK_PRIORITIES: tuple[str, ...] = tuple(SEVERITY_ORDER)

# Legal status transitions. Reopen (done/cancelled → open) is intentionally allowed —
# real backlogs reopen work. Kept small and in ONE constant so the tool + tests share it.
_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"in_progress", "blocked", "cancelled", "done"}),
    "in_progress": frozenset({"blocked", "done", "cancelled", "open"}),
    "blocked": frozenset({"in_progress", "open", "cancelled", "done"}),
    "done": frozenset({"open"}),
    "cancelled": frozenset({"open"}),
}

# Statuses excluded from the default backlog view (list_tasks) and the prime injection.
_TASK_CLOSED_STATUSES: frozenset[str] = frozenset({"done", "cancelled"})

# The "task-parent" provenance tags the one part_of edge that mirrors metadata.parent_id,
# distinguishing it from a /vibe-curate cluster-membership part_of edge (which must NOT
# be disturbed by a re-parent).
_TASK_PARENT_EDGE_SOURCE = "task-parent"


def _task_ancestor_ids(storage: CognitionStorage, start_id: str) -> set[str]:
    """The set of ancestor task ids reachable by walking ``metadata.parent_id`` upward
    from (and excluding) ``start_id``. Cycle-safe (a pre-existing parent_id cycle can't
    hang the walk). Used by the re-parent cycle guard."""
    ancestors: set[str] = set()
    current = start_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        node = storage.get_node(current)
        if not node:
            break
        parent = node.get("metadata", {}).get("parent_id")
        if not parent:
            break
        ancestors.add(parent)
        current = parent
    return ancestors


def _add_task(
    ctx: Context,
    summary: str,
    detail: str,
    context: str,
    priority: str = "normal",
    owner: str | None = None,
    parent_id: str | None = None,
    references: str | None = None,
) -> dict[str, Any]:
    """Create a task node (testable core of cognition_add_task).

    Resolves the git identity SERVER-SIDE (no client ``created_by`` param), seeds the
    lifecycle ``metadata`` (status=open, created_by, owner, parent_id, initial
    transition), mints a collision-free id, creates the explicit child→parent
    ``part_of`` edge when ``parent_id`` is given, and embeds (status/owner surfaced via
    the shared _embed_entity_node path). ``priority`` is stored as the node's
    ``severity`` so it sorts via SEVERITY_ORDER for free."""
    if priority not in _TASK_PRIORITIES:
        return {"error": f"Invalid priority '{priority}'. Valid: {list(_TASK_PRIORITIES)}"}

    lc = get_lifespan(ctx)
    storage: CognitionStorage = lc["cognition_storage"]
    embedding_storage: ChromaDBStorage = lc["cognition_embedding_storage"]
    generator: EmbeddingGenerator = lc["embedding_generator"]

    # Validate the parent BEFORE creating the node, so a bad parent_id never leaves an
    # orphan task behind. Parent must exist AND be a task.
    if parent_id:
        parent = storage.get_node(parent_id)
        if parent is None:
            return {"error": f"Parent task '{parent_id}' does not exist"}
        if parent.get("type") != CognitionNodeType.TASK.value:
            return {"error": f"Parent '{parent_id}' is not a task (type: {parent.get('type')})"}

    context_list = [c.strip() for c in context.split(",") if c.strip()] if context else []
    references_list = [r.strip() for r in references.split(",") if r.strip()] if references else []

    timestamp = datetime.now(UTC).isoformat()
    # Server-resolved identity from the repo backing THIS storage (home project).
    # Assumes the standard layout where the repo root is the .cognition dir's parent
    # (true for every real install; config.py resolves the same root from REPO_PATH).
    created_by = resolve_git_identity(storage.cognition_dir.parent)

    metadata: dict[str, Any] = {
        "status": "open",
        "created_by": created_by,
        "owner": owner,
        "parent_id": parent_id,
        "transitions": [{"status": "open", "at": timestamp, "by": created_by}],
    }

    node_id = generate_node_id(CognitionNodeType.TASK.value, summary, timestamp)
    node = CognitionNode(
        id=node_id,
        type=CognitionNodeType.TASK,
        summary=summary,
        detail=detail,
        context=context_list,
        references=references_list,
        severity=priority,  # priority IS severity (reuses SEVERITY_ORDER sort/surfacing)
        timestamp=timestamp,
        author=created_by["name"],  # author mirrors the server-resolved name (no client author)
        metadata=metadata,
    )
    # WP-ID: mint a collision-free id under the lock; rebind BEFORE the edge + embed so
    # both land under the FINAL id (same A1 discipline as _record_node/_store_document).
    node_id = storage.add_node(node, mint_unique_id=True)
    node = node.model_copy(update={"id": node_id})

    # Explicit parent hierarchy edge (NOT reference-matched — task is matcher-inert). The
    # "task-parent" source distinguishes it from a curate cluster-membership part_of edge.
    if parent_id:
        storage.add_edge(CognitionEdge(
            from_id=node_id,
            to_id=parent_id,
            edge_type=CognitionEdgeType.PART_OF,
            timestamp=timestamp,
            source=_TASK_PARENT_EDGE_SOURCE,
        ))

    # Embed (single node vector; status/owner surfaced by _embed_entity_node). Skip if the
    # model isn't loaded yet — the startup sync backfills it.
    if _embeddings_ready(lc):
        _embed_entity_node(embedding_storage, generator, node)

    return _get_node(storage, node_id)


def _list_tasks(
    storage: CognitionStorage,
    status: str | None = None,
    priority: str | None = None,
    owner: str | None = None,
    parent_id: str | None = None,
    include_done: bool = False,
) -> dict[str, Any]:
    """List tasks filtered + sorted (testable core of cognition_list_tasks).

    Home-project only. Sorts by priority (SEVERITY_ORDER) then recency (newest first).
    Default EXCLUDES done/cancelled. Each row carries a ``depth`` = the number of present
    ancestors in the visible result set (so the hierarchy reads as a tree); a child whose
    parent is filtered out or deleted falls back to depth 0 (shown ungrouped, F10)."""
    if status is not None and status not in _TASK_STATUSES:
        return {"error": f"Invalid status '{status}'. Valid: {list(_TASK_STATUSES)}"}

    tasks = storage.get_nodes_by_type(CognitionNodeType.TASK)

    def _keep(t: dict[str, Any]) -> bool:
        meta = t.get("metadata", {})
        st = meta.get("status", "open")
        # The default closed-status exclusion must NOT fire when the caller is
        # EXPLICITLY filtering for a closed status — otherwise list_tasks(status="done")
        # would return empty, contradicting the docstring ("filtering for a closed
        # status implies include_done"). So skip the exclusion when `status` itself names
        # a closed status (the status filter below then keeps exactly those).
        if (
            not include_done
            and status not in _TASK_CLOSED_STATUSES
            and st in _TASK_CLOSED_STATUSES
        ):
            return False
        return (
            (status is None or st == status)
            and (priority is None or t.get("severity") == priority)
            and (owner is None or meta.get("owner") == owner)
            and (parent_id is None or meta.get("parent_id") == parent_id)
        )

    filtered = [t for t in tasks if _keep(t)]
    # Stable two-pass sort: recency desc first, then severity asc → severity primary,
    # recency secondary (newest-first within a priority band).
    filtered.sort(key=lambda t: t.get("timestamp") or "", reverse=True)
    filtered.sort(key=lambda t: SEVERITY_ORDER.get(t.get("severity") or "normal", 2))

    visible = {t["id"] for t in filtered}
    by_id = {t["id"]: t for t in filtered}

    def _depth(t: dict[str, Any]) -> int:
        d = 0
        seen: set[str] = set()
        cur = t.get("metadata", {}).get("parent_id")
        while cur and cur in visible and cur not in seen:
            seen.add(cur)
            d += 1
            cur = by_id[cur].get("metadata", {}).get("parent_id")
        return d

    rows: list[dict[str, Any]] = []
    for t in filtered:
        meta = t.get("metadata", {})
        rows.append({
            "id": t["id"],
            "summary": t.get("summary"),
            "status": meta.get("status", "open"),
            "priority": t.get("severity"),
            "owner": meta.get("owner"),
            "parent_id": meta.get("parent_id"),
            "created_by": meta.get("created_by"),
            "timestamp": t.get("timestamp"),
            "depth": _depth(t),
        })
    return {"tasks": rows, "count": len(rows)}


def _reparent_task(
    storage: CognitionStorage,
    node_id: str,
    metadata: dict[str, Any],
    new_parent: str,
) -> dict[str, Any] | None:
    """Move a task under a new parent (or detach), mutating ``metadata['parent_id']`` and
    swapping ONLY the ``task-parent`` part_of edge. Returns an error dict on a rejected
    move, else None (success). Performed by the caller under the storage write path so
    metadata.parent_id and the edge stay in sync.

    Three semantics on ``new_parent`` (caller already filtered out None = no change):
      - ``"<task-id>"`` → move under that task.
      - ``""`` (empty sentinel) → DETACH to top-level.
    """
    old_parent = metadata.get("parent_id")
    detach = new_parent == ""

    if not detach:
        if new_parent == node_id:
            return {"error": "A task cannot be its own parent"}
        parent_node = storage.get_node(new_parent)
        if parent_node is None:
            return {"error": f"Parent task '{new_parent}' does not exist"}
        if parent_node.get("type") != CognitionNodeType.TASK.value:
            return {"error": f"Parent '{new_parent}' is not a task (type: {parent_node.get('type')})"}
        # Cycle guard: the new parent must not be a descendant of this task.
        if node_id in _task_ancestor_ids(storage, new_parent):
            return {"error": "Re-parenting would create a cycle (target is a descendant of this task)"}

        # Add the NEW edge FIRST and bail on failure BEFORE removing the old edge or
        # mutating parent_id. storage.add_edge returns False (never raises) if a node
        # vanished since validation (the new parent deleted cross-process between the
        # get_node above and here) — without this check we'd drop the old edge and write
        # a parent_id with no backing edge, breaking depth/cycle traversal.
        if not storage.add_edge(CognitionEdge(
            from_id=node_id,
            to_id=new_parent,
            edge_type=CognitionEdgeType.PART_OF,
            timestamp=datetime.now(UTC).isoformat(),
            source=_TASK_PARENT_EDGE_SOURCE,
        )):
            return {"error": f"Re-parent failed: parent '{new_parent}' is missing"}

    # New edge is in place (or this is a detach) — now safe to remove the OLD parent
    # edge. Targeted by the exact (child, old_parent, part_of) triple (the task-parent
    # edge) — a cluster-membership part_of edge has a DIFFERENT target and is untouched.
    # Guard old != new so a no-op re-parent (already under this parent) doesn't remove
    # the edge we just refreshed.
    if old_parent and old_parent != new_parent:
        storage.remove_edge(node_id, old_parent, CognitionEdgeType.PART_OF)

    metadata["parent_id"] = None if detach else new_parent
    return None


def _update_task(
    storage: CognitionStorage,
    embedding_storage: "ChromaDBStorage",
    generator: "EmbeddingGenerator",
    *,
    node_id: str,
    embeddings_ready: bool,
    status: str | None = None,
    priority: str | None = None,
    owner: str | None = None,
    summary: str | None = None,
    detail: str | None = None,
    parent_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Edit a task's lifecycle + narrative in place (testable core of cognition_update_task).

    This is the ONLY path to ``status``/``owner``/``parent_id``/transition edits:
    ``cognition_update_node`` physically cannot write ``metadata`` (its whitelist is
    summary/detail/context/severity), so there is no competing path. Contract:
      - reject a non-task id (use cognition_update_node for other types);
      - validate the status transition against _TASK_TRANSITIONS;
      - READ-MODIFY-WRITE the WHOLE metadata dict (storage.update_node replaces fields
        wholesale, F8): set status, APPEND {status, at, by(git-resolved)} to transitions,
        set owner, re-parent;
      - map priority→severity and summary/detail to top-level fields via storage.update_node;
      - RE-EMBED ONCE explicitly via _embed_entity_node on the fresh node (NOT via
        _update_node) so search/metadata reflect the new lifecycle, deferred if the model
        isn't ready."""
    node = storage.get_node(node_id)
    if node is None:
        return {"error": f"Node '{node_id}' does not exist"}
    if node.get("type") != CognitionNodeType.TASK.value:
        return {
            "error": (
                f"Node '{node_id}' is not a task (type: {node.get('type')}) — use "
                "cognition_update_node for non-task nodes"
            )
        }

    # Validate priority BEFORE any mutation (same discipline as status/note below) so an
    # invalid priority never half-applies alongside a valid status/owner/parent change.
    if priority is not None and priority not in _TASK_PRIORITIES:
        return {"error": f"Invalid priority '{priority}'. Valid: {list(_TASK_PRIORITIES)}"}

    metadata = dict(node.get("metadata", {}))  # copy for read-modify-write
    metadata_changed = False

    # `note` annotates a status transition — reject it (BEFORE any mutation) when no
    # transition will occur (no status, or status == current), so it can't be silently
    # dropped. Checked here, pre-mutation, so an erroring note never half-applies.
    if note is not None and (status is None or status == metadata.get("status", "open")):
        return {
            "error": (
                "note annotates a status change — pass a new (different) status, or omit note"
            )
        }

    # --- status transition ---
    if status is not None:
        if status not in _TASK_STATUSES:
            return {"error": f"Invalid status '{status}'. Valid: {list(_TASK_STATUSES)}"}
        current = metadata.get("status", "open")
        if status != current:
            allowed = _TASK_TRANSITIONS.get(current, frozenset())
            if status not in allowed:
                return {
                    "error": (
                        f"Illegal status transition {current!r} -> {status!r}. "
                        f"Allowed from {current!r}: {sorted(allowed)}"
                    )
                }
            by = resolve_git_identity(storage.cognition_dir.parent)
            entry: dict[str, Any] = {
                "status": status,
                "at": datetime.now(UTC).isoformat(),
                "by": by,
            }
            if note:
                entry["note"] = note
            metadata["status"] = status
            metadata["transitions"] = [*metadata.get("transitions", []), entry]
            metadata_changed = True

    # --- owner (empty string clears) ---
    if owner is not None:
        metadata["owner"] = owner or None
        metadata_changed = True

    # --- re-parenting (None = no change; "" = detach; "<id>" = move) ---
    if parent_id is not None:
        err = _reparent_task(storage, node_id, metadata, parent_id)
        if err is not None:
            return err
        metadata_changed = True

    if metadata_changed:
        storage.update_node(node_id, metadata=metadata)

    # --- narrative / priority top-level fields (whitelist, like _update_node) ---
    top_updates: dict[str, Any] = {}
    if priority is not None:
        top_updates["severity"] = priority
    if summary is not None:
        top_updates["summary"] = summary
    if detail is not None:
        top_updates["detail"] = detail
    if top_updates:
        storage.update_node(node_id, **top_updates)

    if not metadata_changed and not top_updates:
        return {
            "error": (
                "No updatable fields provided "
                "(status, priority, owner, summary, detail, parent_id)"
            )
        }

    # Re-embed ONCE on the fresh node (NOT via _update_node) so the new status/owner +
    # narrative land in Chroma. Deferred if the model isn't ready (rare — an edit needs a
    # loaded model anyway).
    if embeddings_ready:
        post = storage.get_node(node_id)
        assert post is not None  # just updated it; cannot vanish under the lock
        cnode = _node_from_dict(node_id, post)
        _embed_entity_node(embedding_storage, generator, cnode)
        reembed = "done"
    else:
        reembed = "deferred"

    result = _get_node(storage, node_id)
    result["reembed"] = reembed
    return result


# ── XP1 core logic (module-level so tests can call without an MCP Context) ───


def _load_project_core(lc: dict[str, Any], path: str) -> dict[str, Any]:
    """Core logic for cognition_load_project — extracted for testability."""
    registry: LoadedProjects = lc["loaded_projects"]
    config = lc["config"]

    # Resolve + normalize
    try:
        resolved = Path(path).resolve()
    except Exception as e:
        return {"error": f"invalid path: {e}"}

    # Home-pin guard (resolve() normalises trailing slash / . / mixed sep)
    if registry.is_home(resolved):
        return {"error": "already loaded as home project"}

    # Exact-path re-load guard
    existing = registry.get(resolved)
    if existing is not None:
        return {"error": f"already loaded as '{existing.tag}'", "tag": existing.tag}

    # Validate .cognition/journal.jsonl exists
    journal_path = resolved / ".cognition" / "journal.jsonl"
    if not journal_path.exists():
        return {"error": f"no cognition graph at {resolved} (missing .cognition/journal.jsonl)"}

    # Build CognitionStorage (read-only: only reads/stat the journal)
    b_storage = CognitionStorage(resolved / ".cognition")
    node_count = b_storage.get_statistics().get("nodes", 0)

    # Open B's chroma — read-only path (never creates)
    b_chroma_dir = resolved / ".cognition" / "chromadb"
    b_embeddings = ChromaDBStorage.open_existing(b_chroma_dir)

    # Load-time guard — THE single model/dim drift check (also used for the
    # home collection at startup, WP-2; see compute_model_guard).
    model_guard, warning, vector_count = compute_model_guard(
        b_embeddings, config.embedding_model, config.embedding_dimensions, resolved.name
    )
    # Foreign-specific: a mismatch means this collection is unusable AND
    # unwritten-to by us, so detach it entirely (structural-only attach).
    # Home never takes this branch — see compute_model_guard's docstring.
    if model_guard in ("dim-mismatch", "model-mismatch") and b_embeddings is not None:
        b_embeddings.close()
        b_embeddings = None

    # Assign tag with collision suffix
    base_tag = resolved.name
    tag = registry.unique_tag(base_tag)

    entry = ProjectEntry(
        path=resolved,
        tag=tag,
        storage=b_storage,
        embeddings=b_embeddings,
        pinned=False,
        model_guard=model_guard,
    )
    registry.add_foreign(entry)

    result: dict[str, Any] = {
        "tag": tag,
        "path": str(resolved),
        "node_count": node_count,
        "vector_count": vector_count,
        "model_guard": model_guard,
    }
    if warning:
        result["warning"] = warning
    return result


def _unload_project_core(lc: dict[str, Any], project: str) -> dict[str, Any]:
    """Core logic for cognition_unload_project — extracted for testability."""
    registry: LoadedProjects = lc["loaded_projects"]

    entry = registry.resolve_tag(project)
    if entry is None:
        return {"error": f"no loaded project matching '{project}'"}
    if entry.pinned:
        return {"error": f"'{entry.tag}' is the home project and cannot be unloaded"}

    # Null-guard: structural-only entries have embeddings=None
    if entry.embeddings is not None:
        entry.embeddings.close()

    registry.remove(entry.path)
    return {"unloaded": entry.tag, "path": str(entry.path)}


def _list_projects_core(lc: dict[str, Any]) -> dict[str, Any]:
    """Core logic for cognition_list_projects — extracted for testability."""
    registry: LoadedProjects = lc["loaded_projects"]

    projects = []
    for entry in registry.all_entries():
        try:
            node_count = entry.storage.get_statistics().get("nodes", 0)
        except Exception:
            node_count = -1
        if entry.embeddings is not None:
            try:
                vector_count: int | str = entry.embeddings.count_documents()
            except Exception:
                vector_count = -1
        else:
            vector_count = "n/a"
        projects.append({
            "tag": entry.tag,
            "path": str(entry.path),
            "node_count": node_count,
            "vector_count": vector_count,
            "pinned": entry.pinned,
            "model_guard": entry.model_guard,
        })

    return {"projects": projects, "foreign_count": registry.foreign_count()}


def register_cognition_tools(mcp) -> None:
    """Register cognition graph tools with the MCP server.

    Args:
        mcp: FastMCP server instance
    """

    @mcp.tool()
    def cognition_record(
        ctx: Context,
        node_type: str,
        summary: str,
        detail: str,
        context: str,
        author: str,
        severity: str | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        """Record a cognition node — a decision, failure, discovery, or other knowledge artifact.

        Use this to capture important context from conversations: what was decided,
        what failed, what was discovered, assumptions made, constraints identified,
        production incidents, generalized patterns, or episode summaries of completed work.

        TRIGGERING CURATION IS YOUR JOB for semantic relationships — never author them
        yourself. Deterministic edges are created automatically ONLY for these pairs
        sharing a reference: entity<->episode (part_of, ANY shared ref) and
        document-involving pairs sharing a doc: ref specifically (entity<->document ->
        part_of; document<->episode -> relates_to). entity<->entity, episode<->episode,
        and document<->document NEVER auto-link even when they share a reference — e.g.
        two decisions citing the same commit stay fully edgeless until curation. All
        semantic relationships (led_to, supersedes, contradicts, resolved_by,
        relates_to) and any entity<->entity linking are NOT created for you — after
        recording, run the `/vibe-curate` skill to link the new nodes.

        NODE TYPES:
        - decision: A choice between alternatives. Include what was chosen AND rejected.
        - fail: Something that didn't work — a build, test, approach, or assumption.
        - discovery: A non-obvious finding about the codebase, library, API, or platform.
        - assumption: Something being assumed true without full verification.
        - constraint: A hard limitation, scoping exclusion, or defensive rule.
        - incident: A production problem that affected users.
        - pattern: A reusable approach, convention, or anti-pattern.
        - episode: Full narrative of completed work (Linear task, feature, debugging session).
          Create when a body of work is done — the episode captures the full story.
        - document: NOT creatable here — use cognition_store_document instead (it handles
          content storage, a text sidecar for search, and embedding together).
        - task: NOT creatable here — use cognition_add_task instead (it resolves the git
          creator server-side and seeds the status/transition lifecycle; passing
          node_type="task" to this tool returns an error, not a task node).

        ENTITY NODES (decision, fail, discovery, assumption, constraint, incident, pattern):
        WORKFLOW NODES (workflow):
        - A step-by-step procedure stored as ONE cohesive unit. Verbose detail (like episode).
          Versioned by supersession: to update, record a NEW workflow node with the full revised
          procedure and add a `supersedes` edge; never edit in place (update_node is blocked).
          summary: Brief title of the procedure. detail: The full procedure, verbose.
          Retrieve with cognition_get_workflow(name_or_topic) to resolve to the current HEAD.

        - summary: MAX 250 chars. Write like a commit message — scannable at a glance.
          Good: "Double-filter bug: query filters by language after opening language-scoped box"
          Bad: "Found a bug in the data source that was causing data to be invisible"
        - detail: 1-3 sentences of rationale. NOT the full story — that goes in an episode.

        EPISODE NODES:
        - summary: Brief title of the work (e.g., "LL-298: Data wipe investigation and fix")
        - detail: Full narrative — everything that happened. Verbose is fine for episodes.

        IMPORTANT:
        - Always include references (issue numbers, PR numbers, commit hashes) so nodes
          link to their episode via deterministic part_of matching, and so /vibe-curate
          has the signal to relate them. Format: "issue:LL-298,pr:97,commit:abc123"
        - Use both file paths AND topical terms in context for better discovery.
        - author should be the current git user name.

        Args:
            node_type: One of: decision, fail, discovery, assumption, constraint, incident, pattern, episode, workflow
            summary: Short description (max 250 chars for entities, brief title for episodes/workflows)
            detail: Brief rationale for entities (1-3 sentences), or full narrative for episodes
            context: Related code areas, file paths, AND topical terms (comma-separated).
                     Example: "flashcard_local_datasource.dart, HiveService, data migration, LL-298"
            author: The current git user name (e.g., "Colton Dyck"), OR an agent
                    persona name — this is UNVERIFIED client-supplied text, not
                    server-resolved like a task's created_by. Use whichever
                    identifies who/what actually made this call.
            severity: Optional priority — critical, high, normal, low
            references: Optional external refs, comma-separated. Include issue/PR/commit refs
                        so nodes link to their episode (part_of) and /vibe-curate can
                        relate them. Example: "issue:LL-298,pr:97"

        Returns:
            {id, type, summary, timestamp} plus, when non-empty,
            deterministic_edges_created (int) and, for episode nodes only,
            possible_duplicate_of: [node_id, ...] — other EXISTING episodes
            sharing a reference with this one (e.g. two clones each minted an
            episode for the same commit). The node is still created either
            way (never silently reused/merged) — this is a visibility signal
            for a curator to reconcile manually (e.g. supersedes), not an
            automatic dedup.
        """
        try:
            nt = CognitionNodeType(node_type)
        except ValueError:
            valid = [e.value for e in CognitionNodeType]
            return {"error": f"Invalid node_type '{node_type}'. Valid: {valid}"}

        # task is NOT creatable here: cognition_record would stamp a client-supplied
        # author and skip the server-resolved created_by + status/transition seeding,
        # producing an un-attributed, lifecycle-less task (defeats the type's purpose).
        # Redirect to the dedicated tool. (This is the WRITE path only — `task` stays a
        # valid filter on the read tools.)
        if nt == CognitionNodeType.TASK:
            return {
                "error": (
                    "Use cognition_add_task to create a task node — it resolves the git "
                    "creator server-side and seeds the lifecycle (status, transitions). "
                    "cognition_record cannot attribute or track a task."
                )
            }

        return _record_node(
            ctx, nt, summary, detail, context, author,
            severity, references,
        )

    @mcp.tool()
    def cognition_add_task(
        ctx: Context,
        summary: str,
        detail: str,
        context: str,
        priority: str = "normal",
        owner: str | None = None,
        parent_id: str | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        """Create a trackable task — open, actionable work, attributed to the git user.

        Use this to file work that should be tracked and shown back at session start
        (open tasks inject into context like constraints do) and listed via
        `cognition_list_tasks`. The graph itself becomes the backlog. **Before picking
        up work, check open tasks with `cognition_list_tasks` first.**

        The creator is resolved SERVER-SIDE from this repo's git config — there is no
        `created_by` parameter and a client value cannot override it. The task is seeded
        `status="open"` with an initial transition record; change it later with
        `cognition_update_task` (the only path to status/owner/parent edits).

        Tasks support an ARBITRARY-depth parent hierarchy: pass `parent_id` to file this
        task under a parent task/epic (it creates an explicit `part_of` edge). Re-parent
        later with `cognition_update_task(parent_id=...)`. Tasks are fully curatable —
        run `/vibe-curate` to link a task `relates_to` the decision/pattern it implements,
        or `resolved_by`/`led_to` the episode that closed it.

        Args:
            summary: Short task title (like a commit message — scannable).
            detail: Description / acceptance criteria.
            context: Comma-separated related files AND topical terms.
            priority: critical | high | normal | low (default normal). Stored as the
                node's `severity`, so it sorts the backlog and the session-start view.
            owner: Optional free-text "who's on it" (distinct from the git creator).
            parent_id: Optional id of a parent task to file this under (must be a task).
            references: Optional external refs (issue/PR/commit), comma-separated.

        Returns:
            The created task node ({id, type, summary, ..., severity, metadata}) or
            {"error": ...} if the parent is missing or not a task.
        """
        return _add_task(
            ctx, summary, detail, context,
            priority=priority, owner=owner, parent_id=parent_id, references=references,
        )

    @mcp.tool()
    def cognition_list_tasks(
        ctx: Context,
        status: str | None = None,
        priority: str | None = None,
        owner: str | None = None,
        parent_id: str | None = None,
        include_done: bool = False,
    ) -> dict[str, Any]:
        """List the project's tasks — the backlog view. **Check this before picking up work.**

        Returns open tasks (home project only) sorted by priority then recency, grouped
        as a tree via a `depth` field per row (a child whose parent is filtered out or
        deleted is shown ungrouped at depth 0). By default `done`/`cancelled` tasks are
        EXCLUDED — pass `include_done=true` to see them.

        Args:
            status: Optional exact-status filter (open | in_progress | blocked | done |
                    cancelled). Filtering for a closed status implies include_done.
            priority: Optional priority filter (critical | high | normal | low).
            owner: Optional owner filter (exact match on the free-text owner).
            parent_id: Optional — only tasks whose direct parent is this id.
            include_done: Include done/cancelled tasks (default False).

        Returns:
            {"tasks": [{id, summary, status, priority, owner, parent_id, created_by,
             timestamp, depth}, ...], "count": N} or {"error": ...} for a bad status.
        """
        storage: CognitionStorage = get_lifespan(ctx)["cognition_storage"]
        return _list_tasks(
            storage,
            status=status, priority=priority, owner=owner,
            parent_id=parent_id, include_done=include_done,
        )

    @mcp.tool()
    def cognition_update_task(
        ctx: Context,
        node_id: str,
        status: str | None = None,
        priority: str | None = None,
        owner: str | None = None,
        summary: str | None = None,
        detail: str | None = None,
        parent_id: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Update a task's lifecycle or narrative in place — the ONLY path to status/owner/parent.

        `cognition_update_node` cannot edit a task's `status`/`owner`/`parent` (they live
        in metadata, which it doesn't touch) — use this tool for those. A status change
        is validated against the legal transitions and appends an audit record
        (`{status, at, by}`, `by` server-resolved) to the task's transition log.
        Reopening a done/cancelled task (→ open) is allowed.

        Re-parent via `parent_id`: a task id moves this task (and its whole subtree) under
        that task; the empty string `""` DETACHES it to top-level; omitting it leaves the
        parent unchanged. A move is rejected if it would create a cycle or the target is
        not a task.

        Args:
            node_id: The task to update (must be a task node).
            status: New status (open | in_progress | blocked | done | cancelled).
            priority: New priority (critical | high | normal | low).
            owner: New owner free-text; pass "" to clear it.
            summary: New title, if changing.
            detail: New description, if changing.
            parent_id: Re-parent target task id, "" to detach, or omit for no change.
            note: Optional annotation recorded on the status transition (with `status`).

        Returns:
            The updated task node plus `reembed` ("done" | "deferred"), or {"error": ...}
            for a non-task id, an illegal/invalid status, a bad re-parent, or no fields.
        """
        lc = get_lifespan(ctx)
        return _update_task(
            lc["cognition_storage"],
            lc["cognition_embedding_storage"],
            lc["embedding_generator"],
            node_id=node_id,
            embeddings_ready=_embeddings_ready(lc),
            status=status,
            priority=priority,
            owner=owner,
            summary=summary,
            detail=detail,
            parent_id=parent_id,
            note=note,
        )

    @mcp.tool()
    def cognition_store_document(
        ctx: Context,
        title: str,
        document_text: str,
        context: str,
        author: str,
        file_path: str | None = None,
        content_text: str | None = None,
        references: str | None = None,
        mime: str | None = None,
        force_new: bool = False,
        store_copy: bool = False,
        local_only: bool | None = None,
    ) -> dict[str, Any]:
        """Store a document as a first-class DOCUMENT node.

        Default REFERENCE mode: the node records the document's PATH + metadata +
        content sha256 — the bytes STAY WHERE THEY LIVE. Your extracted
        `document_text` goes into a text sidecar (kept small out of the node so
        journal lines stay small; it powers document search in a later version). To
        capture what's INSIDE the document, record its facts as separate entity
        nodes with cognition_record, citing this document's returned `doc_ref` in
        THEIR `references` — they auto-link `part_of` the document.

        Opt-in COPY mode (`store_copy=true`) also copies the bytes into a content-
        addressed blob under `.cognition/documents/`, so the document survives the
        original moving and can travel via git. By default the blob is committed;
        `local_only=true` keeps it out of git (a per-machine choice). Size policy
        (no hard cap): >=50MB is auto-forced local_only with a warning; >=95MB
        refuses default-commit (forced local_only) so a huge blob can't brick later
        pushes. PRIVACY: a committed blob survives in git history and on the remote
        even after the node is deleted — deleting does NOT un-publish it.

        Provide EITHER `file_path` (a document on disk) OR `content_text` (inline).

        Args:
            title: Short title for the document node (its summary).
            document_text: The full extracted text (you extract it; stored in the sidecar).
            context: Comma-separated topical terms / related areas.
            author: The current git user name.
            file_path: Absolute path to the document on disk.
            content_text: Inline text instead of a file (hashed directly).
            references: Optional extra refs — these go to CONTEXT (the node's own
                references are restricted to its doc: key by design).
            mime: Optional MIME type (metadata only).
            force_new: Store even if a document with the same content already exists.
            store_copy: Copy the bytes into the content-addressed blob store.
            local_only: Keep the copied blob out of git (copy mode only).

        Returns:
            {node_id, doc_ref, mode, size, indexed_text_chars, already_stored?,
             blob_bytes?, blob_path?, local_only?, promoted?, already_committed?, warnings?,
             prior_version_id?, consider_supersedes?}
            prior_version_id/consider_supersedes (WP-12, db65f1568fa5) appear ONLY when
            file_path pointed at a path some EXISTING document node already references,
            with DIFFERENT content (sha changed) — a re-store of a changed file. This is
            an OFFER, never automatic: no supersedes edge is created for you; link it
            yourself with cognition_add_edge if this genuinely is a new version.
        """
        lifespan = get_lifespan(ctx)
        storage: CognitionStorage = lifespan["cognition_storage"]
        # Embed at store time only if the model is ready; else defer to the next sync
        # (require_embeddings returns an error dict when not ready). Never error here.
        ready = require_embeddings(ctx) is None
        embedding_storage = lifespan["cognition_embedding_storage"] if ready else None
        generator = lifespan["embedding_generator"] if ready else None
        return _store_document(
            storage, title, document_text, context, author,
            file_path=file_path, content_text=content_text,
            references=references, mime=mime, force_new=force_new,
            store_copy=store_copy, local_only=local_only,
            embedding_storage=embedding_storage, generator=generator,
        )

    @mcp.tool()
    def cognition_get_document(
        ctx: Context,
        node_id: str | None = None,
        doc_ref_arg: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve a stored document: metadata + full sidecar text + path, with a
        freshness check.

        Resolve by `node_id` or by `doc_ref_arg` (the `doc:<hash>` returned at
        store time). In reference mode the original file is re-hashed: `freshness`
        is `unchanged | modified | missing`.

        Cross-project note: when project is a foreign tag/path, `freshness` is
        always "cross-project: unavailable" — the path in the node is
        foreign-machine-relative and a local re-hash would mislead.

        Args:
            node_id: Node id to retrieve.
            doc_ref_arg: doc:<hash> ref returned at store time.
            project: None (default, home) or tag/path for a loaded foreign project.
                     "*" is rejected.

        Returns:
            {node_id, doc_ref, metadata, text, path, freshness} or {"error": ...}
            When project is not None, also includes "project": tag.
        """
        lc = get_lifespan(ctx)
        if project is None:
            return _get_document(lc["cognition_storage"], node_id=node_id, doc_ref_arg=doc_ref_arg)
        if project == "*":
            return {"error": 'project="*" is not supported for single-node tools'}
        entries, err = resolve_project(lc, project)
        if err:
            return err
        result = _get_document(entries[0].storage, node_id=node_id, doc_ref_arg=doc_ref_arg)
        if "error" not in result:
            result["project"] = entries[0].tag
            result["freshness"] = "cross-project: unavailable"
        return result

    @mcp.tool()
    def cognition_get_node(ctx: Context, node_id: str, project: str | None = None) -> dict[str, Any]:
        """Read a single cognition node's FULL narrative by id.

        Search results and `cognition_get_neighbors` return summaries only (no
        `detail`) — use this after a hit to read the complete node. This is the
        GENERIC node read; for a stored document, `cognition_get_document` is the
        specialized get-by-id (it adds the sidecar text + a freshness check).

        Args:
            node_id: The node to read.
            project: None (default, home) or a tag/path for a loaded foreign
                     project. "*" is rejected (node ids are not project-namespaced).

        Returns:
            {id, type, summary, detail, ...} or {"error": ...} if absent. The
            node's kind is keyed "type" here (the raw graph attribute name) —
            NOTE cognition_search's results use "node_type" instead, not "type";
            the two are NOT interchangeable, check which tool you're reading from.
            When project is not None, also includes "project": tag.
        """
        lc = get_lifespan(ctx)
        if project is None:
            return _get_node(lc["cognition_storage"], node_id)
        if project == "*":
            return {"error": 'project="*" is not supported for single-node tools; node ids are not project-namespaced'}
        entries, err = resolve_project(lc, project)
        if err:
            return err
        result = _get_node(entries[0].storage, node_id)
        if "error" not in result:
            result["project"] = entries[0].tag
        return result

    @mcp.tool()
    def cognition_update_node(
        ctx: Context,
        node_id: str,
        summary: str | None = None,
        detail: str | None = None,
        context: str | None = None,
        severity: str | None = None,
    ) -> dict[str, Any]:
        """Edit a node's narrative in place — fix a typo or refine wording WITHOUT
        delete+re-record (which would lose the id, its edges, and its curation marker).

        Only these narrative fields are editable: `summary`, `detail`,
        `context` (comma-separated), `severity`. Structural fields (id, type,
        references, metadata, timestamp) are intentionally NOT editable — changing
        them would corrupt invariants (a document node's sha/mode/`doc:` ref, the
        reference→part_of index, the minted id). To change those, the node should be
        re-created.

        When any editable field changes and the embedding model is loaded, the node
        is RE-EMBEDDED so `cognition_search` reflects the edit — both the match vector
        (summary/detail) and the result metadata it surfaces (context/severity).
        Otherwise search would keep serving the pre-edit values. The result carries
        `reembed`: "done" | "deferred" (model still loading).

        Note: re-embedding a DOCUMENT node refreshes its node-level vector (its chunk
        vectors, derived from the sidecar, are untouched); this is safe but means an
        edited document node's vector metadata takes the entity shape rather than the
        as-stored document shape.

        For `task` nodes, this tool can edit summary/detail/context/severity, but
        `status`, `owner`, and the parent hierarchy live in metadata — use
        `cognition_update_task` for those (it also records the status-transition log).

        Args:
            node_id: The node to edit.
            summary: New summary, if changing.
            detail: New detail body, if changing.
            context: New comma-separated context tags, if changing.
            severity: New severity, if changing.

        Returns:
            The updated node dict (as cognition_get_node) plus `reembed`, or
            {"error": ...} if the node is absent or no editable field was given.
        """
        lc = get_lifespan(ctx)
        return _update_node(
            lc["cognition_storage"],
            lc["cognition_embedding_storage"],
            lc["embedding_generator"],
            node_id=node_id,
            embeddings_ready=_embeddings_ready(lc),
            summary=summary,
            detail=detail,
            context=context,
            severity=severity,
        )

    @mcp.tool()
    def cognition_search(
        ctx: Context,
        query: str,
        node_type: str | None = None,
        limit: int = 10,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Search PROJECT HISTORY (decisions, failures, discoveries, patterns) by natural language.

        This searches the cognition graph only. It does NOT search code —
        this server provides cognition history tools only.

        Args:
            query: What you're looking for, e.g.:
                   - "caching strategy decisions"
                   - "what failed with the migration"
                   - "localization issues"
            node_type: Optional filter: decision, fail, discovery, assumption,
                       constraint, incident, pattern, episode, workflow, task
            limit: Max results (default: 10)
            project: None (default, home only), a tag/path (one loaded foreign
                     project), or "*" (all loaded projects — aggregate search).
                     Single-node tools reject "*"; this aggregate tool accepts it.

        Returns:
            {
              query: str,
              results: [                 # hits, sorted by score desc
                {id, node_type, summary, score, …, project: tag}
                                         # "project" present when project != None.
                                         # NOTE the key is "node_type" here, NOT
                                         # "type" — cognition_get_node/get_history/
                                         # get_neighbors/get_edgeless_nodes/
                                         # get_uncurated_nodes all use "type" instead
                                         # (the raw graph attribute name). The two
                                         # families are NOT interchangeable; branching
                                         # code must key off the right one per tool.
                                         # A "document" hit ALSO gets "staleness":
                                         # "path_missing"/"size_changed" when a CHEAP
                                         # (stat-only, no file read) check catches
                                         # something -- omitted otherwise. Weaker than
                                         # cognition_get_document's full re-hash
                                         # "freshness" field (misses a same-size edit);
                                         # deliberately cheap so search cost doesn't
                                         # scale with re-reading every matched document.
              ],
              count: int,
              project_notes: {           # project != None only; omitted when empty
                "<tag>": {semantic_unavailable: true, reason: "<model_guard>"}
                          # or {confidence: "degraded (no model provenance for <tag>)"}
              },
              semantic_unavailable: true,  # project=None (home) only; present
              reason: "<model_guard>",     # only when home's embedding model/dims
                                            # have drifted from current config —
                                            # results/count are [] / 0 in this case
              confidence: "degraded — no model provenance in collection metadata "
                          "(pre-stamp)"  # project=None only; present when home
                                         # ran search but provenance is unverified
            }
            semantic_unavailable/project_notes[tag].semantic_unavailable means the
            index is guarded off (no-index / dim-mismatch / model-mismatch) — that
            source contributed no hits, but it is NOT "no history." A confidence
            caveat means search ran but the model provenance couldn't be verified.
        """
        err = require_embeddings(ctx)
        if err:
            return err

        # WP-2: validate node_type via the shared parser BEFORE branching, so an
        # invalid value returns the same clear error shape every other read tool
        # uses (not results:[] — an infra-shaped empty result must never look
        # like "no history"). Both the home and multi-project paths share this
        # one check since it runs before the branch.
        _, nt_err = _parse_node_type(node_type)
        if nt_err:
            return nt_err

        lifespan = get_lifespan(ctx)

        # Default (project=None): byte-identical to the pre-XP2 path UNLESS the
        # home collection's model/dims have drifted from current config (WP-2
        # item 2) — a dim-mismatch would otherwise raise inside Chroma (caught
        # by vector_search's broad except -> silent results:[]) and a
        # model-mismatch with equal dims wouldn't raise at all, just return
        # confidently-wrong nonsense scores. Both must be surfaced honestly
        # instead, mirroring the multi-project project_notes shape.
        if project is None:
            registry: LoadedProjects = lifespan["loaded_projects"]
            home_entry = registry.get(lifespan["config"].repo_path)
            if home_entry is not None and home_entry.model_guard in (
                "dim-mismatch", "model-mismatch",
            ):
                return {
                    "query": query,
                    "results": [],
                    "count": 0,
                    "semantic_unavailable": True,
                    "reason": home_entry.model_guard,
                }
            # WP-3 (8606d59905a5): close the node<->embedding drift for nodes
            # this process only ever saw via journal REPLAY (a teammate's
            # write) before running the search that would otherwise miss them
            # — see _reembed_replayed_nodes. dashboard/api.py's search handler
            # runs the SAME drain (it has its own request pipeline, bypassing
            # this wrapper) so both search surfaces stay covered. Foreign/
            # multi-project entries are deliberately NOT covered here (scoped
            # to home) — writing new embeddings into a project this process
            # doesn't own is a separate design question this WP doesn't settle.
            _reembed_replayed_nodes(
                lifespan["cognition_storage"],
                lifespan["cognition_embedding_storage"],
                lifespan["embedding_generator"],
            )
            result = _search_cognition(
                lifespan["cognition_storage"],
                lifespan["cognition_embedding_storage"],
                lifespan["embedding_generator"],
                query,
                node_type=node_type,
                limit=limit,
            )
            if home_entry is not None and home_entry.model_guard == "unknown":
                result["confidence"] = (
                    "degraded — no model provenance in collection metadata (pre-stamp)"
                )
            return result

        # Multi-project path: resolve entries, embed once, fan out.
        entries, err2 = resolve_project(lifespan, project)
        if err2:
            return err2

        generator: EmbeddingGenerator = lifespan["embedding_generator"]

        # WP-3 redirect: the home-only drain above doesn't run for this branch,
        # but project="*" (or any tag set resolving to include home) searches
        # the SAME home collection through here — without this, a replayed
        # node is visible via a plain search but missing from an aggregate
        # one. Home only (entry.pinned) — foreign entries stay untouched
        # (read-only by design/contract, not a compromise; see XP1 lineage).
        home_in_fanout = next((e for e in entries if e.pinned), None)
        if home_in_fanout is not None and home_in_fanout.embeddings is not None:
            _reembed_replayed_nodes(home_in_fanout.storage, home_in_fanout.embeddings, generator)

        limit = min(limit, 50)
        query_embedding = generator.generate_query_embedding(query)

        all_results: list[dict[str, Any]] = []
        project_notes: dict[str, Any] = {}

        for entry in entries:
            tag = entry.tag
            # WP-4 item 0 (454226d592e0): a dim/model-mismatched entry must be
            # treated the same whether it's a guarded foreign attach
            # (embeddings=None) or home (embeddings stays live so writes keep
            # landing — WP-2 — but must NOT be searched against a stale/wrong
            # stamp either). Foreign entries already null out embeddings on
            # mismatch (_load_project_core), so this check is a no-op there;
            # it only changes behavior for home.
            if entry.embeddings is None or entry.model_guard in (
                "dim-mismatch", "model-mismatch",
            ):
                project_notes[tag] = {
                    "semantic_unavailable": True,
                    "reason": entry.model_guard,
                }
                continue
            results = _search_with_embedding(
                entry.storage, entry.embeddings, query_embedding, node_type, limit
            )
            tag_results(results, tag)
            all_results.extend(results)
            if entry.model_guard == "unknown":
                project_notes[tag] = {
                    "confidence": "degraded — no model provenance in collection metadata (pre-stamp)"
                }

        # Sort merged results by score desc; no cross-project id-dedup —
        # colliding ids across projects are two legitimately different nodes
        # (ids are content-derived, not namespaced). The project tag disambiguates.
        all_results.sort(key=lambda r: r.get("score") or 0.0, reverse=True)
        all_results = all_results[:limit]

        result: dict[str, Any] = {"query": query, "results": all_results, "count": len(all_results)}
        if project_notes:
            result["project_notes"] = project_notes
        return result

    @mcp.tool()
    def cognition_get_chain(
        ctx: Context,
        node_id: str,
        max_depth: int = 5,
        direction: str = "outgoing",
        project: str | None = None,
    ) -> dict[str, Any]:
        """Get the reasoning chain from/to a cognition node via LED_TO edges.

        Follow the chain of causation: what led to what, or what was caused by what.

        Args:
            node_id: Starting node ID
            max_depth: Maximum depth to traverse (default: 5)
            direction: "outgoing" (what it led to) or "incoming" (what led to it)
            project: None (default, home) or tag/path for a loaded foreign project.
                     "*" is rejected — node ids are not project-namespaced; use a
                     specific tag.

        Returns:
            Nested structure showing the reasoning chain. When project is not None,
            also includes "project": tag at the top level.
        """
        lc = get_lifespan(ctx)
        err = _validate_direction(direction, ("outgoing", "incoming"))
        if err:
            return err
        if project is None:
            return get_reasoning_chain(lc["cognition_storage"], node_id, max_depth, direction)
        if project == "*":
            return {"error": 'project="*" is not supported for single-node tools'}
        entries, err2 = resolve_project(lc, project)
        if err2:
            return err2
        result = get_reasoning_chain(entries[0].storage, node_id, max_depth, direction)
        result["project"] = entries[0].tag
        return result

    @mcp.tool()
    def cognition_get_superseded_chain(ctx: Context, node_id: str, project: str | None = None) -> dict[str, Any]:
        """Get a node's version history by following SUPERSEDES edges, newest first.

        When a decision is revised, the new node SUPERSEDES the old one; this walks
        that chain so you can see how the current version came to be (the chain
        `cognition_remove_node` recommends building but no tool could traverse).

        Args:
            node_id: The node to start from (typically the newest version).
            project: None (default, home) or tag/path for a loaded foreign project.
                     "*" is rejected — node ids are not project-namespaced.

        Returns:
            {"node_id": ..., "chain": [ ... ]} newest->oldest.
            When project is not None, also includes "project": tag.
        """
        lc = get_lifespan(ctx)
        if project is None:
            return {"node_id": node_id, "chain": get_superseded_chain(lc["cognition_storage"], node_id)}
        if project == "*":
            return {"error": 'project="*" is not supported for single-node tools'}
        entries, err = resolve_project(lc, project)
        if err:
            return err
        return {"node_id": node_id, "chain": get_superseded_chain(entries[0].storage, node_id), "project": entries[0].tag}

    @mcp.tool()
    def cognition_get_workflow(
        ctx: Context,
        name_or_topic: str,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Find a workflow procedure by name or topic and return the current HEAD version.

        **Before starting any multi-step task**, search for an existing workflow first —
        this is the primary retrieval trigger for workflow nodes.

        Performs a type-filtered semantic search for ``workflow`` nodes matching the
        query, then resolves the top hit to the current HEAD (in case an old version
        was matched) via SUPERSEDES edges. Returns the full procedure body, the full
        version chain, and which node was matched.

        Args:
            name_or_topic: Name or topic of the procedure (e.g. "deploy to production",
                           "onboard a new engineer", "run the release process").
            project: None (default, home) or tag/path for a loaded foreign project.
                     "*" is rejected — node ids are not project-namespaced.

        Returns:
            {"head": {id, type, summary, detail, ...}, "chain": [...], "matched": <id>}
            or {"error": "..."} if no workflow was found or embeddings are not ready.
            "chain" is the full version history (newest first) via get_superseded_chain.
            When project is not None, also includes "project": tag.
        """
        lc = get_lifespan(ctx)
        err = require_embeddings(ctx)
        if err:
            return err
        if project == "*":
            return {"error": 'project="*" is not supported for single-node tools'}

        if project is None:
            storage = lc["cognition_storage"]
            embedding_storage: ChromaDBStorage = lc["cognition_embedding_storage"]
            generator: EmbeddingGenerator = lc["embedding_generator"]
            project_tag = None
        else:
            entries, err2 = resolve_project(lc, project)
            if err2:
                return err2
            storage = entries[0].storage
            if entries[0].embeddings is None:
                return {"error": "No vector index for the requested project"}
            embedding_storage = entries[0].embeddings
            generator = lc["embedding_generator"]
            project_tag = entries[0].tag

        query_embedding = generator.generate_query_embedding(name_or_topic)
        results = _search_with_embedding(
            storage, embedding_storage, query_embedding,
            node_type=CognitionNodeType.WORKFLOW.value, limit=1,
        )
        if not results:
            return {"error": f"No workflow found matching: {name_or_topic!r}"}

        matched_id = results[0]["id"]
        head_id = get_workflow_head(storage, matched_id)
        head_node = storage.get_node(head_id)
        if not head_node:
            return {"error": f"Workflow head node not found: {head_id}"}

        chain = get_superseded_chain(storage, head_id)
        result: dict[str, Any] = {
            "head": {"id": head_id, **head_node},
            "chain": chain,
            "matched": matched_id,
        }
        if project_tag:
            result["project"] = project_tag
        return result

    @mcp.tool()
    def cognition_get_incident_resolution(ctx: Context, node_id: str, project: str | None = None) -> dict[str, Any]:
        """Get an incident node plus everything that resolved or relates to it.

        Follows RESOLVED_BY edges to the fixes, LED_TO edges to follow-on nodes
        (discoveries/decisions the incident produced), and incoming CONTRADICTS
        edges, so the full story of an incident is one call.

        Args:
            node_id: The incident node.
            project: None (default, home) or tag/path for a loaded foreign project.
                     "*" is rejected — node ids are not project-namespaced.

        Returns:
            {id, ...incident fields, resolutions: [...], discoveries: [...],
            contradictions: [...]} or {"error": ...} if the node is absent.
            When project is not None, also includes "project": tag.
        """
        lc = get_lifespan(ctx)
        if project is None:
            return get_incident_resolution(lc["cognition_storage"], node_id)
        if project == "*":
            return {"error": 'project="*" is not supported for single-node tools'}
        entries, err = resolve_project(lc, project)
        if err:
            return err
        result = get_incident_resolution(entries[0].storage, node_id)
        result["project"] = entries[0].tag
        return result

    @mcp.tool()
    def cognition_get_history(
        ctx: Context,
        context_term: str | None = None,
        node_type: str | None = None,
        limit: int = 20,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Get cognition nodes by context area, type, or recency.

        If context_term is provided, filters nodes whose context fields match
        (case-insensitive substring). Otherwise returns the most recent nodes.

        Args:
            context_term: Optional term to search in context fields (file paths, topics)
            node_type: Optional filter: decision, fail, discovery, assumption,
                       constraint, incident, pattern, episode, workflow, task
            limit: Max results (default: 20)
            project: None (default, home), tag/path, or "*" (all loaded projects).
                     Aggregate tool: "*" fans across all loaded projects, merges and
                     sorts rows, adds "projects_queried" to the envelope. Each row
                     carries a "project" tag when project != None.

        Returns:
            {context_term, results: [{id, type, summary, detail, ...}, ...], count}
            Each node's kind is keyed "type" (the raw graph attribute name) — NOT
            "node_type" like cognition_search's results use; the two shapes are
            not interchangeable. (The node_type ARG above, which filters the
            query, is unrelated to this key-naming asymmetry.)
            When project != None also includes "projects_queried": [tag, …].
            (Cross-project note: for semantic search over projects use
            cognition_search with project=; get_history is structural only.)
        """
        lc = get_lifespan(ctx)

        nt, err = _parse_node_type(node_type)
        if err:
            return err

        # Default path: byte-identical to pre-XP2.
        if project is None:
            storage: CognitionStorage = lc["cognition_storage"]
            if context_term:
                results = get_history_for_context(storage, context_term, nt)
                results = results[:limit]
            else:
                results = storage.get_recent_nodes(limit=limit, node_type=nt)
            return {"context_term": context_term, "results": results, "count": len(results)}

        # Multi-project path.
        entries, err2 = resolve_project(lc, project)
        if err2:
            return err2

        all_results: list[dict[str, Any]] = []
        for entry in entries:
            if context_term:
                rows = get_history_for_context(entry.storage, context_term, nt)[:limit]
            else:
                rows = entry.storage.get_recent_nodes(limit=limit, node_type=nt)
            tag_results(rows, entry.tag)
            all_results.extend(rows)

        all_results.sort(key=lambda n: n.get("timestamp") or "", reverse=True)
        all_results = all_results[:limit]

        return {
            "context_term": context_term,
            "results": all_results,
            "count": len(all_results),
            "projects_queried": [e.tag for e in entries],
        }

    @mcp.tool()
    def cognition_add_edge(
        ctx: Context,
        from_id: str,
        to_id: str,
        edge_type: str,
        reason: str | None = None,
        source: str = "manual",
    ) -> dict[str, Any]:
        """Create a directed edge between two existing cognition nodes.

        ONLY the curate-orchestrator agent (launched via /vibe-curate) may use this
        tool. If you are any other agent — including the main instance — do NOT
        call it; to get edges created, run /vibe-curate. No manual carve-out.

        EDGE SEMANTICS — when to use each type (this table's audience is the
        curate-orchestrator, including its degraded no-nesting inline mode — not a
        sanctioned manual path):

          led_to       cause -> effect (earlier -> later). A caused or directly
                       motivated B. Requires real evidence of causation, not
                       mere temporal adjacency ("happened after" != "caused by").
          resolved_by  problem -> solution. A (a fail/incident) was fixed by B.
                       B must explicitly address the problem A describes.
          supersedes   newer -> older. B replaces A for the same concern (a
                       newer approach to the same system/component) — THE
                       reconciliation edge, including for the episode-duplicate
                       case cognition_record's possible_duplicate_of warning
                       flags. Enforced (WP-14, decision 7b9db5a8d675): legal
                       ONLY as (1) same node type on both ends (the "newer
                       version" case), or (2) a fail/incident superseding any
                       NON-workflow node (the RETRACTION case — "this claim/
                       decision turned out to be wrong"; workflow is excluded
                       because get_workflow_head resolves supersession chains
                       to a HEAD it treats as a workflow — a wrong runbook gets
                       retracted by superseding it with a corrected workflow,
                       never by a fail at the head of that chain). Also
                       rejected: any edge that would create a cycle along
                       existing supersedes edges.
          contradicts  either direction. A and B assert incompatible things.
                       Genuinely rare — a real logical conflict, not just
                       disagreement or a different opinion.
          relates_to   either direction. Same topic, no causal link. Use
                       sparingly — if you can't name the specific
                       relationship, don't force one.

        TASK nodes: a task relates_to the decision/discovery/pattern it
        implements or acts on; a DONE task (status: done) is resolved_by (or
        led_to) the episode that closed it. NEVER part_of a task — its parent
        hierarchy is an explicit edge owned by cognition_add_task /
        cognition_update_task; a manually-added part_of would collide with it.

        part_of: avoid proposing/adding this manually (except task parenting,
        which the task tools above own) — it's created automatically by
        deterministic reference matching (see cognition_record's docstring for
        exactly which node-type pairs qualify); a manual mint on a pair the
        matcher also covers just duplicates work the server already does
        idempotently. Not blocked, just usually redundant.

        DOCUMENT nodes need curated edges beyond the deterministic matcher:
        versioning uses an explicit ``supersedes`` edge between document nodes,
        and curated ``relates_to`` edges are expected — all authored by the
        curate-orchestrator like any other semantic edge (documents flow through
        the same uncurated worklist as everything else). The deterministic
        matcher only AUTO-links documents on shared ``doc:`` refs (entity→document
        ``part_of``, document→episode ``relates_to``); a deterministic re-mint
        never overwrites a same-type curated edge.

        Args:
            from_id: Source node ID (must exist)
            to_id: Target node ID (must exist)
            edge_type: One of: led_to, supersedes, contradicts, relates_to,
                       resolved_by, part_of. (``duplicate_of`` was retired —
                       WP-14, decision 7b9db5a8d675 — and is simply not a
                       valid value; supersedes is the reconciliation edge.)
            reason: Optional brief explanation of why this edge exists
            source: Provenance tag (default: "manual")

        Returns:
            {"created": true, ...} or {"error": "..."}
        """
        storage: CognitionStorage = get_lifespan(ctx)["cognition_storage"]
        return _add_edge_core(storage, from_id, to_id, edge_type, reason, source)

    @mcp.tool()
    def cognition_add_edges_batch(
        ctx: Context,
        edges: str,
    ) -> dict[str, Any]:
        """Create multiple edges in one call.

        ONLY the curate-orchestrator agent (launched via /vibe-curate) may use this
        tool. If you are any other agent — including the main instance — do NOT
        call it; to get edges created, run /vibe-curate. No manual carve-out.

        Each edge in the JSON array needs from_id, to_id, and edge_type.
        Edges are validated individually — invalid ones are skipped and reported.

        EDGE SEMANTICS — when to use each type (this table's audience is the
        curate-orchestrator, including its degraded no-nesting inline mode — not a
        sanctioned manual path):

          led_to       cause -> effect (earlier -> later). A caused or directly
                       motivated B. Requires real evidence of causation, not
                       mere temporal adjacency ("happened after" != "caused by").
          resolved_by  problem -> solution. A (a fail/incident) was fixed by B.
                       B must explicitly address the problem A describes.
          supersedes   newer -> older. B replaces A for the same concern (a
                       newer approach to the same system/component) — THE
                       reconciliation edge, including for the episode-duplicate
                       case cognition_record's possible_duplicate_of warning
                       flags. Enforced (WP-14, decision 7b9db5a8d675): legal
                       ONLY as (1) same node type on both ends (the "newer
                       version" case), or (2) a fail/incident superseding any
                       NON-workflow node (the RETRACTION case — "this claim/
                       decision turned out to be wrong"; workflow is excluded
                       because get_workflow_head resolves supersession chains
                       to a HEAD it treats as a workflow — a wrong runbook gets
                       retracted by superseding it with a corrected workflow,
                       never by a fail at the head of that chain). Also
                       rejected: any edge that would create a cycle along
                       existing supersedes edges.
          contradicts  either direction. A and B assert incompatible things.
                       Genuinely rare — a real logical conflict, not just
                       disagreement or a different opinion.
          relates_to   either direction. Same topic, no causal link. Use
                       sparingly — if you can't name the specific
                       relationship, don't force one.

        TASK nodes: a task relates_to the decision/discovery/pattern it
        implements or acts on; a DONE task (status: done) is resolved_by (or
        led_to) the episode that closed it. NEVER part_of a task — its parent
        hierarchy is an explicit edge owned by cognition_add_task /
        cognition_update_task; a manually-added part_of would collide with it.

        part_of: avoid proposing/adding this manually (except task parenting,
        which the task tools above own) — it's created automatically by
        deterministic reference matching (see cognition_record's docstring for
        exactly which node-type pairs qualify); a manual mint on a pair the
        matcher also covers just duplicates work the server already does
        idempotently. Not blocked, just usually redundant.

        Args:
            edges: JSON array string of edge objects, max 500. edge_type is one
                   of: led_to, supersedes, contradicts, relates_to, resolved_by,
                   part_of. (``duplicate_of`` was retired — WP-14, decision
                   7b9db5a8d675 — and is simply not a valid value; supersedes
                   is the reconciliation edge.) Example:
                   '[{"from_id":"abc","to_id":"def","edge_type":"led_to"}]'

        Returns:
            {"created": N, "skipped": N, "errors": [...]}
        """
        storage: CognitionStorage = get_lifespan(ctx)["cognition_storage"]
        return _add_edges_batch_core(storage, edges)

    @mcp.tool()
    def cognition_get_edgeless_nodes(
        ctx: Context,
        node_type: str | None = None,
        limit: int = 50,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Get cognition nodes that have zero edges (no incoming or outgoing).

        Useful for graph health diagnostics — finding truly isolated nodes.
        For curation tracking, prefer cognition_get_uncurated_nodes instead.

        Args:
            node_type: Optional filter: decision, fail, discovery, assumption,
                       constraint, incident, pattern, episode, workflow, task
            limit: Max results (default: 50, max: 500)
            project: None (default, home), tag/path, or "*" (all loaded projects).
                     Aggregate tool: "*" fans across all loaded projects and merges.
                     Each row carries a "project" tag when project != None.

        Returns:
            {"nodes": [...], "count": N, "total_edgeless": N}
            When project != None also includes "projects_queried": [tag, …].
        """
        lc = get_lifespan(ctx)
        nt, err = _parse_node_type(node_type)
        if err:
            return err

        def _edgeless_for(storage: CognitionStorage) -> list[dict[str, Any]]:
            all_nodes = storage.get_all_nodes()
            edgeless = []
            for node in all_nodes:
                nid = node["id"]
                if nt and node.get("type") != nt.value:
                    continue
                if not storage.get_successors(nid) and not storage.get_predecessors(nid):
                    edgeless.append(node)
            edgeless.sort(key=lambda n: n.get("timestamp") or "", reverse=True)
            return edgeless

        # Default path: byte-identical to pre-XP2.
        if project is None:
            edgeless = _edgeless_for(lc["cognition_storage"])
            total = len(edgeless)
            edgeless = edgeless[:min(limit, 500)]
            return {"nodes": edgeless, "count": len(edgeless), "total_edgeless": total}

        # Multi-project path.
        entries, err2 = resolve_project(lc, project)
        if err2:
            return err2

        all_edgeless: list[dict[str, Any]] = []
        for entry in entries:
            rows = _edgeless_for(entry.storage)
            tag_results(rows, entry.tag)
            all_edgeless.extend(rows)

        all_edgeless.sort(key=lambda n: n.get("timestamp") or "", reverse=True)
        total = len(all_edgeless)
        all_edgeless = all_edgeless[:min(limit, 500)]
        return {
            "nodes": all_edgeless,
            "count": len(all_edgeless),
            "total_edgeless": total,
            "projects_queried": [e.tag for e in entries],
        }

    @mcp.tool()
    def cognition_get_uncurated_nodes(
        ctx: Context,
        node_type: str | None = None,
        limit: int = 50,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Get cognition nodes not yet reviewed by the curate skill.

        This is the curate-orchestrator's worklist — run `/vibe-curate` to launch it
        and process this list. Returns nodes lacking a curated_by_skill_at marker.
        Nodes with only deterministic (or legacy) edges are still considered
        uncurated until the curate-orchestrator reviews them.

        Args:
            node_type: Optional filter: decision, fail, discovery, assumption,
                       constraint, incident, pattern, episode, workflow, task
            limit: Max results (default: 50, max: 500)
            project: None (default, home), tag/path, or "*" (all loaded projects).
                     Aggregate tool: "*" fans across all loaded projects and merges.
                     Each row carries a "project" tag when project != None.

        Returns:
            {"nodes": [...], "count": N, "total_uncurated": N}
            When project != None also includes "projects_queried": [tag, …].
        """
        lc = get_lifespan(ctx)
        nt, err = _parse_node_type(node_type)
        if err:
            return err

        # Default path: byte-identical to pre-XP2.
        if project is None:
            storage: CognitionStorage = lc["cognition_storage"]
            # The returned list is capped (storage caps at 500); the TOTAL is an honest,
            # uncapped count (T-2 — deriving the total from the capped list under-reported
            # any backlog over 500).
            nodes = storage.get_uncurated_nodes(limit=min(limit, 500), node_type=nt)
            return {
                "nodes": nodes,
                "count": len(nodes),
                "total_uncurated": storage.count_uncurated_nodes(node_type=nt),
            }

        # Multi-project path.
        entries, err2 = resolve_project(lc, project)
        if err2:
            return err2

        all_nodes: list[dict[str, Any]] = []
        total_uncurated = 0
        for entry in entries:
            rows = entry.storage.get_uncurated_nodes(limit=min(limit, 500), node_type=nt)
            tag_results(rows, entry.tag)
            all_nodes.extend(rows)
            total_uncurated += entry.storage.count_uncurated_nodes(node_type=nt)

        all_nodes = all_nodes[:min(limit, 500)]
        return {
            "nodes": all_nodes,
            "count": len(all_nodes),
            "total_uncurated": total_uncurated,
            "projects_queried": [e.tag for e in entries],
        }

    @mcp.tool()
    def cognition_mark_curated(
        ctx: Context,
        node_ids: str,
    ) -> dict[str, Any]:
        """Mark nodes as reviewed by the curate skill.

        Called ONLY by the curate-orchestrator agent (launched via /vibe-curate)
        after reviewing a batch, even if no edges were created — marking nodes
        curated without analysis permanently skips them. If you are any other
        agent — including the main instance — do NOT call it.

        Args:
            node_ids: Comma-separated node IDs to mark as curated

        Returns:
            {"marked": N, "not_found": [...]}
        """
        storage: CognitionStorage = get_lifespan(ctx)["cognition_storage"]
        ids = [nid.strip() for nid in node_ids.split(",") if nid.strip()]

        marked = 0
        not_found = []
        for nid in ids:
            if storage.mark_curated_by_skill(nid):
                marked += 1
            else:
                not_found.append(nid)

        return {"marked": marked, "not_found": not_found}

    @mcp.tool()
    def cognition_get_neighbors(
        ctx: Context,
        node_id: str,
        edge_type: str | None = None,
        direction: str = "both",
        project: str | None = None,
    ) -> dict[str, Any]:
        """Get all nodes connected to a given node, optionally filtered by edge type.

        Unlike cognition_get_chain (which only follows led_to), this returns
        ALL connected nodes across all edge types.

        Args:
            node_id: The node to query
            edge_type: Optional filter (led_to, supersedes, etc.)
            direction: "incoming", "outgoing", or "both"
            project: None (default, home) or tag/path for a loaded foreign project.
                     "*" is rejected — node ids are not project-namespaced, so the
                     same id can exist in two projects; use a specific tag.

        Returns:
            {"node_id": "..."}, PLUS "outgoing": [...] when direction is
            "outgoing" or "both", and/or "incoming": [...] when direction is
            "incoming" or "both". Each entry is {id, edge_type, reason, type}
            (that connected node's raw graph "type", not "node_type"). The key
            for a direction NOT requested is entirely ABSENT (not an empty
            list) — e.g. direction="outgoing" never puts "incoming" in the
            result at all; check with .get() or "in", don't assume both keys
            are always present. When project is not None, also includes
            "project": tag. {"error": "..."} on a missing node/bad edge_type/
            unsupported direction/rejected "*".
        """
        lc = get_lifespan(ctx)

        err = _validate_direction(direction, ("incoming", "outgoing", "both"))
        if err:
            return err

        proj_tag: str | None = None
        if project is None:
            storage: CognitionStorage = lc["cognition_storage"]
        elif project == "*":
            return {"error": 'project="*" is not supported for single-node tools'}
        else:
            entries, err2 = resolve_project(lc, project)
            if err2:
                return err2
            storage = entries[0].storage
            proj_tag = entries[0].tag

        if not storage.has_node(node_id):
            return {"error": f"Node '{node_id}' does not exist"}

        et = None
        if edge_type:
            try:
                et = CognitionEdgeType(edge_type)
            except ValueError:
                valid = [e.value for e in CognitionEdgeType]
                return {"error": f"Invalid edge_type '{edge_type}'. Valid: {valid}"}

        result: dict[str, Any] = {"node_id": node_id}
        if proj_tag is not None:
            result["project"] = proj_tag

        if direction in ("outgoing", "both"):
            outgoing = []
            for tid, edata in storage.get_successors(node_id, et):
                node_data = storage.get_node(tid)
                outgoing.append({
                    "id": tid,
                    "edge_type": edata.get("type"),
                    "reason": edata.get("reason"),
                    "type": node_data.get("type") if node_data else None,
                    "summary": node_data.get("summary") if node_data else None,
                })
            result["outgoing"] = outgoing

        if direction in ("incoming", "both"):
            incoming = []
            for sid, edata in storage.get_predecessors(node_id, et):
                node_data = storage.get_node(sid)
                incoming.append({
                    "id": sid,
                    "edge_type": edata.get("type"),
                    "reason": edata.get("reason"),
                    "type": node_data.get("type") if node_data else None,
                    "summary": node_data.get("summary") if node_data else None,
                })
            result["incoming"] = incoming

        return result

    @mcp.tool()
    def cognition_remove_edge(
        ctx: Context,
        from_id: str,
        to_id: str,
        edge_type: str,
    ) -> dict[str, Any]:
        """Remove a specific edge between two cognition nodes.

        Args:
            from_id: Source node ID
            to_id: Target node ID
            edge_type: The edge type to remove (led_to, supersedes, etc.)

        Returns:
            {"removed": true} or {"error": "..."}
        """
        storage: CognitionStorage = get_lifespan(ctx)["cognition_storage"]

        try:
            et = CognitionEdgeType(edge_type)
        except ValueError:
            valid = [e.value for e in CognitionEdgeType]
            return {"error": f"Invalid edge_type '{edge_type}'. Valid: {valid}"}

        if not storage.has_node(from_id):
            return {"error": f"Source node '{from_id}' does not exist"}
        if not storage.has_node(to_id):
            return {"error": f"Target node '{to_id}' does not exist"}

        removed = storage.remove_edge(from_id, to_id, et)
        if not removed:
            return {"error": f"No {edge_type} edge exists from {from_id} to {to_id}"}

        return {"removed": True, "from_id": from_id, "to_id": to_id, "edge_type": edge_type}

    @mcp.tool()
    def cognition_remove_node(
        ctx: Context,
        node_id: str,
    ) -> dict[str, Any]:
        """Delete a cognition node and ALL of its attached edges.

        DESTRUCTIVE and not undoable. Removing a node cascades to every edge
        incident to it (incoming and outgoing) and purges its embedding so it
        no longer appears in cognition_search. The deletion converges across
        concurrent sessions on the shared journal. Use this to prune junk,
        test, or duplicate nodes — for an outdated-but-real node, prefer adding
        a `supersedes` edge (cognition_add_edge) over deleting the history.
        The acting author is resolved SERVER-SIDE from this repo's git config
        (same as cognition_add_task) and recorded in the journal tombstone.

        WARNING — parent tasks: deleting a task that has child tasks DETACHES
        the children; they keep reporting the deleted id as their `parent_id`
        (this stale-pointer behavior is deliberate, not a bug). Reparent or
        close children first if that matters.

        Args:
            node_id: ID of the node to delete.

        Returns:
            {"removed": true, "id": ..., "removed_edges": [...], "edges_removed": N}
            on success, where removed_edges lists each orphaned edge
            ({"from", "to", "type"}). For document nodes the result additionally
            carries "unlinked_artifacts": a list of managed on-disk artifacts
            (text sidecar / content blob, repo-relative under
            .cognition/documents/) reclaimed because no other document
            references them — note a previously committed blob still survives
            in git history. Returns {"error": "..."} if the node does not exist.
        """
        lc = get_lifespan(ctx)
        storage: CognitionStorage = lc["cognition_storage"]
        embed_storage: ChromaDBStorage = lc["cognition_embedding_storage"]

        # Delete provenance (WP-1): server-resolved identity, same source as
        # cognition_add_task's creator (pure file reads, never raises).
        removed_by = resolve_git_identity(storage.cognition_dir.parent)

        result = delete_cognition_node(storage, embed_storage, node_id, removed_by=removed_by)
        if result is None:
            return {"error": f"Node '{node_id}' does not exist"}

        return {"removed": True, **result}

    @mcp.tool()
    def cognition_reload(ctx: Context) -> dict[str, Any]:
        """Force-reload the cognition graph from the on-disk journal.

        The store auto-catches-up on the shared journal before every operation,
        so concurrent sessions normally converge on their own. This tool is an
        explicit lever / diagnostic: it fully re-replays journal.jsonl and
        reports node/edge counts before and after, so you can confirm the
        in-memory graph matches what's on disk (e.g. after another agent on the
        same project recorded nodes).

        Returns:
            {"nodes_before", "edges_before", "nodes_after", "edges_after"}
        """
        storage: CognitionStorage = get_lifespan(ctx)["cognition_storage"]
        return storage.reload()

    # ── Cross-project tools (XP1) ─────────────────────────────────

    @mcp.tool()
    def cognition_load_project(ctx: Context, path: str) -> dict[str, Any]:
        """Load a foreign cognition project so all read tools can query it.

        Attaches the project at <path> to this session and runs the embedding-model
        guard. After loading, pass the returned `tag` (or the path) as the `project`
        arg on any read tool to route that call to the foreign project:
          - Structural reads (get_node, get_chain, get_neighbors, get_history, …)
          - Semantic search (cognition_search) — availability depends on model_guard

        The load is READ-ONLY: never writes to the foreign journal or chroma, never
        runs sync/backfill, and never creates a chroma collection if one is absent.

        Re-loading an already-loaded project (home or foreign) returns an error;
        unload it first with cognition_unload_project if you need to reload.

        model_guard values and what they mean for you:
          match         — embedding model confirmed compatible; semantic search enabled
          unknown       — pre-stamp collection (no model metadata); search runs with a
                          degraded-confidence caveat in project_notes
          no-index      — no chroma collection found; semantic search DISABLED for this
                          project; structural reads still work
          dim-mismatch  — vector dimensions differ from home; semantic DISABLED
          model-mismatch — different embedding model; semantic DISABLED

        For dim/model/no-index guard states, semantic search silently returns no hits
        for that project (project_notes[tag].semantic_unavailable explains why); this
        is not "no history" — it means the index is guarded off.

        Args:
            path: Absolute or relative path to the foreign project root (must contain
                  a .cognition/journal.jsonl).

        Returns:
            On success: {tag, path, node_count, vector_count, model_guard, warning?}
            vector_count is "n/a" when no chroma index exists for the project.

            On failure: {"error": "..."} — invalid path, path is already loaded
            as home, no .cognition/journal.jsonl found at path, or path is
            already loaded as a foreign project (in which case the error dict
            ALSO carries "tag": <existing tag> — the already-loaded project's
            tag, not confirmation of a new load; check for "error" first).
        """
        return _load_project_core(get_lifespan(ctx), path)

    @mcp.tool()
    def cognition_unload_project(ctx: Context, project: str) -> dict[str, Any]:
        """Unload a previously loaded foreign project.

        Refuses to unload the home project (always pinned). Releases the chroma
        client handle so B's directory is unlocked on Windows.

        Args:
            project: Tag or path of the foreign project to unload.

        Returns:
            {unloaded: tag, path} or {error: ...}
        """
        return _unload_project_core(get_lifespan(ctx), project)

    @mcp.tool()
    def cognition_list_projects(ctx: Context) -> dict[str, Any]:
        """List all loaded cognition projects (home + foreign).

        Use this to see which projects are available for the `project` arg on read
        tools, and to understand each project's semantic search availability.

        model_guard values per entry:
          match         — semantic search enabled for this project
          unknown       — pre-stamp; search runs with degraded-confidence caveat
          no-index      — no chroma collection; semantic DISABLED, structural only
          dim-mismatch  — vector dimensions differ; semantic DISABLED
          model-mismatch — different embedding model; semantic DISABLED

        Returns:
            {
              foreign_count: N,          # number of loaded foreign projects
              projects: [{
                tag,                     # short label used as the project= arg
                path,
                node_count,
                vector_count,            # int, or "n/a" (no index), or -1 (stat failed)
                pinned,                  # true for home (cannot be unloaded)
                model_guard,
              }]
            }
        """
        return _list_projects_core(get_lifespan(ctx))

