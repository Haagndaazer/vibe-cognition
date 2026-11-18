"""MCP tools for curation, edges, node removal, reload and cross-project loading."""

import secrets
from datetime import UTC, datetime
from typing import Any

from fastmcp import Context

from ..cognition import CognitionEdgeType, CognitionStorage, delete_cognition_node
from ..embeddings import VectorStore
from .cognition_tools import (
    _CURATION_TOKEN_KEY,
    _add_edge_core,
    _add_edges_batch_core,
    _flag_constraint_scope,
    _gated_identity,
    _list_projects_core,
    _load_project_core,
    _parse_node_type,
    _require_curation,
    _unload_project_core,
    _validate_direction,
)
from .dispatch import dispatch_tool
from .project_registry import resolve_project, tag_results
from .utils import get_lifespan


def register_graph_tools(mcp) -> None:

    @dispatch_tool(mcp)
    def cognition_begin_curation(ctx: Context) -> dict[str, Any]:
        """Start a curation session and mint the token the edge-writing tools require.

        Called ONLY by the curate-orchestrator at the start of a run. Returns
        {"curation_token", "session_id", "uncurated"}. Pass curation_token on every
        cognition_add_edge / cognition_add_edges_batch / cognition_mark_curated call;
        each accepted edge is stamped with session_id (visible in
        cognition_get_neighbors as ``curation_session`` and counted in get_status's
        ``curation_sessions``). Tokens live for this server process only -- after a
        server restart, begin a new session. This is friction plus audit, not an
        access control: the server cannot see who is calling. A subagent may be
        talking to a different server process than its launcher (Codex spawns
        one per agent thread); mint the token in the process that will write.

        Args:
            (none)

        Identity-gated even though it writes nothing to the graph. Every write it
        leads to IS gated, and the curate-orchestrator has no way to confirm an
        identity (cognition_set_identity is deliberately not in its tool list), so
        without this the run would assess the backlog, spawn analyzers, burn their
        tokens, and only then be refused at the first edge write. Failing here
        costs nothing and says exactly what to do.

        Returns:
            {"curation_token": str, "session_id": str ("cur-<hex>"),
             "uncurated": int (the full uncurated backlog, uncapped)}
            Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity or its profile is incomplete. The orchestrator
            cannot fix that itself: stop and report it, so the human's own session
            can run cognition_set_identity.
        """
        lc = get_lifespan(ctx)
        storage: CognitionStorage = lc["cognition_storage"]
        gate, _ = _gated_identity(storage)
        if gate is not None:
            return gate
        sessions = lc.setdefault(_CURATION_TOKEN_KEY, {})
        token = secrets.token_urlsafe(18)
        session_id = f"cur-{secrets.token_hex(6)}"
        sessions[token] = {
            "session_id": session_id,
            "started_at": datetime.now(UTC).isoformat(),
            "writes": 0,
        }
        return {
            "curation_token": token,
            "session_id": session_id,
            "uncurated": storage.count_uncurated_nodes(),
        }

    @dispatch_tool(mcp)
    def cognition_add_edge(
        ctx: Context,
        from_id: str,
        to_id: str,
        edge_type: str,
        reason: str | None = None,
        source: str = "manual",
        curation_token: str | None = None,
    ) -> dict[str, Any]:
        """Create a directed edge between two existing cognition nodes.

        ONLY the curate-orchestrator agent (launched via /vibe-curate) may use this
        tool. If you are any other agent — including the main instance — do NOT
        call it; to get edges created, run /vibe-curate. No manual carve-out.
        Requires ``curation_token`` from cognition_begin_curation; without a valid
        token the write is refused. This is friction plus audit, not an ACL (the
        server cannot see who is calling): every accepted edge is stamped with the
        curation session id, and get_status's edges_outside_curation count (backed by
        the edge_sources histogram) still tracks legacy/manual sources.

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
            curation_token: Token from cognition_begin_curation for this server
                            process. Required -- absent or unknown tokens are
                            refused with {"error": "curation token required: ..."}
                            and nothing is written.

        Returns:
            {"created": true, "from_id", "to_id", "edge_type", "timestamp"} on
            success; the accepted edge carries ``curation_session`` (the session
            id from cognition_begin_curation). {"error": "..."} otherwise.
            Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity, or its profile is missing role/seniority/
            reports_to — the payload names what to ask the human for and the
            `cognition_set_identity` call that fixes it. Nothing is written.
            A valid token is checked FIRST, so a caller with no token is told that
            rather than told to onboard.
        """
        lc = get_lifespan(ctx)
        session, refusal = _require_curation(lc, curation_token)
        if refusal:
            return refusal
        assert session is not None
        storage: CognitionStorage = lc["cognition_storage"]
        gate, _ = _gated_identity(storage)
        if gate is not None:
            return gate
        result = _add_edge_core(
            storage, from_id, to_id, edge_type, reason, source,
            curation_session=session["session_id"],
        )
        if result.get("created"):
            session["writes"] += 1
        return result

    @dispatch_tool(mcp)
    def cognition_add_edges_batch(
        ctx: Context,
        edges: str,
        curation_token: str | None = None,
    ) -> dict[str, Any]:
        """Create multiple edges in one call.

        ONLY the curate-orchestrator agent (launched via /vibe-curate) may use this
        tool. If you are any other agent — including the main instance — do NOT
        call it; to get edges created, run /vibe-curate. No manual carve-out.
        Requires ``curation_token`` from cognition_begin_curation; without a valid
        token the write is refused. This is friction plus audit, not an ACL (the
        server cannot see who is calling): every accepted edge is stamped with the
        curation session id, and get_status's edges_outside_curation count (backed by
        the edge_sources histogram) still tracks legacy/manual sources.

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
            curation_token: Token from cognition_begin_curation for this server
                            process. Required -- absent or unknown tokens are
                            refused with {"error": "curation token required: ..."}
                            and no edge in the batch is written.

        Returns:
            {"created": N, "skipped": N, "errors": [...]}; every created edge
            carries ``curation_session``.
            Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity, or its profile is missing role/seniority/
            reports_to — the payload names what to ask the human for and the
            `cognition_set_identity` call that fixes it. Nothing is written.
            A valid token is checked FIRST, so a caller with no token is told that
            rather than told to onboard.
        """
        lc = get_lifespan(ctx)
        session, refusal = _require_curation(lc, curation_token)
        if refusal:
            return refusal
        assert session is not None
        storage: CognitionStorage = lc["cognition_storage"]
        gate, _ = _gated_identity(storage)
        if gate is not None:
            return gate
        result = _add_edges_batch_core(storage, edges, curation_session=session["session_id"])
        session["writes"] += int(result.get("created", 0) or 0)
        return result

    @dispatch_tool(mcp)
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
                       constraint, incident, pattern, episode, workflow, task, person
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

    @dispatch_tool(mcp)
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
                       constraint, incident, pattern, episode, workflow, task, person
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

    @dispatch_tool(mcp)
    def cognition_mark_curated(
        ctx: Context,
        node_ids: str,
        curation_token: str | None = None,
    ) -> dict[str, Any]:
        """Mark nodes as reviewed by the curate skill.

        Called ONLY by the curate-orchestrator agent (launched via /vibe-curate)
        after reviewing a batch, even if no edges were created — marking nodes
        curated without analysis permanently skips them. If you are any other
        agent — including the main instance — do NOT call it. Requires
        ``curation_token`` from cognition_begin_curation; refused without it.

        Args:
            node_ids: Comma-separated node IDs to mark as curated
            curation_token: Token from cognition_begin_curation for this server
                            process. Required -- absent or unknown tokens are
                            refused with {"error": "curation token required: ..."}
                            and nothing is marked.

        Returns:
            {"marked": N, "not_found": [...]} or {"error": "..."}
            Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity, or its profile is missing role/seniority/
            reports_to — the payload names what to ask the human for and the
            `cognition_set_identity` call that fixes it. Nothing is written.
            A valid token is checked FIRST, so a caller with no token is told that
            rather than told to onboard.
        """
        lc = get_lifespan(ctx)
        session, refusal = _require_curation(lc, curation_token)
        if refusal:
            return refusal
        assert session is not None
        storage: CognitionStorage = lc["cognition_storage"]
        gate, _ = _gated_identity(storage)
        if gate is not None:
            return gate
        ids = [nid.strip() for nid in node_ids.split(",") if nid.strip()]

        marked = 0
        not_found = []
        for nid in ids:
            if storage.mark_curated_by_skill(nid):
                marked += 1
            else:
                not_found.append(nid)

        session["writes"] += marked
        return {"marked": marked, "not_found": not_found}

    @dispatch_tool(mcp)
    def cognition_flag_constraint_scope(
        ctx: Context,
        node_id: str,
        suggested_scope: str,
        reason: str,
        curation_token: str | None = None,
    ) -> dict[str, Any]:
        """Flag a constraint whose scope looks wrong, for its owner to rule on.

        Called ONLY by the curate-orchestrator agent (launched via /vibe-curate)
        after its scope review. Nothing about the constraint's visibility changes:
        the flag is stored on the node and listed under "Constraints to Review" in
        the OWNER's session-start context until the owner rules with
        cognition_update_node(node_id, scope=...) (passing the current scope keeps
        it and clears the flag). If you are any other agent — including the main
        instance — do NOT call it. Requires ``curation_token`` from
        cognition_begin_curation; refused without it.

        Args:
            node_id: The constraint to flag.
            suggested_scope: "personal" or "project" — the scope it should probably
                have. Must differ from its current scope.
            reason: One sentence on why the suggested scope fits better (trimmed to
                300 characters).
            curation_token: Token from cognition_begin_curation for this server
                process. Required -- absent or unknown tokens are refused with
                {"error": "curation token required: ..."} and nothing is written.

        Returns:
            {"flagged": true, "node_id", "current_scope", "suggested_scope",
             "owner_is_you": bool} — owner_is_you is true when the constraint
            belongs to this checkout's confirmed identity, so its ruling belongs to
            the human in this session. {"error": ...} when the node is absent
            (including a teammate's personal constraint), not a constraint, the
            suggestion equals the current scope, it has no recorded owner, it has
            been superseded, or the reason is blank. Nothing is written on error.
            The flag is stored as the node's top-level `scope_review` attribute
            ({suggested, reason, flagged_at, curation_session}) and only the
            constraint's owner can read it.
            Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity, or its profile is missing role/seniority/
            reports_to — the payload names what to ask the human for and the
            `cognition_set_identity` call that fixes it. Nothing is written.
            A valid token is checked FIRST, so a caller with no token is told that
            rather than told to onboard.
        """
        lc = get_lifespan(ctx)
        session, refusal = _require_curation(lc, curation_token)
        if refusal:
            return refusal
        assert session is not None
        storage: CognitionStorage = lc["cognition_storage"]
        gate, _ = _gated_identity(storage)
        if gate is not None:
            return gate
        result = _flag_constraint_scope(
            storage, node_id, suggested_scope, reason, session["session_id"],
        )
        if result.get("flagged"):
            session["writes"] += 1
        return result

    @dispatch_tool(mcp)
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
            NOT HEAD-filtered and carries no conflict/supersession summary flag
            (unlike cognition_search's `conflicted`/`superseded_by`, WP-SearchFlags):
            the raw edges in the result ARE the signal — a `supersedes`/`contradicts`
            entry here means exactly what its `edge_type` says, you read it directly
            rather than a precomputed marker on `node_id` itself.
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
                    "curation_session": edata.get("curation_session"),
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
                    "curation_session": edata.get("curation_session"),
                    "type": node_data.get("type") if node_data else None,
                    "summary": node_data.get("summary") if node_data else None,
                })
            result["incoming"] = incoming

        return result

    @dispatch_tool(mcp)
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
            Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity, or its profile is missing role/seniority/
            reports_to — the payload names what to ask the human for and the
            `cognition_set_identity` call that fixes it. Nothing is written.
        """
        storage: CognitionStorage = get_lifespan(ctx)["cognition_storage"]
        gate, _ = _gated_identity(storage)
        if gate is not None:
            return gate

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

    @dispatch_tool(mcp)
    def cognition_remove_node(
        ctx: Context,
        node_id: str,
    ) -> dict[str, Any]:
        """Delete a cognition node and ALL of its attached edges.

        DESTRUCTIVE and not undoable. Removing a node cascades to every edge
        incident to it (incoming and outgoing) and purges its embedding so it
        no longer appears in cognition_search. The deletion converges across
        concurrent sessions on the shared journal. Use this to prune junk,
        test, or duplicate nodes — for an outdated-but-real node, record the
        correction and run /vibe-curate to add a `supersedes` edge (the only
        path that writes one) over deleting the history.
        The acting author is resolved SERVER-SIDE from the confirmed identity for this checkout
        (same as cognition_add_task) and recorded in the journal tombstone.

        WARNING — parent tasks: deleting a task that has child tasks DETACHES
        the children; they keep reporting the deleted id as their `parent_id`
        (this stale-pointer behavior is deliberate, not a bug). Reparent or
        close children first if that matters.

        WARNING — person nodes: deleting a person node does NOT touch their
        env-fact file (.cognition/people/<slug>.jsonl) — the facts registry is
        graph-independent, so their stored environment facts persist and keep
        surfacing via cognition_list_env_facts as `registered: false` until
        THEY run cognition_clear_env_facts (self-only) or the file is removed
        from git by hand. Removing a person is the natural moment to prompt
        that cleanup.

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
            Refused with `{"identity_required": true, ...}` when this checkout has
            no CONFIRMED identity, or its profile is missing role/seniority/
            reports_to — the payload names what to ask the human for and the
            `cognition_set_identity` call that fixes it. Nothing is written.
        """
        lc = get_lifespan(ctx)
        storage: CognitionStorage = lc["cognition_storage"]
        embed_storage: VectorStore = lc["cognition_embedding_storage"]

        # Delete provenance (WP-1): server-resolved identity, same source as
        # cognition_add_task's creator (pure file reads, never raises).
        gate, removed_by = _gated_identity(storage)
        if gate is not None:
            return gate

        result = delete_cognition_node(storage, embed_storage, node_id, removed_by=removed_by)
        if result is None:
            return {"error": f"Node '{node_id}' does not exist"}

        return {"removed": True, **result}

    @dispatch_tool(mcp)
    def cognition_reload(ctx: Context) -> dict[str, Any]:
        """Force-reload the cognition graph from the on-disk journal files.

        The store auto-catches-up on every journal file before every operation,
        so concurrent sessions normally converge on their own. This tool is an
        explicit lever / diagnostic: it fully re-replays the legacy
        .cognition/journal.jsonl and every per-person file under
        .cognition/journal/, and reports node/edge counts before and after, so you can confirm the
        in-memory graph matches what's on disk (e.g. after another agent on the
        same project recorded nodes).

        Returns:
            {"nodes_before", "edges_before", "nodes_after", "edges_after"}
        """
        storage: CognitionStorage = get_lifespan(ctx)["cognition_storage"]
        return storage.reload()

    # ── Cross-project tools (XP1) ─────────────────────────────────

    @dispatch_tool(mcp)
    def cognition_load_project(ctx: Context, path: str) -> dict[str, Any]:
        """Load a foreign cognition project so all read tools can query it.

        Attaches the project at <path> to this session and runs the embedding-model
        guard. After loading, pass the returned `tag` (or the path) as the `project`
        arg on any read tool to route that call to the foreign project:
          - Structural reads (get_node, get_chain, get_neighbors, get_history, …)
          - Semantic search (cognition_search) — availability depends on model_guard

        The load is READ-ONLY: never writes to the foreign journal or vector store,
        never runs sync/backfill, and never creates a vector store if one is absent.

        Re-loading an already-loaded project (home or foreign) returns an error;
        unload it first with cognition_unload_project if you need to reload.

        model_guard values and what they mean for you:
          match         — embedding model confirmed compatible; semantic search enabled
          unknown       — unstamped store (no model metadata); search runs with a
                          degraded-confidence caveat in project_notes
          no-index      — no vector store found (index_reason says why: usually the
                          project has not been opened yet on a version that builds
                          this store); semantic search DISABLED for this project;
                          structural reads still work
          dim-mismatch  — vector dimensions differ from home; semantic DISABLED
          model-mismatch — different embedding model; semantic DISABLED

        For dim/model/no-index guard states, semantic search silently returns no hits
        for that project (project_notes[tag].semantic_unavailable explains why); this
        is not "no history" — it means the index is guarded off.

        Args:
            path: Absolute or relative path to the foreign project root (must contain
                  a .cognition/journal.jsonl or per-person journal files under
                  .cognition/journal/).

        Returns:
            On success: {tag, path, node_count, vector_count, model_guard, warning?,
            index_reason?, index_incomplete?, indexed_nodes?}
            vector_count is "n/a" when no vector index exists for the project.
            index_reason is present only when no usable index was found: either the
            project has never been opened on a version that builds this store (open a
            session in that project once to build it), or its store could not be
            opened (see the server log).
            index_incomplete (true) and indexed_nodes are present only when the index
            exists but is missing more than 2% of node_count (and more than one node):
            a session in that project is
            still building it, or stopped before finishing (it resumes the next time
            a session opens there). Its search results are partial until then, and
            cognition_search says so in project_notes[tag].

            On failure: {"error": "..."} — invalid path, path is already loaded
            as home, no journal file (.cognition/journal.jsonl or
            .cognition/journal/*.jsonl) found at path, or path is
            already loaded as a foreign project (in which case the error dict
            ALSO carries "tag": <existing tag> — the already-loaded project's
            tag, not confirmation of a new load; check for "error" first).
        """
        return _load_project_core(get_lifespan(ctx), path)

    @dispatch_tool(mcp)
    def cognition_unload_project(ctx: Context, project: str) -> dict[str, Any]:
        """Unload a previously loaded foreign project.

        Refuses to unload the home project (always pinned). Closes the foreign vector
        store's database handle so B's directory is unlocked on Windows.

        Args:
            project: Tag or path of the foreign project to unload.

        Returns:
            {unloaded: tag, path} or {error: ...}
        """
        return _unload_project_core(get_lifespan(ctx), project)

    @dispatch_tool(mcp)
    def cognition_list_projects(ctx: Context) -> dict[str, Any]:
        """List all loaded cognition projects (home + foreign).

        Use this to see which projects are available for the `project` arg on read
        tools, and to understand each project's semantic search availability.

        model_guard values per entry:
          match         — semantic search enabled for this project
          unknown       — pre-stamp; search runs with degraded-confidence caveat
          no-index      — no vector store; semantic DISABLED, structural only
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
