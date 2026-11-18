"""MCP tools for the Cognition History Graph."""

import json
import logging
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastmcp import Context

from .. import harness
from ..cognition import (
    DEFAULT_MACHINE_CAP,
    SENIORITY_LEVELS,
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    CognitionStorage,
    conflict_details,
    conflict_flags,
    generate_node_id,
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
    freshness_by_rehash,
    gitignore_has_entry,
    read_text_sidecar,
    remove_gitignore_entry,
    sanitize_extension,
    sha256_bytes,
    sha256_file,
    write_blob,
    write_text_sidecar,
)
from ..cognition.identity import (
    is_valid_email,
    require_identity,
    resolve_identity,
)
from ..cognition.journal_shards import has_journal
from ..cognition.people_facts import fold_email
from ..cognition.prime import SEVERITY_ORDER, _node_email
from ..cognition.profiles import NO_MANAGER, PROFILE_FIELDS
from ..cognition.roster import Person, Roster
from ..cognition.scope import (
    SCOPE_KEY,
    SCOPE_PERSONAL,
    SCOPE_REVIEW_KEY,
    SCOPES,
    node_scope,
    recorded_by_email,
)
from ..cognition.svn_hygiene import add_new_files_in_background

# WP-TC16 re-export: keeps tests/test_task.py:32-37's direct
# `from ...cognition_tools import _task_claimed_at` path valid; the
# implementation itself lives in task_meta.py (shared with prime, which
# cannot import cognition_tools).
from ..cognition.task_meta import _task_claimed_at  # noqa: F401
from ..config import resolve_foreign_vectors_dir
from ..embeddings import EmbeddingGenerator, VectorStore, adaptive_vector_search
from ..running_version import code_version
from .project_registry import (
    LoadedProjects,
    ProjectEntry,
    compute_model_guard,
)
from .utils import get_lifespan

logger = logging.getLogger(__name__)


def _embeddings_ready(lc: dict[str, Any]) -> bool:
    """True when the embedding model is loaded and didn't error — the boolean form
    of require_embeddings (which returns a tool error dict). Used by the internal
    record/update paths to decide whether to embed inline or defer to the sync.

    WP-Wedge state contract (AC4): also requires ``embedding_generator`` to be
    installed — ready-set + no-error but generator still ``None`` is the
    watchdog-fired-but-not-yet-late-recovered tuple, and must read as not-ready
    here too (require_embeddings' sibling check), never as a false green light."""
    event = lc.get("embedding_ready")
    return bool(
        event
        and event.is_set()
        and not lc.get("embedding_error")
        and lc.get("embedding_generator") is not None
    )


def _embed_entity_node(
    embedding_storage: "VectorStore",
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
    Surface BOTH into the vector metadata dict AND the embed text (only when present) so
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
    # WP-TC6: surface from_agent (when stamped) into vector metadata so search hits
    # carry it too (get_node/list_tasks already surface it via the raw metadata dict).
    # "in" (not truthiness) -- False is a real, present value, distinct from missing.
    if "from_agent" in node.metadata:
        metadata["from_agent"] = node.metadata["from_agent"]
    embedding_storage.upsert_embedding(node.id, embedding, metadata)


def _embed_workflow(
    embedding_storage: "VectorStore",
    generator: "EmbeddingGenerator",
    node: CognitionNode,
) -> int:
    """Embed a workflow node: ONE node-level vector + full body chunked into
    ``<node_id>#chunk-N`` vectors (each carrying ``is_chunk: True``). Mirrors the
    document chunking path so long procedures remain fully searchable.

    The chunk set is replaced in one transaction (``replace_chunks``) after every
    chunk is embedded, so a re-chunk never orphans stale high-N chunks and a peer
    writing the same node can never interleave a half set.
    The node-level vector's metadata carries ``chunk_count`` (WP-4,
    41ced8d1fa63) so a completeness check (e.g. the startup reconciler) can
    verify the FULL chunk set landed, not just chunk-0 — a crash mid-loop
    used to read as "fully synced" once chunk-0 existed, permanently hiding
    chunks 1..N. Returns the chunk count written."""
    wf_type = CognitionNodeType.WORKFLOW.value
    node_text = f"{wf_type}: {node.summary}\n{node.detail[:2000]}"
    chunks = chunk_text(node.detail)
    wf_metadata: dict[str, Any] = {
        "entity_type": wf_type,
        "summary": node.summary,
        "author": node.author,
        "timestamp": node.timestamp,
        "context": ",".join(node.context),
        "chunk_count": len(chunks),
    }
    # WP-TC6 (see _embed_entity_node's matching comment): "in", not truthiness.
    if "from_agent" in node.metadata:
        wf_metadata["from_agent"] = node.metadata["from_agent"]
    node_vector = generator.generate(node_text, input_type="document")
    chunk_rows = [
        (
            f"{node.id}#chunk-{i}",
            generator.generate(chunk, input_type="document"),
            {"node_id": node.id, "entity_type": wf_type, "is_chunk": True},
            chunk,
        )
        for i, chunk in enumerate(chunks)
    ]
    embedding_storage.upsert_embedding(node.id, node_vector, wf_metadata)
    embedding_storage.replace_chunks(node.id, chunk_rows)
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


def _acting_identity(cognition_dir: Path) -> dict[str, str]:
    """Identity stamped on writes: confirmed file, else this checkout's VCS identity, else unresolved.

    Carries `source`/`confirmed` onto the stamp so a later audit can tell a
    human-confirmed identity from whatever happened to be in a shared machine's
    VCS config — the shared-build-account case that otherwise reproduces, one
    layer down, the anonymous-attribution bug this gate exists to close.
    Consumers reading only name/email are unaffected.
    """
    ident = resolve_identity(cognition_dir.parent, cognition_dir)
    return {
        "name": ident["name"],
        "email": ident["email"],
        "source": ident["source"],
        "confirmed": ident["confirmed"],
    }


def _identity_gate(cognition_dir: Path) -> dict[str, Any] | None:
    """The refusal payload for a READ path that defaults to "self" and found no email.

    Only reachable once the caller has already established that no email resolves,
    which is why the profile-completeness argument is omitted: a confirmed person
    with a half-filled profile must still be able to read their own facts. This
    does NOT return None merely because an email resolved -- require_identity also
    requires confirmation -- so it is not a usable "any identity" check. Every
    WRITE path uses _gated_identity instead.
    """
    return require_identity(cognition_dir.parent, cognition_dir)


def _gated_identity(
    storage: CognitionStorage,
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """(refusal_or_None, identity_to_stamp) for every graph WRITE.

    Two conditions, both required: the identity is CONFIRMED (an answered
    identity.json, not merely whatever sits in this machine's VCS config), and
    its committed profile carries all of name/email/role/seniority/reports_to.
    Why an unconfirmed git email is not enough: see cognition/identity.py.

    Resolved ONCE, through _acting_identity, and the same resolution serves both
    the decision and the stamp. Two independent reads can disagree if
    identity.json changes in between (a concurrent cognition_set_identity, a
    remap run, a second server process), letting a write pass the gate and then
    stamp a different -- possibly empty -- identity.
    """
    cognition_dir = storage.cognition_dir
    stamp = _acting_identity(cognition_dir)
    missing: list[str] | None = None
    removed = False
    if stamp.get("confirmed") and stamp.get("email"):
        email = str(stamp["email"])
        missing = storage.profile_missing_required(email)
        removed = email in storage.removed_profile_emails()
        if not missing and not removed:
            return None, stamp
    # Refusal path only: the stamp is discarded, so re-resolving here is harmless
    # and buys the richer payload (candidate suggestions) for the error.
    return (
        require_identity(cognition_dir.parent, cognition_dir, missing, removed),
        stamp,
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
    from_agent: bool = True,
    scope: str | None = None,
) -> dict[str, Any]:
    """Shared logic for cognition_record tool."""
    scope, scope_error = _resolve_record_scope(node_type, scope)
    if scope_error is not None:
        return scope_error
    lc = get_lifespan(ctx)
    storage: CognitionStorage = lc["cognition_storage"]
    embedding_storage: VectorStore = lc["cognition_embedding_storage"]

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

    # WP-P13n-1: server-stamped provenance — "whose machine/git identity recorded
    # this", distinct from `author` (who dictated the record, caller-provided free
    # text). Same resolve_git_identity used by _add_task's created_by (file-read
    # only, never subprocess — v0.12.1 P0).
    gate, recorded_by = _gated_identity(storage)
    if gate is not None:
        return gate
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
        metadata={
            "recorded_by": recorded_by,
            "from_agent": from_agent,
            **({SCOPE_KEY: scope} if scope else {}),
        },
    )
    # WP-ID: mint a collision-free id under the lock (global fix). Rebind node_id to
    # the returned id BEFORE the embedding upsert + edges + result — else a salted node
    # lands in the graph under the minted id while its vector lands under the stale id,
    # leaving it silently unsearchable (A1). Carry the minted id into the node copy so
    # the shared embed path upserts under the FINAL id.
    node_id = storage.add_node(node, mint_unique_id=True)
    node = node.model_copy(update={"id": node_id})

    # Embed and upsert to the vector store (skip if model not loaded yet — startup sync catches it later).
    # Workflows chunk the full body for searchability; all other types use a single node vector.
    # WP-Wedge (AC4): re-read the generator HERE, at the gate, rather than an early
    # capture at function entry — a watchdog-fired -> late-recovery sequence can
    # install the real generator between entry and this check, and an early-captured
    # local would still be the stale None, AttributeError-ing inside the embed call
    # even though _embeddings_ready just confirmed the generator is live.
    if _embeddings_ready(lc):
        generator: EmbeddingGenerator = lc["embedding_generator"]
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
    if scope:
        result["scope"] = scope
    if det_edges:
        result["deterministic_edges_created"] = det_edges
    if possible_duplicates:
        result["possible_duplicate_of"] = possible_duplicates
    return result


_SCOPE_GUIDANCE = (
    "personal = how THIS person works or wants agents to behave for them (their "
    "preferences, who authors what, tool/style choices); visible only to them. "
    "project = true for anyone in the repo (platform/API limits, build or ship "
    "rules, client requirements, security rules); visible to everyone."
)


def _resolve_record_scope(
    node_type: CognitionNodeType, scope: str | None
) -> tuple[str | None, dict[str, Any] | None]:
    if node_type != CognitionNodeType.CONSTRAINT:
        if scope is not None:
            return None, {"error": "scope applies only to constraint nodes; omit it for other types"}
        return None, None
    value = (scope or SCOPE_PERSONAL).strip().lower()
    if value not in SCOPES:
        return None, {"error": f"scope must be 'personal' or 'project', got {scope!r}. {_SCOPE_GUIDANCE}"}
    return value, None


#: The roster moved out of the graph into committed profiles, which carry no
#: vector. "person" is still a legal stored node type (pre-migration graphs have
#: them) but is no longer a searchable one, so the error names the tool that
#: replaces it rather than listing it as invalid vocabulary.
_UNSEARCHABLE_NODE_TYPES: dict[str, str] = {
    CognitionNodeType.PERSON.value: (
        "people are no longer searchable: the roster is committed profiles, not "
        "graph nodes, so there is no vector to match. Use cognition_list_people "
        "for the roster or cognition_get_person for one person."
    ),
}


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


def _parse_searchable_node_type(
    node_type: str | None,
) -> tuple[CognitionNodeType | None, dict[str, Any] | None]:
    """_parse_node_type, plus a clear refusal for a type that carries no vector."""
    if node_type is not None and node_type in _UNSEARCHABLE_NODE_TYPES:
        return None, {"error": _UNSEARCHABLE_NODE_TYPES[node_type]}
    return _parse_node_type(node_type)


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


_CURATION_TOKEN_KEY = "curation_sessions"


def _curation_refusal() -> dict[str, Any]:
    hz = harness.current()
    if hz.curation_available:
        how = (
            f"run {hz.skill_invoke('vibe-curate')}, which launches the curate-orchestrator; "
            "it calls cognition_begin_curation and passes curation_token on every write"
        )
    else:
        how = (
            f"background curation is not available on {hz.display_name} yet -- record only; "
            "the curator runs from Claude Code"
        )
    return {
        "error": (
            "curation token required: edge writes and curation marks belong to the "
            f"curate-orchestrator. To get edges created, {how}."
        )
    }


def _require_curation(
    lc: dict[str, Any], token: str | None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    sessions = lc.setdefault(_CURATION_TOKEN_KEY, {})
    if not token or token not in sessions:
        return None, _curation_refusal()
    return sessions[token], None


def _add_edge_core(
    storage: CognitionStorage,
    from_id: str,
    to_id: str,
    edge_type: str,
    reason: str | None = None,
    source: str = "manual",
    curation_session: str | None = None,
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
        source=source, reason=reason, curation_session=curation_session,
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


def _add_edges_batch_core(
    storage: CognitionStorage, edges: str, curation_session: str | None = None
) -> dict[str, Any]:
    """Validate + create a batch of edges (testable core of cognition_add_edges_batch).
    Every malformed input is skip-and-reported — no element can crash the batch after
    earlier edges were already committed (T-3)."""
    try:
        edge_list = json.loads(edges)
    except json.JSONDecodeError as e:
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
            reason=e.get("reason"), curation_session=curation_session,
        )
        if not storage.add_edge(edge):  # C-5: surface a failed add, don't count it created
            errors.append(f"[{i}] Not created: a node is missing ({fid} or {tid})")
            skipped += 1
            continue
        created += 1

    return {"created": created, "skipped": skipped, "errors": errors[:50]}


_MATCHED_EXCERPT_LEN = 500   # chars of chunk text returned as the match excerpt

# WP-TC10: exclude_people's never-wipe carve-out (same doctrine as TC9) — a
# constraint/incident hit is NEVER dropped by the filter, and passing one through
# does not count toward excluded_count (nothing was actually excluded).
_NEVER_EXCLUDED_SEARCH_TYPES = frozenset({
    CognitionNodeType.CONSTRAINT.value, CognitionNodeType.INCIDENT.value,
})


def _parse_exclude_people(exclude_people: str | None) -> frozenset[str]:
    """Comma-separated emails -> casefolded frozenset (WP-TC10). Blank entries and
    a blank/None param both collapse to the empty set (= no filter)."""
    if not exclude_people:
        return frozenset()
    return frozenset(
        email for raw in exclude_people.split(",") if (email := _casefold_email(raw))
    )


# WP-TC9: seniority + agent-origin weighting -- penalty-only, multiplicative from a
# 1.0 baseline (score itself is never mutated; weighted_score derives from it). A
# single named table, no config knob / env var (a product-ruled behavior, not a
# preference -- matches exclude_people's no-persistent-muting precedent). Every
# agent-attributed multiplier is strictly below every human-attributed one (human
# always outweighs agent, ruled) -- checked programmatically at test time, not just
# by these numbers.
_SENIORITY_MULTIPLIERS: dict[str, float] = {
    "owner": 1.0,
    "senior": 1.0,
    "mid": 0.95,
    "junior": 0.9,
}
_AGENT_MULTIPLIER = 0.85


def _person_seniority_map(storage: CognitionStorage) -> dict[str, str]:
    """Casefolded email -> seniority, from ONE roster snapshot (WP-TC9).

    Built ONCE per top-level search call by the caller (``_search_with_embedding``)
    and threaded down as a plain dict -- NEVER rebuilt inside
    ``_format_search_results``, which runs once per adaptive-widening round
    (rebuilding there would silently degrade the per-call cost model to per-round,
    the exact accidentally-quadratic shape C2 exists to catch). A read-only cache of
    data immutable for the call's duration -- contrast TC10's ``excluded_count``,
    which is per-round OUTPUT and must NOT be cached this way; this caches INPUTS,
    never outputs.

    C1 cross-version doctrine: anyone with no recorded seniority is simply absent
    here rather than raising -- readers degrade on unknown/absent vocab, never
    crash."""
    return storage.roster().seniority_map()


def _hit_weight(
    node_type: str | None,
    email: str,
    from_agent: bool | None,
    person_seniority: dict[str, str],
) -> dict[str, Any]:
    """WP-TC9: the ``{multiplier, seniority, from_agent, basis}`` weight for one
    search hit. Penalty-only -- ``multiplier`` is always in ``(0, 1.0]``, never a
    boost. Basis vocabulary mirrors the dashboard's EXACT unverified boundary
    (dashboard/api.py): a stamped identity with no matching person node is a KNOWN,
    unrostered author (``"human:unregistered"``), not ``"unverified"`` (no stamp at
    all) -- conflating the two is the improvisation the battle plan forbids.
    Precedence: exempt type > agent origin > stamp presence > person registration.

    C1 cross-version doctrine: an out-of-vocabulary seniority (a newer tier this
    build doesn't know, e.g. a future "principal") never crashes and never gets a
    penalty -- it maps to the neutral multiplier while the RAW seniority string is
    still surfaced verbatim in ``weight.seniority``/``basis`` (visible, not
    hidden)."""
    seniority = person_seniority.get(email)
    if node_type in _NEVER_EXCLUDED_SEARCH_TYPES:
        return {
            "multiplier": 1.0,
            "seniority": seniority,
            "from_agent": from_agent,
            "basis": f"exempt:{node_type}",
        }
    if from_agent:
        return {
            "multiplier": _AGENT_MULTIPLIER,
            "seniority": seniority,
            "from_agent": from_agent,
            "basis": "agent",
        }
    if not email:
        return {
            "multiplier": 1.0,
            "seniority": None,
            "from_agent": from_agent,
            "basis": "unverified",
        }
    if seniority is not None:
        return {
            "multiplier": _SENIORITY_MULTIPLIERS.get(seniority, 1.0),
            "seniority": seniority,
            "from_agent": from_agent,
            "basis": f"human:{seniority}",
        }
    return {
        "multiplier": 1.0,
        "seniority": None,
        "from_agent": from_agent,
        "basis": "human:unregistered",
    }


def _format_search_results(
    results: list[dict[str, Any]],
    storage: CognitionStorage,
    limit: int,
    exclude_emails: frozenset[str] = frozenset(),
    person_seniority: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Dedupe over-queried hits to the BEST hit per node, dropping graph-absent
    nodes, and carry a ``matched_excerpt`` for chunk hits — the N1 fix + D2 dedupe.
    WP-TC10 additionally applies the optional per-author exclusion filter here.

    WP-TC9: also computes each hit's ``weight``/``weighted_score`` (see
    ``_hit_weight``) and sorts the returned list by ``weighted_score`` descending
    (stable -- ties keep vector-search order) so the caller's ``[:limit]`` slice
    takes the weighted top, not the raw-score top. ``person_seniority`` is a plain
    dict built ONCE by the caller (``_search_with_embedding``'s enclosing scope) --
    this function never queries person nodes itself; ``None`` (no map given) treats
    every hit as unregistered, same as an empty dict.

    N1 (§9): a cross-process remove_node replays into the graph but never un-embeds,
    so the vector store serves hits for nodes deleted on another machine — escalated by
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

    WP-TC10: fetches the full node (``storage.get_node``, unconditional — the N1
    membership check above is presence-only, it never returns node data) so the
    exclusion filter can match the SAME server-resolved identity stamp personalization
    uses (``prime._node_email``: ``recorded_by.email`` / task ``created_by.email`` —
    never the free-text ``author`` field, never an unstamped pre-P13n node). A
    constraint/incident hit is NEVER excluded (``_NEVER_EXCLUDED_SEARCH_TYPES``) and
    passing one through does not increment ``excluded_count``.

    WP-SearchFlags: each entry also carries ``conflicted``/``superseded_by``
    (see ``cognition.queries.conflict_flags`` for the semantics) — computed here,
    once, in this per-result loop, not a second pass. TC2 (888a21f729dd): a
    conflicted entry additionally carries ``conflicted_with`` — the list of
    counterparty decisions naming id/summary/author/reason (see
    ``cognition.queries.conflict_details``) — so the caller doesn't have to
    chase the bare boolean down themselves.

    Returns ``(all live deduped (post-exclusion) hits for this round, excluded_count)``
    — the list is NOT capped to ``limit`` (the caller, ``adaptive_vector_search``,
    owns both the limit-slice and the widen-vs-stop decision; capping here would
    make its ``total_found``/exhaustive accounting wrong)."""
    person_seniority = person_seniority or {}
    formatted: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    excluded_count = 0
    for r in results:
        raw_id = r.get("_id") or ""
        if not storage.search_hit_is_live(raw_id):  # N1 drop (shared predicate)
            continue
        node_id = raw_id.split("#chunk-")[0]
        if node_id in seen_nodes:  # keep only the best (first) hit per node
            continue
        seen_nodes.add(node_id)
        node = storage.get_node(node_id)  # cheap: in-memory graph lookup
        if node is None:  # vanished between the live-check and here (race) — same drop as N1
            continue
        node_type = r.get("entity_type")
        email = _node_email(node)  # WP-TC9: reused below for weighting, not re-derived
        if (
            exclude_emails
            and node_type not in _NEVER_EXCLUDED_SEARCH_TYPES
            and email in exclude_emails
        ):
            excluded_count += 1
            continue
        from_agent = r.get("from_agent")
        score = r.get("score") or 0.0
        weight = _hit_weight(node_type, email, from_agent, person_seniority)
        # WP-SearchFlags: computed via the single shared implementation
        # (cognition.queries.conflict_flags, also used by the dashboard) so
        # search and the dashboard's ⚠ agree on what counts as a conflict. Costs
        # up to 3 edge lookups per formatted hit (post-dedupe, so <= 3*limit per
        # search round) -- negligible on the in-memory graph. H3 (peer-review):
        # cognition_get_workflow's internal top-1 match also flows through this
        # function via _search_with_embedding and discards everything but the
        # id -- these two lookups are wasted for that caller, accepted (not
        # worth a special-case bypass).
        conflicted, superseded_by = conflict_flags(storage, node_id)
        entry: dict[str, Any] = {
            "id": node_id,  # the NODE id, never the chunk id
            "node_type": node_type,
            "summary": r.get("summary") or r.get("name"),
            "author": r.get("author"),
            "timestamp": r.get("timestamp"),
            "severity": r.get("severity"),
            "context": r.get("context", ""),
            "score": r.get("score"),
            # WP-TC6: present (True/False) when the writer stamped it; None for
            # every pre-TC6 node -- NEVER coerced to False (that would misrepresent
            # unknown provenance as "known agent-written").
            "from_agent": from_agent,
            # WP-TC9: never silent (ruled) -- present on every hit, including
            # neutral ones. `score` above stays the raw similarity, untouched.
            "weight": weight,
            "weighted_score": score * weight["multiplier"],
            # WP-SearchFlags: ALWAYS present (false/null on a clean hit), same
            # "never silent" doctrine as `weight` above -- a missing key must
            # never be readable as "checked and clean". NO ranking change: these
            # flags do not affect weighted_score or sort order (pinned decision).
            "conflicted": conflicted,
            "superseded_by": superseded_by,
        }
        # Task 888a21f729dd (TC2): NAME the counterparty when conflicted -- a bare
        # boolean makes the caller go fetch the conflicting decision themselves.
        # Only computed for conflicted hits (a clean hit never pays this cost).
        if conflicted:
            entry["conflicted_with"] = conflict_details(storage, node_id)
        if node_type == CognitionNodeType.CONSTRAINT.value:
            entry["scope"] = node_scope(node)
        if node_type == CognitionNodeType.DOCUMENT.value:
            staleness = cheap_staleness_signal(node.get("metadata", {}))
            if staleness:
                entry["staleness"] = staleness
        matched = r.get("matched_text")
        if matched:
            entry["matched_excerpt"] = matched[:_MATCHED_EXCERPT_LEN]
        formatted.append(entry)
    # WP-TC9: stable sort -- ties (equal weighted_score) keep vector-search order.
    formatted.sort(key=lambda e: e["weighted_score"], reverse=True)
    return formatted, excluded_count


def _store_unavailable_reason(store: Any) -> str | None:
    """``vector-store-unavailable`` / ``newer-schema`` when the store cannot serve a
    search, so an empty result never reads as "no history"."""
    state = getattr(store, "state", None)
    if not isinstance(state, str) or state == "ok":
        return None
    return "newer-schema" if state == "newer-schema" else "vector-store-unavailable"


def _store_last_error(store: Any) -> str | None:
    last_error = getattr(store, "last_error", None)
    if not callable(last_error):
        return None
    error = last_error()
    return error if isinstance(error, str) and error else None


def _index_syncing(lc: dict[str, Any]) -> bool:
    """True while the start-up (re)build of the vector index is still running."""
    done = lc.get("embedding_sync_done")
    return bool(done is not None and hasattr(done, "is_set") and not done.is_set()
                and not lc.get("embedding_error"))


_INDEX_COVERAGE_SLACK = 0.02


def _index_coverage(storage: CognitionStorage, store: Any) -> dict[str, Any] | None:
    """``{index_incomplete, indexed_nodes, graph_nodes}`` when a project's index is missing
    more than 2% of its graph nodes (and more than one); None when complete or uncountable."""
    if store is None:
        return None
    nodes = storage.get_statistics().get("nodes", 0)
    if not isinstance(nodes, int) or nodes <= 0:
        return None
    indexed = store.count_documents({"is_chunk": False})
    if _store_last_error(store) or nodes - indexed <= max(1, nodes * _INDEX_COVERAGE_SLACK):
        return None
    return {"index_incomplete": True, "indexed_nodes": indexed, "graph_nodes": nodes}


def _reembed_replayed_nodes(
    storage: CognitionStorage,
    embedding_storage: VectorStore,
    generator: EmbeddingGenerator,
    index_syncing: bool = False,
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

    Filters to ids ACTUALLY missing from the vector store (one batched existence check)
    before embedding anything, so redundant catch-up of this process's OWN
    just-written nodes doesn't cost a model call on every search. Never
    raises — a per-node failure is logged and skipped so it can't block the
    search that triggered this or take down the rest of the batch.
    """
    if index_syncing:
        return 0
    pending = storage.pop_replayed_node_ids()
    if not pending:
        return 0

    existing = embedding_storage.existing_ids(pending)
    if _store_last_error(embedding_storage):
        _requeue_replayed(storage, pending)
        return 0
    missing = [nid for nid in pending if nid not in existing]
    if not missing:
        return 0
    if len(missing) > _INLINE_REEMBED_CAP:
        _requeue_replayed(storage, missing[_INLINE_REEMBED_CAP:])
        request = getattr(embedding_storage, "request_resync", None)
        if callable(request):
            request()
        missing = missing[:_INLINE_REEMBED_CAP]

    doc_type = CognitionNodeType.DOCUMENT.value
    embedded = 0
    for index, node_id in enumerate(missing):
        data = storage.get_node(node_id)
        if data is None or data.get("type") == doc_type:
            continue
        try:
            node = _node_from_dict(node_id, data)
            if node.type == CognitionNodeType.WORKFLOW:
                _embed_workflow(embedding_storage, generator, node)
            else:
                _embed_entity_node(embedding_storage, generator, node)
        except Exception as e:
            logger.warning(f"re-embed-on-replay: failed for {node_id}: {e}")
            continue
        if _store_last_error(embedding_storage):
            _requeue_replayed(storage, missing[index:])
            break
        embedded += 1
    return embedded


_INLINE_REEMBED_CAP = 10


def _requeue_replayed(storage: Any, node_ids: list[str]) -> None:
    requeue = getattr(storage, "requeue_replayed_node_ids", None)
    if callable(requeue):
        requeue(node_ids)


def _search_with_embedding(
    storage: CognitionStorage,
    embedding_storage: VectorStore,
    query_embedding: list[float],
    node_type: str | None,
    limit: int,
    exclude_emails: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Inner search: takes a pre-computed embedding, returns the adaptive_vector_search
    envelope (``results``/``total_found``/``exhaustive``/``excluded_count`` — WP-TC10).

    Extracted so multi-project search can embed the query ONCE and fan over entries
    without re-embedding per project. ``storage`` must be the storage for THIS
    ``embedding_storage`` — the N1 ghost-filter (search_hit_is_live) in
    _format_search_results uses it to check node presence; passing mismatched
    storage silently drops all hits from the foreign graph (XP2 correctness trap).

    WP-TC9: builds THIS call's person-seniority map exactly ONCE, here in the
    enclosing scope (never inside the dedupe lambda or _format_search_results
    itself, which runs once per adaptive-widening round) — every widening round of
    the same call reuses the same dict via closure. A multi-project fan-out calls
    this function once per entry, so each entry naturally gets its OWN map built
    from its OWN storage (per-entry registries, by construction — no special-casing
    needed here).
    """
    person_seniority = _person_seniority_map(storage)
    return adaptive_vector_search(
        embedding_storage,
        query_embedding,
        entity_type=node_type,
        limit=limit,
        dedupe=lambda results, lim: _format_search_results(
            results, storage, lim, exclude_emails, person_seniority
        ),
    )


def _search_cognition(
    storage: CognitionStorage,
    embedding_storage: VectorStore,
    generator: EmbeddingGenerator,
    query: str,
    node_type: str | None = None,
    limit: int = 10,
    exclude_people: str | None = None,
) -> dict[str, Any]:
    """Semantic search core (testable; cognition_search is the thin ctx wrapper).

    Embeds the query, ADAPTIVELY over-queries the vector store, then dedupes-to-best-hit-per-
    node + drops graph-absent ghosts (N1) via _format_search_results. Keeping
    vector_search + the filter together here lets the cross-process ghost test
    exercise the REAL search path end-to-end.

    Adaptive over-query (peer-review B3): a FIXED limit×k cannot guarantee `limit`
    distinct nodes — a single document can yield more than limit×k chunks and starve
    other live nodes. So widen n_results (doubling) until we have `limit` distinct
    live nodes, OR the store is exhausted (fewer hits than asked), OR a cap is hit.
    Doubling keeps round-trips logarithmic. The single-document-with->cap-chunks case
    is the only residual (capped), and only degrades recall (never serves a wrong or
    deleted node — the dedupe + N1 filter are exact).

    WP-TC10: `exclude_people` (comma-separated emails) drops hits authored by those
    identities (constraints/incidents exempt — see _format_search_results) BEFORE the
    limit-fill, so adaptive widening naturally refills excluded slots. `total_found`/
    `exhaustive` (M4) are always present; `excluded_count`/`excluded_for` are present
    only when the param was passed AND something was actually excluded."""
    limit = min(limit, 50)
    exclude_emails = _parse_exclude_people(exclude_people)
    query_embedding = generator.generate_query_embedding(query)
    search = _search_with_embedding(
        storage, embedding_storage, query_embedding, node_type, limit, exclude_emails
    )
    result: dict[str, Any] = {
        "query": query,
        "results": search["results"],
        "count": len(search["results"]),
        "total_found": search["total_found"],
        "exhaustive": search["exhaustive"],
    }
    if exclude_emails and search["excluded_count"] > 0:
        result["excluded_count"] = search["excluded_count"]
        result["excluded_for"] = sorted(exclude_emails)
    return result


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
        warnings.append(f"size {size}B >= 50MB: auto local_only (exceeds common hosting push limits)")

    status: dict[str, Any] = {}
    already_committed = eff_local and blob_existed and not was_ignored
    if eff_local and already_committed:
        # Already on the default-commit track. A .gitignore line here would be
        # INERT (git keeps tracking an already-tracked file), so don't write a
        # misleading one — just warn that local_only can't retroactively un-publish.
        status["already_committed"] = True
        warnings.append(
            "blob is on the default-commit track; local_only cannot retroactively "
            "un-publish a committed blob (version-control history retains it)"
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
    embedding_storage: VectorStore,
    generator: EmbeddingGenerator,
    node_id: str,
    summary: str,
    detail: str,
    sidecar_text: str,
    from_agent: bool | None = None,
) -> int:
    """Embed a document: ONE node-level vector (so it shows in node search) + its
    sidecar text chunked into ``<node_id>#chunk-N`` vectors (each carrying its chunk
    text + ``is_chunk: True``). THE single chunk-write path — store-time (D2 c3) and
    re-sync (c4) both call it, so the chunk-metadata contract can't drift.

    The chunk set is replaced in one transaction (``replace_chunks``, peer-review A5)
    after every chunk is embedded, so a re-chunk that yields FEWER chunks can't orphan
    stale high-N chunks under the live node_id (which would surface as ghost excerpts
    of deleted text), and a peer can never interleave a half set. The node-level vector's metadata carries ``chunk_count`` (WP-4,
    41ced8d1fa63) so a completeness check can verify the FULL chunk set landed,
    not just chunk-0 — a crash mid-loop used to read as "fully synced" once
    chunk-0 existed, permanently hiding chunks 1..N. Returns the chunk count
    written."""
    doc_type = CognitionNodeType.DOCUMENT.value
    node_text = f"{doc_type}: {summary}\n{detail}"
    chunks = chunk_text(sidecar_text)
    doc_metadata: dict[str, Any] = {
        "entity_type": doc_type, "summary": summary, "chunk_count": len(chunks),
    }
    # WP-TC6: "is not None", not truthiness -- False is a real, present value.
    if from_agent is not None:
        doc_metadata["from_agent"] = from_agent
    node_vector = generator.generate(node_text, input_type="document")
    chunk_rows = [
        (
            f"{node_id}#chunk-{i}",
            generator.generate(chunk, input_type="document"),
            {"node_id": node_id, "entity_type": doc_type, "is_chunk": True},
            chunk,
        )
        for i, chunk in enumerate(chunks)
    ]
    embedding_storage.upsert_embedding(node_id, node_vector, doc_metadata)
    embedding_storage.replace_chunks(node_id, chunk_rows)
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
    from_agent: bool = True,
    embedding_storage: "VectorStore | None" = None,
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
                add_new_files_in_background(cognition_dir)
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
    # Task 2858ae93bf17: documents previously never stamped recorded_by, unlike every
    # other write path (_record_node, _add_task, _register_person) — same
    # resolve_git_identity, same verbatim shape. This is the live-path fix only; it does
    # NOT touch the dedup ("already_stored") branch above, which returns an existing node
    # as-is — backfilling legacy documents is legacy-identity-backfill's job (962ab7b442d5),
    # scoped OUT of that WP until this ships (v2 there, not here).
    gate, recorded_by = _gated_identity(storage)
    if gate is not None:
        return gate
    metadata: dict[str, Any] = {
        "filename": filename,
        "mime": mime or "",
        "size": size,
        "sha256": sha,
        "mode": mode,
        "indexed_text_chars": indexed_chars,
        "from_agent": from_agent,
        "recorded_by": recorded_by,
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
                        document_text[:2000], document_text, from_agent=from_agent)

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
    add_new_files_in_background(cognition_dir)
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
    # file; that cost scales with the referenced document's size.) WP-DashV2:
    # relocated to documents.freshness_by_rehash (single-implementation
    # doctrine) — same behavior, no-path still defaults to "unchanged" here.
    freshness = freshness_by_rehash(meta)
    path = meta.get("path")

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
    embedding_storage: "VectorStore",
    generator: "EmbeddingGenerator | None",
    *,
    node_id: str,
    embeddings_ready: bool,
    summary: str | None = None,
    detail: str | None = None,
    context: str | None = None,
    severity: str | None = None,
    scope: str | None = None,
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
                "node with the full updated procedure, then run /vibe-curate (the only "
                "path that writes the `supersedes` edge to this one); do not edit in "
                "place."
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
    if scope is not None:
        scope_update = _scope_update(storage, existing, scope)
        if "error" in scope_update:
            return scope_update
        updates.update(scope_update)

    if not updates:
        return {"error": "No updatable fields provided (summary, detail, context, severity, scope)"}

    storage.update_node(node_id, **updates)

    # Re-embed on ANY whitelisted change. summary/detail change the searchable VECTOR;
    # context/severity don't, but they ARE stored in the vector metadata that
    # _format_search_results surfaces in every hit — so a context/severity-only edit
    # would otherwise leave search results DISPLAYING the old values (the same silent
    # search-staleness this tool exists to kill). For such an edit _embed_entity_node
    # regenerates an identical vector (the embed text is unchanged) but refreshes the
    # metadata via the same upsert — negligible cost on a rare path. If the model isn't
    # ready, defer (the vector/metadata stay stale until a future re-embed — rare, an
    # edit needs a loaded model anyway).
    if embeddings_ready:
        assert generator is not None  # callers pass a generator whenever ready=True
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


def _scope_update(
    storage: CognitionStorage, existing: dict[str, Any] | None, scope: str
) -> dict[str, Any]:
    if not existing or existing.get("type") != CognitionNodeType.CONSTRAINT.value:
        return {"error": "scope applies only to constraint nodes"}
    value = scope.strip().lower()
    if value not in SCOPES:
        return {"error": f"scope must be 'personal' or 'project', got {scope!r}. {_SCOPE_GUIDANCE}"}
    owner = recorded_by_email(existing)
    if not owner:
        return {
            "error": "this constraint has no recorded owner (it predates identity "
                     "stamping), so it stays project scope"
        }
    if owner != storage.viewer_email():
        return {"error": "only the person who recorded this constraint can change its scope"}
    return {
        "metadata": {**(existing.get("metadata") or {}), SCOPE_KEY: value},
        SCOPE_REVIEW_KEY: None,
    }


_SCOPE_REVIEW_REASON_MAX = 300


def _flag_constraint_scope(
    storage: CognitionStorage,
    node_id: str,
    suggested_scope: str,
    reason: str,
    curation_session: str,
) -> dict[str, Any]:
    node = storage.get_node(node_id)
    if node is None:
        return {"error": f"Node '{node_id}' does not exist"}
    if node.get("type") != CognitionNodeType.CONSTRAINT.value:
        return {"error": "only constraint nodes can be flagged for a scope review"}
    suggested = (suggested_scope or "").strip().lower()
    if suggested not in SCOPES:
        return {"error": f"suggested_scope must be 'personal' or 'project', got {suggested_scope!r}"}
    current = node_scope(node)
    if suggested == current:
        return {"error": f"this constraint is already {current}; flag only a suggested change"}
    owner = recorded_by_email(node)
    if not owner:
        return {"error": "this constraint has no recorded owner, so its scope cannot change; do not flag it"}
    if storage.get_predecessors(node_id, CognitionEdgeType.SUPERSEDES):
        return {"error": "this constraint has been superseded; review its current version instead"}
    reason = " ".join((reason or "").split())
    if not reason:
        return {"error": "reason is required: one sentence on why the other scope fits better"}
    review = {
        "suggested": suggested,
        "reason": reason[:_SCOPE_REVIEW_REASON_MAX],
        "flagged_at": datetime.now(UTC).isoformat(),
        "curation_session": curation_session,
    }
    storage.update_node(node_id, **{SCOPE_REVIEW_KEY: review})
    return {
        "flagged": True,
        "node_id": node_id,
        "current_scope": current,
        "suggested_scope": suggested,
        "owner_is_you": owner == storage.viewer_email(),
    }


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


def _task_closer(transitions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The LATEST transitions entry that closed the task (status done/cancelled),
    last-wins scan (mirrors _task_claimed_at). None when no closing entry exists
    (legacy/hand-built journal) -- the closer cannot be attributed (WP-TC4 design 3)."""
    closer = None
    for tr in transitions:
        if tr.get("status") in ("done", "cancelled"):
            closer = tr
    return closer


def _add_task(
    ctx: Context,
    summary: str,
    detail: str,
    context: str,
    priority: str = "normal",
    owner: str | None = None,
    parent_id: str | None = None,
    references: str | None = None,
    from_agent: bool = True,
    assigned_to_email: str | None = None,
) -> dict[str, Any]:
    """Create a task node (testable core of cognition_add_task).

    Resolves the git identity SERVER-SIDE (no client ``created_by`` param), seeds the
    lifecycle ``metadata`` (status=open, created_by, owner, parent_id, initial
    transition), mints a collision-free id, creates the explicit child→parent
    ``part_of`` edge when ``parent_id`` is given, and embeds (status/owner surfaced via
    the shared _embed_entity_node path). ``priority`` is stored as the node's
    ``severity`` so it sorts via SEVERITY_ORDER for free.

    ``assigned_to_email`` (WP-TC8): blank/whitespace-only at CREATION time means
    NOT PROVIDED — seeds nothing (never stores ``""``, unlike ``owner``'s raw-store
    convention). A non-blank value casefolds, seeds ``metadata.assigned_to`` AND
    the first ``metadata.assignments`` audit entry in one shot."""
    if priority not in _TASK_PRIORITIES:
        return {"error": f"Invalid priority '{priority}'. Valid: {list(_TASK_PRIORITIES)}"}

    lc = get_lifespan(ctx)
    storage: CognitionStorage = lc["cognition_storage"]
    embedding_storage: VectorStore = lc["cognition_embedding_storage"]

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
    gate, created_by = _gated_identity(storage)
    if gate is not None:
        return gate

    metadata: dict[str, Any] = {
        "status": "open",
        "created_by": created_by,
        "owner": owner,
        "parent_id": parent_id,
        "transitions": [{"status": "open", "at": timestamp, "by": created_by}],
        "from_agent": from_agent,
    }

    # WP-TC8: creation-time assignment. Blank/whitespace == not provided — seed
    # nothing (contrast with `owner` above, which stores the raw value including "").
    assigned_to = _casefold_email(assigned_to_email) if assigned_to_email else ""
    if assigned_to:
        metadata["assigned_to"] = assigned_to
        metadata["assignments"] = [{"to": assigned_to, "at": timestamp, "by": created_by}]

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
    # WP-Wedge (AC4): read the generator fresh HERE, at the gate — see _record_node's
    # matching comment for why an early capture at function entry is unsafe.
    if _embeddings_ready(lc):
        generator: EmbeddingGenerator = lc["embedding_generator"]
        _embed_entity_node(embedding_storage, generator, node)

    return _get_node(storage, node_id)


def _list_tasks(
    storage: CognitionStorage,
    status: str | None = None,
    priority: str | None = None,
    owner: str | None = None,
    parent_id: str | None = None,
    include_done: bool = False,
    exclude_people: str | None = None,
) -> dict[str, Any]:
    """List tasks filtered + sorted (testable core of cognition_list_tasks).

    Home-project only. Sorts by priority (SEVERITY_ORDER) then recency (newest first).
    Default EXCLUDES done/cancelled. Each row carries a ``depth`` = the number of present
    ancestors in the visible result set (so the hierarchy reads as a tree); a child whose
    parent is filtered out or deleted falls back to depth 0 (shown ungrouped, F10).

    WP-TC10: ``exclude_people`` (comma-separated emails, casefolded) drops tasks whose
    ``created_by.email`` matches — the task identity stamp, same ``prime._node_email``
    match used by cognition_search (never the free-text ``owner``; an unstamped task is
    never excluded). Applied BEFORE the depth/visible computation, so an excluded
    task's children fall back to depth 0 exactly like any other filtered-out parent
    (F10 — no special-casing needed).

    Gate B-final finding (task 8c7bab562c37): rows also carry ``claimed_by`` (the
    server-resolved identity dict from the task's live claim, or None if never claimed)
    and ``claimed_at`` (the ``at`` of the latest ->in_progress transition, via
    ``_task_claimed_at`` — same single implementation the dashboard and prime's manager
    rollup use). This gives a read-only way to see who holds a claim before attempting
    to take it over, instead of discovering it by collision on cognition_update_task."""
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

    exclude_emails = _parse_exclude_people(exclude_people)
    excluded_count = 0
    if exclude_emails:
        kept: list[dict[str, Any]] = []
        for t in filtered:
            if _node_email(t) in exclude_emails:
                excluded_count += 1
                continue
            kept.append(t)
        filtered = kept

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
            "claimed_by": meta.get("claimed_by"),
            "timestamp": t.get("timestamp"),
            "depth": _depth(t),
            "from_agent": meta.get("from_agent"),
            "assigned_to": meta.get("assigned_to"),
            "claimed_at": _task_claimed_at(meta.get("transitions") or []),
        })
    result: dict[str, Any] = {"tasks": rows, "count": len(rows)}
    if exclude_emails and excluded_count > 0:
        result["excluded_count"] = excluded_count
        result["excluded_for"] = sorted(exclude_emails)
    return result


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
    embedding_storage: "VectorStore",
    generator: "EmbeddingGenerator | None",
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
    assigned_to_email: str | None = None,
) -> dict[str, Any]:
    """Edit a task's lifecycle + narrative in place (testable core of cognition_update_task).

    This is the ONLY path to ``status``/``owner``/``parent_id``/``assigned_to``/transition
    edits: ``cognition_update_node`` physically cannot write ``metadata`` (its whitelist is
    summary/detail/context/severity), so there is no competing path. Contract:
      - reject a non-task id (use cognition_update_node for other types);
      - validate the status transition against _TASK_TRANSITIONS;
      - READ-MODIFY-WRITE the WHOLE metadata dict (storage.update_node replaces fields
        wholesale, F8): set status, APPEND {status, at, by(git-resolved)} to transitions,
        set owner, re-parent, set/clear assignment;
      - map priority→severity and summary/detail to top-level fields via storage.update_node;
      - RE-EMBED ONCE explicitly via _embed_entity_node on the fresh node (NOT via
        _update_node) so search/metadata reflect the new lifecycle, deferred if the model
        isn't ready.

    ``assigned_to_email`` (WP-TC8): ``None`` = no change; ``""`` (or whitespace) unassigns;
    a non-blank value sets/reassigns. Casefolded, then compared against the CURRENT
    ``metadata.assigned_to`` (treating absent as ``""``) BEFORE appending — a same-email
    resubmission is a no-op that appends NOTHING to ``metadata.assignments``, modeled on
    ``_update_person``'s compare-before-append ``reports_to_email`` handling, not on the
    `owner` block just above (which unconditionally sets ``metadata_changed`` on any
    non-None value, including a same-value resubmission — the wrong template for an
    audited field). An effective change appends exactly one ``{to, at, by}`` entry
    (``to=""`` for an explicit unassign) and, since ``metadata.assigned_to`` must stay
    ABSENT rather than ``""`` when unassigned (mirrors the seed-nothing rule in
    ``_add_task``), pops the key on unassign rather than storing an empty string.

    claimed_by (WP-P13n-1) strictly mirrors the ``by`` of the last ACTUAL ->in_progress
    transition — a call that passes ``status="in_progress"`` while the task is ALREADY
    in_progress is a same-status no-op (the transition branch below is skipped entirely,
    same as it always was for the transitions log), so it never re-stamps claimed_by even
    when combined with another field that DOES apply (e.g. owner=/priority=) UNLESS it is
    a same-status TAKEOVER with a note (WP-TC4 2b, below) — the one same-status shape that
    does restamp. A takeover of claimed_by otherwise requires a real transition
    (blocked/open -> in_progress); this is deliberate, not an oversight.

    Claim collisions (WP-TC4) never block, except one unambiguous case. A foreign LIVE
    claim (``claimed_by`` present, current status in_progress/blocked; open/done/cancelled
    = released, not live) being displaced or poked always SUCCEEDS and may carry a
    ``claim_warning`` in the response:
      - blocked -> in_progress over a live foreign claim WITHOUT ``note``: the one
        enforced exception — rejects with an error naming the claimant + claim age
        (retry with ``note=`` to take over); WITH ``note`` — proceeds, restamps, and the
        response carries ``claim_warning`` kind ``"claim_collision"``.
      - ``status="in_progress"`` on an already-in_progress task held by a foreign live
        claimant WITH ``note`` — a single-call takeover: restamps + logs the seizure +
        ``claim_warning`` kind ``"claim_collision"``.
      - same shape WITHOUT ``note`` (including a bare ``status="in_progress"`` poke,
        which bypasses the "no updatable fields" error for this one shape) — no seizure,
        no restamp, no error, ``claim_warning`` kind ``"takeover_note_required"``.
      - done/cancelled -> open (reopen) where the closing transition's author is a
        different verified identity — ``claim_warning`` kind ``"reopen"``; suppressed
        when no closing transitions entry exists to attribute.
    ``claim_warning`` shape: ``{"kind", "claimant": {"name", "email"}, "claimed_at",
    "assigned_to" (only when set), "message"}``. Key is ABSENT for self-actions, released
    claims, unverifiable identity (either side's email blank casefolds to unmatched), and
    fresh claims — never present with a false/empty value."""
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
    current_status = metadata.get("status", "open")
    claim_warning: dict[str, Any] | None = None

    # WP-TC4 (2-MECH #1): resolve the caller identity ONCE, early -- was previously
    # resolved inside the transition body below; that call is now dropped in favor of
    # this hoisted result, which also feeds takeover detection.
    gate, caller = _gated_identity(storage)
    if gate is not None:
        return gate
    caller_email = _casefold_email(caller.get("email", ""))

    # Snapshot the PRIOR claim before any mutation (rev-2 finding #8: metadata mutates
    # in place below) -- every claim_warning reports this snapshot, never post-mutation
    # state.
    prior_claimed_by = metadata.get("claimed_by")
    prior_claimant_email = _casefold_email((prior_claimed_by or {}).get("email", ""))
    prior_transitions = metadata.get("transitions", [])
    # Unverifiable identity (either side's email empty) disengages ALL claim machinery.
    identity_verifiable = bool(caller_email) and bool(prior_claimant_email)
    foreign_claimant = identity_verifiable and prior_claimant_email != caller_email

    # WP-TC4 (2-MECH #2): same-status takeover shape -- status="in_progress" on an
    # already-in_progress task held by a verified foreign claimant. Detected before the
    # note guard so the guard's own carve-out (below) can see it.
    same_status_takeover_shape = (
        status == "in_progress" and current_status == "in_progress" and foreign_claimant
    )

    # `note` annotates a status transition — reject it (BEFORE any mutation) when no
    # transition will occur (no status, or status == current), so it can't be silently
    # dropped. A same-status TAKEOVER (2b) is the one exception (2-MECH #3): its note
    # annotates the takeover's own new transitions entry, appended by the branch below
    # rather than this one -- the takeover IS the status event.
    if (
        note is not None
        and (status is None or status == current_status)
        and not same_status_takeover_shape
    ):
        return {
            "error": (
                "note annotates a status change — pass a new (different) status, or omit note"
            )
        }

    # --- status transition ---
    if status is not None:
        if status not in _TASK_STATUSES:
            return {"error": f"Invalid status '{status}'. Valid: {list(_TASK_STATUSES)}"}
        if status != current_status:
            allowed = _TASK_TRANSITIONS.get(current_status, frozenset())
            if status not in allowed:
                return {
                    "error": (
                        f"Illegal status transition {current_status!r} -> {status!r}. "
                        f"Allowed from {current_status!r}: {sorted(allowed)}"
                    )
                }

            # WP-TC4 (2a, 2-MECH #6): blocked -> in_progress over a LIVE foreign claim
            # (an open task's claim is not live by definition) is an unambiguous act of
            # claim-taking -- requires a transition note, validated BEFORE mutation. A
            # retry with note= always succeeds.
            live_claim = (
                current_status == "blocked" and status == "in_progress" and foreign_claimant
            )
            if live_claim and not note:
                assert prior_claimed_by is not None  # live_claim implies foreign_claimant
                return {
                    "error": (
                        f"Task is claimed by {prior_claimed_by['name']} "
                        f"<{prior_claimed_by['email']}> "
                        f"(claimed_at={_task_claimed_at(prior_transitions)!r}) — "
                        "pass note= to take over"
                    )
                }

            entry: dict[str, Any] = {
                "status": status,
                "at": datetime.now(UTC).isoformat(),
                "by": caller,
            }
            if note:
                entry["note"] = note
            metadata["status"] = status
            metadata["transitions"] = [*metadata.get("transitions", []), entry]
            # WP-P13n-1: claiming a task (-> in_progress) stamps who claimed it,
            # server-resolved (same identity as the transition's `by`, no second
            # resolve). Re-claiming (blocked/open -> in_progress again) re-stamps;
            # every other transition leaves claimed_by untouched.
            if status == "in_progress":
                if live_claim:
                    # Reached only with a note (the no-note case returned above) --
                    # proceeded over a live foreign claim.
                    assert prior_claimed_by is not None  # live_claim implies foreign_claimant
                    claim_warning = {
                        "kind": "claim_collision",
                        "claimant": prior_claimed_by,
                        "claimed_at": _task_claimed_at(prior_transitions),
                        "message": (
                            f"took over from {prior_claimed_by['name']} "
                            f"<{prior_claimed_by['email']}>"
                        ),
                    }
                metadata["claimed_by"] = caller
            elif status == "open" and current_status in ("done", "cancelled"):
                # WP-TC4 (3): reopening someone else's closed task -- warn, no note
                # required (the ruling scopes the note requirement to takeover).
                # Suppressed when no closing transitions entry exists (unattributable).
                closer = _task_closer(prior_transitions)
                closer_by = closer.get("by") or {} if closer else {}
                closer_email = _casefold_email(closer_by.get("email", ""))
                if closer and closer_email and caller_email and closer_email != caller_email:
                    claim_warning = {
                        "kind": "reopen",
                        "claimant": closer_by,
                        "claimed_at": closer.get("at"),
                        "message": (
                            f"reopened a task closed by {closer_by.get('name', 'unknown')} "
                            f"<{closer_email}>"
                        ),
                    }
            metadata_changed = True

    # WP-TC4 (2-MECH #4): same-status takeover (2b/2c), PARALLEL to the transition body
    # above -- status == current_status there means that body's `if status !=
    # current_status` gate skips it entirely; this is new mutation code, not a reroute.
    if same_status_takeover_shape:
        assert prior_claimed_by is not None  # same_status_takeover_shape implies foreign_claimant
        if note:
            # (2b) seizes: restamp + append a transitions entry recording the takeover.
            claim_warning = {
                "kind": "claim_collision",
                "claimant": prior_claimed_by,
                "claimed_at": _task_claimed_at(prior_transitions),
                "message": (
                    f"took over from {prior_claimed_by['name']} <{prior_claimed_by['email']}>"
                ),
            }
            entry = {
                "status": "in_progress",
                "at": datetime.now(UTC).isoformat(),
                "by": caller,
                "note": note,
            }
            metadata["claimed_by"] = caller
            metadata["transitions"] = [*metadata.get("transitions", []), entry]
            metadata_changed = True
        else:
            # (2c) pokes: no seizure, no restamp, no error -- just the warning. Other
            # fields in the same call (owner=/priority=/etc.) still apply normally.
            claim_warning = {
                "kind": "takeover_note_required",
                "claimant": prior_claimed_by,
                "claimed_at": _task_claimed_at(prior_transitions),
                "message": (
                    f"task remains claimed by {prior_claimed_by['name']} "
                    f"<{prior_claimed_by['email']}>; you did NOT take it over — "
                    "pass note= to take over"
                ),
            }

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

    # --- assignment (WP-TC8): None = no change; "" unassigns; compare-before-append
    # modeled on _update_person's reports_to_email handling, NOT the owner block above ---
    if assigned_to_email is not None:
        new_assigned_to = _casefold_email(assigned_to_email)
        current_assigned_to = metadata.get("assigned_to") or ""
        if new_assigned_to != current_assigned_to:
            entry = {"to": new_assigned_to, "at": datetime.now(UTC).isoformat(), "by": caller}
            metadata["assignments"] = [*metadata.get("assignments", []), entry]
            if new_assigned_to:
                metadata["assigned_to"] = new_assigned_to
            else:
                metadata.pop("assigned_to", None)
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

    if not metadata_changed and not top_updates and claim_warning is None:
        return {
            "error": (
                "No updatable fields provided "
                "(status, priority, owner, summary, detail, parent_id, assigned_to_email)"
            )
        }
    # WP-TC4 (2-MECH #5): a stashed claim_warning with nothing else changed is the
    # "bare poke" shape (2c) -- fall through to the normal response assembly below
    # rather than erroring; an attempted claim deserves an answer, and re-embedding an
    # unmutated node is harmless.

    # Re-embed ONCE on the fresh node (NOT via _update_node) so the new status/owner +
    # narrative land in the vector store. Deferred if the model isn't ready (rare — an edit needs a
    # loaded model anyway).
    if embeddings_ready:
        assert generator is not None  # callers pass a generator whenever ready=True
        post = storage.get_node(node_id)
        assert post is not None  # just updated it; cannot vanish under the lock
        cnode = _node_from_dict(node_id, post)
        _embed_entity_node(embedding_storage, generator, cnode)
        reembed = "done"
    else:
        reembed = "deferred"

    result = _get_node(storage, node_id)
    result["reembed"] = reembed
    if claim_warning is not None:
        # WP-TC4 (1): TC8 info, zero semantics -- only present when the task is assigned.
        assigned_to = metadata.get("assigned_to")
        if assigned_to:
            claim_warning = {**claim_warning, "assigned_to": assigned_to}
        result["claim_warning"] = claim_warning
    return result


# ── Roster tools (cognition_register_person / _update_person / _get_person /
# _list_people) ───────────────────────────────────────────────────────────────
#
# The roster models HUMANS ONLY — agent identity lives in teammate-comms, never
# here. It is now committed per-person PROFILES
# (.cognition/people/<email>.profile.jsonl), append-only with a last-write-wins
# fold; legacy `person` nodes are still read via Roster for any email that has no
# profile yet. Email is the identity key, casefolded.

_SENIORITY_SET: frozenset[str] = frozenset(SENIORITY_LEVELS)


def _casefold_email(email: str) -> str:
    """The single email-normalization point for person matching. Delegates to
    fold_email, which is NFC + lower rather than casefold — see its docstring for
    why casefold merged two distinct people."""
    return fold_email(email)


def _person_summary(name: str, role: str) -> str:
    return f"{name} — {role}"


def _normalize_reports_to(value: str | None) -> str | dict[str, Any] | None:
    """A reporting line, or an error dict. None means "not supplied".

    Accepts an email or the literal "nobody"; a NAME is rejected, because the chain
    is resolved by email and a name would break it with no error anywhere. An empty
    string is also rejected -- "nobody" is the way to say top-of-chain, and a
    skippable field gets skipped.
    """
    if value is None:
        return None
    folded = fold_email(value)
    if folded == NO_MANAGER:
        return NO_MANAGER
    if not folded or not is_valid_email(value):
        return {
            "error": (
                f"reports_to must be the manager's EMAIL or the literal \"{NO_MANAGER}\", got "
                f"{value!r} — the reporting chain is resolved by email, so a name cannot be "
                f"stored here. \"{NO_MANAGER}\" is valid and expected on a solo project."
            )
        }
    return folded


def _person_row(person: Person, roster: Roster) -> dict[str, Any]:
    """One roster row, the shape all four person tools return.

    `reports_to` is an email or "nobody"; `answered_reports_to` distinguishes a
    deliberate "nobody" from never having been asked, which is what the write gate
    turns on. `id` is the legacy person-node id and is None once the node is gone.
    """
    return {
        "email": person.email,
        "name": person.name,
        "role": person.role,
        "seniority": person.seniority,
        "reports_to": person.reports_to_display,
        "answered_reports_to": person.answered_reports_to,
        "reports_to_registered": bool(person.reports_to) and roster.is_registered(person.reports_to),
        "detail": person.detail,
        "summary": _person_summary(person.name, person.role),
        "source": person.source,
        "id": person.node_id,
    }


def _register_person(
    ctx: Context,
    name: str,
    role: str,
    seniority: str,
    reports_to: str | None = None,
    email: str | None = None,
    detail: str = "",
    from_agent: bool = True,
) -> dict[str, Any]:
    """Write someone's committed profile (testable core of cognition_register_person).

    Identity rule: when ``email`` is omitted this registers SELF, using the
    server-resolved confirmed email (impersonation-resistant onboarding path). An
    explicit ``email`` registers someone ELSE -- a manager pre-registering a
    teammate -- which is allowed and trust-based; the profile records carry the
    caller as ``by``, so who did it is in the audit trail rather than enforced.

    Registering an email that already has a complete profile returns it with
    ``already_registered: True`` and writes nothing: never a silent duplicate,
    never a data-losing error. An INCOMPLETE profile is filled in instead, which is
    what makes the manager-pre-registers-then-person-confirms flow work.
    """
    lc = get_lifespan(ctx)
    storage: CognitionStorage = lc["cognition_storage"]

    seniority_norm = seniority.strip().casefold()
    if seniority_norm not in _SENIORITY_SET:
        return {"error": f"Invalid seniority '{seniority}'. Valid: {list(SENIORITY_LEVELS)}"}

    # Gated like every other write path: registering someone ELSE still records the
    # CALLER as the author, so an unconfirmed caller would write an unattributable
    # profile record.
    gate, by = _gated_identity(storage)
    if gate is not None:
        return gate

    if email:
        resolved_email = _casefold_email(email)
        if not resolved_email:
            return {"error": "email, if provided, must not be blank"}
    else:
        resolved_email = _casefold_email(by.get("email", ""))
        if not resolved_email:
            return {"error": "could not resolve your email to self-register; pass email explicitly"}

    roster = storage.roster()
    existing = roster.get(resolved_email)
    if existing is not None and not storage.profile_missing_required(resolved_email):
        result = _person_row(existing, roster)
        result["already_registered"] = True
        return result

    manager = _normalize_reports_to(reports_to)
    if isinstance(manager, dict):
        return manager
    if manager and manager != NO_MANAGER:
        if manager == resolved_email:
            return {"error": "A person cannot report to themselves"}
        if roster.reports_to_cycle(resolved_email, manager):
            return {
                "error": (
                    f"reports_to rejected: '{manager}' transitively reports to "
                    f"'{resolved_email}' — would create a cycle"
                )
            }

    fields: dict[str, Any] = {
        "name": name,
        "email": resolved_email,
        "role": role,
        "seniority": seniority_norm,
    }
    if manager:
        fields["reports_to"] = manager
    if detail:
        fields["detail"] = detail
    try:
        written = storage.set_profile_fields(resolved_email, fields, by, from_agent)
    except OSError as exc:
        return {"error": f"could not write to .cognition/people/: {exc}"}
    if "error" in written:
        return written

    roster = storage.roster()
    person = roster.get(resolved_email)
    if person is None:  # pragma: no cover - a write that folded to nothing
        return {"error": f"profile for '{resolved_email}' did not persist"}
    result = _person_row(person, roster)
    result["already_registered"] = False
    result["profile_written"] = written.get("written", [])
    result["missing_profile_fields"] = storage.profile_missing_required(resolved_email)
    result["from_agent"] = from_agent
    return result


def _update_person(
    ctx: Context,
    email_or_id: str,
    name: str | None = None,
    role: str | None = None,
    seniority: str | None = None,
    reports_to: str | None = None,
    detail: str | None = None,
    from_agent: bool = True,
) -> dict[str, Any]:
    """Change fields on someone's committed profile (core of cognition_update_person).

    Anyone may update anyone (local trust domain; the append-only profile record
    trail is the control, not an ACL). Only fields whose value actually CHANGES are
    journalled, so a no-op update writes nothing. ``reports_to="nobody"`` sets top
    of chain; omitting it leaves the reporting line unchanged (None = no change,
    mirroring cognition_update_task's parent_id convention).
    """
    lc = get_lifespan(ctx)
    storage: CognitionStorage = lc["cognition_storage"]

    roster = storage.roster()
    person = roster.resolve(email_or_id)
    if person is None:
        return {"error": f"No person found for '{email_or_id}'"}
    self_email = person.email

    # Validate BEFORE any mutation (same discipline as _update_task).
    if seniority is not None:
        seniority = seniority.strip().casefold()
        if seniority not in _SENIORITY_SET:
            return {"error": f"Invalid seniority '{seniority}'. Valid: {list(SENIORITY_LEVELS)}"}

    manager: str | None = None
    if reports_to is not None:
        normalized = _normalize_reports_to(reports_to)
        if isinstance(normalized, dict):
            return normalized
        manager = normalized
        if manager and manager != NO_MANAGER:
            if manager == self_email:
                return {"error": "A person cannot report to themselves"}
            if roster.reports_to_cycle(self_email, manager):
                return {
                    "error": (
                        f"reports_to rejected: '{manager}' transitively reports to "
                        f"'{self_email}' — would create a cycle"
                    )
                }

    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = name
    if role is not None:
        fields["role"] = role
    if seniority is not None:
        fields["seniority"] = seniority
    if manager is not None:
        fields["reports_to"] = manager
    if detail is not None:
        fields["detail"] = detail
    if not fields:
        return {
            "error": "No updatable fields provided (name, role, seniority, reports_to, detail)"
        }

    gate, by = _gated_identity(storage)
    if gate is not None:
        return gate

    try:
        written = storage.set_profile_fields(self_email, fields, by, from_agent)
    except OSError as exc:
        return {"error": f"could not write to .cognition/people/: {exc}"}
    if "error" in written:
        return written

    roster = storage.roster()
    updated = roster.get(self_email)
    if updated is None:  # pragma: no cover - a write that folded to nothing
        return {"error": f"profile for '{self_email}' did not persist"}
    result = _person_row(updated, roster)
    result["profile_written"] = written.get("written", [])
    result["profile_skipped"] = written.get("skipped", [])
    result["missing_profile_fields"] = storage.profile_missing_required(self_email)
    result["from_agent"] = from_agent
    return result


def _get_person(storage: CognitionStorage, email_or_id: str) -> dict[str, Any]:
    """One person's full profile, audit trail and environment facts.

    Testable core of cognition_get_person. Joins three sources: the committed
    profile (or a legacy person node, for an email with no profile yet), the
    append-only record trail behind it, and the env-facts registry.
    """
    roster = storage.roster()
    person = roster.resolve(email_or_id)
    if person is None:
        return {"error": f"No person found for '{email_or_id}'"}
    result = _person_row(person, roster)
    result["profile_history"] = _profile_history_for(storage, person)
    result["environment"] = storage.get_env_facts(person.email)
    return result


def _profile_history_for(storage: CognitionStorage, person: Person) -> list[dict[str, Any]]:
    """The append-only trail behind a profile, oldest first.

    A profile-backed row returns the raw records -- one per field change, each with
    `field`, `value`, `by` and `at`. A row still backed by a legacy person node
    returns that node's own `metadata.profile_history` entries unchanged, which use
    the older `{changed: {field: {from, to}}, at, by}` shape.
    """
    records = storage.profile_history(person.email)
    if records:
        return records
    if person.node_id:
        node = storage.get_node(person.node_id) or {}
        return list((node.get("metadata") or {}).get("profile_history") or [])
    return []


# ── WP-EnvFacts-A: per-person environment facts (self-only tool surface) ────
# Policy lives HERE, not in storage: the target identity is ALWAYS the
# server-resolved git identity — the write tools take NO email parameter at
# all (impersonation-resistant by construction, decision 29faeeca5190;
# mirrors register_person's self path). Reads stay open to any email
# (divergence detection needs them). Every successful write returns a
# `disclosure` string the calling agent MUST surface to the human
# (removal-on-request ruling cb8605c710fb; until WP-B's prime listing ships
# this is the only disclosure surface, agent-relayed).


def _self_identity(storage: CognitionStorage) -> tuple[dict[str, str] | None, str]:
    """(recorded_by, casefolded email) for the server-resolved identity, or
    (None, "") when no email resolves at all.

    READ paths only — they need *an* email to default "self" to. Env-fact WRITES
    go through _gated_identity like every other write, so a confirmed identity
    with an incomplete profile can still read its own facts but cannot add one.
    """
    by = _acting_identity(storage.cognition_dir)
    email = _casefold_email(by.get("email", ""))
    return (by, email) if email else (None, "")


def _gated_self(
    storage: CognitionStorage,
) -> tuple[dict[str, Any] | None, dict[str, str], str]:
    """(refusal_or_None, by, casefolded email) for an env-fact WRITE."""
    gate, by = _gated_identity(storage)
    return gate, by, "" if gate is not None else _casefold_email(by.get("email", ""))


def _machine_key(machine: str | None) -> str:
    """Casefolded machine key: the explicit arg, else this machine's hostname.
    Empty result (blank arg AND unresolvable hostname) -> "" — callers error."""
    return (machine if machine is not None else platform.node()).strip().casefold()


_NO_IDENTITY_ERROR = (
    "env facts are self-only and no email resolves for you — ASK THE HUMAN for "
    "their name and work email, then call cognition_set_identity(name=..., email=...)"
)
_NO_MACHINE_ERROR = (
    "no machine key: the machine argument was blank, or (when omitted) this "
    "system's hostname is unresolvable — pass a non-empty machine=, then retry"
)


def _set_env_fact(
    ctx: Context,
    key: str,
    value: Any,
    machine: str | None = None,
    from_agent: bool = True,
) -> dict[str, Any]:
    """Testable core of cognition_set_env_fact."""
    lc = get_lifespan(ctx)
    storage: CognitionStorage = lc["cognition_storage"]
    gate, by, email = _gated_self(storage)
    if gate is not None:
        return gate
    machine_key = _machine_key(machine)
    if not machine_key:
        return {"error": _NO_MACHINE_ERROR}
    cap = getattr(lc.get("config"), "env_fact_machine_cap", DEFAULT_MACHINE_CAP)
    try:
        r = storage.set_env_fact(
            email, machine_key, key, value, by, from_agent, machine_cap=cap
        )
    except ValueError as e:
        return {"error": str(e)}
    result: dict[str, Any] = {
        "email": email,
        "machine": machine_key,
        "key": key.strip(),
        "value": value,
        **r,
    }
    if r["written"]:
        result["disclosure"] = (
            f"Stored to your identity ({email}): {key.strip()}={value!r} "
            f"(machine {machine_key}). Tell the user this was stored — they can "
            f"ask for it to be removed at any time."
        )
    else:
        result["disclosure"] = (
            f"Already stored (no change): {key.strip()} on machine {machine_key}."
        )
    return result


def _delete_env_fact(
    ctx: Context, key: str, machine: str | None = None, from_agent: bool = True
) -> dict[str, Any]:
    """Testable core of cognition_delete_env_fact."""
    lc = get_lifespan(ctx)
    storage: CognitionStorage = lc["cognition_storage"]
    gate, by, email = _gated_self(storage)
    if gate is not None:
        return gate
    machine_key = _machine_key(machine)
    if not machine_key:
        return {"error": _NO_MACHINE_ERROR}
    try:
        r = storage.delete_env_fact(email, machine_key, key, by, from_agent)
    except ValueError as e:
        return {"error": str(e)}
    result: dict[str, Any] = {"email": email, "machine": machine_key, "key": key.strip(), **r}
    result["disclosure"] = (
        f"Removed from your identity ({email}): {key.strip()} (machine {machine_key})."
        if r["written"]
        else f"Nothing to remove: {key.strip()} is not stored for machine {machine_key}."
    )
    return result


def _clear_env_facts(
    ctx: Context, machine: str | None = None, from_agent: bool = True
) -> dict[str, Any]:
    """Testable core of cognition_clear_env_facts. machine=None clears ALL —
    deliberately NOT defaulted to the current hostname, because the no-arg call
    means "forget everything about me" (removal-on-request), not "forget this
    machine"."""
    lc = get_lifespan(ctx)
    storage: CognitionStorage = lc["cognition_storage"]
    gate, by, email = _gated_self(storage)
    if gate is not None:
        return gate
    machine_key: str | None = None
    if machine is not None:
        machine_key = _machine_key(machine)
        if not machine_key:
            return {"error": _NO_MACHINE_ERROR}
    try:
        r = storage.clear_env_facts(email, machine_key, by, from_agent)
    except ValueError as e:
        return {"error": str(e)}
    scope = f"machine {machine_key}" if machine_key else "ALL machines"
    result: dict[str, Any] = {"email": email, "machine": machine_key, **r}
    result["disclosure"] = (
        f"Cleared {len(r['cleared_keys'])} fact(s) from your identity ({email}), "
        f"{scope}: {', '.join(r['cleared_keys'])}."
        if r["written"]
        else f"Nothing stored for {scope} — nothing cleared."
    )
    return result


def _list_env_facts(ctx: Context, email_or_id: str | None = None) -> dict[str, Any]:
    """Testable core of cognition_list_env_facts. Reads are OPEN (any email or
    person id; divergence checks need teammates' facts) — only writes are
    self-only. Default target: the server-resolved self."""
    lc = get_lifespan(ctx)
    storage: CognitionStorage = lc["cognition_storage"]
    if email_or_id is None:
        _, email = _self_identity(storage)
        if not email:
            return _identity_gate(storage.cognition_dir) or {"error": _NO_IDENTITY_ERROR}
    else:
        person = storage.roster().resolve(email_or_id)
        email = person.email if person is not None else _casefold_email(email_or_id)
        if not email:
            return {"error": f"could not resolve '{email_or_id}' to an email"}
    environment = storage.get_env_facts(email)
    return {
        "email": email,
        # Facts for an identity with no profile are first-class legal — flagged,
        # never an error.
        "registered": storage.roster().is_registered(email),
        "environment": environment,
        "machine_count": len(environment),
        "current_machine": _machine_key(None),
    }


def _remove_person(ctx: Context, email: str, from_agent: bool = True) -> dict[str, Any]:
    """Take someone off the roster (testable core of cognition_remove_person).

    Profiles are append-only, so this clears every field rather than deleting the
    file: the record trail of who they were and who changed it stays intact and
    still merges cleanly, while the person stops appearing on the roster, in the
    reporting chain and in search ranking.

    Deliberately NOT cascading: their env facts and everything they authored stay
    exactly as they are. Attribution is history — rewriting it because someone
    left would make the graph lie about who decided what.
    """
    lc = get_lifespan(ctx)
    storage: CognitionStorage = lc["cognition_storage"]

    roster = storage.roster()
    person = roster.resolve(email)
    if person is None:
        return {"error": f"No person found for '{email}'"}

    gate, by = _gated_identity(storage)
    if gate is not None:
        return gate

    if person.email == _casefold_email(by.get("email", "")):
        return {
            "error": (
                f"refusing to remove {person.email}: that is the identity driving "
                "this checkout, and clearing its profile would close the write gate "
                "on you immediately — the next record or task would be refused with "
                "no obvious cause. To correct your own details use "
                "cognition_update_person; to hand this checkout to someone else use "
                "cognition_set_identity."
            )
        }

    reports = [r.email for r in roster.direct_reports(person.email)]
    try:
        cleared = [
            field for field in PROFILE_FIELDS
            if "error" not in storage.unset_profile_field(person.email, field, by)
        ]
        # Clearing the fields is not enough on its own. An empty profile is
        # indistinguishable from one that was never written, so a legacy person
        # node would be folded straight back onto the roster -- and the migration
        # would then re-create the profile from that node's stale values. The
        # tombstone is what makes the removal durable.
        storage.mark_profile_removed(person.email, by)
    except OSError as exc:
        return {"error": f"could not write to .cognition/people/: {exc}"}
    result: dict[str, Any] = {
        "removed": True,
        "email": person.email,
        "name": person.name,
        "cleared_fields": cleared,
        "orphaned_reports": reports,
        "from_agent": from_agent,
    }
    if node_id := roster.legacy_node_id(person.email):
        result["legacy_node_id"] = node_id
        result["warning_legacy_node"] = (
            f"a legacy person node ({node_id}) for this email is still in the "
            "graph. They are off the roster and will stay off, but the node itself "
            "still shows in graph listings and still has an embedding — remove it "
            f"with cognition_remove_node(node_id='{node_id}')."
        )
    if reports:
        result["warning"] = (
            f"{person.name or person.email} was the manager of "
            f"{', '.join(reports)} — their reporting line now dangles. Ask the "
            "human who those people report to now, then use "
            "cognition_update_person to set it."
        )
    return result


def _list_people(storage: CognitionStorage) -> dict[str, Any]:
    """Roster view (testable core of cognition_list_people): one row per person,
    name-sorted, with the reporting line and whether each manager is on the roster.
    """
    roster = storage.roster()
    rows = [_person_row(p, roster) for p in roster.all()]
    return {"people": rows, "count": len(rows)}


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

    if not has_journal(resolved / ".cognition"):
        return {
            "error": (
                f"no cognition graph at {resolved} (no .cognition/journal.jsonl and "
                "no .cognition/journal/ shards)"
            )
        }

    b_storage = CognitionStorage(resolved / ".cognition", read_only=True)
    node_count = b_storage.get_statistics().get("nodes", 0)

    # Open B's vector store read-only (never creates): the keyed plugin-data dir
    # when it exists, else B's in-repo fallback; none until B has been opened once
    # on a version that writes this store.
    b_vectors_dir = resolve_foreign_vectors_dir(resolved)
    b_embeddings = VectorStore.open_existing(b_vectors_dir) if b_vectors_dir is not None else None
    if b_vectors_dir is None:
        index_reason: str | None = (
            "search index not built yet for this project; open a session in it once on "
            f"vibe-cognition {_running_version_label()} or later to build it"
        )
    elif b_embeddings is None:
        index_reason = f"search index at {b_vectors_dir} could not be opened (see the server log)"
    else:
        index_reason = None

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
    if index_reason:
        result["index_reason"] = index_reason
    coverage = _index_coverage(b_storage, b_embeddings)
    if coverage:
        result["index_incomplete"] = True
        result["indexed_nodes"] = coverage["indexed_nodes"]
    return result


def _running_version_label() -> str:
    return code_version() or "0.47.0"


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


from . import graph_tools, read_tools, record_tools  # noqa: E402


def register_cognition_tools(mcp) -> None:
    """Register cognition graph tools with the MCP server."""
    record_tools.register_record_tools(mcp)
    read_tools.register_read_tools(mcp)
    graph_tools.register_graph_tools(mcp)
