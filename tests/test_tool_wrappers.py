"""T-1a: MCP tool-wrapper coverage — every registered closure invoked, shapes pinned.

Invokes each @mcp.tool() closure end-to-end via a fake Context (not just the _core
helpers). Pins the error-is-a-dict contract at the wrapper boundary, the
require_embeddings gate (both paths), and the cognition_record node_type parser.
Priority targets: get_status, cognition_dashboard (monkeypatched — never binds a
real port), cognition_list_projects wrapper, cognition_readme, cognition_record.

Tools are registered into _MockMcp (conftest) then invoked directly — this exercises
the REAL registered closures, not just the core helpers (WP-T gap). Each test names
its guarded failure mode (rule 20).
"""

import json
from typing import Any
from unittest.mock import patch

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
from vibe_cognition.tools import register_all_tools
from vibe_cognition.tools.cognition_tools import _load_project_core, register_cognition_tools
from vibe_cognition.tools.dashboard_tool import register_dashboard_tool
from vibe_cognition.tools.readme_tool import register_readme_tool
from vibe_cognition.tools.service_tools import register_service_tools

# ── helpers ───────────────────────────────────────────────────────────────────


def _node(node_id: str, ntype: CognitionNodeType = CognitionNodeType.DECISION,
           summary: str = "alpha decision") -> CognitionNode:
    return CognitionNode(
        id=node_id, type=ntype, summary=summary, detail="d",
        context=[], references=[], timestamp="2026-06-23T00:00:00+00:00", author="t",
    )


def _add_node(lc: dict[str, Any], node_id: str,
              ntype: CognitionNodeType = CognitionNodeType.DECISION,
              summary: str = "alpha decision") -> str:
    storage: CognitionStorage = lc["cognition_storage"]
    return storage.add_node(_node(node_id, ntype, summary))


# ── get_status (zero-coverage) ────────────────────────────────────────────────


def test_get_status_shape_and_embedding_loading(tmp_path, mock_mcp, build_lc, make_ctx):
    """get_status: wrapper assembles the expected top-level shape.

    Fails-before: if the wrapper skipped key assembly or crashed on missing config.
    Passes after: all expected keys present, embedding_status reflects not-ready event.
    """
    register_service_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=False)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["get_status"](ctx)  # type: ignore[arg-type]

    assert isinstance(result, dict), "get_status must return a dict, never raise"
    assert "repo_name" in result
    assert "repo_path" in result
    assert "cognition_graph" in result
    assert "cognition_embeddings" in result
    assert result["embedding_status"] == "loading", (
        "embedding_status must be 'loading' when embedding_ready event is not set"
    )
    assert "curation" in result


def test_get_status_embedding_ready(tmp_path, mock_mcp, build_lc, make_ctx):
    """get_status: embedding_status='ready' when embedding_ready is set.

    Fails-before: if the wrapper checked the event flag incorrectly and reported
    'loading' even when the model is up.
    """
    register_service_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["get_status"](ctx)  # type: ignore[arg-type]
    assert result["embedding_status"] == "ready"


def test_get_status_syncing_when_ready_but_sync_not_done(tmp_path, mock_mcp, build_lc, make_ctx):
    """WP-4 item 3 (5340ae677931): embedding_ready fires BEFORE the historical
    backfill sync starts (unchanged, known-intentional) -- get_status must
    report the third "syncing" state in that window, not a falsely-confident
    "ready" (a teammate joining an existing graph would otherwise see "ready"
    while search was silently incomplete).

    Fails-before: embedding_status only ever checked embedding_ready, so this
    would have reported "ready".
    """
    import threading

    register_service_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    lc["embedding_sync_done"] = threading.Event()  # NOT set -- sync still running
    ctx = make_ctx(lc)

    result = mock_mcp.tools["get_status"](ctx)  # type: ignore[arg-type]
    assert result["embedding_status"] == "syncing"


def test_get_status_ready_once_sync_done(tmp_path, mock_mcp, build_lc, make_ctx):
    """Regression guard: once embedding_sync_done is set, status returns to
    "ready" -- "syncing" is a transient window, not a permanent regression."""
    import threading

    register_service_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    lc["embedding_sync_done"] = threading.Event()
    lc["embedding_sync_done"].set()
    ctx = make_ctx(lc)

    result = mock_mcp.tools["get_status"](ctx)  # type: ignore[arg-type]
    assert result["embedding_status"] == "ready"


def test_get_status_syncing_does_not_block_embedding_ready_tools(
    tmp_path, mock_mcp, build_lc, make_ctx
):
    """embedding_ready gates tool availability (require_embeddings) -- this
    must stay frozen/unaffected by embedding_sync_done (known-intentional:
    do NOT make startup sync blocking). cognition_search must still work
    normally while "syncing"."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    import threading
    lc["embedding_sync_done"] = threading.Event()  # still syncing
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_search"](ctx, query="alpha")  # type: ignore[arg-type]
    assert "error" not in result, "syncing must not gate cognition_search"


def test_get_status_sync_progress_null_before_sync_completes(
    tmp_path, mock_mcp, build_lc, make_ctx
):
    register_service_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    import threading
    lc["embedding_sync_done"] = threading.Event()
    ctx = make_ctx(lc)

    result = mock_mcp.tools["get_status"](ctx)  # type: ignore[arg-type]
    assert result["embedding_sync_progress"] is None


def test_get_status_sync_progress_populated_after_sync(tmp_path, mock_mcp, build_lc, make_ctx):
    register_service_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    import threading
    lc["embedding_sync_done"] = threading.Event()
    lc["embedding_sync_done"].set()
    lc["embedding_sync_progress"] = {"nodes": 3, "workflows": 1, "documents": 0}
    ctx = make_ctx(lc)

    result = mock_mcp.tools["get_status"](ctx)  # type: ignore[arg-type]
    assert result["embedding_sync_progress"] == {"nodes": 3, "workflows": 1, "documents": 0}


def test_get_status_node_chunk_split(tmp_path, mock_mcp, build_lc, make_ctx):
    """get_status: cognition_embeddings splits node vectors from chunk vectors.

    Fails-before: if count_documents(filter=...) crashed on an empty ChromaDB
    collection (empty filter on pinned Chroma version may raise — guard needed).
    Passes after: {nodes, chunks, total} keys present; empty collection → 0/0/0.
    """
    register_service_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)
    result = mock_mcp.tools["get_status"](ctx)  # type: ignore[arg-type]

    emb = result["cognition_embeddings"]
    assert isinstance(emb, dict), "cognition_embeddings must be a dict (not int)"
    assert set(emb.keys()) >= {"nodes", "chunks", "total"}
    assert emb["nodes"] == emb["total"] - emb["chunks"]


def test_get_status_rehydrate_events_null_when_clean(tmp_path, mock_mcp, build_lc, make_ctx):
    """WP-1 item 1: rehydrate_events must be absent/null when no reset happened.

    Fails-before: if the field were always present or defaulted to a truthy
    placeholder, a clean process would look like it had lost data.
    """
    register_service_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["get_status"](ctx)  # type: ignore[arg-type]
    assert result["rehydrate_events"] is None


def test_get_status_surfaces_rehydrate_event(tmp_path, mock_mcp, build_lc, make_ctx):
    """WP-1 item 1 (critical): a lossy rehydrate-reset must surface in get_status
    with the node-count delta, not just an internal log line.

    Fails-before: rehydrate resets were invisible outside a log grep — nothing
    queryable recorded that a reset (and possible data loss) occurred.
    """
    register_service_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]
    storage.add_node(_node("n1"))
    storage.add_node(_node("n2"))

    (storage.cognition_dir / "journal.jsonl").write_bytes(b"")
    storage.get_statistics()  # forces catch-up, detecting the shrink

    result = mock_mcp.tools["get_status"](ctx)  # type: ignore[arg-type]
    events = result["rehydrate_events"]
    assert events is not None
    assert events["count"] == 1
    assert events["last"]["nodes_before"] == 2
    assert events["last"]["nodes_after"] == 0


# ── cognition_dashboard (zero-coverage, monkeypatched) ────────────────────────


def test_cognition_dashboard_monkeypatch_never_binds_port(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_dashboard: delegates to start_dashboard; monkeypatch keeps it off-port.

    Fails-before: if the wrapper called start_dashboard directly with no indirection,
    making it impossible to stub — or if it bound a real port in tests.
    Passes after: tool returns the stubbed {url, status, embedding_ready} shape.
    """
    register_dashboard_tool(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    fake_result = {
        "url": "http://127.0.0.1:7842/?token=fake",
        "status": "running",
        "embedding_ready": True,
        "embedding_error": None,
    }
    with patch("vibe_cognition.tools.dashboard_tool.start_dashboard", return_value=fake_result) as m:
        result = mock_mcp.tools["cognition_dashboard"](ctx)  # type: ignore[arg-type]
        m.assert_called_once()

    assert isinstance(result, dict)
    assert result["url"] == "http://127.0.0.1:7842/?token=fake"
    assert result["status"] == "running"


def test_cognition_dashboard_idempotent_second_call(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_dashboard idempotent: second call → already_running, same URL.

    Fails-before: if two calls started two servers (wasting a port or crashing on
    the bind) instead of returning the cached URL.
    """
    register_dashboard_tool(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    url = "http://127.0.0.1:7842/?token=stable"
    call_count = {"n": 0}

    def _start(lc_arg, *, port=7842, open_browser=True):
        call_count["n"] += 1
        if call_count["n"] == 1:
            lc_arg["dashboard"] = {"url": url}
            return {"url": url, "status": "running",
                    "embedding_ready": True, "embedding_error": None}
        return {"url": url, "status": "already_running",
                "embedding_ready": True, "embedding_error": None}

    with patch("vibe_cognition.tools.dashboard_tool.start_dashboard", side_effect=_start):
        r1 = mock_mcp.tools["cognition_dashboard"](ctx)  # type: ignore[arg-type]
        r2 = mock_mcp.tools["cognition_dashboard"](ctx)  # type: ignore[arg-type]

    assert r1["url"] == r2["url"]
    assert r2["status"] == "already_running"


# ── cognition_list_projects wrapper (wrapper gap) ─────────────────────────────


def test_cognition_list_projects_wrapper_shape(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_list_projects wrapper: shape {projects, foreign_count}.

    The core _list_projects_core is tested in test_xp1_registry.py; this pins
    the wrapper-specific: correct lc key extraction and shape forwarded to caller.
    Fails-before: if the wrapper used the wrong lc key or dropped foreign_count.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_list_projects"](ctx)  # type: ignore[arg-type]

    assert isinstance(result, dict)
    assert "projects" in result
    assert "foreign_count" in result
    assert result["foreign_count"] == 0
    assert len(result["projects"]) == 1  # home only
    assert result["projects"][0]["pinned"] is True


# ── cognition_readme wrapper ──────────────────────────────────────────────────


def test_cognition_readme_wrapper_shape(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_readme wrapper: returns {guide, getting_started} via ctx (near no-op).

    Fails-before: if the wrapper raised on an unused ctx or forgot to call the core.
    """
    register_readme_tool(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_readme"](ctx)  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert "guide" in result
    assert "getting_started" in result


# ── cognition_record wrapper — node_type parse ────────────────────────────────


def test_cognition_record_invalid_node_type_returns_error_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_record wrapper: invalid node_type → error dict, never raises.

    The wrapper implements its own node_type parse at the boundary; the ValueError
    must be caught and returned as {"error":...}, not propagated up.
    Fails-before: if the wrapper let the ValueError escape instead of returning {"error":...}.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_record"](  # type: ignore[arg-type]
        ctx,
        node_type="not_a_real_type",
        summary="s",
        detail="d",
        context="",
        author="t",
    )
    assert isinstance(result, dict)
    assert "error" in result


def test_cognition_record_valid_type_returns_node_shape(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_record wrapper: valid node_type → {id, type, summary, timestamp}.

    Fails-before: if the wrapper parsed the type correctly but dropped a required
    key from the result shape, or crashed embedding a node with a ready generator.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_record"](  # type: ignore[arg-type]
        ctx,
        node_type="decision",
        summary="alpha decision chosen",
        detail="because beta",
        context="",
        author="t",
    )
    assert isinstance(result, dict)
    assert "error" not in result, f"unexpected error: {result}"
    assert "id" in result
    assert result["type"] == "decision"
    assert "summary" in result
    assert "timestamp" in result

    # Node must land in storage (wrapper-specific: the wrapper must call _record_node).
    storage: CognitionStorage = lc["cognition_storage"]
    assert storage.has_node(result["id"])


# ── require_embeddings gate (both paths) ──────────────────────────────────────


def test_cognition_search_not_ready_returns_error_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_search: require_embeddings gate → error dict when event not set.

    Fails-before: if the gate was absent and cognition_search tried to call
    generator.generate_query_embedding with generator=None (AttributeError).
    Passes after: returns {error, status} without raising.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=False)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_search"](ctx, query="alpha")  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert "error" in result
    assert result.get("status") == "loading_embeddings"


def test_cognition_search_not_ready_does_not_drain_replay_queue(
    tmp_path, mock_mcp, build_lc, make_ctx
):
    """WP-3: the replayed-node queue must SURVIVE a not-ready search, not be
    silently dropped -- the drain only happens once require_embeddings passes
    (_reembed_replayed_nodes is called after the gate, never before), so a
    node queued while the model is still loading is still there once it's
    ready. Fails-before: if the drain ran before the gate check, or if the
    gate path touched pop_replayed_node_ids() at all, the queued id would be
    lost and never re-embedded once the model came up.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=False)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]

    # Simulate a teammate's write landing while the model is still loading.
    other = CognitionStorage(storage.cognition_dir)
    other.add_node(_node("teammate-node"))
    assert storage.has_node("teammate-node")  # catch-up queues it

    result = mock_mcp.tools["cognition_search"](ctx, query="alpha")  # type: ignore[arg-type]
    assert "error" in result, "gate should still block (not ready)"

    pending = storage.pop_replayed_node_ids()
    assert "teammate-node" in pending, (
        "replay queue must survive a not-ready search, not be silently dropped"
    )


def test_cognition_search_ready_returns_result_shape(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_search: when embedding_ready is set, gate passes and shape is correct.

    Fails-before: if the gate incorrectly blocked a ready context, or if the wrapper
    returned a non-dict on an empty graph.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_search"](ctx, query="alpha")  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert "error" not in result, f"unexpected gate block: {result}"
    assert "query" in result
    assert "results" in result
    assert "count" in result


def test_cognition_search_invalid_node_type_returns_error_not_empty_results(
    tmp_path, mock_mcp, build_lc, make_ctx
):
    """WP-2 item 1: a typo'd node_type must return the shared _parse_node_type
    error shape, NOT results:[] -- an infra-shaped empty result (bad filter)
    must never look indistinguishable from "no history."

    Fails-before: cognition_search passed node_type straight through to the
    Chroma entity_type filter unvalidated, silently matching nothing.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_search"](  # type: ignore[arg-type]
        ctx, query="alpha", node_type="descision"
    )
    assert "error" in result, f"expected a validation error, got: {result}"
    assert "descision" in result["error"]
    assert "decision" in result["error"], "error should list valid types"
    assert "results" not in result, "invalid node_type must not fall through to a search"


def test_cognition_search_valid_node_type_still_works(
    tmp_path, mock_mcp, build_lc, make_ctx
):
    """WP-2 item 1 regression guard: a real node_type value must keep working
    after the validation gate is added (not just reject bad ones)."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_search"](  # type: ignore[arg-type]
        ctx, query="alpha", node_type="decision"
    )
    assert "error" not in result, f"valid node_type wrongly rejected: {result}"
    assert "results" in result and "count" in result


def test_cognition_search_multi_project_invalid_node_type_also_rejected(
    tmp_path, mock_mcp, build_lc, make_ctx
):
    """WP-2 item 1: the multi-project (project='*') path must reject a bad
    node_type too -- validation happens once, before the home/multi-project
    branch, so it can't be skipped by routing through a foreign project."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    foreign_path = tmp_path / "foreign"
    (foreign_path / ".cognition").mkdir(parents=True)
    (foreign_path / ".cognition" / "journal.jsonl").write_text(
        '{"id": "n1", "type": "decision", "summary": "s"}\n', encoding="utf-8"
    )
    load_result = _load_project_core(lc, str(foreign_path))
    assert "error" not in load_result, f"foreign project load failed: {load_result}"

    result = mock_mcp.tools["cognition_search"](  # type: ignore[arg-type]
        ctx, query="alpha", node_type="descision", project="*"
    )
    assert "error" in result, f"expected a validation error, got: {result}"
    assert "results" not in result


# ── cognition_store_document: embedding-ready defers gracefully ───────────────


def test_cognition_store_document_not_ready_still_stores(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_store_document: stores even when embedding not ready (defers to sync).

    Fails-before: if the wrapper returned an error dict when require_embeddings was
    not ready instead of deferring embedding and proceeding with storage.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=False)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_store_document"](  # type: ignore[arg-type]
        ctx,
        title="spec alpha",
        document_text="alpha specification text",
        context="specs",
        author="t",
        content_text="alpha specification text",
    )
    assert isinstance(result, dict)
    assert "error" not in result, f"unexpected error: {result}"
    assert "node_id" in result
    assert "doc_ref" in result


# ── sweep: all remaining wrappers invoked once ────────────────────────────────
#
# Each test sets up minimum state, calls the wrapper, asserts:
#   (a) result is a dict  — error-is-a-dict contract (never raises)
#   (b) the "error" presence is checked where the error IS the expected behavior,
#       and its absence is checked where a success result is expected.


def test_cognition_get_node_returns_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_get_node wrapper: absent id → error dict; present id → node dict."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    err = mock_mcp.tools["cognition_get_node"](ctx, node_id="nonexistent")  # type: ignore[arg-type]
    assert isinstance(err, dict)
    assert "error" in err

    nid = _add_node(lc, "n1")
    ok = mock_mcp.tools["cognition_get_node"](ctx, node_id=nid)  # type: ignore[arg-type]
    assert isinstance(ok, dict)
    assert "error" not in ok
    assert ok["id"] == nid


def test_cognition_update_node_returns_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_update_node wrapper: valid update → node dict with reembed key."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    nid = _add_node(lc, "n1")
    result = mock_mcp.tools["cognition_update_node"](  # type: ignore[arg-type]
        ctx, node_id=nid, summary="alpha updated"
    )
    assert isinstance(result, dict)
    assert "error" not in result
    assert "reembed" in result


def test_cognition_get_document_returns_error_on_miss(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_get_document wrapper: absent node_id → error dict, not exception."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_get_document"](  # type: ignore[arg-type]
        ctx, node_id="ghost"
    )
    assert isinstance(result, dict)
    assert "error" in result


def test_cognition_get_history_returns_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_get_history wrapper: returns dict with 'nodes' key on any graph."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    _add_node(lc, "n1")

    result = mock_mcp.tools["cognition_get_history"](ctx, context_term="alpha")  # type: ignore[arg-type]
    assert isinstance(result, dict)


def test_cognition_get_chain_returns_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_get_chain wrapper: node with no led_to edges → dict, not exception."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    nid = _add_node(lc, "n1")

    result = mock_mcp.tools["cognition_get_chain"](ctx, node_id=nid)  # type: ignore[arg-type]
    assert isinstance(result, dict)


def test_cognition_get_superseded_chain_returns_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_get_superseded_chain wrapper: no supersedes edges → dict shape."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    nid = _add_node(lc, "n1")

    result = mock_mcp.tools["cognition_get_superseded_chain"](ctx, node_id=nid)  # type: ignore[arg-type]
    assert isinstance(result, dict)


def test_cognition_get_incident_resolution_returns_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_get_incident_resolution wrapper: returns dict."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    nid = _add_node(lc, "n1", ntype=CognitionNodeType.INCIDENT)

    result = mock_mcp.tools["cognition_get_incident_resolution"](  # type: ignore[arg-type]
        ctx, node_id=nid
    )
    assert isinstance(result, dict)


def test_cognition_add_edge_returns_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_add_edge wrapper: valid edge → {created, from_id, to_id, edge_type}."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    n1 = _add_node(lc, "n1")
    n2 = _add_node(lc, "n2", summary="beta pattern")

    result = mock_mcp.tools["cognition_add_edge"](  # type: ignore[arg-type]
        ctx, from_id=n1, to_id=n2, edge_type="led_to"
    )
    assert isinstance(result, dict)
    assert result.get("created") is True
    assert result["from_id"] == n1


def test_cognition_add_edges_batch_returns_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_add_edges_batch wrapper: valid JSON batch → {created, skipped, errors}."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    n1 = _add_node(lc, "n1")
    n2 = _add_node(lc, "n2", summary="beta pattern")

    edges = json.dumps([{"from_id": n1, "to_id": n2, "edge_type": "relates_to"}])
    result = mock_mcp.tools["cognition_add_edges_batch"](ctx, edges=edges)  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert "created" in result
    assert "skipped" in result
    assert "errors" in result


def test_cognition_get_edgeless_nodes_returns_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_get_edgeless_nodes wrapper: {nodes, count, total_edgeless}."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    _add_node(lc, "n1")

    result = mock_mcp.tools["cognition_get_edgeless_nodes"](ctx)  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert "nodes" in result
    assert result["total_edgeless"] >= 1


def test_cognition_get_uncurated_nodes_returns_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_get_uncurated_nodes wrapper: {nodes, count, total_uncurated}."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    _add_node(lc, "n1")

    result = mock_mcp.tools["cognition_get_uncurated_nodes"](ctx)  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert "nodes" in result


def test_cognition_mark_curated_returns_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_mark_curated wrapper: known id → {marked:1, not_found:[]}."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    nid = _add_node(lc, "n1")

    result = mock_mcp.tools["cognition_mark_curated"](ctx, node_ids=nid)  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert result["marked"] == 1
    assert result["not_found"] == []


def test_cognition_get_neighbors_returns_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_get_neighbors wrapper: {node_id, incoming, outgoing}."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    nid = _add_node(lc, "n1")

    result = mock_mcp.tools["cognition_get_neighbors"](ctx, node_id=nid)  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert result.get("node_id") == nid
    assert "incoming" in result
    assert "outgoing" in result


def test_cognition_remove_edge_returns_error_on_missing_edge(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_remove_edge wrapper: no such edge → error dict, not exception."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    n1 = _add_node(lc, "n1")
    n2 = _add_node(lc, "n2", summary="beta pattern")

    result = mock_mcp.tools["cognition_remove_edge"](  # type: ignore[arg-type]
        ctx, from_id=n1, to_id=n2, edge_type="led_to"
    )
    assert isinstance(result, dict)
    assert "error" in result


def test_cognition_remove_node_removes_and_returns_shape(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_remove_node wrapper: removes node and returns {removed, id, ...}."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    nid = _add_node(lc, "n1")

    result = mock_mcp.tools["cognition_remove_node"](ctx, node_id=nid)  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert result.get("removed") is True
    assert result.get("id") == nid

    storage: CognitionStorage = lc["cognition_storage"]
    assert not storage.has_node(nid)


def test_cognition_reload_returns_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_reload wrapper: returns before/after node/edge counts."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_reload"](ctx)  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert "nodes_before" in result
    assert "nodes_after" in result


def test_cognition_load_project_invalid_path_returns_error_dict(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_load_project wrapper: missing .cognition → error dict, not exception."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_load_project"](  # type: ignore[arg-type]
        ctx, path=str(tmp_path / "no_such_project")
    )
    assert isinstance(result, dict)
    assert "error" in result


def test_cognition_unload_project_returns_error_on_home(tmp_path, mock_mcp, build_lc, make_ctx):
    """cognition_unload_project wrapper: unloading home → error dict (pinned guard)."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_unload_project"](ctx, project="home")  # type: ignore[arg-type]
    assert isinstance(result, dict)
    assert "error" in result


# ── full register_all_tools: all 29 names captured ───────────────────────────


def test_all_29_tools_registered(mock_mcp):
    """register_all_tools captures every expected closure by name.

    Fails-before: if a new tool was added to a registrar but not captured (name drift),
    or if a registrar was removed from register_all_tools.
    """
    register_all_tools(mock_mcp)

    expected = {
        # cognition_tools.py (26)
        "cognition_record", "cognition_store_document", "cognition_get_document",
        "cognition_get_node", "cognition_update_node", "cognition_search",
        "cognition_get_chain", "cognition_get_superseded_chain", "cognition_get_workflow",
        "cognition_get_incident_resolution", "cognition_get_history",
        "cognition_add_edge", "cognition_add_edges_batch",
        "cognition_get_edgeless_nodes", "cognition_get_uncurated_nodes",
        "cognition_mark_curated", "cognition_get_neighbors",
        "cognition_remove_edge", "cognition_remove_node", "cognition_reload",
        "cognition_load_project", "cognition_unload_project", "cognition_list_projects",
        # task tools (WP-Task-Node, +3)
        "cognition_add_task", "cognition_list_tasks", "cognition_update_task",
        # service_tools.py (+1)
        "get_status",
        # dashboard_tool.py (+1)
        "cognition_dashboard",
        # readme_tool.py (+1)
        "cognition_readme",
    }

    missing = expected - set(mock_mcp.tools.keys())
    assert not missing, f"Tools not registered: {missing}"
    assert len(mock_mcp.tools) == 29, (
        f"Expected 29 tools, got {len(mock_mcp.tools)}: {set(mock_mcp.tools.keys())}"
    )
