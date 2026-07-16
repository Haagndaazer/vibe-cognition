"""HTTP API handlers for the dashboard.

Handlers are sync `def` so Starlette runs them in a threadpool — this
matches CognitionStorage's RLock-based threading model.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse

from ..cognition import (
    CognitionEdgeType,
    CognitionNodeType,
    CognitionStorage,
    delete_cognition_node,
)
from ..cognition.documents import documents_dir, freshness_by_rehash, text_sidecar_path
from ..cognition.queries import conflict_flags, get_superseded_chain
from ..cognition.task_meta import _task_claimed_at  # WP-TC16: relocated (was cognition_tools)
from ..embeddings import adaptive_vector_search
from ..tools.cognition_tools import _reembed_replayed_nodes

# Deliberately NOT `from ..cognition.prime import SEVERITY_ORDER` (scope-dashboard-v1
# brief, doc:4c0b9d426f4c): prime.py carries markdown-formatting + CLI-facing deps this
# read-only JSON aggregation has no business pulling in. A local copy is one line and
# keeps the dashboard's import surface independent of prime.py's.
_SEVERITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}
_TASK_CLOSED_STATUSES = frozenset({"done", "cancelled"})
_STALE_CLAIM_DAYS = 5
_DONE_THIS_WEEK_DAYS = 7
_RECENT_INCIDENT_DAYS = 14
_HIGH_SEVERITIES = frozenset({"critical", "high"})

# WP-DashV2: Activity feed type set (peer-review H4 — five named in the design
# doc's §4.5 prose plus constraint/pattern/assumption by elimination against
# models.py's 12-type enum: task/document/workflow/person are owned by other
# views, no SUMMARY type exists). Deliberately unfiltered by HEAD/supersession
# (unlike Overview's active-constraints view) — this is a chronological record.
_ACTIVITY_NODE_TYPES = (
    CognitionNodeType.EPISODE,
    CognitionNodeType.DECISION,
    CognitionNodeType.FAIL,
    CognitionNodeType.DISCOVERY,
    CognitionNodeType.INCIDENT,
    CognitionNodeType.CONSTRAINT,
    CognitionNodeType.PATTERN,
    CognitionNodeType.ASSUMPTION,
)
_ACTIVITY_DEFAULT_LIMIT = 100
_ACTIVITY_MAX_LIMIT = 500

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_MEDIA_TYPE = re.compile(r"^[\w.+-]+/[\w.+-]+$")  # strict type/subtype, no params/CRLF


def _safe_filename(name: str, fallback: str = "document") -> str:
    """Sanitize a Content-Disposition filename: strip any path components, collapse
    unsafe chars. The filename comes from stored metadata, but we never trust it as a
    path or let it carry separators/control chars into the response header."""
    base = Path(name).name
    base = _UNSAFE_FILENAME.sub("_", base).strip("._")
    return (base or fallback)[:120]


def _safe_media_type(mime: str | None) -> str:
    """Validate the AGENT-controlled mime before it becomes a Content-Type header.
    Anything that isn't a strict ``type/subtype`` (no params, no CRLF/control chars)
    falls back to a safe default — we never rely on the HTTP parser to reject our own
    header injection (ledger 17), and this mirrors _safe_filename's discipline."""
    if mime and _SAFE_MEDIA_TYPE.match(mime):
        return mime
    return "application/octet-stream"

logger = logging.getLogger(__name__)


def _ctx(request) -> dict[str, Any]:
    return request.app.state.lifespan_ctx


def _document_has_blob(node: dict[str, Any]) -> bool:
    """Whether a document node has a stored content-addressed blob (copy mode). THE
    single source for the has-blob decision — both the list endpoint and the download
    endpoint use it (ledger 11) so they can't disagree on what's downloadable."""
    return (node.get("metadata") or {}).get("mode") == "copy"


def _document_blob_path(cognition_dir: Path, node: dict[str, Any]) -> Path | None:
    """Resolve a copy-mode document's on-disk blob path, VALIDATED to live under the
    documents dir — None for reference mode, a missing blob_path, or any path that
    resolves outside documents_dir (path-safety defense even though the path is
    server-derived from the node's stored, sanitized metadata, never client input)."""
    if not _document_has_blob(node):
        return None
    rel = (node.get("metadata") or {}).get("blob_path")
    if not rel:
        return None
    docs = documents_dir(cognition_dir).resolve()
    candidate = (docs / rel).resolve()
    if not candidate.is_relative_to(docs):
        return None
    return candidate


def _document_freshness(meta: dict[str, Any]) -> str | None:
    """Dashboard freshness badge for one document's metadata (WP-DashV2,
    peer-review-corrected null semantics — distinct from
    cognition_get_document's tool-facing default):

    - mode == "copy" -> null. EXPLICIT mode gate, not path-absence: a
      copy-mode doc stored via file_path RETAINS its path key
      (cognition_tools._store_document sets path independent of
      store_copy), so checking path-presence alone would wrongly compute
      a verdict for it.
    - mode == "reference" with no path key (stored via content_text — a
      common state, not just legacy) -> null. No source file exists to
      check; documents.freshness_by_rehash's own "unchanged" default for
      this state implies a check that never ran, and the badge must not
      repeat that implication.
    - mode == "reference" WITH a path -> "unchanged" | "modified" |
      "missing" via the shared full re-hash (documents.freshness_by_rehash
      — single-implementation doctrine, same logic cognition_get_document
      uses). KNOWN COST: O(referenced file bytes) per row; no caching
      (a stale badge defeats the badge) — fine at current scale.
    """
    if meta.get("mode") == "copy":
        return None
    if not meta.get("path"):
        return None
    return freshness_by_rehash(meta)


def _embedding_status(lc: dict[str, Any]) -> tuple[bool, str | None]:
    """Return (ready, error_or_loading_status)."""
    if lc.get("embeddings_disabled"):
        return False, "disabled"
    error = lc.get("embedding_error")
    if error:
        return False, "error"
    event = lc.get("embedding_ready")
    if event and event.is_set() and lc.get("embedding_generator") is not None:
        return True, None
    return False, "loading"


def get_graph(request):
    """Return all nodes + edges, shaped for Cytoscape.

    Excludes the `detail` field per node to keep payloads small;
    fetch full detail via /api/node/{id} on click.
    """
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    # snapshot() catches up on the journal and returns nodes + edges together
    # under the lock — a consistent, converged view (no raw graph/_lock reach-in).
    snap = storage.snapshot()

    nodes_out = [
        {
            "data": {
                "id": n["id"],
                "label": (n.get("summary") or n["id"])[:80],
                "type": n.get("type", ""),
                "summary": n.get("summary", ""),
                "timestamp": n.get("timestamp", ""),
            }
        }
        for n in snap["nodes"]
    ]

    edges_out = [
        {
            "data": {
                "id": f"{source_id}__{key}__{target_id}",
                "source": source_id,
                "target": target_id,
                "type": edge_data.get("type", key),
            }
        }
        for source_id, target_id, key, edge_data in snap["edges"]
    ]

    return JSONResponse({"nodes": nodes_out, "edges": edges_out})


def get_node(request):
    """Return full node data + neighbors.

    A PERSON node additionally gains `person_activity` (WP-DashV3, task
    5d4e2bd60d17): the drilldown the People-view drawer renders in place —
    node counts by type, last-active timestamp, currently-claimed tasks,
    open tasks created. Read-only; wired from the SAME roster-row click that
    already opens this drawer, no new endpoint or click surface.
    """
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    node_id = request.path_params["node_id"]

    node_data = storage.get_node(node_id)
    if node_data is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    successors = [
        {"id": tid, "edge_type": ed.get("type", "")}
        for tid, ed in storage.get_successors(node_id)
    ]
    predecessors = [
        {"id": sid, "edge_type": ed.get("type", "")}
        for sid, ed in storage.get_predecessors(node_id)
    ]

    result = {
        "id": node_id,
        **node_data,
        "successors": successors,
        "predecessors": predecessors,
    }
    if node_data.get("type") == CognitionNodeType.PERSON.value:
        email = ((node_data.get("metadata") or {}).get("person") or {}).get("email") or ""
        result["person_activity"] = _person_activity(storage, email)
    return JSONResponse(result)


def delete_node(request):
    """Remove a node from the graph and ChromaDB.

    Provenance: the journal tombstone records the acting surface ("dashboard") —
    the dashboard has no per-user identity (deliberately: token-gated, single
    local user), so the surface tag is the honest attribution.
    """
    lc = _ctx(request)
    node_id = request.path_params["node_id"]

    result = delete_cognition_node(
        lc["cognition_storage"],
        lc["cognition_embedding_storage"],
        node_id,
        removed_by="dashboard",
    )
    if result is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    return JSONResponse({"deleted": True, "id": node_id})


async def search(request):
    """Semantic search via embeddings.

    Async because we need request.json(); we then offload the blocking
    embedding+vector work via run_in_threadpool.
    """
    lc = _ctx(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    query = str(body.get("query", "")).strip()
    try:
        limit = int(body.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))  # clamp: no 0/negative/huge fan-out
    entity_type = body.get("entity_type")

    if not query:
        return JSONResponse({"error": "missing query"}, status_code=400)

    ready, status = _embedding_status(lc)
    if not ready:
        return JSONResponse(
            {
                "embedding_status": status,
                "error": lc.get("embedding_error") or "embedding model still loading",
            },
            status_code=503,
        )

    generator = lc["embedding_generator"]
    embed_storage = lc["cognition_embedding_storage"]
    cognition_storage = lc["cognition_storage"]

    def _do_search():
        # WP-3 (8606d59905a5): same drain cognition_search's home path runs —
        # the dashboard has its own request pipeline (adaptive_vector_search
        # directly, not through cognition_search), so without this a
        # teammate's replayed node would stay invisible in dashboard search
        # until an MCP search happened to run first. The dashboard is always
        # home-only (no project routing), so this is an unambiguous 1:1 fit —
        # no foreign-store-write scoping question like the MCP side has.
        _reembed_replayed_nodes(cognition_storage, embed_storage, generator)
        vector = generator.generate_query_embedding(query)

        def _dedupe(hits: list[dict], lim: int) -> tuple[list[dict], int]:
            # N1 ghost-search SAFETY (WP-D2): drop hits whose node was deleted cross-process
            # but never un-embedded — else the dashboard would serve verbatim deleted client
            # document chunk text.
            # D-6 NAVIGATION (WP-D4): dedupe chunk hits to best hit per node, rewrite _id
            # to the navigable node id, hydrate summary from the graph.
            # WP-TC10 dedupe-contract conformance: returns (list, excluded_count) — the
            # list is no longer capped to `lim` here (adaptive_vector_search owns the
            # limit-slice now, so it can report total_found honestly). The dashboard
            # never excludes by author, so excluded_count is always 0 here.
            out: list[dict] = []
            seen: set[str] = set()
            for h in hits:
                raw_id = h.get("_id") or ""
                if not cognition_storage.search_hit_is_live(raw_id):
                    continue
                node_id = raw_id.split("#chunk-")[0]
                if node_id in seen:
                    continue
                seen.add(node_id)
                row = dict(h)
                row["_id"] = node_id
                node = cognition_storage.get_node(node_id)
                if node:
                    row["summary"] = node.get("summary") or row.get("summary")
                matched = h.get("matched_text")
                if matched:
                    row["matched_excerpt"] = matched[:500]
                out.append(row)
            return out, 0

        # WP-TC10: adaptive_vector_search now returns an envelope
        # ({"results", "total_found", "exhaustive", "excluded_count"}) — the dashboard
        # JSON response deliberately stays results-only (no feature change here, see
        # brief), so only the "results" key is threaded through.
        return adaptive_vector_search(
            embed_storage, vector, entity_type=entity_type, limit=limit, dedupe=_dedupe
        )["results"]

    results = await run_in_threadpool(_do_search)
    return JSONResponse({"results": results})


def get_stats(request):
    """Graph stats + embedding readiness."""
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    embed_storage = lc["cognition_embedding_storage"]

    ready, status = _embedding_status(lc)
    try:
        embedding_count = embed_storage.count_documents()
    except Exception as e:
        embedding_count = 0
        logger.warning(f"count_documents failed: {e}")

    return JSONResponse({
        "graph": storage.get_statistics(),
        "embeddings": embedding_count,
        "embedding_ready": ready,
        "embedding_status": status,
        "embedding_error": lc.get("embedding_error"),
        "embedding_generator_loaded": lc.get("embedding_generator") is not None,
    })


def _parse_ts(ts: str | None) -> datetime | None:
    """Best-effort ISO-8601 parse; None on missing/malformed (never raises)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _is_conflicted(storage: CognitionStorage, node_id: str) -> bool:
    """Whether a node carries any conflict signal (WP-DashV3) -- the list-level
    ``conflicted`` flag behind the Board-card and Activity-row ⚠ indicator.

    WP-SearchFlags: the membership logic itself relocated to the single shared
    implementation ``cognition.queries.conflict_flags`` (also used by
    cognition_search's per-result flags). This wrapper composes the dashboard's
    ORIGINAL single-bool semantics from that tuple -- conflicted-by-contradicts
    OR superseded (the dashboard collapses both into one ⚠, unlike search, which
    keeps them separate) -- so behavior is byte-identical to before the
    relocation: a node with ONLY an incoming SUPERSEDES edge (superseded, no
    contradicts) still returns True here, even though ``conflict_flags``'s own
    ``conflicted`` element only tracks contradicts.
    """
    conflicted, superseded_by = conflict_flags(storage, node_id)
    return conflicted or superseded_by is not None


def _task_row(
    storage: CognitionStorage, t: dict[str, Any], by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Shape one task node for the dashboard Board view.

    Built independently against storage.get_nodes_by_type (WP-TC11 brief) rather than
    reusing cognition_tools._list_tasks -- claimed_by/claimed_at/transition timestamps
    are dashboard-only fields not in that tool's row shape.
    """
    meta = t.get("metadata", {}) or {}
    transitions = meta.get("transitions") or []

    depth = 0
    seen: set[str] = set()
    cur = meta.get("parent_id")
    while cur and cur in by_id and cur not in seen:
        seen.add(cur)
        depth += 1
        cur = (by_id[cur].get("metadata", {}) or {}).get("parent_id")

    return {
        "id": t["id"],
        "summary": t.get("summary"),
        "status": meta.get("status", "open"),
        "priority": t.get("severity"),
        "owner": meta.get("owner"),
        "parent_id": meta.get("parent_id"),
        "depth": depth,
        "created_by": meta.get("created_by"),
        "claimed_by": meta.get("claimed_by"),
        "author": t.get("author"),
        "from_agent": meta.get("from_agent"),
        "timestamp": t.get("timestamp"),
        "claimed_at": _task_claimed_at(transitions),
        "last_transition_at": transitions[-1]["at"] if transitions else None,
        "transitions_count": len(transitions),
        "conflicted": _is_conflicted(storage, t["id"]),
    }


def get_tasks(request):
    """List every task, shaped for the Board view (kanban + tree).

    Unfiltered — the client caps "done" to a recent window and hides
    "cancelled" behind a toggle (design doc §4.2); the API returns the full
    set once so both are cheap client-side re-slices, not extra round-trips.
    """
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    tasks = storage.get_nodes_by_type(CognitionNodeType.TASK)
    by_id = {t["id"]: t for t in tasks}
    rows = [_task_row(storage, t, by_id) for t in tasks]
    rows.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    rows.sort(key=lambda r: _SEVERITY_ORDER.get(r.get("priority") or "normal", 2))
    return JSONResponse({"tasks": rows, "count": len(rows)})


def _entity_row(n: dict[str, Any], storage: CognitionStorage | None = None) -> dict[str, Any]:
    """Shape a non-task entity node (episode/incident/constraint/...) for list views.

    Includes both provenance fields so the frontend can apply trust-class labeling
    (design doc §4.7): `recorded_by` is server-resolved (WP-P13n+), `author` is the
    free-text fallback every node has always had. A pre-P13n node has `recorded_by`
    absent -- the frontend renders `author` with a dashed "unverified" chip in that case.

    ``storage`` (WP-DashV3, optional): when provided, the row gains a `conflicted`
    bool (see `_is_conflicted`). Deliberately opt-in, not unconditional -- the
    brief pins Overview's constraint/attention lists to NO CHANGE (they are
    already HEAD-filtered; a superseded constraint never appears there, and
    adding the flag to a filtered list invites confusion), while `/api/activity`
    (unfiltered, a chronological record) DOES want it. Callers that omit
    `storage` get the exact pre-V3 row shape.
    """
    meta = n.get("metadata", {}) or {}
    row = {
        "id": n["id"],
        "type": n.get("type"),
        "summary": n.get("summary"),
        "timestamp": n.get("timestamp"),
        "severity": n.get("severity"),
        "author": n.get("author"),
        "recorded_by": meta.get("recorded_by"),
        "from_agent": meta.get("from_agent"),
    }
    if storage is not None:
        row["conflicted"] = _is_conflicted(storage, n["id"])
    return row


def get_overview(request):
    """Server-computed aggregate for the Overview view (REQUIRED, scope-dashboard-v1
    brief doc:4c0b9d426f4c): task counts, done-this-week, active constraints,
    needs-attention (stale claims / blocked), recent episodes + incidents."""
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    now = datetime.now(UTC)
    week_ago = now - timedelta(days=_DONE_THIS_WEEK_DAYS)
    stale_cutoff = now - timedelta(days=_STALE_CLAIM_DAYS)
    incident_cutoff = now - timedelta(days=_RECENT_INCIDENT_DAYS)

    tasks = storage.get_nodes_by_type(CognitionNodeType.TASK)
    counts = {"open": 0, "in_progress": 0, "blocked": 0, "done": 0, "cancelled": 0}
    done_this_week = 0
    stale_claims = []
    blocked = []
    for t in tasks:
        meta = t.get("metadata", {}) or {}
        status = meta.get("status", "open")
        counts[status] = counts.get(status, 0) + 1
        transitions = meta.get("transitions") or []
        last_at = transitions[-1]["at"] if transitions else t.get("timestamp")

        if status == "done":
            dt = _parse_ts(last_at)
            if dt is not None and dt >= week_ago:
                done_this_week += 1
        elif status == "in_progress":
            claimed_at = _task_claimed_at(transitions)
            dt = _parse_ts(claimed_at)
            if dt is not None and dt <= stale_cutoff:
                stale_claims.append({
                    "id": t["id"], "summary": t.get("summary"),
                    "claimed_at": claimed_at, "claimed_by": meta.get("claimed_by"),
                })
        elif status == "blocked":
            blocked.append({"id": t["id"], "summary": t.get("summary")})

    documents_count = len(storage.get_nodes_by_type(CognitionNodeType.DOCUMENT))

    workflows = storage.get_nodes_by_type(CognitionNodeType.WORKFLOW)
    workflow_head_count = sum(
        1 for w in workflows
        if not storage.get_predecessors(w["id"], CognitionEdgeType.SUPERSEDES)
    )

    constraints = storage.get_nodes_by_type(CognitionNodeType.CONSTRAINT)
    active_constraints = [
        _entity_row(c) for c in constraints
        if (c.get("severity") or "normal") != "low"
        and not storage.get_predecessors(c["id"], CognitionEdgeType.SUPERSEDES)
    ]
    active_constraints.sort(key=lambda c: _SEVERITY_ORDER.get(c.get("severity") or "normal", 2))

    recent_episodes = [
        _entity_row(e)
        for e in storage.get_recent_nodes(limit=5, node_type=CognitionNodeType.EPISODE)
    ]

    incidents = storage.get_nodes_by_type(CognitionNodeType.INCIDENT)
    recent_incidents = []
    for i in incidents:
        if i.get("severity") not in _HIGH_SEVERITIES:
            continue
        dt = _parse_ts(i.get("timestamp"))
        if dt is not None and dt >= incident_cutoff:
            recent_incidents.append(_entity_row(i))
    recent_incidents.sort(key=lambda i: i.get("timestamp") or "", reverse=True)

    return JSONResponse({
        "tasks": {**counts, "done_this_week": done_this_week},
        "documents": documents_count,
        "workflows": workflow_head_count,
        "constraints": active_constraints,
        "needs_attention": {"stale_claims": stale_claims, "blocked": blocked},
        "recent_episodes": recent_episodes,
        "recent_incidents": recent_incidents,
    })


def list_documents(request):
    """List stored document nodes (metadata only — never the text or blob bytes).

    WP-DashV2 additions per row: freshness (see _document_freshness) and
    cited_by (int — count of entity/document nodes citing this document via
    a PART_OF edge INTO it; _deterministic_edge_for_pair mints entity/doc
    part_of as entity_id -> document_id, so get_predecessors on the document
    node returns the citing sources — storage.py:702-747, esp. :736-740).
    """
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    out = []
    for n in storage.get_nodes_by_type(CognitionNodeType.DOCUMENT):
        meta = n.get("metadata") or {}
        refs = n.get("references") or []
        out.append({
            "node_id": n["id"],
            "doc_ref": refs[0] if refs else None,
            "summary": n.get("summary", ""),  # for documents the title IS the summary
            "mode": meta.get("mode", "reference"),
            "size": meta.get("size"),
            "mime": meta.get("mime", ""),
            "filename": meta.get("filename", ""),
            "indexed_text_chars": meta.get("indexed_text_chars"),
            "timestamp": n.get("timestamp", ""),
            "has_blob": _document_has_blob(n),
            "freshness": _document_freshness(meta),
            "cited_by": len(storage.get_predecessors(n["id"], CognitionEdgeType.PART_OF)),
        })
    out.sort(key=lambda d: d["timestamp"], reverse=True)  # newest first
    return JSONResponse({"documents": out})


def get_workflows(request):
    """List HEAD-only workflow cards, each with its version chain inline
    (WP-DashV2). A HEAD workflow has no incoming SUPERSEDES edge — mirrors
    get_overview's workflow_head_count filter and prime.py's _format_workflows
    semantics. Superseded (non-HEAD) workflows appear ONLY inside a HEAD's
    chain, never as a top-level card. One get_superseded_chain walk per HEAD
    (chains are short by construction — versioning is manual and rare), no
    N+1 HTTP round-trips from the frontend.
    """
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    workflows = storage.get_nodes_by_type(CognitionNodeType.WORKFLOW)
    heads = [
        w for w in workflows
        if not storage.get_predecessors(w["id"], CognitionEdgeType.SUPERSEDES)
    ]

    out = []
    for w in heads:
        chain = get_superseded_chain(storage, w["id"])
        out.append({
            "id": w["id"],
            "summary": w.get("summary"),
            "author": w.get("author"),
            "timestamp": w.get("timestamp"),
            "chain": [
                {"id": c["id"], "summary": c.get("summary"), "timestamp": c.get("timestamp")}
                for c in chain
            ],
        })
    out.sort(key=lambda w: w.get("timestamp") or "", reverse=True)
    return JSONResponse({"workflows": out, "count": len(out)})


def get_activity(request):
    """Chronological activity feed across entity node types (WP-DashV2).

    Merge shape (peer-review H3, pinned): storage.get_recent_nodes takes ONE
    node_type or None=all — this is N per-type calls (one per type in
    _ACTIVITY_NODE_TYPES), merged and re-sliced to the overall limit. NOT a
    single all-types call filtered afterward: with that approach, a recent
    flood of an EXCLUDED type (tasks especially, the most active) would
    displace genuinely-recent included rows out of the fetch window before
    filtering ever saw them.

    Rows reuse _entity_row, passing `storage` (WP-DashV3) so each row also
    carries `conflicted` (see `_is_conflicted`) — Overview's calls to
    _entity_row omit `storage` deliberately, so this is the one list view
    where the flag appears; recorded_by and author still stay separate
    fields — the client's trust-class rendering (identityChipHTML) needs
    both, never a pre-collapsed author string.

    Deliberately unfiltered by HEAD/supersession and un-deduped against other
    views (e.g. Overview's active-constraints is HEAD-filtered; this shows
    ALL constraint nodes) — a chronological record, not a standing-priorities
    view; an entity can legitimately appear in both.
    """
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    try:
        limit = int(request.query_params.get("limit", _ACTIVITY_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = _ACTIVITY_DEFAULT_LIMIT
    limit = max(1, min(limit, _ACTIVITY_MAX_LIMIT))

    rows = []
    for node_type in _ACTIVITY_NODE_TYPES:
        rows.extend(
            _entity_row(n, storage) for n in storage.get_recent_nodes(limit=limit, node_type=node_type)
        )
    rows.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    rows = rows[:limit]
    return JSONResponse({"activity": rows, "count": len(rows)})


def _stamped_identity(node: dict[str, Any]) -> tuple[str, str | None]:
    """A node's ONE stamped (email, name) pair, mirroring prime._node_email's
    precedence (recorded_by, else created_by for task nodes; never the
    free-text `author` fallback) -- a local sibling, not an import, matching
    this file's existing SEVERITY_ORDER precedent of keeping the dashboard's
    import surface independent of prime.py's markdown/CLI-facing deps.
    Casefolded email; empty string when the node carries no live stamp."""
    meta = node.get("metadata", {}) or {}
    for key in ("recorded_by", "created_by"):
        stamp = meta.get(key)
        if isinstance(stamp, dict) and stamp.get("email"):
            return stamp["email"].casefold(), stamp.get("name")
    return "", None


def _registered_person_emails(storage: CognitionStorage) -> set[str]:
    """Casefolded emails of every registered PERSON node -- shared by get_people
    and get_unregistered_writers so the two views can't disagree on who counts
    as registered."""
    emails = {
        (n.get("metadata", {}).get("person", {}).get("email") or "").casefold()
        for n in storage.get_nodes_by_type(CognitionNodeType.PERSON)
    }
    emails.discard("")
    return emails


def get_unregistered_writers(request):
    """Emails stamped on graph nodes with NO matching person node (task
    5d4e2bd60d17, People-view management gap): the "who is writing to the
    graph and needs onboarding" list. One full node scan (same cost class as
    other dashboard endpoints); casefolded-email semantics match
    prime._distinct_stamped_emails via _stamped_identity above.
    """
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    registered = _registered_person_emails(storage)

    writers: dict[str, dict[str, Any]] = {}
    for n in storage.get_all_nodes():
        email, name = _stamped_identity(n)
        if not email or email in registered:
            continue
        ts = n.get("timestamp")
        w = writers.setdefault(email, {
            "email": email, "names": set(), "node_count": 0,
            "first_seen": ts, "last_seen": ts,
        })
        w["names"].add(name or email)
        w["node_count"] += 1
        if ts:
            if not w["first_seen"] or ts < w["first_seen"]:
                w["first_seen"] = ts
            if not w["last_seen"] or ts > w["last_seen"]:
                w["last_seen"] = ts

    out = [{**w, "names": sorted(w["names"])} for w in writers.values()]
    out.sort(key=lambda w: w["node_count"], reverse=True)
    return JSONResponse({"unregistered_writers": out, "count": len(out)})


def _person_activity(storage: CognitionStorage, email: str) -> dict[str, Any]:
    """Drilldown stats for one registered person's email (task 5d4e2bd60d17):
    node counts by type, last-active timestamp, currently-claimed tasks
    (status in_progress + claimed_by match), open tasks created (status open
    + created_by match). One full node scan; read-only.

    Claimed-tasks membership is checked against EVERY task node, not gated by
    the creator-stamp match node_counts/created_tasks use — a task created by
    one person and claimed by another must show under the CLAIMANT's
    claimed_tasks (see the loop body for why a naive single gate breaks this)."""
    email = (email or "").casefold()
    empty = {"node_counts": {}, "last_active": None, "claimed_tasks": [], "created_tasks": []}
    if not email:
        return empty

    node_counts: dict[str, int] = {}
    last_active: str | None = None
    claimed_tasks: list[dict[str, Any]] = []
    created_tasks: list[dict[str, Any]] = []
    for n in storage.get_all_nodes():
        ntype = n.get("type", "")

        # Vince's catch (Train A review): claiming is a distinct action from
        # creation. A task's _stamped_identity resolves to its CREATOR
        # (created_by, tasks carry no recorded_by) -- gating this check on
        # that match would skip every task claimed by X but created by
        # someone else, which is exactly the manager-assigns/subordinate-
        # claims flow this drilldown exists to answer. Evaluated against
        # EVERY task node, independent of the creator-stamp gate below.
        if ntype == CognitionNodeType.TASK.value:
            meta = n.get("metadata", {}) or {}
            claimed_by = meta.get("claimed_by")
            if (
                meta.get("status", "open") == "in_progress"
                and isinstance(claimed_by, dict)
                and (claimed_by.get("email") or "").casefold() == email
            ):
                claimed_tasks.append({"id": n["id"], "summary": n.get("summary")})

        node_email, _name = _stamped_identity(n)
        if node_email != email:
            continue
        node_counts[ntype] = node_counts.get(ntype, 0) + 1
        ts = n.get("timestamp")
        if ts and (last_active is None or ts > last_active):
            last_active = ts

        if ntype == CognitionNodeType.TASK.value:
            meta = n.get("metadata", {}) or {}
            if meta.get("status", "open") == "open":
                created_tasks.append({"id": n["id"], "summary": n.get("summary")})

    return {
        "node_counts": node_counts,
        "last_active": last_active,
        "claimed_tasks": claimed_tasks,
        "created_tasks": created_tasks,
    }


def get_people(request):
    """Roster for the People view (WP-DashV3): one row per PERSON node, sorted
    by name (mirrors cognition_list_people's sort only — see below for why the
    derivation does NOT mirror that tool's implementation).

    `reports_to_registered` (peer-review corrected): cognition_list_people's
    own derivation calls _find_person_by_email PER ROW (cognition_tools.py
    _list_people -> _reports_to_registered -> _find_person_by_email), each of
    which re-runs the full PERSON scan — O(N²) over the roster. This endpoint
    instead does ONE storage.get_nodes_by_type(PERSON) scan, builds a
    casefolded-email set from it, and does O(1) membership checks per row —
    same output, better shape. (The tool's O(N²) is a separate pre-existing
    nit, out of scope here.)
    """
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    nodes = storage.get_nodes_by_type(CognitionNodeType.PERSON)
    registered_emails = _registered_person_emails(storage)

    rows = []
    for n in nodes:
        meta = n.get("metadata", {}) or {}
        person = meta.get("person", {}) or {}
        reports_to = person.get("reports_to_email") or None
        rows.append({
            "id": n["id"],
            "name": person.get("name"),
            "email": person.get("email"),
            "role": person.get("role"),
            "seniority": person.get("seniority"),
            "reports_to_email": reports_to,
            "reports_to_registered": bool(reports_to) and (reports_to or "").casefold() in registered_emails,
            "recorded_by": meta.get("recorded_by"),
            "from_agent": meta.get("from_agent"),
            "timestamp": n.get("timestamp"),
        })
    rows.sort(key=lambda r: (r.get("name") or "").casefold())
    return JSONResponse({"people": rows, "count": len(rows)})


def download_document(request):
    """Download a stored document. Copy mode → the content-addressed blob; reference
    mode → the agent-extracted text SIDECAR (NEVER the absolute original path — that
    would be an arbitrary-local-file-read, and §9 N2 says reference mode never touches
    the original). ``node_id`` is a graph KEY only (never a path segment); the blob
    path is server-reconstructed + validated under documents_dir."""
    lc = _ctx(request)
    storage = lc["cognition_storage"]
    node_id = request.path_params["node_id"]
    node = storage.get_node(node_id)
    if not node or node.get("type") != CognitionNodeType.DOCUMENT.value:
        return JSONResponse({"error": "document not found"}, status_code=404)

    meta = node.get("metadata") or {}
    cognition_dir = storage.cognition_dir
    title = node.get("summary") or node_id

    if _document_has_blob(node):
        blob = _document_blob_path(cognition_dir, node)  # validated under documents_dir
        if blob is None or not blob.exists():
            return JSONResponse({"error": "blob not found"}, status_code=404)
        return FileResponse(
            blob,
            filename=_safe_filename(meta.get("filename") or title),
            media_type=_safe_media_type(meta.get("mime")),
        )

    # Reference mode: serve the extracted-text sidecar (sha-named, server-derived).
    sha = meta.get("sha256")
    if sha:
        sidecar = text_sidecar_path(cognition_dir, sha)
        if sidecar.exists():
            return FileResponse(
                sidecar,
                filename=_safe_filename(title) + ".txt",
                media_type="text/plain",
            )
    return JSONResponse({"error": "no downloadable artifact"}, status_code=404)
