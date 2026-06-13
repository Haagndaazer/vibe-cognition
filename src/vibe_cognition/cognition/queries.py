"""Query functions for traversing the cognition history graph."""

import logging
from typing import Any

from .models import CognitionEdgeType, CognitionNodeType
from .storage import CognitionStorage

logger = logging.getLogger(__name__)


def get_reasoning_chain(
    storage: CognitionStorage,
    node_id: str,
    max_depth: int = 5,
    direction: str = "outgoing",
) -> dict[str, Any]:
    """Get the reasoning chain from/to a node via LED_TO edges.

    Args:
        storage: Cognition storage instance
        node_id: ID of the starting node
        max_depth: Maximum depth to traverse
        direction: "outgoing" (what it led to) or "incoming" (what led to it)

    Returns:
        Nested dictionary representing the reasoning tree
    """
    def traverse(nid: str, depth: int, path: set[str]) -> dict[str, Any]:
        # C-7: cycle detection is PATH-based — `path` is the set of ancestors on the
        # current root→node path, not a traversal-global visited set. A node is a
        # cycle only if it is one of its own ancestors (`nid in path`). A global
        # visited set (never popped) wrongly flagged a re-convergent DAG node (a
        # diamond A→B→D, A→C→D) as a cycle on the second arm. Each recursion passes a
        # fresh `path | {nid}`, so siblings don't see each other's nodes and no pop is
        # needed. Trade-off: a re-convergent node is now fully re-expanded once per
        # path — worst case O(b^max_depth) tree nodes (b = avg LED_TO out-degree),
        # bounded by max_depth (default 5; the tool exposes it to callers).
        node_data = storage.get_node(nid)
        result: dict[str, Any] = {
            "id": nid,
            "type": node_data.get("type") if node_data else "unknown",
            "summary": node_data.get("summary", "") if node_data else "",
            "severity": node_data.get("severity") if node_data else None,
            "timestamp": node_data.get("timestamp", "") if node_data else "",
        }

        if depth > max_depth or nid in path:
            result["truncated"] = depth > max_depth
            result["cycle"] = nid in path
            result["chain"] = []
            return result

        result["truncated"] = False
        result["cycle"] = False
        next_path = path | {nid}

        chain = []
        if direction == "outgoing":
            for target_id, _ in storage.get_successors(nid, CognitionEdgeType.LED_TO):
                chain.append(traverse(target_id, depth + 1, next_path))
        else:
            for source_id, _ in storage.get_predecessors(nid, CognitionEdgeType.LED_TO):
                chain.append(traverse(source_id, depth + 1, next_path))

        result["chain"] = chain
        return result

    return traverse(node_id, 0, set())


def get_superseded_chain(
    storage: CognitionStorage,
    node_id: str,
) -> list[dict[str, Any]]:
    """Follow SUPERSEDES edges to get the full version history of a decision.

    Returns the chain from newest to oldest.

    Args:
        storage: Cognition storage instance
        node_id: ID of the starting node

    Returns:
        List of nodes in the supersedes chain (newest first)
    """
    chain = []
    visited: set[str] = set()
    current_id = node_id

    while current_id and current_id not in visited:
        visited.add(current_id)
        node_data = storage.get_node(current_id)
        if not node_data:
            break

        chain.append({"id": current_id, **node_data})

        # Follow SUPERSEDES edge to the node this one replaced
        successors = storage.get_successors(current_id, CognitionEdgeType.SUPERSEDES)
        if not successors:
            current_id = None
        else:
            if len(successors) > 1:
                logger.warning(
                    f"Node {current_id} has {len(successors)} SUPERSEDES successors "
                    f"(expected 1) — following first match"
                )
            current_id = successors[0][0]

    return chain


def get_history_for_context(
    storage: CognitionStorage,
    context_term: str,
    node_type: CognitionNodeType | None = None,
) -> list[dict[str, Any]]:
    """Get all cognition nodes whose context field matches a term.

    Args:
        storage: Cognition storage instance
        context_term: Term to search for in context fields (case-insensitive substring)
        node_type: Optional type filter

    Returns:
        List of matching nodes sorted by timestamp descending
    """
    term_lower = context_term.lower()
    results = []

    # get_all_nodes() is synced (catches up on the journal under the lock), so
    # this reflects writes from other processes — unlike a raw storage.graph read.
    for data in storage.get_all_nodes():
        if node_type and data.get("type") != node_type.value:
            continue

        context_list = data.get("context", [])
        if any(term_lower in c.lower() for c in context_list):
            results.append(data)

    results.sort(key=lambda n: n.get("timestamp", ""), reverse=True)
    return results


def get_incident_resolution(
    storage: CognitionStorage,
    node_id: str,
) -> dict[str, Any]:
    """Get an incident and everything that resolved it.

    Follows RESOLVED_BY edges from the incident node to find all fixes,
    and includes related discoveries.

    Args:
        storage: Cognition storage instance
        node_id: ID of the incident node

    Returns:
        Incident details with resolutions and related discoveries
    """
    node_data = storage.get_node(node_id)
    if not node_data:
        return {"error": f"Node not found: {node_id}"}

    result: dict[str, Any] = {
        "id": node_id,
        **node_data,
        "resolutions": [],
        "discoveries": [],
        "contradictions": [],
    }

    # Get all nodes this incident is connected to
    for target_id, edge_data in storage.get_successors(node_id):
        target_node = storage.get_node(target_id)
        if not target_node:
            continue

        edge_type = edge_data.get("type")
        entry = {"id": target_id, **target_node}

        if edge_type == CognitionEdgeType.RESOLVED_BY.value:
            result["resolutions"].append(entry)
        elif edge_type == CognitionEdgeType.LED_TO.value:
            # Every LED_TO target is a follow-on the incident produced. Both arms of
            # the former DISCOVERY if/else appended here identically (the type check
            # was inert) — collapsed to one append. Filtering to DISCOVERY-only would
            # be a behavior change with nowhere for non-discovery led_to targets to go.
            result["discoveries"].append(entry)

    # Check for contradictions (incoming CONTRADICTS edges)
    for source_id, edge_data in storage.get_predecessors(node_id):
        if edge_data.get("type") == CognitionEdgeType.CONTRADICTS.value:
            source_node = storage.get_node(source_id)
            if source_node:
                result["contradictions"].append({"id": source_id, **source_node})

    return result
