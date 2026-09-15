"""JSONL-backed graph storage for the Cognition History Graph."""

import contextlib
import hashlib
import json
import logging
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, overload

import networkx as nx

from ..running_version import code_version
from .documents import doc_ref
from .git_hygiene import ensure_git_hygiene
from .identity import read_confirmed_identity
from .journal_io import append_journal_line
from .journal_shards import (
    LEGACY_JOURNAL_FILENAME,
    SHARD_START_ACTION,
    Stamp,
    adoption,
    encode_entry,
    legacy_stamp,
    line_hash,
    next_at,
    shard_dir,
    shard_filename,
    shard_stamp,
    shard_start_line,
    split_entries,
    straggler_report,
)
from .journal_watch import check_between_sessions, note_created
from .jsonl_dir_registry import DIR_MTIME_RACY_WINDOW_NS
from .local_paths import write_path as local_write_path
from .models import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    generate_node_id,
)
from .people_facts import DEFAULT_MACHINE_CAP, PeopleFactsRegistry
from .person_migration import ensure_person_migration
from .profiles import ProfileRegistry
from .roster import Roster

logger = logging.getLogger(__name__)

# Minimum prefix length for commit SHA short-form matching
_COMMIT_SHORT_PREFIX_LEN = 7

JOURNAL_FILENAME = LEGACY_JOURNAL_FILENAME

_NODE_ADD_FIELDS = (
    "type", "summary", "detail", "context", "references", "severity",
    "timestamp", "author", "metadata",
)
_NODE_IDENTITY_FIELDS = ("type", "timestamp", "author")


class JournalWriterUnavailableError(RuntimeError):
    """No confirmed identity in this checkout, so there is no shard to write to."""


@dataclass
class _JournalFile:
    """Replay state for one journal file (the legacy journal, or one shard)."""

    path: Path
    legacy: bool
    offset: int = 0
    hasher: "hashlib._Hash" = field(default_factory=hashlib.sha256)
    mtime_ns: int | None = None
    line_hashes: set[str] = field(default_factory=set)
    entries_read: int = 0
    own_unread: set[str] = field(default_factory=set)

    def reset(self) -> None:
        self.offset = 0
        self.hasher = hashlib.sha256()
        self.mtime_ns = None
        self.line_hashes = set()
        self.entries_read = 0

# Sidecar flag written on a LOSSY rehydrate-reset (nodes vanished from memory) so
# the next session-start prime — a separate process — can surface the loss. Consumed
# (deleted) by prime.py after it is shown once. Git-ignored via git_hygiene.py.
REHYDRATE_FLAG_FILENAME = ".last-rehydrate.json"

# WP-TC15: curation-containment observability. Edge `source` values produced by
# the ONLY sanctioned edge-writing path (the background curate-orchestrator agent,
# plus the two deterministic/task-linking internal writers) — get_statistics counts
# every OTHER source (an unrecognized value, "manual", or "batch") as a write that
# happened outside a curation run. Conservative by construction: a future legitimate
# producer must be added here deliberately, in code, with a CHANGELOG note — it does
# not get exempted just by being common. "curator" is the legacy default (old
# journals predating this feature; also the replay fallback for a missing source
# field, storage.py's _catch_up "add_edge" branch above) and is exempt so every
# pre-existing graph doesn't show a false baseline of violations.
_EDGE_SOURCE_CURATION_EXEMPT: frozenset[str] = frozenset({
    "deterministic",
    "task-parent",
    "curate-skill",
    "curate-conflict",
    "curate-cluster",
    "curator",
})


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

    def __init__(self, cognition_dir: Path, read_only: bool = False):
        """Initialize storage, hydrating from JSONL if it exists.

        Args:
            cognition_dir: Directory for .cognition/ files (Git-committed)
            read_only: hydrate only. Skips every startup side effect that writes
                into the project -- ignore-file hygiene, person migration, and the
                between-session loss check with its snapshot. For opening ANOTHER
                project's graph, where re-baselining its loss record would mask a
                loss its own next session should have reported.
        """
        self._read_only = read_only
        self._dir = cognition_dir
        self._journal_path = cognition_dir / JOURNAL_FILENAME
        self._graph = nx.MultiDiGraph()
        self._reference_index: dict[str, list[str]] = defaultdict(list)
        self._lock = threading.RLock()
        # One replay state per journal file: the legacy journal plus every shard
        # under journal/. Offsets only advance past complete lines; a prefix hash
        # per file detects a replaced or merged file (C-3). See _catch_up.
        self._files: dict[str, _JournalFile] = {
            LEGACY_JOURNAL_FILENAME: _JournalFile(self._journal_path, legacy=True),
        }
        self._shard_dir_mtime_ns: int | None = None
        self._shard_dir_racy = False
        # Last-writer-wins state (docs/wp-journal-shards-plan.md §3c): the stamp
        # behind every node attribute, node add, node removal and edge, so the
        # graph is the same whatever order files are read in.
        self._attr_stamps: dict[str, dict[str, Stamp]] = {}
        self._add_stamps: dict[str, tuple[Stamp, str]] = {}
        self._node_tombstones: dict[str, Stamp] = {}
        self._edge_stamps: dict[tuple[str, str, str], tuple[Stamp, bool]] = {}
        # Entries whose target is not in the graph yet, kept across passes: with
        # several files a dependency can live in a shard not discovered until later.
        self._pending: dict[Stamp, dict[str, Any]] = {}
        self.unresolved_entries = 0
        self.id_collisions = 0
        self.glued_lines = 0
        self._last_at: str | None = None
        self._op_writer: str | None = None
        self._op_writer_resolved = False
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
        # _apply's remove_node branch — without this, a process's own
        # remove_node tombstone read back on its NEXT catch-up (C-6: appends
        # don't advance the offset, so a process re-reads its own just-
        # appended lines) always found the target already gone and defer-
        # then-warned on every ordinary deletion. Never drained: legitimate
        # deletions are rare relative to adds, so this stays small in
        # practice — same trade as _replayed_node_ids being an unbounded-but-
        # naturally-small handoff set.
        self._removed_node_ids: set[str] = set()
        # WP-EnvFacts-A: per-person environment facts, folded from
        # .cognition/people/*.jsonl delta files into a registry OUTSIDE the
        # graph (same non-graph-replay-state pattern as _reference_index).
        # Guarded by self._lock like everything else; caught up alongside the
        # main journal in _synced.
        self._people_facts = PeopleFactsRegistry(cognition_dir)
        # Caught up under the lock in _synced, like the facts registry.
        self._profiles = ProfileRegistry(cognition_dir)

        self.person_migration_report: dict[str, Any] | None = None
        if read_only:
            self._catch_up()
            self._warn_unresolved()
            self._people_facts.catch_up()
            self._profiles.catch_up()
            return

        self._dir.mkdir(parents=True, exist_ok=True)

        try:
            ensure_git_hygiene(cognition_dir.parent, cognition_dir)
        except Exception as exc:  # noqa: BLE001
            logger.debug("git-hygiene: unexpected error (swallowed): %s", exc)

        # Initial hydrate is just a catch-up from offset 0.
        self._catch_up()
        self._warn_unresolved()
        self._people_facts.catch_up()
        self._profiles.catch_up()

        # Runs here, not in prime, so a session that never primes still converges:
        # the write gate reads PROFILES, so an unmigrated person node would have
        # its owner re-answer all five onboarding questions. Report is stashed for
        # prime to announce, since this wrote committed files nobody asked for.
        try:
            self.person_migration_report = ensure_person_migration(self)
        except Exception as exc:  # noqa: BLE001
            logger.debug("person-migration: unexpected error (swallowed): %s", exc)

        # Loss a live server cannot see: it happened while no session was running.
        loss = check_between_sessions(self)
        if loss is not None:
            logger.warning(
                "journal-watch: %d node(s) this checkout had seen are gone from the "
                "journal without a deletion record since the last session",
                loss["nodes_lost"],
            )
            self.last_rehydrate = loss
            try:
                flag = local_write_path(self._dir, REHYDRATE_FLAG_FILENAME)
                # An alert nobody has read yet is ADDED to, not replaced: two losses
                # before the next session start must report both, not the smaller.
                with contextlib.suppress(OSError, ValueError):
                    unread = json.loads(flag.read_text(encoding="utf-8"))
                    if isinstance(unread, dict):
                        loss = {
                            **loss,
                            "nodes_lost": int(unread.get("nodes_lost") or 0) + loss["nodes_lost"],
                            "sample_missing_ids": list(dict.fromkeys(
                                [*(unread.get("sample_missing_ids") or []),
                                 *loss["sample_missing_ids"]]
                            ))[:5],
                        }
                flag.write_text(json.dumps(loss), encoding="utf-8")
            except OSError as exc:
                logger.debug("journal-watch: could not write loss flag: %s", exc)

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

    # ── Per-person environment facts (WP-EnvFacts-A) ──────────────────
    # Thin synced facade over PeopleFactsRegistry. Policy (self-only writes,
    # machine defaulting, disclosure) lives in the TOOLS layer — these methods
    # take email as data, exactly like the person-node storage paths.

    def set_env_fact(
        self,
        email: str,
        machine: str,
        key: str,
        value: Any,
        by: dict[str, str],
        from_agent: bool = True,
        machine_cap: int = DEFAULT_MACHINE_CAP,
    ) -> dict[str, Any]:
        """Set one env fact (delta append; no-op on identical value).

        Raises ValueError on a NEW machine at the cap (retryable — the caller
        surfaces the prune remedy) or blank email/machine/key.
        """
        with self._synced():
            return self._people_facts.set_fact(
                email, machine, key, value, by, from_agent, machine_cap
            )

    def delete_env_fact(
        self, email: str, machine: str, key: str, by: dict[str, str], from_agent: bool = True
    ) -> dict[str, Any]:
        """Delete one env fact (delta append; no-op on an absent key)."""
        with self._synced():
            return self._people_facts.delete_fact(email, machine, key, by, from_agent)

    def clear_env_facts(
        self, email: str, machine: str | None, by: dict[str, str], from_agent: bool = True
    ) -> dict[str, Any]:
        """Bulk-remove env facts (one machine, or ALL when machine is None)."""
        with self._synced():
            return self._people_facts.clear_facts(email, machine, by, from_agent)

    def get_env_facts(self, email: str) -> dict[str, dict[str, Any]]:
        """One identity's facts, machine -> key -> value (empty dict if none)."""
        with self._synced():
            return self._people_facts.facts_for(email)

    # ── Profiles (committed, per person; replaces person nodes) ─────────

    def set_profile_fields(
        self, email: str, fields: dict[str, Any], by: dict[str, str], from_agent: bool = True
    ) -> dict[str, Any]:
        with self._synced():
            return self._profiles.set_fields(email, fields, by, from_agent)

    def unset_profile_field(self, email: str, field: str, by: dict[str, str]) -> dict[str, Any]:
        with self._synced():
            return self._profiles.unset_field(email, field, by)

    def get_profile(self, email: str) -> dict[str, Any] | None:
        with self._synced():
            return self._profiles.get(email)

    def all_profiles(self) -> list[dict[str, Any]]:
        with self._synced():
            return self._profiles.all_profiles()

    def profile_emails(self) -> list[str]:
        with self._synced():
            return self._profiles.all_emails()

    def removed_profile_emails(self) -> set[str]:
        """Emails deliberately taken off the roster — tombstoned, not merely empty."""
        with self._synced():
            return self._profiles.removed_emails()

    def mark_profile_removed(self, email: str, by: dict[str, str]) -> dict[str, Any]:
        with self._synced():
            return self._profiles.mark_removed(email, by)

    def profile_missing_required(self, email: str) -> list[str]:
        """Which required fields a profile still lacks — the gate's refusal names these."""
        with self._synced():
            return self._profiles.missing_required(email)

    def profile_direct_reports(self, email: str) -> list[str]:
        with self._synced():
            return self._profiles.direct_reports(email)

    def roster(self) -> Roster:
        """Snapshot of who is on the project: profiles, with legacy person nodes
        folded in for any email that has no profile yet."""
        with self._synced():
            return Roster.load(self)

    def append_legacy_profile_history(
        self, email: str, entries: list[dict[str, Any]], node_id: str
    ) -> dict[str, Any]:
        with self._synced():
            return self._profiles.append_legacy_history(email, entries, node_id)

    def profile_history(self, email: str) -> list[dict[str, Any]]:
        """Append-only record trail for one profile — powers the tamper alert."""
        with self._synced():
            return self._profiles.history_for(email)

    def env_fact_emails(self) -> list[str]:
        """Every email with folded facts — registered person or not."""
        with self._synced():
            return self._people_facts.all_emails()

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
                self._op_writer_resolved = False
                self._catch_up()
                self._people_facts.catch_up()
                self._profiles.catch_up()
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
        replay (``_apply`` writes ``self._graph.add_node`` directly and never
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
            # runs on replay (_apply writes self._graph directly).
            self._append_journal("add_node", node.model_dump(mode="json"))
        if not self._read_only:
            note_created(self._dir, node.id)
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
            self._append_journal("update_node", {"id": node_id, **kwargs})
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
                return len(keys) > 0

    def pop_replayed_node_ids(self) -> list[str]:
        """Drain and return node ids added via journal REPLAY (this or another
        process's write, discovered through catch-up/rehydrate) since the last
        call. Used by the tools-layer re-embed-on-replay reconciliation
        (WP-3, 8606d59905a5) so a teammate's node written elsewhere becomes
        searchable without a server restart — see discovery 4b99fa9f44d5.

        Does NOT itself trigger a catch-up; callers already do via a preceding
        public storage call (e.g. cognition_search reads the graph first).
        Thread-safe under the storage lock against a concurrent _apply.
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

    @overload
    def get_recent_nodes(
        self,
        limit: int = 10,
        node_type: CognitionNodeType | None = None,
        *,
        with_total: Literal[False] = False,
    ) -> list[dict[str, Any]]: ...

    @overload
    def get_recent_nodes(
        self,
        limit: int = 10,
        node_type: CognitionNodeType | None = None,
        *,
        with_total: Literal[True],
    ) -> tuple[list[dict[str, Any]], int]: ...

    def get_recent_nodes(
        self,
        limit: int = 10,
        node_type: CognitionNodeType | None = None,
        *,
        with_total: bool = False,
    ) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], int]:
        """Get the most recent nodes, optionally filtered by type.

        Args:
            limit: Maximum number of nodes to return
            node_type: Optional type filter
            with_total: WP-TC10, additive/keyword-only, default False — every
                pre-existing caller is untouched. When True, returns
                ``(sliced, total)`` instead of just ``sliced`` — ``total`` is the
                count of ALL matching nodes before the ``limit`` slice (this is a
                full structural scan, so ``total`` is always exact, never a floor).

        Returns:
            List of node data dicts sorted by timestamp descending (default), or
            ``(list, total)`` when ``with_total=True``.
        """
        with self._synced():
            nodes = []
            for node_id, data in self._graph.nodes(data=True):
                if node_type and data.get("type") != node_type.value:
                    continue
                nodes.append({"id": node_id, **data})

        nodes.sort(key=lambda n: n.get("timestamp", ""), reverse=True)
        sliced = nodes[:limit]
        if with_total:
            return sliced, len(nodes)
        return sliced

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

    def get_statistics(self) -> dict[str, int | dict[str, int]]:
        """Get graph statistics.

        Returns:
            Dictionary with node/edge counts by type, plus (WP-TC15) an
            edge_sources histogram and edges_outside_curation count -- see
            those keys' inline comments below for the source taxonomy.
        """
        with self._synced():
            # Kept narrowly int-valued internally (unlike the widened return
            # type) so every existing `stats[key] += 1` below stays a plain
            # int += int and pyright doesn't need to re-narrow a union on
            # each one -- the histogram is merged in as a sibling structure
            # only at the return boundary (WP-TC15 peer-review HIGH: the
            # alternative, widening this dict's own annotation, ripples a
            # union type into every increment line above and below).
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

            # Edge counts by type, plus (WP-TC15) the source histogram and
            # outside-curation count -- derived in this SAME pass (no second
            # O(edges) walk) since it already visits every edge's data.
            for edge_type in CognitionEdgeType:
                stats[f"edge_{edge_type.value}"] = 0
            edge_sources: dict[str, int] = {}
            edges_outside_curation = 0
            for _, _, edge_data in self._graph.edges(data=True):
                et = edge_data.get("type", "")
                key = f"edge_{et}"
                if key in stats:
                    stats[key] += 1

                es = edge_data.get("source", "curator")
                edge_sources[es] = edge_sources.get(es, 0) + 1
                if es not in _EDGE_SOURCE_CURATION_EXEMPT:
                    edges_outside_curation += 1

            stats["edges_outside_curation"] = edges_outside_curation

            stats["uncurated"] = sum(
                1 for _, data in self._graph.nodes(data=True)
                if data.get("curated_by_skill_at") is None
            )

            # WP-TC15: histogram of every edge source seen (dynamic, like the
            # per-edge-type counts above) -- semantic-edge writes made outside
            # a curation run are the ones NOT in _EDGE_SOURCE_CURATION_EXEMPT
            # ("manual", "batch", or any unknown value), already summed above
            # as edges_outside_curation. Always present, even on a zero-edge
            # graph ({} and 0 respectively) -- no absent-key ambiguity.
            result: dict[str, int | dict[str, int]] = dict(stats)
            result["edge_sources"] = edge_sources
            return result

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
    # is a direct part_of edge set at creation, never reference-matched); ``person``
    # (WP-TC5) has no references at all and must never auto-mint a part_of edge on a
    # coincidentally shared ref. NOTE: ``document`` is deliberately NOT inert — it has
    # its own ``doc_gated`` pair rules below, reached only after this gate passes.
    _INERT_TYPES: frozenset[str] = frozenset({
        CognitionNodeType.WORKFLOW.value,
        CognitionNodeType.TASK.value,
        CognitionNodeType.PERSON.value,
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

    def _current_writer(self) -> str:
        """The confirmed email this operation writes as, resolved once per outermost
        operation so a multi-line write cannot straddle an identity change."""
        if self._read_only:
            raise JournalWriterUnavailableError("read-only storage never writes")
        if not self._op_writer_resolved or self._sync_depth == 0:
            confirmed = read_confirmed_identity(self._dir)
            self._op_writer = (confirmed or {}).get("email") or None
            self._op_writer_resolved = self._sync_depth > 0
        if not self._op_writer:
            raise JournalWriterUnavailableError(
                "no confirmed identity in this checkout -- confirm it with "
                "cognition_set_identity before writing to the graph"
            )
        return self._op_writer

    def can_write(self) -> bool:
        """Whether a journal write would have a shard to land in."""
        if self._read_only:
            return False
        confirmed = read_confirmed_identity(self._dir)
        return bool((confirmed or {}).get("email"))

    def journal_status(self) -> dict[str, Any]:
        """Which journal files feed the graph, where writes go, and what needs attention."""
        with self._synced():
            confirmed = read_confirmed_identity(self._dir)
            email = (confirmed or {}).get("email")
            start = adoption(self._dir)
            return {
                "legacy_journal_bytes": _size(self._journal_path) if self._journal_path.exists() else None,
                "shards": [
                    {"file": state.path.name, "bytes": _size(state.path)}
                    for _, state in self._ordered_files()
                    if not state.legacy and state.path.exists()
                ],
                "writing_to": f"{shard_dir(self._dir).name}/{shard_filename(email)}" if email else None,
                "adopted_at": start["at"] if start else None,
                "stragglers": straggler_report(self._dir),
                "unresolved_entries": self.unresolved_entries,
                "id_collisions": self.id_collisions,
                "glued_lines": self.glued_lines,
            }

    def _floor_for(self, action: str, data: dict[str, Any]) -> str | None:
        """The newest write time this write must beat (skew protection)."""
        candidates: list[Stamp] = []
        if action in ("add_node", "update_node", "remove_node"):
            node_id = data.get("id", "")
            if node_id in self._node_tombstones:
                candidates.append(self._node_tombstones[node_id])
            if node_id in self._add_stamps:
                candidates.append(self._add_stamps[node_id][0])
            stamps = self._attr_stamps.get(node_id, {})
            keys = stamps.keys() if action != "update_node" else [k for k in data if k != "id"]
            candidates.extend(stamps[k] for k in keys if k in stamps)
        elif action in ("add_edge", "remove_edge"):
            key = (data.get("from_id", ""), data.get("to_id", ""), data.get("edge_type", ""))
            if key in self._edge_stamps:
                candidates.append(self._edge_stamps[key][0])
        ats = [s[1] for s in candidates if s[1]]
        return max(ats) if ats else None

    def _shard_state(self, name: str) -> _JournalFile:
        key = f"{shard_dir(self._dir).name}/{name}"
        if key not in self._files:
            self._files[key] = _JournalFile(shard_dir(self._dir) / name, legacy=False)
        return self._files[key]

    def _own_shard(self) -> tuple[str, _JournalFile]:
        """This checkout's person's shard, created with its adoption record if new."""
        name = shard_filename(self._current_writer())
        state = self._shard_state(name)
        if not state.path.exists():
            state.path.parent.mkdir(parents=True, exist_ok=True)
            start_at = next_at(None, self._last_at)
            self._last_at = start_at
            start = shard_start_line(self._dir, _plugin_version(), start_at)
            append_journal_line(state.path, start)
            state.own_unread.add(line_hash(start))
        return name, state

    def adopt_shards(self) -> Path:
        """Create this person's shard now, before any write. Moves no data."""
        with self._synced():
            return self._own_shard()[1].path

    def _append_journal(self, action: str, data: dict[str, Any]) -> str:
        """Append one entry to this checkout's person's shard, then apply it.

        Journal-FIRST (C-4): the line is durable before the graph changes, so a
        failing append mutates nothing. The line is applied through the same stamp
        comparison replay uses -- one code path, so this process's view always equals
        a rebuild from the same bytes.

        C-6 still holds: appends never advance the file's offset. The line is re-read
        on the next catch-up and re-applied idempotently (same stamp).
        """
        name, state = self._own_shard()
        path = state.path
        at = next_at(self._floor_for(action, data), self._last_at)
        self._last_at = at
        line = encode_entry(action, data, at)
        append_journal_line(path, line)
        state.own_unread.add(line_hash(line))
        return self._apply({"action": action, "data": data}, shard_stamp(at, name, line))

    # ── Replay ────────────────────────────────────────────────────────

    def _reset_replay_state(self) -> None:
        """Wipe the graph and every file's replay state for a full rebuild."""
        self._graph = nx.MultiDiGraph()
        self._reference_index = defaultdict(list)
        self._attr_stamps = {}
        self._add_stamps = {}
        self._node_tombstones = {}
        self._edge_stamps = {}
        self._pending = {}
        self.glued_lines = 0
        self.id_collisions = 0
        for state in self._files.values():
            state.reset()

    def _rehydrate_reset(self) -> None:
        self._reset_replay_state()

    def _record_rehydrate(self, before_ids: set[str], *, ambiguous_first_observation: bool) -> None:
        """Make a rebuild LOUD and durable (WP-1 loss visibility) — unless it is a
        known-benign trigger with nothing lost, in which case stay quiet.

        IDENTITY, not count, is the loss signal: ``missing = before_ids - after_ids``.
        Surfaces: a WARNING, ``last_rehydrate`` / ``rehydrate_count`` for get_status,
        and on real loss a sidecar flag the next session start shows once.
        """
        after_ids = set(self._graph.nodes)
        missing = sorted(before_ids - after_ids)

        if ambiguous_first_observation and not missing:
            logger.debug(
                "Journal rebuild with nothing lost (nodes before=%d, after=%d)",
                len(before_ids), len(after_ids),
            )
            return

        logger.warning(
            "Journal changed under our replay offset; re-hydrated from top "
            "(nodes before=%d, after=%d; %d node(s) recorded this session are no "
            "longer on disk)",
            len(before_ids), len(after_ids), len(missing),
        )
        self.rehydrate_count += 1
        self.last_rehydrate = {
            "at": datetime.now(UTC).isoformat(),
            "nodes_before": len(before_ids),
            "nodes_after": len(after_ids),
            "nodes_lost": len(missing),
            "sample_missing_ids": missing[:5],
        }
        if missing and not self._read_only:
            try:
                (local_write_path(self._dir, REHYDRATE_FLAG_FILENAME)).write_text(
                    json.dumps(self.last_rehydrate), encoding="utf-8"
                )
            except OSError as exc:
                logger.debug("could not write rehydrate flag file: %s", exc)

    def _discover_shards(self) -> None:
        """Track every shard file. One dir stat gates the listing; while the dir was
        modified within the racy window every pass re-lists (the Windows CI catch)."""
        directory = shard_dir(self._dir)
        try:
            st = directory.stat()
        except OSError:
            return
        if st.st_mtime_ns == self._shard_dir_mtime_ns and not self._shard_dir_racy:
            return
        self._shard_dir_mtime_ns = st.st_mtime_ns
        self._shard_dir_racy = abs(time.time_ns() - st.st_mtime_ns) < DIR_MTIME_RACY_WINDOW_NS
        try:
            names = sorted(p.name for p in directory.iterdir() if p.name.endswith(".jsonl"))
        except OSError:
            return
        for name in names:
            self._shard_state(name)

    def _ordered_files(self) -> list[tuple[str, _JournalFile]]:
        legacy = [(k, v) for k, v in self._files.items() if v.legacy]
        shards = sorted((k, v) for k, v in self._files.items() if not v.legacy)
        return legacy + shards

    def _scan_file(self, state: _JournalFile) -> tuple[str, bytes]:
        """Decide what one file needs: "none", "append", "insert" or "rebuild".

        Rebuild only when something this process already applied from the file can
        no longer be there: the file vanished, shrank, or a line it held is gone. A
        merge that only INSERTED lines into a shard applies just the new lines --
        stamps make that order-free (§11 M1). The legacy journal orders by position,
        so any rewrite of it still rebuilds, as before.
        """
        try:
            st = state.path.stat()
        except OSError:
            return ("rebuild" if state.line_hashes else "none"), b""
        if st.st_size == state.offset and st.st_mtime_ns == state.mtime_ns:
            return "none", b""
        try:
            data = state.path.read_bytes()
        except OSError:
            return "none", b""
        state.mtime_ns = st.st_mtime_ns
        if st.st_size < state.offset:
            return "rebuild", data
        if state.offset > 0 and hashlib.sha256(data[: state.offset]).digest() != state.hasher.digest():
            if state.legacy:
                return "rebuild", data
            present = {line_hash(raw.strip()) for raw in _complete_lines(data) if raw.strip()}
            return ("insert" if state.line_hashes <= present else "rebuild"), data
        return "append", data

    def _consume(self, name: str, state: _JournalFile, data: bytes, *, whole: bool) -> int:
        """Apply the complete lines of `data` not yet applied from this file."""
        start = 0 if whole else state.offset
        raw = data[start:]
        last_nl = raw.rfind(b"\n")
        if last_nl == -1:
            if whole:
                state.offset = 0
                state.hasher = hashlib.sha256()
            return 0
        complete = raw[: last_nl + 1]
        if whole:
            state.offset = len(complete)
            state.hasher = hashlib.sha256(complete)
        else:
            state.offset += len(complete)
            state.hasher.update(complete)

        shard_name = state.path.name
        count = 0
        for text in complete.decode("utf-8", errors="replace").splitlines():
            line = text.strip()
            if not line:
                continue
            digest = line_hash(line)
            if digest in state.line_hashes and not state.legacy:
                continue
            state.line_hashes.add(digest)
            state.own_unread.discard(digest)
            entries, remainder = split_entries(line)
            if remainder:
                logger.warning("Skipping unreadable journal text in %s: %.80s", name, remainder)
            if len(entries) > 1:
                self.glued_lines += 1
                logger.warning(
                    "Journal line in %s holds %d entries with no line break between them; "
                    "all were read -- repair the file by splitting the line", name, len(entries),
                )
            for index, entry in enumerate(entries):
                if entry.get("action") == SHARD_START_ACTION:
                    continue
                if state.legacy:
                    stamp = legacy_stamp(state.entries_read)
                    state.entries_read += 1
                else:
                    salted = line if index == 0 else f"{line}#{index}"
                    stamp = shard_stamp(str(entry.get("at") or ""), shard_name, salted)
                try:
                    if self._apply(entry, stamp) == "deferred":
                        self._pending[stamp] = entry
                    count += 1
                except (KeyError, ValueError, TypeError) as exc:
                    logger.warning("Skipping malformed journal entry in %s: %s", name, exc)
        return count

    def _retry_pending(self) -> None:
        for _ in range(3):
            progressed = False
            for stamp in sorted(self._pending):
                if self._apply(self._pending[stamp], stamp) != "deferred":
                    del self._pending[stamp]
                    progressed = True
            if not progressed or not self._pending:
                break

    def _unresolved(self) -> list[Stamp]:
        """Pending entries that are genuinely unresolved: an edit waiting on a node
        that was deleted is expected to wait, possibly forever."""
        return [
            stamp for stamp, entry in sorted(self._pending.items())
            if (entry.get("data") or {}).get("id") not in self._node_tombstones
        ]

    def _warn_unresolved(self) -> None:
        """After a full hydration every file has been read, so anything still pending
        refers to something that genuinely is not in any journal file."""
        unresolved = self._unresolved()
        self.unresolved_entries = len(unresolved)
        for stamp in unresolved[:5]:
            entry = self._pending[stamp]
            logger.warning(
                "Dropped journal entry during replay (dependency never appeared in "
                "any journal file): action=%s data=%s",
                entry.get("action"), entry.get("data"),
            )

    def _catch_up(self) -> int:
        """Replay what changed in any journal file since the last pass; return count.

        Caller MUST hold ``self._lock``. Reads are not under the cross-process append
        lock; torn tails are parked (offsets only pass complete lines), replay is
        idempotent, and every live process detects a replaced file independently.
        """
        self._discover_shards()
        scans = []
        rebuild = False
        for name, state in self._ordered_files():
            verdict, data = self._scan_file(state)
            if verdict == "rebuild":
                rebuild = True
            scans.append((name, state, verdict, data))

        if rebuild:
            return self._rebuild(ambiguous=False)

        count = 0
        for name, state, verdict, data in scans:
            if verdict == "append":
                count += self._consume(name, state, data, whole=False)
            elif verdict == "insert":
                count += self._consume(name, state, data, whole=True)

        lost_own = any(
            state.own_unread and state.offset >= _size(state.path)
            for state in self._files.values()
        )
        if lost_own:
            return self._rebuild(ambiguous=False)
        if count and self._pending:
            self._retry_pending()
            self.unresolved_entries = len(self._unresolved())
        if count:
            logger.info(
                "Cognition graph replayed +%d journal entries "
                "(includes this process's own appends): %d nodes, %d edges",
                count, self._graph.number_of_nodes(), self._graph.number_of_edges(),
            )
        return count

    def _rebuild(self, *, ambiguous: bool) -> int:
        before_ids = set(self._graph.nodes)
        self._reset_replay_state()
        self._discover_shards()
        count = 0
        for name, state in self._ordered_files():
            try:
                data = state.path.read_bytes()
                state.mtime_ns = state.path.stat().st_mtime_ns
            except OSError:
                continue
            count += self._consume(name, state, data, whole=True)
        self._retry_pending()
        self.unresolved_entries = len(self._unresolved())
        lost = any(state.own_unread for state in self._files.values())
        for key, state in list(self._files.items()):
            state.own_unread = set()
            if not state.legacy and not state.path.exists():
                del self._files[key]
        self._record_rehydrate(before_ids, ambiguous_first_observation=ambiguous and not lost)
        return count

    def reload(self) -> dict[str, int]:
        """Force a full re-hydrate from every journal file; return before/after stats."""
        with self._lock:
            before = {
                "nodes": self._graph.number_of_nodes(),
                "edges": self._graph.number_of_edges(),
            }
            self._reset_replay_state()
            self._shard_dir_mtime_ns = None
            self._catch_up()
            self._warn_unresolved()
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

    def _apply(self, entry: dict[str, Any], stamp: Stamp) -> str:
        """Apply one journal entry under last-writer-wins by stamp.

        Returns "applied", "deferred" (its target is not in the graph yet -- kept
        pending and retried) or "skipped" (older than what is already known, or a
        no-op). Whatever order entries arrive in, the result is the same.
        """
        action = entry["action"]
        data = entry["data"]
        if action == "add_node":
            return self._apply_add_node(data, stamp)
        if action == "update_node":
            return self._apply_update_node(data, stamp)
        if action == "remove_node":
            return self._apply_remove_node(data, stamp)
        if action == "add_edge":
            return self._apply_edge(data, stamp, present=True)
        if action == "remove_edge":
            return self._apply_edge(data, stamp, present=False)
        return "skipped"

    def _apply_add_node(self, data: dict[str, Any], stamp: Stamp) -> str:
        node_id = data["id"]
        tomb = self._node_tombstones.get(node_id)
        if tomb is not None and stamp <= tomb:
            return "skipped"
        values = {
            "type": data["type"],
            "summary": data["summary"],
            "detail": data["detail"],
            "context": data.get("context", []),
            "references": data.get("references", []),
            "severity": data.get("severity"),
            "timestamp": data["timestamp"],
            "author": data["author"],
            "metadata": data.get("metadata", {}),
        }
        existing = self._add_stamps.get(node_id)
        if node_id in self._graph and existing is not None:
            prior_stamp, prior_file = existing
            identity_changed = any(
                self._graph.nodes[node_id].get(k) != values[k] for k in _NODE_IDENTITY_FIELDS
            )
            different_file = stamp[2] != prior_file
            if identity_changed and different_file:
                # Two different nodes share an id (§11 M4): the earlier one is kept
                # whole rather than merged field by field.
                self.id_collisions += 1
                logger.warning("Node id collision on %s between %s and %s", node_id, prior_file, stamp[2])
                if stamp >= prior_stamp:
                    return "skipped"
                self._unindex_node_refs(node_id)
                self._graph.nodes[node_id].clear()
                self._graph.nodes[node_id].update(values)
                self._attr_stamps[node_id] = dict.fromkeys(_NODE_ADD_FIELDS, stamp)
                self._add_stamps[node_id] = (stamp, stamp[2])
                self._index_node_refs(node_id, values["references"])
                return "applied"

        if node_id not in self._graph:
            self._graph.add_node(node_id, **values)
            self._attr_stamps[node_id] = dict.fromkeys(_NODE_ADD_FIELDS, stamp)
            self._index_node_refs(node_id, values["references"])
        else:
            stamps = self._attr_stamps.setdefault(node_id, {})
            self._unindex_node_refs(node_id)
            for key in _NODE_ADD_FIELDS:
                if key not in stamps or stamp >= stamps[key]:
                    self._graph.nodes[node_id][key] = values[key]
                    stamps[key] = stamp
            self._index_node_refs(node_id, self._graph.nodes[node_id].get("references", []))
        if existing is None or stamp >= existing[0]:
            self._add_stamps[node_id] = (stamp, stamp[2])
        if tomb is not None:
            del self._node_tombstones[node_id]
            self._removed_node_ids.discard(node_id)
        self._replayed_node_ids.add(node_id)
        return "applied"

    def _apply_update_node(self, data: dict[str, Any], stamp: Stamp) -> str:
        node_id = data["id"]
        if node_id not in self._graph:
            tomb = self._node_tombstones.get(node_id)
            if tomb is not None and stamp <= tomb:
                return "skipped"
            # Newer than the deletion: the node may be re-added with an older stamp
            # than this edit, so wait for it rather than dropping the edit.
            return "deferred"
        stamps = self._attr_stamps.setdefault(node_id, {})
        for key, value in data.items():
            if key == "id":
                continue
            if key not in stamps or stamp >= stamps[key]:
                self._graph.nodes[node_id][key] = value
                stamps[key] = stamp
        return "applied"

    def _apply_remove_node(self, data: dict[str, Any], stamp: Stamp) -> str:
        node_id = data["id"]
        tomb = self._node_tombstones.get(node_id)
        if tomb is not None and stamp <= tomb:
            return "skipped"
        added = self._add_stamps.get(node_id)
        if stamp[0] == 0:
            # Legacy lines keep their pre-shard meaning: a merge can place a removal
            # before its node's add, and the removal still wins once the node appears.
            if node_id not in self._graph:
                if node_id in self._removed_node_ids or added is not None:
                    return "skipped"
                return "deferred"
        elif added is not None and stamp < added[0] and node_id in self._graph:
            return "skipped"
        self._node_tombstones[node_id] = stamp
        self._removed_node_ids.add(node_id)
        if node_id not in self._graph:
            return "applied"
        self._unindex_node_refs(node_id)
        self._graph.remove_node(node_id)
        self._attr_stamps.pop(node_id, None)
        self._add_stamps.pop(node_id, None)
        return "applied"

    def _apply_edge(self, data: dict[str, Any], stamp: Stamp, *, present: bool) -> str:
        from_id = data["from_id"]
        to_id = data["to_id"]
        edge_type = data.get("edge_type")
        if not edge_type:
            return "skipped"
        key = (from_id, to_id, edge_type)
        known = self._edge_stamps.get(key)
        if known is not None and stamp <= known[0]:
            return "skipped"
        if present and (from_id not in self._graph or to_id not in self._graph):
            return "deferred"
        self._edge_stamps[key] = (stamp, present)
        if present:
            self._graph.add_edge(
                from_id,
                to_id,
                key=edge_type,
                type=edge_type,
                timestamp=data.get("timestamp", ""),
                # Historical provenance tag, NOT an active curator. Old journals
                # contain many edges sourced "curator"; the tag is left intact.
                source=data.get("source", "curator"),
                reason=data.get("reason"),
                curation_session=data.get("curation_session"),
            )
            return "applied"
        if (
            from_id in self._graph and to_id in self._graph
            and self._graph.has_edge(from_id, to_id, key=edge_type)
        ):
            self._graph.remove_edge(from_id, to_id, key=edge_type)
            return "applied"
        return "skipped"


def _complete_lines(data: bytes) -> list[str]:
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return []
    return data[: last_nl + 1].decode("utf-8", errors="replace").splitlines()


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _plugin_version() -> str | None:
    with contextlib.suppress(Exception):
        return code_version()
    return None
