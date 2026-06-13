"""JSONL-backed graph storage for the Cognition History Graph."""

import hashlib
import json
import logging
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import networkx as nx

from .journal_io import append_journal_line
from .models import CognitionEdge, CognitionEdgeType, CognitionNode, CognitionNodeType

logger = logging.getLogger(__name__)

# Minimum prefix length for commit SHA short-form matching
_COMMIT_SHORT_PREFIX_LEN = 7

JOURNAL_FILENAME = "journal.jsonl"


class CognitionStorage:
    """Cognition graph storage: JSONL source of truth + NetworkX in-memory graph.

    Writes append to JSONL immediately. The NetworkX graph is hydrated from
    JSONL at startup and updated in-place on every write.

    The journal is the shared source of truth. Multiple server processes (one
    per Claude session) may share a single project journal, so before every
    public operation the store *catches up* — replaying any journal lines
    appended since it last read (by this or any other process). This keeps
    concurrent sessions converged without a restart or a background watcher.
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
        self._lock = threading.RLock()
        # Byte offset into the journal up to which we've replayed. Only ever
        # advanced past complete, newline-terminated lines (see _catch_up).
        self._offset = 0
        # Journal identity (C-3): a running sha256 of every byte we've replayed
        # (i.e. of journal bytes[0:offset]) plus the last-seen mtime. Before
        # replaying from the stored offset after the file changes, we re-hash the
        # on-disk prefix and compare: if it differs, the journal was REPLACED or
        # divergently MERGED under us (git pull/merge — which preserves line 1, so
        # a first-line check would miss it) and we re-hydrate from the top rather
        # than replay from a now-meaningless offset.
        self._journal_hasher = hashlib.sha256()
        self._journal_mtime_ns: int | None = None
        # Re-entrancy depth for _synced(): catch-up runs once per outermost op.
        self._sync_depth = 0

        self._dir.mkdir(parents=True, exist_ok=True)

        # Initial hydrate is just a catch-up from offset 0.
        self._catch_up()

    @property
    def graph(self) -> nx.MultiDiGraph:
        """Access the underlying NetworkX graph.

        NOTE: this is an UNSYNCED view — it does not trigger journal catch-up.
        Prefer the public synced methods (or ``snapshot()``) when correctness
        across concurrent processes matters.
        """
        return self._graph

    @property
    def cognition_dir(self) -> Path:
        """The .cognition/ directory backing this store (for sidecar/blob paths)."""
        return self._dir

    def find_nodes_by_ref(self, ref: str) -> list[str]:
        """Node IDs whose (normalized) references include ``ref`` — O(1) lookup via
        the reference index. Used for dedup-by-doc-ref. Synced so cross-process
        writes are visible.
        """
        with self._synced():
            out: list[str] = []
            for key in self._normalize_refs([ref]):
                for nid in self._reference_index.get(key, []):
                    if nid not in out:
                        out.append(nid)
            return out

    @contextmanager
    def _synced(self):
        """Acquire the lock and catch up on the journal before the operation.

        Re-entrant: the RLock allows nested public calls (e.g.
        ``create_deterministic_edges`` -> ``add_edge``), and the depth counter
        ensures the journal catch-up runs only for the outermost call, not on
        every inner write.
        """
        with self._lock:
            if self._sync_depth == 0:
                self._catch_up()
            self._sync_depth += 1
            try:
                yield
            finally:
                self._sync_depth -= 1

    # ── Write operations ──────────────────────────────────────────────

    def add_node(self, node: CognitionNode) -> None:
        """Add a cognition node to the graph and journal.

        Args:
            node: The cognition node to add
        """
        with self._synced():
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
                metadata=node.metadata,
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
        with self._synced():
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
        with self._synced():
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
        with self._synced():
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
        with self._synced():
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
        with self._synced():
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
        with self._synced():
            if node_id in self._graph:
                return dict(self._graph.nodes[node_id])
            return None

    def has_node(self, node_id: str) -> bool:
        """Check if a node exists."""
        with self._synced():
            return node_id in self._graph

    def get_all_nodes(self) -> list[dict[str, Any]]:
        """Get all nodes in the graph.

        Returns:
            List of node data dicts with 'id' included
        """
        with self._synced():
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
        with self._synced():
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
        with self._synced():
            nodes = []
            for node_id, data in self._graph.nodes(data=True):
                if node_type and data.get("type") != node_type.value:
                    continue
                nodes.append({"id": node_id, **data})

        nodes.sort(key=lambda n: n.get("timestamp", ""), reverse=True)
        return nodes[:limit]

    def get_uncurated_nodes(
        self,
        limit: int = 50,
        node_type: CognitionNodeType | None = None,
    ) -> list[dict[str, Any]]:
        """Get nodes not yet reviewed by the curate skill.

        A node is "uncurated" if it lacks a ``curated_by_skill_at`` attribute.
        Nodes with only deterministic (or legacy) edges are still considered
        uncurated until the curate skill explicitly marks them.

        Args:
            limit: Maximum number of nodes to return (max 500)
            node_type: Optional type filter

        Returns:
            List of uncurated node dicts, sorted oldest-first by timestamp
        """
        with self._synced():
            uncurated = []
            for node_id, data in self._graph.nodes(data=True):
                if node_type and data.get("type") != node_type.value:
                    continue
                if data.get("curated_by_skill_at") is not None:
                    continue
                uncurated.append({"id": node_id, **data})

        uncurated.sort(key=lambda n: n.get("timestamp", ""))
        return uncurated[:min(limit, 500)]

    def mark_curated_by_skill(self, node_id: str) -> bool:
        """Mark a node as reviewed by the curate skill.

        Set regardless of whether edges were created, so nodes with no
        meaningful relationships are not re-processed on subsequent runs.

        Args:
            node_id: ID of the node to mark

        Returns:
            True if the node exists and was marked
        """
        timestamp = datetime.now(UTC).isoformat()
        return self.update_node(node_id, curated_by_skill_at=timestamp)

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
        with self._synced():
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
        with self._synced():
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
        with self._synced():
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

            stats["uncurated"] = sum(
                1 for _, data in self._graph.nodes(data=True)
                if data.get("curated_by_skill_at") is None
            )

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
        with self._synced():
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

                    # D1a: documents are graph-inert until D1b adds the real pair
                    # rules. Skip ANY pair involving a document — the wrong edge
                    # would otherwise fire from the OTHER node's record call (e.g.
                    # an episode citing doc:<hash> makes this matcher treat the
                    # document as an entity and mint a part_of). Pair-level guard,
                    # not a guard on the recorded node, because either side may be
                    # the document. D1b replaces this with entity↔doc / doc↔episode.
                    if CognitionNodeType.DOCUMENT.value in (node_type, other_type):
                        continue

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

                    timestamp = datetime.now(UTC).isoformat()
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
        append_journal_line(self._journal_path, line)

    def _rehydrate_reset(self) -> None:
        """Wipe in-memory state for a full re-hydrate from the top of the journal."""
        self._graph = nx.MultiDiGraph()
        self._reference_index = defaultdict(list)
        self._offset = 0
        self._journal_hasher = hashlib.sha256()

    def _catch_up(self) -> int:
        """Replay journal lines appended since we last read; return entry count.

        Caller MUST hold ``self._lock`` (``_synced`` and ``reload`` do). This is
        the single mechanism that keeps a running process converged with writes
        made by other processes sharing the same journal.

        Safety:
          - Reads in BINARY so the byte offset matches ``stat().st_size`` exactly
            (the journal is written CRLF on Windows; binary read + ``splitlines()``
            handles ``\\r\\n`` and keeps byte accounting consistent).
          - Advances the offset ONLY past complete, newline-terminated lines. A
            concurrent writer's half-written final line leaves the offset before
            it; we re-read it next pass once complete — never losing the entry.
          - C-3 — REPLACEMENT / divergent MERGE detection. We keep a running hash
            of every byte we've replayed (journal ``bytes[0:offset]``). When the
            file changes, we re-hash the on-disk prefix and compare before
            replaying; a mismatch means the journal was replaced or divergently
            merged under our offset, so we re-hydrate from the top. A first-line-
            only check would MISS the real case — a git pull/merge preserves line
            1 (append-only journal, shared first line) — so only the full-prefix
            check catches a divergent merge that left our offset pointing into
            freshly-inserted remote content. Cost: one O(offset) read+hash, and
            ONLY when the file actually changed (the cheap ``size==offset & mtime``
            path skips it) — sub-millisecond at journal scale (KB–low-MB).
            Residual: a replacement that coincidentally matches BOTH size and
            ``st_mtime_ns`` evades the cheap path (vanishing at ns granularity).

        Rebuild-vs-append safety (INVARIANT — do not "tighten" by assuming a
        lock): this read is NOT under the cross-process append lock (that lock,
        in journal_io, serializes APPENDS only). A rebuild reading while another
        process appends at EOF is safe purely because of torn-tail parking +
        idempotent replay + the per-process prefix check — never mutual exclusion.
        Convergence after a replacement relies on EVERY live process independently
        detecting it; correct only because replay is idempotent.
        """
        try:
            st = self._journal_path.stat()
        except FileNotFoundError:
            return 0
        size = st.st_size
        mtime = st.st_mtime_ns

        # Cheap path: size AND mtime unchanged → nothing happened (one stat, no
        # read). mtime also catches an equal-byte-size replacement.
        if size == self._offset and mtime == self._journal_mtime_ns:
            return 0
        self._journal_mtime_ns = mtime

        with open(self._journal_path, "rb") as f:
            data = f.read()

        rehydrate = False
        if size < self._offset:
            rehydrate = True  # shrank: truncated / rotated / reset
        elif self._offset == 0 and self._graph.number_of_nodes() > 0:
            # Reading from the TOP with a non-empty graph = a re-hydrate: the
            # journal was replaced before this store advanced its offset past its
            # own first appends (appends don't move the offset — see C-6).
            rehydrate = True
        elif (
            self._offset > 0
            # C-3: the replayed prefix must still be byte-identical.
            and hashlib.sha256(data[: self._offset]).digest() != self._journal_hasher.digest()
        ):
            rehydrate = True

        if rehydrate:
            logger.info("Journal changed under our replay offset; re-hydrating from top")
            self._rehydrate_reset()

        raw = data[self._offset :]
        last_nl = raw.rfind(b"\n")
        if last_nl == -1:
            return 0  # no complete line yet — do not advance past a torn append

        complete = raw[: last_nl + 1]
        self._offset += len(complete)
        self._journal_hasher.update(complete)

        count = 0
        for line in complete.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self._replay_entry(json.loads(line))
                count += 1
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning(f"Skipping malformed journal line: {e}")

        if count:
            logger.info(
                f"Cognition graph caught up: +{count} entries, "
                f"{self._graph.number_of_nodes()} nodes, "
                f"{self._graph.number_of_edges()} edges"
            )
        return count

    def reload(self) -> dict[str, int]:
        """Force a full re-hydrate from the journal; return before/after stats.

        Auto catch-up makes this unnecessary for correctness, but it's an
        explicit lever (and a "am I converged?" diagnostic) exposed via the
        ``cognition_reload`` MCP tool.
        """
        with self._lock:
            before = {
                "nodes": self._graph.number_of_nodes(),
                "edges": self._graph.number_of_edges(),
            }
            self._rehydrate_reset()
            self._journal_mtime_ns = None
            self._catch_up()
            after = {
                "nodes": self._graph.number_of_nodes(),
                "edges": self._graph.number_of_edges(),
            }
            return {
                "nodes_before": before["nodes"],
                "edges_before": before["edges"],
                "nodes_after": after["nodes"],
                "edges_after": after["edges"],
            }

    def snapshot(self) -> dict[str, Any]:
        """Return a synced, point-in-time copy of nodes and edges.

        Catches up on the journal first (via ``_synced``), then returns plain
        lists so callers (e.g. the dashboard) never iterate the live graph
        unlocked. Edges are ``(from_id, to_id, type, data)`` tuples.
        """
        with self._synced():
            nodes = [
                {"id": node_id, **data}
                for node_id, data in self._graph.nodes(data=True)
            ]
            edges = [
                (u, v, key, dict(data))
                for u, v, key, data in self._graph.edges(keys=True, data=True)
            ]
            return {"nodes": nodes, "edges": edges}

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
                metadata=data.get("metadata", {}),
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
                    # Historical provenance tag, NOT an active curator. Old journals
                    # contain many edges sourced "curator"; the background curator
                    # feature was removed, but the stored tag is left intact.
                    source=data.get("source", "curator"),
                )
        elif action == "remove_edge":
            from_id = data["from_id"]
            to_id = data["to_id"]
            edge_type = data.get("edge_type")
            if (
                self._graph.has_edge(from_id, to_id)
                and edge_type
                and edge_type in self._graph[from_id][to_id]
            ):
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
