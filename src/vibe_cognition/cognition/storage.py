"""JSONL-backed graph storage for the Cognition History Graph."""

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx

from .models import CognitionEdge, CognitionEdgeType, CognitionNode, CognitionNodeType

logger = logging.getLogger(__name__)

# Minimum prefix length for commit SHA short-form matching
_COMMIT_SHORT_PREFIX_LEN = 7

JOURNAL_FILENAME = "journal.jsonl"


class CognitionStorage:
    """Cognition graph storage: JSONL source of truth + NetworkX in-memory graph.

    Writes append to JSONL immediately. The NetworkX graph is hydrated from
    JSONL at startup and updated in-place on every write.
    """

    def __init__(self, cognition_dir: Path):
        """Initialize storage, hydrating from JSONL if it exists.

        Args:
            cognition_dir: Directory for .cognition/ files (Git-committed)
        """
        self._dir = cognition_dir
        self._journal_path = cognition_dir / JOURNAL_FILENAME
        self._graph = nx.MultiDiGraph()
        self._reference_index: dict[str, list[str]] = defaultdict(list)

        self._dir.mkdir(parents=True, exist_ok=True)

        if self._journal_path.exists():
            self._hydrate()

    @property
    def graph(self) -> nx.MultiDiGraph:
        """Access the underlying NetworkX graph."""
        return self._graph

    # ── Write operations ──────────────────────────────────────────────

    def add_node(self, node: CognitionNode) -> None:
        """Add a cognition node to the graph and journal.

        Args:
            node: The cognition node to add
        """
        self._graph.add_node(
            node.id,
            type=node.type.value,
            summary=node.summary,
            detail=node.detail,
            context=node.context,
            references=node.references,
            severity=node.severity,
            timestamp=node.timestamp,
            author=node.author,
        )
        self._index_node_refs(node.id, node.references)
        self._append_journal("add_node", node.model_dump(mode="json"))

    def add_edge(self, edge: CognitionEdge) -> bool:
        """Add an edge between two existing nodes.

        Uses edge_type as the MultiDiGraph key, so the same (from, to, type)
        triple is idempotent (overwrites), while different types between
        the same pair create separate edges.

        Args:
            edge: The cognition edge to add

        Returns:
            True if both nodes exist and the edge was added
        """
        if edge.from_id not in self._graph or edge.to_id not in self._graph:
            logger.warning(
                f"Cannot add edge: node(s) missing "
                f"(from={edge.from_id}, to={edge.to_id})"
            )
            return False

        self._graph.add_edge(
            edge.from_id,
            edge.to_id,
            key=edge.edge_type.value,
            type=edge.edge_type.value,
            timestamp=edge.timestamp,
            source=edge.source,
        )
        self._append_journal("add_edge", edge.model_dump(mode="json"))
        return True

    def update_node(self, node_id: str, **kwargs: Any) -> bool:
        """Update fields on an existing node.

        Args:
            node_id: ID of the node to update
            **kwargs: Fields to update (summary, detail, context, etc.)

        Returns:
            True if the node exists and was updated
        """
        if node_id not in self._graph:
            return False

        for key, value in kwargs.items():
            self._graph.nodes[node_id][key] = value

        data = {"id": node_id, **kwargs}
        self._append_journal("update_node", data)
        return True

    def remove_node(self, node_id: str) -> bool:
        """Remove a node and all its edges from the graph.

        Args:
            node_id: ID of the node to remove

        Returns:
            True if the node existed and was removed
        """
        if node_id not in self._graph:
            return False

        self._unindex_node_refs(node_id)
        self._graph.remove_node(node_id)
        self._append_journal("remove_node", {"id": node_id})
        return True

    def remove_edge(
        self,
        from_id: str,
        to_id: str,
        edge_type: CognitionEdgeType | None = None,
    ) -> bool:
        """Remove an edge between two nodes.

        With edge_type specified, removes only that edge type (key-based
        removal in MultiDiGraph). Without edge_type, removes ALL edges
        between the pair.

        Args:
            from_id: Source node ID
            to_id: Target node ID
            edge_type: Specific edge type to remove, or None for all

        Returns:
            True if at least one edge was removed
        """
        if not self._graph.has_edge(from_id, to_id):
            return False

        if edge_type is not None:
            key = edge_type.value
            if key not in self._graph[from_id][to_id]:
                return False
            self._graph.remove_edge(from_id, to_id, key=key)
            self._append_journal("remove_edge", {
                "from_id": from_id,
                "to_id": to_id,
                "edge_type": edge_type.value,
            })
            return True
        else:
            # Remove all edges between the pair
            keys = list(self._graph[from_id][to_id].keys())
            for key in keys:
                self._graph.remove_edge(from_id, to_id, key=key)
                self._append_journal("remove_edge", {
                    "from_id": from_id,
                    "to_id": to_id,
                    "edge_type": key,
                })
            return len(keys) > 0

    def redirect_edges(self, old_node_id: str, new_node_id: str) -> int:
        """Redirect all edges from/to old_node_id to point to/from new_node_id.

        Args:
            old_node_id: The node being replaced
            new_node_id: The node that takes over

        Returns:
            Number of edges redirected
        """
        if old_node_id not in self._graph or new_node_id not in self._graph:
            return 0

        redirected = 0

        # Redirect outgoing edges
        for _, target_id, key, edge_data in list(
            self._graph.out_edges(old_node_id, data=True, keys=True)
        ):
            if target_id != new_node_id:  # Avoid self-loops
                edge_type = edge_data.get("type", key)
                self._graph.add_edge(
                    new_node_id, target_id, key=edge_type, **edge_data
                )
                self._append_journal("add_edge", {
                    "from_id": new_node_id, "to_id": target_id,
                    "edge_type": edge_type,
                    "timestamp": edge_data.get("timestamp", ""),
                    "source": edge_data.get("source", "curator"),
                })
                redirected += 1

        # Redirect incoming edges
        for source_id, _, key, edge_data in list(
            self._graph.in_edges(old_node_id, data=True, keys=True)
        ):
            if source_id != new_node_id:  # Avoid self-loops
                edge_type = edge_data.get("type", key)
                self._graph.add_edge(
                    source_id, new_node_id, key=edge_type, **edge_data
                )
                self._append_journal("add_edge", {
                    "from_id": source_id, "to_id": new_node_id,
                    "edge_type": edge_type,
                    "timestamp": edge_data.get("timestamp", ""),
                    "source": edge_data.get("source", "curator"),
                })
                redirected += 1

        return redirected

    # ── Read operations ───────────────────────────────────────────────

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Get a node by its ID.

        Args:
            node_id: ID of the node

        Returns:
            Node data dict or None if not found
        """
        if node_id in self._graph:
            return dict(self._graph.nodes[node_id])
        return None

    def has_node(self, node_id: str) -> bool:
        """Check if a node exists."""
        return node_id in self._graph

    def get_all_nodes(self) -> list[dict[str, Any]]:
        """Get all nodes in the graph.

        Returns:
            List of node data dicts with 'id' included
        """
        return [
            {"id": node_id, **data}
            for node_id, data in self._graph.nodes(data=True)
        ]

    def get_nodes_by_type(self, node_type: CognitionNodeType) -> list[dict[str, Any]]:
        """Get all nodes of a specific type.

        Args:
            node_type: Type to filter by

        Returns:
            List of matching node data dicts
        """
        return [
            {"id": node_id, **data}
            for node_id, data in self._graph.nodes(data=True)
            if data.get("type") == node_type.value
        ]

    def get_recent_nodes(
        self,
        limit: int = 10,
        node_type: CognitionNodeType | None = None,
    ) -> list[dict[str, Any]]:
        """Get the most recent nodes, optionally filtered by type.

        Args:
            limit: Maximum number of nodes to return
            node_type: Optional type filter

        Returns:
            List of node data dicts sorted by timestamp descending
        """
        nodes = []
        for node_id, data in self._graph.nodes(data=True):
            if node_type and data.get("type") != node_type.value:
                continue
            nodes.append({"id": node_id, **data})

        nodes.sort(key=lambda n: n.get("timestamp", ""), reverse=True)
        return nodes[:limit]

    def get_successors(
        self,
        node_id: str,
        edge_type: CognitionEdgeType | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Get all nodes that this node points to.

        Args:
            node_id: Source node ID
            edge_type: Optional edge type filter

        Returns:
            List of (target_id, edge_data) tuples
        """
        if node_id not in self._graph:
            return []

        result = []
        for _, target_id, edge_data in self._graph.out_edges(node_id, data=True):
            if edge_type is None or edge_data.get("type") == edge_type.value:
                result.append((target_id, edge_data))
        return result

    def get_predecessors(
        self,
        node_id: str,
        edge_type: CognitionEdgeType | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Get all nodes that point to this node.

        Args:
            node_id: Target node ID
            edge_type: Optional edge type filter

        Returns:
            List of (source_id, edge_data) tuples
        """
        if node_id not in self._graph:
            return []

        result = []
        for source_id, _, edge_data in self._graph.in_edges(node_id, data=True):
            if edge_type is None or edge_data.get("type") == edge_type.value:
                result.append((source_id, edge_data))
        return result

    def get_statistics(self) -> dict[str, int]:
        """Get graph statistics.

        Returns:
            Dictionary with node/edge counts by type
        """
        stats: dict[str, int] = {
            "nodes": self._graph.number_of_nodes(),
            "edges": self._graph.number_of_edges(),
        }
        for node_type in CognitionNodeType:
            stats[node_type.value] = 0

        for _, data in self._graph.nodes(data=True):
            t = data.get("type", "")
            if t in stats:
                stats[t] += 1

        # Edge counts by type
        for edge_type in CognitionEdgeType:
            stats[f"edge_{edge_type.value}"] = 0
        for _, _, edge_data in self._graph.edges(data=True):
            et = edge_data.get("type", "")
            key = f"edge_{et}"
            if key in stats:
                stats[key] += 1

        return stats

    # ── Reference index ────────────────────────────────────────────────

    @staticmethod
    def _normalize_refs(references: list[str]) -> list[str]:
        """Normalize reference strings for index matching.

        Returns a list of normalized keys for each reference.
        For commit refs, also produces a short-SHA prefix key.
        """
        keys: list[str] = []
        for ref in references:
            normed = ref.strip().lower()
            if not normed:
                continue
            keys.append(normed)
            # For commit refs, also index the short prefix
            if normed.startswith("commit:"):
                sha = normed.split(":", 1)[1]
                if len(sha) > _COMMIT_SHORT_PREFIX_LEN:
                    keys.append(f"commit:{sha[:_COMMIT_SHORT_PREFIX_LEN]}")
        return keys

    def _index_node_refs(self, node_id: str, references: list[str]) -> None:
        """Add a node's references to the reference index."""
        for key in self._normalize_refs(references):
            if node_id not in self._reference_index[key]:
                self._reference_index[key].append(node_id)

    def _unindex_node_refs(self, node_id: str) -> None:
        """Remove a node from all reference index entries."""
        empty_keys = []
        for key, node_ids in self._reference_index.items():
            if node_id in node_ids:
                node_ids.remove(node_id)
                if not node_ids:
                    empty_keys.append(key)
        for key in empty_keys:
            del self._reference_index[key]

    def create_deterministic_edges(self, node_id: str) -> int:
        """Create part_of edges by matching shared references.

        Bidirectional: if the new node is an entity and matches an existing
        episode (or vice versa), creates entity -> episode part_of edges.

        Args:
            node_id: ID of the node to match

        Returns:
            Number of edges created
        """
        node_data = self.get_node(node_id)
        if not node_data:
            return 0

        refs = node_data.get("references", [])
        if not refs:
            return 0

        node_type = node_data.get("type", "")
        is_episode = node_type == CognitionNodeType.EPISODE.value

        created = 0
        seen_pairs: set[tuple[str, str]] = set()

        for key in self._normalize_refs(refs):
            for other_id in self._reference_index.get(key, []):
                if other_id == node_id:
                    continue

                other_data = self.get_node(other_id)
                if not other_data:
                    continue

                other_type = other_data.get("type", "")
                other_is_episode = other_type == CognitionNodeType.EPISODE.value

                # One must be episode, other must be entity
                if is_episode == other_is_episode:
                    continue

                # Direction: entity -> episode
                if is_episode:
                    from_id, to_id = other_id, node_id
                else:
                    from_id, to_id = node_id, other_id

                pair = (from_id, to_id)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                # Skip if a part_of edge already exists between this pair
                if (self._graph.has_edge(from_id, to_id) and
                        CognitionEdgeType.PART_OF.value in self._graph[from_id][to_id]):
                    continue

                timestamp = datetime.now(timezone.utc).isoformat()
                edge = CognitionEdge(
                    from_id=from_id,
                    to_id=to_id,
                    edge_type=CognitionEdgeType.PART_OF,
                    timestamp=timestamp,
                    source="deterministic",
                )
                self.add_edge(edge)
                created += 1

        if created:
            logger.info(
                f"Deterministic matching: created {created} part_of edge(s) "
                f"for node {node_id}"
            )
        return created

    # ── Internal ──────────────────────────────────────────────────────

    def _append_journal(self, action: str, data: dict[str, Any]) -> None:
        """Append a single JSON line to the journal file.

        Args:
            action: The action type (add_node, add_edge, update_node)
            data: The action payload
        """
        line = json.dumps({"action": action, "data": data}, ensure_ascii=False)
        with open(self._journal_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _hydrate(self) -> None:
        """Replay the JSONL journal to rebuild the in-memory graph."""
        count = 0
        with open(self._journal_path, encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    self._replay_entry(entry)
                    count += 1
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.warning(f"Skipping malformed journal line {line_num}: {e}")

        logger.info(f"Cognition graph hydrated: {count} entries, "
                     f"{self._graph.number_of_nodes()} nodes, "
                     f"{self._graph.number_of_edges()} edges")

    def _replay_entry(self, entry: dict[str, Any]) -> None:
        """Replay a single journal entry into the graph (no journal write).

        Args:
            entry: Journal entry with 'action' and 'data' keys
        """
        action = entry["action"]
        data = entry["data"]

        if action == "add_node":
            node_id = data["id"]
            references = data.get("references", [])
            self._graph.add_node(
                node_id,
                type=data["type"],
                summary=data["summary"],
                detail=data["detail"],
                context=data.get("context", []),
                references=references,
                severity=data.get("severity"),
                timestamp=data["timestamp"],
                author=data["author"],
            )
            self._index_node_refs(node_id, references)
        elif action == "add_edge":
            from_id = data["from_id"]
            to_id = data["to_id"]
            edge_type = data["edge_type"]
            if from_id in self._graph and to_id in self._graph:
                self._graph.add_edge(
                    from_id,
                    to_id,
                    key=edge_type,
                    type=edge_type,
                    timestamp=data.get("timestamp", ""),
                    source=data.get("source", "curator"),
                )
        elif action == "remove_edge":
            from_id = data["from_id"]
            to_id = data["to_id"]
            edge_type = data.get("edge_type")
            if self._graph.has_edge(from_id, to_id):
                if edge_type and edge_type in self._graph[from_id][to_id]:
                    self._graph.remove_edge(from_id, to_id, key=edge_type)
        elif action == "remove_node":
            node_id = data["id"]
            if node_id in self._graph:
                self._unindex_node_refs(node_id)
                self._graph.remove_node(node_id)
        elif action == "update_node":
            node_id = data["id"]
            if node_id in self._graph:
                for key, value in data.items():
                    if key != "id":
                        self._graph.nodes[node_id][key] = value
