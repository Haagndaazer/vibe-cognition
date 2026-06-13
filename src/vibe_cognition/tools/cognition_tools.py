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
    get_reasoning_chain,
)
from ..cognition.documents import (
    BLOB_REFUSE_BYTES,
    BLOB_WARN_BYTES,
    add_gitignore_entry,
    blob_path,
    blob_rel_path,
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
from ..embeddings import ChromaDBStorage, EmbeddingGenerator
from .utils import get_lifespan, require_embeddings

logger = logging.getLogger(__name__)


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
    storage: CognitionStorage = ctx.request_context.lifespan_context["cognition_storage"]
    embedding_storage: ChromaDBStorage = ctx.request_context.lifespan_context[
        "cognition_embedding_storage"
    ]
    generator: EmbeddingGenerator = ctx.request_context.lifespan_context["embedding_generator"]

    # Parse comma-separated strings into lists
    context_list = [c.strip() for c in context.split(",") if c.strip()] if context else []
    references_list = [r.strip() for r in references.split(",") if r.strip()] if references else []

    timestamp = datetime.now(UTC).isoformat()
    node_id = generate_node_id(node_type.value, summary, timestamp)

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
    storage.add_node(node)

    # Embed and upsert to ChromaDB (skip if model not loaded yet — startup sync catches it later)
    embedding_ready = ctx.request_context.lifespan_context.get("embedding_ready")
    if embedding_ready and embedding_ready.is_set() and not ctx.request_context.lifespan_context.get("embedding_error"):
        embed_text = f"{node_type.value}: {summary}\n{detail}"
        embedding = generator.generate_query_embedding(embed_text)
        metadata: dict[str, Any] = {
            "entity_type": node_type.value,
            "summary": summary,
            "author": author,
            "timestamp": timestamp,
            "context": ",".join(context_list),
        }
        if severity:
            metadata["severity"] = severity
        if references_list:
            metadata["references"] = ",".join(references_list)
        embedding_storage.upsert_embedding(node_id, embedding, metadata)

    # Create deterministic part_of edges via reference matching. This is the ONLY
    # automatic edge creation — semantic curation (led_to, supersedes, contradicts,
    # etc.) is the agent's job via the /vibe-curate skill after recording.
    det_edges = storage.create_deterministic_edges(node_id)

    result: dict[str, Any] = {
        "id": node_id,
        "type": node_type.value,
        "summary": summary,
        "timestamp": timestamp,
    }
    if det_edges:
        result["deterministic_edges_created"] = det_edges
    return result


def _format_search_results(
    results: list[dict[str, Any]], storage: CognitionStorage
) -> list[dict[str, Any]]:
    """Format vector-search hits, DROPPING any whose (chunk-stripped) node id is
    absent from the graph — the N1 ghost-search fix (§9 N1).

    A cross-process remove_node replays into the graph but never un-embeds (replay
    touches only the graph; the embedding sync only ADDS), so Chroma serves hits for
    nodes deleted on another machine — escalated by documents to verbatim deleted
    client text. Filtering on graph presence is the CORRECTNESS guarantee (it never
    deletes; the startup sweep is best-effort reclamation). The ``#chunk-`` strip is
    forward-compatible with D2 chunk ids (``<node_id>#chunk-N``)."""
    formatted: list[dict[str, Any]] = []
    for r in results:
        raw_id = r.get("_id") or ""
        node_id = raw_id.split("#chunk-")[0]
        if not storage.has_node(node_id):
            continue
        formatted.append({
            "id": raw_id,
            "node_type": r.get("entity_type"),
            "summary": r.get("summary") or r.get("name"),
            "author": r.get("author"),
            "timestamp": r.get("timestamp"),
            "severity": r.get("severity"),
            "context": r.get("context", ""),
            "score": r.get("score"),
        })
    return formatted


def _materialize_blob(
    cognition_dir: Path, sha: str, ext: str, blob_rel: str, size: int,
    local_only: bool | None, *, data: bytes | None, src: Path | None,
) -> tuple[bool, dict[str, Any], list[str]]:
    """Write the content-addressed blob (write-once) and reconcile its git policy.

    Returns ``(effective_local_only, status, warnings)``. Size policy §9 S1 (no
    hard cap): ≥50MB auto local_only + warn; ≥95MB default-commit refused (forced
    local_only). S3 dedup transitions: a blob that WAS local_only going to default
    is ``promoted`` (its .gitignore line removed); a blob already committed going
    to local_only reports ``already_committed`` (git can't un-publish it)."""
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

    write_blob(cognition_dir, sha, ext, data=data, src_path=src)

    status: dict[str, Any] = {}
    if eff_local:
        add_gitignore_entry(cognition_dir, blob_rel)
        if blob_existed and not was_ignored:
            status["already_committed"] = True
            warnings.append(
                "blob already committed; local_only cannot un-publish it (git history retains it)"
            )
    elif remove_gitignore_entry(cognition_dir, blob_rel):
        status["promoted"] = True
    return eff_local, status, warnings


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
) -> dict[str, Any]:
    """Document store, reference (default) or opt-in copy mode (testable core of
    cognition_store_document). ``store_copy`` copies the bytes into a content-
    addressed blob; ``local_only`` keeps that blob out of git (else committed,
    subject to the size policy)."""
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
    # generate_node_id hashes type:summary:timestamp. The Windows clock resolution
    # is ~15 ms, so two stores of the same title within one tick (the force_new
    # twin case, or just two docs sharing a title) hash to the SAME id — and
    # add_node would then silently OVERWRITE the first node. Salt the summary until
    # the id is free so each store gets a distinct node (the salt only feeds the id
    # hash; the stored summary stays the title).
    node_id = generate_node_id(CognitionNodeType.DOCUMENT.value, title, timestamp)
    salt = 0
    while storage.has_node(node_id):
        salt += 1
        node_id = generate_node_id(
            CognitionNodeType.DOCUMENT.value, f"{title}#{salt}", timestamp
        )
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
    storage.add_node(node)
    # NOT embedded into ChromaDB in this version (document search lands later);
    # create_deterministic_edges is graph-inert for documents until the pair rules ship.
    storage.create_deterministic_edges(node_id)

    return {
        "node_id": node_id,
        "doc_ref": ref,
        "mode": mode,
        "size": size,
        "indexed_text_chars": indexed_chars,
        **blob_result,
    }


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

        CURATION IS YOUR JOB. The only edges created automatically are deterministic
        `part_of` edges, formed when nodes share references (commit/issue/PR). All
        semantic relationships (led_to, supersedes, contradicts, resolved_by, relates_to)
        are NOT created for you — after recording, run the `/vibe-curate` skill to link
        the new nodes (or add edges manually with cognition_add_edge).

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

        ENTITY NODES (decision, fail, discovery, assumption, constraint, incident, pattern):
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
            node_type: One of: decision, fail, discovery, assumption, constraint, incident, pattern, episode
            summary: Short description (max 250 chars for entities, brief title for episodes)
            detail: Brief rationale for entities (1-3 sentences), or full narrative for episodes
            context: Related code areas, file paths, AND topical terms (comma-separated).
                     Example: "flashcard_local_datasource.dart, HiveService, data migration, LL-298"
            author: The current git user name (e.g., "Colton Dyck")
            severity: Optional priority — critical, high, normal, low
            references: Optional external refs, comma-separated. Include issue/PR/commit refs
                        so nodes link to their episode (part_of) and /vibe-curate can
                        relate them. Example: "issue:LL-298,pr:97"

        Returns:
            The created node with ID and timestamp
        """
        try:
            nt = CognitionNodeType(node_type)
        except ValueError:
            valid = [e.value for e in CognitionNodeType]
            return {"error": f"Invalid node_type '{node_type}'. Valid: {valid}"}

        return _record_node(
            ctx, nt, summary, detail, context, author,
            severity, references,
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
             blob_bytes?, blob_path?, local_only?, promoted?, already_committed?, warnings?}
        """
        storage: CognitionStorage = get_lifespan(ctx)["cognition_storage"]
        return _store_document(
            storage, title, document_text, context, author,
            file_path=file_path, content_text=content_text,
            references=references, mime=mime, force_new=force_new,
            store_copy=store_copy, local_only=local_only,
        )

    @mcp.tool()
    def cognition_get_document(
        ctx: Context,
        node_id: str | None = None,
        doc_ref_arg: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve a stored document: metadata + full sidecar text + path, with a
        freshness check.

        Resolve by `node_id` or by `doc_ref_arg` (the `doc:<hash>` returned at
        store time). In reference mode the original file is re-hashed: `freshness`
        is `unchanged | modified | missing`.

        Returns:
            {node_id, doc_ref, metadata, text, path, freshness} or {"error": ...}
        """
        storage: CognitionStorage = get_lifespan(ctx)["cognition_storage"]
        return _get_document(storage, node_id=node_id, doc_ref_arg=doc_ref_arg)

    @mcp.tool()
    def cognition_search(
        ctx: Context,
        query: str,
        node_type: str | None = None,
        limit: int = 10,
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
                       constraint, incident, pattern, episode
            limit: Max results (default: 10)

        Returns:
            Matching cognition nodes with similarity scores
        """
        err = require_embeddings(ctx)
        if err:
            return err

        lifespan = get_lifespan(ctx)
        embedding_storage: ChromaDBStorage = lifespan["cognition_embedding_storage"]
        generator: EmbeddingGenerator = lifespan["embedding_generator"]
        storage: CognitionStorage = lifespan["cognition_storage"]

        limit = min(limit, 50)
        query_embedding = generator.generate_query_embedding(query)

        results = embedding_storage.vector_search(
            query_embedding=query_embedding,
            limit=limit,
            entity_type=node_type,
        )

        formatted = _format_search_results(results, storage)
        return {
            "query": query,
            "results": formatted,
            "count": len(formatted),
        }

    @mcp.tool()
    def cognition_get_chain(
        ctx: Context,
        node_id: str,
        max_depth: int = 5,
        direction: str = "outgoing",
    ) -> dict[str, Any]:
        """Get the reasoning chain from/to a cognition node via LED_TO edges.

        Follow the chain of causation: what led to what, or what was caused by what.

        Args:
            node_id: Starting node ID
            max_depth: Maximum depth to traverse (default: 5)
            direction: "outgoing" (what it led to) or "incoming" (what led to it)

        Returns:
            Nested structure showing the reasoning chain
        """
        storage: CognitionStorage = ctx.request_context.lifespan_context["cognition_storage"]
        return get_reasoning_chain(storage, node_id, max_depth, direction)

    @mcp.tool()
    def cognition_get_history(
        ctx: Context,
        context_term: str | None = None,
        node_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Get cognition nodes by context area, type, or recency.

        If context_term is provided, filters nodes whose context fields match
        (case-insensitive substring). Otherwise returns the most recent nodes.

        Args:
            context_term: Optional term to search in context fields (file paths, topics)
            node_type: Optional filter: decision, fail, discovery, assumption,
                       constraint, incident, pattern, episode
            limit: Max results (default: 20)

        Returns:
            Matching cognition nodes sorted by timestamp (newest first)
        """
        storage: CognitionStorage = ctx.request_context.lifespan_context["cognition_storage"]

        nt = None
        if node_type:
            try:
                nt = CognitionNodeType(node_type)
            except ValueError:
                valid = [e.value for e in CognitionNodeType]
                return {"error": f"Invalid node type '{node_type}'. Valid: {valid}"}

        if context_term:
            results = get_history_for_context(storage, context_term, nt)
            results = results[:limit]
        else:
            results = storage.get_recent_nodes(limit=limit, node_type=nt)

        return {
            "context_term": context_term,
            "results": results,
            "count": len(results),
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

        Use this to curate relationships directly — either while running the
        `/vibe-curate` skill or to add a single edge by hand.

        Args:
            from_id: Source node ID (must exist)
            to_id: Target node ID (must exist)
            edge_type: One of: led_to, supersedes, contradicts, relates_to,
                       resolved_by, part_of
            reason: Optional brief explanation of why this edge exists
            source: Provenance tag (default: "manual")

        Returns:
            {"created": true, ...} or {"error": "..."}
        """
        storage: CognitionStorage = ctx.request_context.lifespan_context["cognition_storage"]

        try:
            et = CognitionEdgeType(edge_type)
        except ValueError:
            valid = [e.value for e in CognitionEdgeType if e != CognitionEdgeType.DUPLICATE_OF]
            return {"error": f"Invalid edge_type '{edge_type}'. Valid: {valid}"}

        if et == CognitionEdgeType.DUPLICATE_OF:
            return {"error": "duplicate_of edges require merge logic and are not supported here."}

        if from_id == to_id:
            return {"error": "Self-referencing edges are not allowed"}

        if not storage.has_node(from_id):
            return {"error": f"Source node '{from_id}' does not exist"}
        if not storage.has_node(to_id):
            return {"error": f"Target node '{to_id}' does not exist"}

        # Check if same triple already exists
        existing = storage.get_successors(from_id, et)
        if any(tid == to_id for tid, _ in existing):
            return {"error": f"Edge already exists: {from_id} -[{edge_type}]-> {to_id}"}

        timestamp = datetime.now(UTC).isoformat()
        edge = CognitionEdge(
            from_id=from_id,
            to_id=to_id,
            edge_type=et,
            timestamp=timestamp,
            source=source,
        )
        storage.add_edge(edge)

        if reason:
            logger.info(f"Edge created: {from_id} -[{edge_type}]-> {to_id} (reason: {reason})")

        return {
            "created": True,
            "from_id": from_id,
            "to_id": to_id,
            "edge_type": edge_type,
            "timestamp": timestamp,
        }

    @mcp.tool()
    def cognition_add_edges_batch(
        ctx: Context,
        edges: str,
    ) -> dict[str, Any]:
        """Create multiple edges in one call.

        Each edge in the JSON array needs from_id, to_id, and edge_type.
        Edges are validated individually — invalid ones are skipped and reported.

        Args:
            edges: JSON array string of edge objects, max 500. Example:
                   '[{"from_id":"abc","to_id":"def","edge_type":"led_to"}]'

        Returns:
            {"created": N, "skipped": N, "errors": [...]}
        """
        import json as _json
        storage: CognitionStorage = ctx.request_context.lifespan_context["cognition_storage"]

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
            if et == CognitionEdgeType.DUPLICATE_OF:
                errors.append(f"[{i}] duplicate_of not allowed in batch")
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

            # Check for duplicates (in batch and in graph)
            if triple in seen_triples:
                errors.append(f"[{i}] Duplicate in batch: {fid} -[{etype_str}]-> {tid}")
                skipped += 1
                continue
            existing = storage.get_successors(fid, et)
            if any(t == tid for t, _ in existing):
                errors.append(f"[{i}] Already exists: {fid} -[{etype_str}]-> {tid}")
                skipped += 1
                continue

            seen_triples.add(triple)
            timestamp = datetime.now(UTC).isoformat()
            edge = CognitionEdge(
                from_id=fid, to_id=tid, edge_type=et,
                timestamp=timestamp, source=src,
            )
            storage.add_edge(edge)
            created += 1

        return {"created": created, "skipped": skipped, "errors": errors[:50]}

    @mcp.tool()
    def cognition_get_edgeless_nodes(
        ctx: Context,
        node_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Get cognition nodes that have zero edges (no incoming or outgoing).

        Useful for graph health diagnostics — finding truly isolated nodes.
        For curation tracking, prefer cognition_get_uncurated_nodes instead.

        Args:
            node_type: Optional filter: decision, fail, discovery, assumption,
                       constraint, incident, pattern, episode
            limit: Max results (default: 50, max: 500)

        Returns:
            {"nodes": [...], "count": N, "total_edgeless": N}
        """
        storage: CognitionStorage = ctx.request_context.lifespan_context["cognition_storage"]
        all_nodes = storage.get_all_nodes()

        edgeless = []
        for node in all_nodes:
            nid = node["id"]
            if node_type and node.get("type") != node_type:
                continue
            if not storage.get_successors(nid) and not storage.get_predecessors(nid):
                edgeless.append(node)

        edgeless.sort(key=lambda n: n.get("timestamp", ""), reverse=True)
        total = len(edgeless)
        edgeless = edgeless[:min(limit, 500)]

        return {"nodes": edgeless, "count": len(edgeless), "total_edgeless": total}

    @mcp.tool()
    def cognition_get_uncurated_nodes(
        ctx: Context,
        node_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Get cognition nodes not yet reviewed by the curate skill.

        This is the agent's curation worklist — run `/vibe-curate` to process it.
        Returns nodes lacking a curated_by_skill_at marker. Nodes with only
        deterministic (or legacy) edges are still considered uncurated until the
        curate skill reviews them.

        Args:
            node_type: Optional filter: decision, fail, discovery, assumption,
                       constraint, incident, pattern, episode
            limit: Max results (default: 50, max: 500)

        Returns:
            {"nodes": [...], "count": N, "total_uncurated": N}
        """
        storage: CognitionStorage = ctx.request_context.lifespan_context["cognition_storage"]
        nt = CognitionNodeType(node_type) if node_type else None
        capped = min(limit, 500)

        # Get total count (uncapped) for reporting
        all_uncurated = storage.get_uncurated_nodes(limit=999999, node_type=nt)
        nodes = all_uncurated[:capped]

        return {
            "nodes": nodes,
            "count": len(nodes),
            "total_uncurated": len(all_uncurated),
        }

    @mcp.tool()
    def cognition_mark_curated(
        ctx: Context,
        node_ids: str,
    ) -> dict[str, Any]:
        """Mark nodes as reviewed by the curate skill.

        Call after analyzing a batch of nodes, even if no edges were created.
        Prevents re-processing nodes that were reviewed but had no meaningful
        relationships.

        Args:
            node_ids: Comma-separated node IDs to mark as curated

        Returns:
            {"marked": N, "not_found": [...]}
        """
        storage: CognitionStorage = ctx.request_context.lifespan_context["cognition_storage"]
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
    ) -> dict[str, Any]:
        """Get all nodes connected to a given node, optionally filtered by edge type.

        Unlike cognition_get_chain (which only follows led_to), this returns
        ALL connected nodes across all edge types.

        Args:
            node_id: The node to query
            edge_type: Optional filter (led_to, supersedes, etc.)
            direction: "incoming", "outgoing", or "both"

        Returns:
            {"node_id": "...", "incoming": [...], "outgoing": [...]}
        """
        storage: CognitionStorage = ctx.request_context.lifespan_context["cognition_storage"]

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

        if direction in ("outgoing", "both"):
            outgoing = []
            for tid, edata in storage.get_successors(node_id, et):
                node_data = storage.get_node(tid)
                outgoing.append({
                    "id": tid,
                    "edge_type": edata.get("type"),
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
        storage: CognitionStorage = ctx.request_context.lifespan_context["cognition_storage"]

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

        Args:
            node_id: ID of the node to delete.

        Returns:
            {"removed": true, "id": ..., "removed_edges": [...], "edges_removed": N}
            on success, where removed_edges lists each orphaned edge
            ({"from", "to", "type"}); or {"error": "..."} if the node does not exist.
        """
        storage: CognitionStorage = ctx.request_context.lifespan_context["cognition_storage"]
        embed_storage: ChromaDBStorage = ctx.request_context.lifespan_context[
            "cognition_embedding_storage"
        ]

        result = delete_cognition_node(storage, embed_storage, node_id)
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
        storage: CognitionStorage = ctx.request_context.lifespan_context["cognition_storage"]
        return storage.reload()

