"""MCP tools for the Cognition History Graph."""

import logging
from datetime import datetime, timezone
from typing import Any

from fastmcp import Context

from ..cognition import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    CognitionStorage,
    generate_node_id,
    get_history_for_context,
    get_reasoning_chain,
)
from ..cognition.curator import CognitionCurator
from ..embeddings import ChromaDBStorage, EmbeddingGenerator
from .utils import require_embeddings

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

    timestamp = datetime.now(timezone.utc).isoformat()
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

    # Create deterministic part_of edges via reference matching
    det_edges = storage.create_deterministic_edges(node_id)

    # Enqueue for curator (only if background curation is enabled)
    config = ctx.request_context.lifespan_context.get("config")
    if config and config.curator_enabled:
        curator: CognitionCurator | None = ctx.request_context.lifespan_context.get("cognition_curator")
        if curator is not None:
            curator.enqueue(node)

    result: dict[str, Any] = {
        "id": node_id,
        "type": node_type.value,
        "summary": summary,
        "timestamp": timestamp,
    }
    if det_edges:
        result["deterministic_edges_created"] = det_edges
    return result


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

        Edges to related existing nodes are created automatically by a curator LLM
        in the background — you do not need to specify relationships manually.

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
        - Always include references (issue numbers, PR numbers, commit hashes) so the
          curator can link related nodes. Format: "issue:LL-298,pr:97,commit:abc123"
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
                        so the curator can link related nodes. Example: "issue:LL-298,pr:97"

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

        embedding_storage: ChromaDBStorage = ctx.request_context.lifespan_context[
            "cognition_embedding_storage"
        ]
        generator: EmbeddingGenerator = ctx.request_context.lifespan_context["embedding_generator"]

        limit = min(limit, 50)
        query_embedding = generator.generate_query_embedding(query)

        results = embedding_storage.vector_search(
            query_embedding=query_embedding,
            limit=limit,
            entity_type=node_type,
        )

        formatted = []
        for r in results:
            formatted.append({
                "id": r.get("_id"),
                "node_type": r.get("entity_type"),
                "summary": r.get("summary") or r.get("name"),
                "author": r.get("author"),
                "timestamp": r.get("timestamp"),
                "severity": r.get("severity"),
                "context": r.get("context", ""),
                "score": r.get("score"),
            })

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

        Use this to manually curate relationships that the background curator
        missed or to create edges during bulk curation.

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
            return {"error": "duplicate_of edges require merge logic. Use cognition_record to let the curator handle duplicates."}

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

        timestamp = datetime.now(timezone.utc).isoformat()
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
            timestamp = datetime.now(timezone.utc).isoformat()
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

        Returns nodes lacking a curated_by_skill_at marker. Nodes with only
        deterministic or background-curator edges are still considered
        uncurated until the curate skill reviews them.

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

