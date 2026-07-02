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

from .documents import doc_ref
from .git_hygiene import ensure_git_hygiene
from .journal_io import append_journal_line
from .models import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    generate_node_id,
)

logger = logging.getLogger(__name__)

# Minimum prefix length for commit SHA short-form matching
_COMMIT_SHORT_PREFIX_LEN = 7

JOURNAL_FILENAME = "journal.jsonl"

# Sidecar flag written on a LOSSY rehydrate-reset (nodes vanished from memory) so
# the next session-start prime — a separate process — can surface the loss. Consumed
# (deleted) by prime.py after it is shown once. Git-ignored via git_hygiene.py.
REHYDRATE_FLAG_FILENAME = ".last-rehydrate.json"


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
        # Loss visibility (WP-1): process-lifetime record of rehydrate resets —
        # a shrunk/replaced journal silently discarding in-memory state is the
        # exact failure this product exists to prevent, so every reset is counted
        # and the last one kept for get_status to surface.
        self.rehydrate_count = 0
        self.last_rehydrate: dict[str, Any] | None = None
        # Embedding drift closure (WP-3, 8606d59905a5): node ids added via
        # journal REPLAY (this or another process's write, discovered through
        # catch-up/rehydrate) since the last pop_replayed_node_ids() call.
        # storage.py has no embeddings dependency (by design — see that
        # method's docstring), so this is just a handoff queue; the tools
        # layer (which HAS both storage and embeddings) drains it and embeds
        # via the shared _embed_entity_node/_embed_workflow paths.
        self._replayed_node_ids: set[str] = set()
        # WP-5 gate redirect (d6cd1495b23a): node ids this process has seen
        # legitimately removed (via its own live remove_node call, or an
        # already-APPLIED replay of someone else's). Distinguishes "target
        # absent because it was validly deleted and we already know it" from
        # "target absent because it hasn't been replayed yet" in
        # _replay_entry's remove_node branch — without this, a process's own
        # remove_node tombstone read back on its NEXT catch-up (C-6: appends
        # don't advance the offset, so a process re-reads its own just-
        # appended lines) always found the target already gone and defer-
        # then-warned on every ordinary deletion. Never drained: legitimate
        # deletions are rare relative to adds, so this stays small in
        # practice — same trade as _replayed_node_ids being an unbounded-but-
        # naturally-small handoff set.
        self._removed_node_ids: set[str] = set()

        self._dir.mkdir(parents=True, exist_ok=True)

        try:
            ensure_git_hygiene(cognition_dir.parent, cognition_dir)
        except Exception as exc:  # noqa: BLE001
            logger.debug("git-hygiene: unexpected error (swallowed): %s", exc)

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

    def search_hit_is_live(self, raw_id: str) -> bool:
        """True if a search hit's node is still in the graph — the N1 drop predicate.

        Strips a ``#chunk-N`` suffix to the node id, then checks ``has_node``. THE
        single "is this search hit's node still live?" expression: BOTH search
        surfaces (the MCP ``cognition_search`` formatter and the dashboard search)
        call it, so the chunk-id format and the cross-process-ghost drop live in ONE
        place and can't drift (ledger 11, same discipline as ``documents_with_sha``).
        """
        return self.has_node(raw_id.split("#chunk-")[0])

    def documents_with_sha(self, sha: str) -> list[str]:
        """Node IDs of DOCUMENT nodes whose content ``sha256 == sha``.

        THE single document-identity predicate. dedup (store), sidecar reclaim and
        blob reclaim (delete), AND their guarding tests all call this — so retain
        and reclaim are the SAME expression and cannot drift (the asymmetry that
        caused the F1 sidecar leak: two filters trusted to agree by reading). With
        no FK to enforce it (JSONL + networkx + filesystem), this function IS the
        structural binding. Mode refinement (reference vs copy) is caller-side: the
        sidecar reclaim purges when this returns empty (any mode); the blob reclaim
        filters to ``mode=="copy"`` (a reference twin has no blob stake). Confirms
        the full sha (the doc: ref index key is only a 12-char prefix)."""
        out: list[str] = []
        with self._synced():
            for nid in self.find_nodes_by_ref(doc_ref(sha)):
                node = self.get_node(nid)
                if (node
                        and node.get("type") == CognitionNodeType.DOCUMENT.value
                        and node.get("metadata", {}).get("sha256") == sha):
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

    def add_node(self, node: CognitionNode, *, mint_unique_id: bool = False) -> str:
        """Add a cognition node to the graph and journal; return the final node id.

        ``mint_unique_id=False`` (default): add the node under ``node.id`` as-is
        (overwrites if the id already exists — current behavior; used by replay-adjacent
        and explicit-id callers). ``mint_unique_id=True``: GLOBAL id-collision guard
        (WP-ID) — under the lock, if ``node.id`` is already taken, salt the id-hash
        input (``<summary>#<n>``, leaving the stored summary unchanged) and retry until
        free, so two same-type+summary nodes minted in one coarse clock tick get
        DISTINCT ids instead of one silently overwriting the other (data loss).

        THE MINT FIRES ONLY HERE, at the generation/journaling boundary — NEVER during
        replay (``_replay_entry`` writes ``self._graph.add_node`` directly and never
        calls this method), so a replayed id that already exists is idempotent
        cross-process convergence, not a collision to salt around. Do NOT hoist this
        into the replay path. Running under ``_synced`` (which catches up the journal
        first) means the check also sees other processes' journaled nodes — closing the
        in-process collision and SHRINKING (not eliminating) the cross-process
        has_node→add_node TOCTOU; a truly concurrent cross-process mint landing between
        this op's catch-up and its append is the documented residual (backlog #2).
        """
        with self._synced():
            if mint_unique_id:
                salt = 0
                while node.id in self._graph:
                    salt += 1
                    node = node.model_copy(update={
                        "id": generate_node_id(node.type.value, f"{node.summary}#{salt}", node.timestamp),
                    })
            # C-4 journal-FIRST: the (validated, minted) node is durably recorded
            # BEFORE any in-memory mutation, so a failing append leaves NOTHING
            # mutated — no phantom node the journal never recorded (invisible to other
            # processes, lost on the next re-hydrate). The mint above stays first: it
            # needs the caught-up in-memory graph to detect collisions, and it never
            # runs on replay (_replay_entry writes self._graph directly).
            self._append_journal("add_node", node.model_dump(mode="json"))
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
            return node.id

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

            # C-4 journal-FIRST (see add_node): record before mutating the graph.
            self._append_journal("add_edge", edge.model_dump(mode="json"))
            self._graph.add_edge(
                edge.from_id,
                edge.to_id,
                key=edge.edge_type.value,
                type=edge.edge_type.value,
                timestamp=edge.timestamp,
                source=edge.source,
                reason=edge.reason,
            )
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

            # C-4 journal-FIRST (see add_node): record before mutating the graph.
            data = {"id": node_id, **kwargs}
            self._append_journal("update_node", data)
            for key, value in kwargs.items():
                self._graph.nodes[node_id][key] = value
            return True

    def remove_node(
        self, node_id: str, removed_by: dict[str, str] | str | None = None
    ) -> bool:
        """Remove a node and all its edges from the graph.

        Args:
            node_id: ID of the node to remove
            removed_by: Acting author for the journal tombstone (provenance) —
                a resolved git identity dict or a surface tag like "dashboard".
                Optional; omitted from the tombstone when None. Replay ignores
                it, so old tombstones without the field keep replaying fine.

        Returns:
            True if the node existed and was removed
        """
        with self._synced():
            if node_id not in self._graph:
                return False

            # C-4 journal-FIRST (see add_node): record before mutating the graph.
            tombstone: dict[str, Any] = {"id": node_id}
            if removed_by is not None:
                tombstone["removed_by"] = removed_by
            self._append_journal("remove_node", tombstone)
            self._unindex_node_refs(node_id)
            self._graph.remove_node(node_id)
            # WP-5 gate redirect: remember this was a real removal so our OWN
            # tombstone read-back on the next catch-up (C-6) doesn't defer-
            # then-warn about a target that's "missing" only because we just
            # validly deleted it ourselves.
            self._removed_node_ids.add(node_id)
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
                # C-4 journal-FIRST (see add_node): record before mutating the graph.
                self._append_journal("remove_edge", {
                    "from_id": from_id,
                    "to_id": to_id,
                    "edge_type": edge_type.value,
                })
                self._graph.remove_edge(from_id, to_id, key=key)
                return True
            else:
                # Remove all edges between the pair
                keys = list(self._graph[from_id][to_id].keys())
                for key in keys:
                    # C-4 journal-FIRST per edge: a mid-loop append failure leaves a
                    # clean journaled+mutated prefix (no phantom removal).
                    self._append_journal("remove_edge", {
                        "from_id": from_id,
                        "to_id": to_id,
                        "edge_type": key,
                    })
                    self._graph.remove_edge(from_id, to_id, key=key)
                return len(keys) > 0

    def pop_replayed_node_ids(self) -> list[str]:
        """Drain and return node ids added via journal REPLAY (this or another
        process's write, discovered through catch-up/rehydrate) since the last
        call. Used by the tools-layer re-embed-on-replay reconciliation
        (WP-3, 8606d59905a5) so a teammate's node written elsewhere becomes
        searchable without a server restart — see discovery 4b99fa9f44d5.

        Does NOT itself trigger a catch-up; callers already do via a preceding
        public storage call (e.g. cognition_search reads the graph first).
        Thread-safe under the storage lock against a concurrent _replay_entry.
        """
        with self._lock:
            ids = list(self._replayed_node_ids)
            self._replayed_node_ids.clear()
            return ids

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

    def count_uncurated_nodes(self, node_type: CognitionNodeType | None = None) -> int:
        """Count uncurated nodes with NO cap — the honest backlog total.

        ``get_uncurated_nodes`` caps the returned LIST at 500; callers that also
        derived the total from that list under-reported any backlog over 500 (T-2).
        Mirrors the get filter EXACTLY: uncurated == lacks ``curated_by_skill_at``,
        with the same optional type filter.
        """
        with self._synced():
            count = 0
            for _node_id, data in self._graph.nodes(data=True):
                if node_type and data.get("type") != node_type.value:
                    continue
                if data.get("curated_by_skill_at") is not None:
                    continue
                count += 1
            return count

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

    # Types that are graph-inert: a pair involving one of these mints NO deterministic
    # edge — the gate short-circuits before any pair rule below. ``workflow`` is
    # versioned via supersession; ``task`` is curated explicitly (its parent hierarchy
    # is a direct part_of edge set at creation, never reference-matched). NOTE:
    # ``document`` is deliberately NOT inert — it has its own ``doc_gated`` pair rules
    # below, reached only after this gate passes.
    _INERT_TYPES: frozenset[str] = frozenset({
        CognitionNodeType.WORKFLOW.value,
        CognitionNodeType.TASK.value,
    })

    @staticmethod
    def _deterministic_edge_for_pair(
        type_a: str, id_a: str, type_b: str, id_b: str
    ) -> tuple[str, str, CognitionEdgeType, bool] | None:
        """The deterministic edge (if any) for an unordered {a, b} node pair.

        Returns ``(from_id, to_id, edge_type, doc_gated)`` or ``None``. ``doc_gated``
        means the edge fires ONLY when the shared reference is a ``doc:`` key (the
        §9 S4 vacuum defense — document links must not form on a popular issue:/
        commit: ref). Truth table (DESIGN §1/§9 S4):

        - entity ↔ episode  → part_of   (entity → episode),  ANY shared ref
        - entity ↔ document → part_of   (entity → document), doc: ref ONLY
        - document ↔ episode → relates_to (document → episode), doc: ref ONLY
        - document ↔ document / episode ↔ episode / entity ↔ entity → no edge
        - workflow ↔ anything → no edge (graph-inert; versioned via supersession)
        - task ↔ anything → no edge (graph-inert; parent hierarchy is an explicit edge)
        """
        # Inert-type gate: workflow- and task-involving pairs are graph-inert (B1/B2).
        if type_a in CognitionStorage._INERT_TYPES or type_b in CognitionStorage._INERT_TYPES:
            return None

        doc = CognitionNodeType.DOCUMENT.value
        ep = CognitionNodeType.EPISODE.value
        a_doc, a_ep = type_a == doc, type_a == ep
        b_doc, b_ep = type_b == doc, type_b == ep
        a_entity = not a_doc and not a_ep
        b_entity = not b_doc and not b_ep

        # entity ↔ episode (direction entity → episode), any ref
        if a_entity and b_ep:
            return (id_a, id_b, CognitionEdgeType.PART_OF, False)
        if a_ep and b_entity:
            return (id_b, id_a, CognitionEdgeType.PART_OF, False)
        # entity ↔ document (direction entity → document), doc: only
        if a_entity and b_doc:
            return (id_a, id_b, CognitionEdgeType.PART_OF, True)
        if a_doc and b_entity:
            return (id_b, id_a, CognitionEdgeType.PART_OF, True)
        # document ↔ episode (direction document → episode), doc: only
        if a_doc and b_ep:
            return (id_a, id_b, CognitionEdgeType.RELATES_TO, True)
        if a_ep and b_doc:
            return (id_b, id_a, CognitionEdgeType.RELATES_TO, True)
        # doc↔doc, episode↔episode, entity↔entity: no deterministic edge
        return None

    def create_deterministic_edges(self, node_id: str) -> int:
        """Create deterministic edges by matching shared references.

        Six-pair truth table (see ``_deterministic_edge_for_pair``): entity↔episode
        and entity↔document mint ``part_of``; document↔episode mints ``relates_to``;
        document-involving pairs require a shared ``doc:`` ref (§9 S4 vacuum defense).
        Idempotency is keyed per ``(from, to, edge_type)``: an edge of the type a rule
        would mint blocks a re-mint REGARDLESS of source, so a curator's same-type
        manual edge is never clobbered (``add_edge`` overwrites by that key), while a
        different-type edge on the pair does not block.

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

            created = 0
            seen: set[tuple[str, str, str]] = set()

            for key in self._normalize_refs(refs):
                for other_id in self._reference_index.get(key, []):
                    if other_id == node_id:
                        continue

                    other_data = self.get_node(other_id)
                    if not other_data:
                        continue

                    other_type = other_data.get("type", "")

                    match = self._deterministic_edge_for_pair(
                        node_type, node_id, other_type, other_id
                    )
                    if match is None:
                        continue
                    from_id, to_id, edge_type, doc_gated = match

                    # §9 S4: a document-involving edge fires only on the doc: key,
                    # not on a shared issue:/commit: ref (vacuum via popular refs).
                    if doc_gated and not key.startswith("doc:"):
                        continue

                    triple = (from_id, to_id, edge_type.value)
                    if triple in seen:
                        continue
                    seen.add(triple)

                    # Idempotent + non-destructive: skip if an edge of THIS type
                    # already exists (any source). add_edge keys by edge_type, so a
                    # re-mint would overwrite — and clobber a same-type manual edge's
                    # provenance. A different-type edge on the pair does not block.
                    if (self._graph.has_edge(from_id, to_id) and
                            edge_type.value in self._graph[from_id][to_id]):
                        continue

                    timestamp = datetime.now(UTC).isoformat()
                    edge = CognitionEdge(
                        from_id=from_id,
                        to_id=to_id,
                        edge_type=edge_type,
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

        C-6 — DELIBERATELY does NOT advance ``self._offset`` / ``self._journal_hasher``
        for the bytes it writes. This process re-reads its own appended line on the
        next ``_catch_up`` and replays it idempotently. That self-replay is the source
        of the "+N entries" catch-up log (reworded to not imply a remote write), but
        re-reading from disk is what keeps the byte-offset/prefix-hash invariant (C-3)
        correct WITHOUT this process having to know where its bytes landed — and it
        cannot know: the in-process RLock and the journal_io append lock are DIFFERENT
        locks, so another process can append between this op's ``_catch_up`` and this
        ``append_journal_line``. Our bytes therefore need not land at ``self._offset``
        (un-replayed remote bytes may sit in front), so advancing the offset by
        ``len(our_blob)`` would point it into the wrong place and corrupt the prefix
        hash → a spurious full re-hydrate. (A ``_pending_self_appends`` counter to
        suppress the log was considered and rejected: it would mis-attribute under the
        very same interleave, so it is no safer.) Idempotent replay makes the re-read a
        no-op convergence either way.

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

    def _record_rehydrate(self, before_ids: set[str], *, ambiguous_first_observation: bool) -> None:
        """Make a rehydrate-reset LOUD and durable (WP-1 loss visibility) — unless
        it's a known-benign false trigger, in which case stay quiet.

        Called by ``_catch_up`` after the reset AND the replay-from-top, so
        ``self._graph`` reflects what actually survived on disk.

        IDENTITY, not count, is the loss signal: ``missing = before_ids -
        after_ids``. A replacement journal can have MORE total nodes than we had
        in memory (a divergent branch with its own unrelated history) while still
        having silently dropped one of OUR nodes — a pure count comparison misses
        exactly that case (the incident that motivated this task: a branch-switch
        clobbered 2 live nodes). Before/after COUNTS are still recorded for
        context, but they never drive the warn/quiet or flag-write decision.

        ``ambiguous_first_observation``: True when the reset was detected via the
        "offset==0, graph already non-empty" branch on this instance's FIRST-EVER
        stat of the journal — which is indistinguishable from, and in the
        overwhelming common case simply IS, this process reading its own recent
        writes back for the first time (see ``_catch_up``). That is expected,
        constant, harmless behavior, not a "reset" a human needs to see — so
        UNLESS ``missing`` is non-empty (an external actor really did
        truncate/replace the journal in that exact narrow window), skip all loud
        surfacing and log a quiet DEBUG line instead of a WARNING.

        Loud path (the common shrink/hash-mismatch case, or any case with an
        actual identity loss) has three surfaces:
          1. WARNING log naming the lost-node count (was a silent logger.info);
          2. ``self.last_rehydrate`` / ``self.rehydrate_count`` for get_status;
          3. on ``missing``, a best-effort sidecar flag file so the next
             session-start prime — a separate process — can alert once (prime
             consumes/deletes it). Benign rehydrates (a divergent merge that only
             ADDED remote nodes, none of ours missing) don't spam prime.
        Never raises: the flag write is best-effort (the reset itself already
        succeeded; visibility must not break convergence).

        NOTE: ``cognition_reload`` deliberately does NOT hit this path — it calls
        ``_rehydrate_reset`` itself before ``_catch_up``, so the graph is already
        empty at offset 0 and the rehydrate detection stays False.
        """
        after_ids = set(self._graph.nodes)
        missing = sorted(before_ids - after_ids)

        if ambiguous_first_observation and not missing:
            logger.debug(
                "Journal first observed non-empty while reading back this "
                "process's own recent writes (nodes before=%d, after=%d) — "
                "not a loss event, staying quiet",
                len(before_ids),
                len(after_ids),
            )
            return

        logger.warning(
            "Journal changed under our replay offset; re-hydrated from top "
            "(nodes before=%d, after=%d; %d node(s) recorded this session are no "
            "longer on disk)",
            len(before_ids),
            len(after_ids),
            len(missing),
        )
        self.rehydrate_count += 1
        self.last_rehydrate = {
            "at": datetime.now(UTC).isoformat(),
            "nodes_before": len(before_ids),
            "nodes_after": len(after_ids),
            "nodes_lost": len(missing),
            "sample_missing_ids": missing[:5],
        }
        if missing:
            try:
                (self._dir / REHYDRATE_FLAG_FILENAME).write_text(
                    json.dumps(self.last_rehydrate), encoding="utf-8"
                )
            except OSError as exc:
                logger.debug("could not write rehydrate flag file: %s", exc)

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
        # First time this instance has ever stat'd the file with content (WP-1):
        # distinguishes the ambiguous "offset==0, graph already non-empty" case
        # below from a genuine replacement — see that branch's comment.
        first_observation = self._journal_mtime_ns is None

        # Cheap path: size AND mtime unchanged → nothing happened (one stat, no
        # read). mtime also catches an equal-byte-size replacement.
        if size == self._offset and mtime == self._journal_mtime_ns:
            return 0
        self._journal_mtime_ns = mtime

        with open(self._journal_path, "rb") as f:
            data = f.read()

        rehydrate = False
        ambiguous_first_observation = False
        if size < self._offset:
            rehydrate = True  # shrank: truncated / rotated / reset
        elif self._offset == 0 and self._graph.number_of_nodes() > 0:
            # Reading from the TOP with a non-empty graph = a re-hydrate: the
            # journal was replaced before this store advanced its offset past its
            # own first appends (appends don't move the offset — see C-6).
            #
            # WP-1 refinement: when this is ALSO this instance's first-ever stat
            # of the file (first_observation), the graph's only possible source
            # is this process's OWN prior writes (nothing else could have landed
            # in self._graph before any replay ran) — so this is indistinguishable
            # from, and in practice almost always IS, catch-up simply reading its
            # own just-appended lines back (see _append_journal's C-6 note), not a
            # real external reset. _record_rehydrate downgrades this specific
            # combination to quiet unless it turns out nodes were actually lost.
            rehydrate = True
            ambiguous_first_observation = first_observation
        elif (
            self._offset > 0
            # C-3: the replayed prefix must still be byte-identical.
            and hashlib.sha256(data[: self._offset]).digest() != self._journal_hasher.digest()
        ):
            rehydrate = True

        before_ids: set[str] = set()
        if rehydrate:
            before_ids = set(self._graph.nodes)
            self._rehydrate_reset()

        raw = data[self._offset :]
        last_nl = raw.rfind(b"\n")
        if last_nl == -1:
            # No complete line yet — do not advance past a torn append. Still
            # record the reset (the replaced journal may simply be empty/torn).
            if rehydrate:
                self._record_rehydrate(
                    before_ids, ambiguous_first_observation=ambiguous_first_observation
                )
            return 0

        complete = raw[: last_nl + 1]
        self._offset += len(complete)
        self._journal_hasher.update(complete)

        count = 0
        # WP-5 (d6cd1495b23a — merge-shaped replay defense): a merge=union
        # merge (the supported separate-clones mechanism) can interleave
        # divergent journal tails so an edge/update/remove line lands BEFORE
        # its target node's add_node line within this same batch. Collect
        # entries _replay_entry defers (target not in the graph yet) and
        # retry them ONCE more after every line in this batch has had its
        # first pass — by then any add_node from later in the batch has
        # landed, so ordinary within-batch reordering self-heals instead of
        # silently and permanently dropping the edge.
        deferred: list[dict[str, Any]] = []
        for line in complete.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if self._replay_entry(parsed) == "deferred":
                    deferred.append(parsed)
                count += 1
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning(f"Skipping malformed journal line: {e}")

        for parsed in deferred:
            if self._replay_entry(parsed) == "deferred":
                logger.warning(
                    "Dropped journal entry during replay (dependency never "
                    "appeared in this batch — merge-interleaved or genuinely "
                    "missing): action=%s data=%s",
                    parsed.get("action"), parsed.get("data"),
                )

        if rehydrate:
            # Record AFTER the replay-from-top so the identity comparison sees
            # what actually survived on disk.
            self._record_rehydrate(
                before_ids, ambiguous_first_observation=ambiguous_first_observation
            )

        if count:
            # C-6: +N includes THIS process's own just-appended lines re-read from
            # disk (appends don't advance the offset — see _append_journal), not only
            # other processes' writes. Worded neutrally so it doesn't imply remote origin.
            logger.info(
                f"Cognition graph replayed +{count} journal entries "
                f"(includes this process's own appends): "
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

    def _replay_entry(self, entry: dict[str, Any]) -> str:
        """Replay a single journal entry into the graph (no journal write).

        Args:
            entry: Journal entry with 'action' and 'data' keys

        Returns one of (WP-5, d6cd1495b23a — merge-shaped replay defense):
          - "applied": the entry mutated the graph as intended.
          - "deferred": the entry's target node(s) aren't in the graph YET.
            ``merge=union`` (the supported separate-clones mechanism) can
            interleave divergent journal tails so an edge/update/remove line
            precedes its endpoint's ``add_node`` line within the SAME batch —
            the caller (``_catch_up``) retries deferred entries once more
            after the full batch's ``add_node`` lines have all been applied,
            so ordinary within-batch reordering self-heals instead of
            silently and permanently losing the edge.
          - "skipped": a genuine no-op (e.g. removing an edge that's already
            gone, given both endpoint nodes DO exist) — never worth a retry
            or a warning.
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
            # WP-3 (8606d59905a5): queue for the tools-layer re-embed-on-replay
            # reconciliation. Queuing unconditionally (even for this process's
            # own writes read back during catch-up) is deliberate — the
            # consumer does one batched Chroma existence check before
            # embedding anything, so an already-embedded id costs nothing.
            self._replayed_node_ids.add(node_id)
            return "applied"
        elif action == "add_edge":
            from_id = data["from_id"]
            to_id = data["to_id"]
            edge_type = data["edge_type"]
            if from_id not in self._graph or to_id not in self._graph:
                return "deferred"
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
                # Graceful for pre-WP-Cap journals (no reason field) — like the
                # D1a metadata round-trip: absent -> None, never a KeyError.
                reason=data.get("reason"),
            )
            return "applied"
        elif action == "remove_edge":
            from_id = data["from_id"]
            to_id = data["to_id"]
            edge_type = data.get("edge_type")
            if from_id not in self._graph or to_id not in self._graph:
                return "deferred"
            if edge_type and self._graph.has_edge(from_id, to_id, key=edge_type):
                self._graph.remove_edge(from_id, to_id, key=edge_type)
                return "applied"
            return "skipped"  # both nodes exist; edge is simply already gone
        elif action == "remove_node":
            node_id = data["id"]
            if node_id not in self._graph:
                # WP-5 gate redirect (d6cd1495b23a): a target already known
                # removed (our own live remove_node, or an already-applied
                # replay) is a BENIGN own-tombstone/duplicate read-back, not
                # a loss — only a never-before-seen missing target is worth
                # deferring/retrying/warning about.
                if node_id in self._removed_node_ids:
                    return "skipped"
                return "deferred"
            self._unindex_node_refs(node_id)
            self._graph.remove_node(node_id)
            self._removed_node_ids.add(node_id)
            return "applied"
        elif action == "update_node":
            node_id = data["id"]
            if node_id not in self._graph:
                return "deferred"
            for key, value in data.items():
                if key != "id":
                    self._graph.nodes[node_id][key] = value
            return "applied"
        return "skipped"  # unrecognized action — nothing to apply or retry
