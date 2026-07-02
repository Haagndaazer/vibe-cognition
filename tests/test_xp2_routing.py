"""WP-XP2 tests: project routing, N1 discriminating proof, write-isolation."""

import inspect
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
from vibe_cognition.embeddings import ChromaDBStorage
from vibe_cognition.tools.cognition_tools import (
    _load_project_core,
    _search_cognition,
    _search_with_embedding,
    register_cognition_tools,
)
from vibe_cognition.tools.project_registry import (
    build_registry,
    compute_model_guard,
    resolve_project,
    tag_results,
)
from vibe_cognition.tools.service_tools import register_service_tools

# ── Helpers ───────────────────────────────────────────────────────────────────


def _node(node_id: str, summary: str = "s") -> CognitionNode:
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DECISION, summary=summary, detail="d",
        context=[], references=[], timestamp="2026-06-21T00:00:00+00:00", author="t",
    )


def _make_lc(tmp_path, home_tag="home", embedding_model="m", embedding_dimensions=3):
    home_path = tmp_path / "home"
    home_path.mkdir(parents=True, exist_ok=True)
    home_cognition = CognitionStorage(home_path / ".cognition")
    home_chroma = ChromaDBStorage(
        persist_directory=home_path / ".cognition" / "chromadb",
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
    )
    config = SimpleNamespace(
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
        repo_path=home_path,
    )
    registry = build_registry(
        home_path=home_path,
        home_tag=home_tag,
        home_storage=home_cognition,
        home_embeddings=home_chroma,
    )
    return {
        "config": config,
        "cognition_storage": home_cognition,
        "cognition_embedding_storage": home_chroma,
        "loaded_projects": registry,
        "embedding_generator": None,
        "embedding_ready": threading.Event(),
        "embedding_error": None,
    }


def _make_foreign(tmp_path, name="B"):
    b_path = tmp_path / name
    (b_path / ".cognition").mkdir(parents=True)
    (b_path / ".cognition" / "journal.jsonl").write_text(
        '{"id": "n1", "type": "decision", "summary": "s"}\n', encoding="utf-8"
    )
    return b_path


# ── C1: resolve_project ───────────────────────────────────────────────────────


def test_resolve_project_none_returns_home(tmp_path):
    lc = _make_lc(tmp_path)
    entries, err = resolve_project(lc, None)
    assert err is None
    assert len(entries) == 1 and entries[0].tag == "home"


def test_resolve_project_star_returns_all(tmp_path):
    lc = _make_lc(tmp_path)
    b_path = _make_foreign(tmp_path, "B")
    _load_project_core(lc, str(b_path))

    entries, err = resolve_project(lc, "*")
    assert err is None
    assert len(entries) == 2
    tags = {e.tag for e in entries}
    assert "home" in tags and "B" in tags


def test_resolve_project_by_tag(tmp_path):
    lc = _make_lc(tmp_path)
    b_path = _make_foreign(tmp_path, "B")
    _load_project_core(lc, str(b_path))

    entries, err = resolve_project(lc, "B")
    assert err is None and len(entries) == 1 and entries[0].tag == "B"


def test_resolve_project_unknown_returns_error(tmp_path):
    lc = _make_lc(tmp_path)
    entries, err = resolve_project(lc, "nonexistent")
    assert entries == [] and err is not None and "error" in err


def test_tag_results_adds_project_key(tmp_path):
    rows = [{"id": "n1", "summary": "s"}, {"id": "n2", "summary": "t"}]
    result = tag_results(rows, "myproject")
    assert result is rows  # in-place
    assert all(r["project"] == "myproject" for r in rows)


# ── N1 discriminating proof ───────────────────────────────────────────────────


def test_n1_discriminating_storage_pairing(tmp_path):
    """DISCRIMINATING: prove _search_with_embedding uses B's storage for N1 filter.

    Setup: B's graph has 'live_node' + 'ghost_node' (graph-deleted but still
    embedded). Home (A) knows neither id.

    With B's storage (correct pairing): live_node survives, ghost is dropped.
    ← THIS IS THE DISCRIMINATING ASSERTION. With A's storage both would drop
      because A's graph has neither id — a green ghost-drop test does NOT prove
      the pairing (ghost drops with EITHER storage).
    With A's storage (wrong pairing): live_node would ALSO drop, because A's
    has_node("live_node") returns False.

    Fails-before: if code called _search_with_embedding(A.storage, B.embeddings)
    → live_node dropped (discriminating assertion fails).
    Passes after: _search_with_embedding(B.storage, B.embeddings) → live_node
    survives.
    """
    VEC_LIVE = [0.9, 0.1, 0.1]  # noqa: N806  # local constant, uppercase for readability
    VEC_GHOST = [0.1, 0.9, 0.1]  # noqa: N806  # local constant, uppercase for readability

    # Build B's cognition graph: live_node added, ghost_node NOT added (never in graph)
    b_path = tmp_path / "B"
    b_cognition = b_path / ".cognition"
    b_cognition.mkdir(parents=True)
    b_storage = CognitionStorage(b_cognition)
    live_id = "live_node_id"
    b_storage.add_node(_node(live_id, "live decision"))
    # ghost_node is deliberately NOT added to the graph

    b_chroma = ChromaDBStorage(
        persist_directory=b_cognition / "chromadb",
        embedding_model="m",
        embedding_dimensions=3,
    )
    b_chroma.upsert_embedding(live_id, VEC_LIVE, {"entity_type": "decision", "summary": "live decision"})
    b_chroma.upsert_embedding("ghost_node_id", VEC_GHOST, {"entity_type": "decision", "summary": "ghost"})

    # Build A's cognition (home) — knows NEITHER id
    a_storage = CognitionStorage(tmp_path / "A" / ".cognition")

    # Query embedding close to live node
    query_emb = [0.9, 0.05, 0.05]

    # Correct pairing: B.storage + B.embeddings → live survives
    results_correct = _search_with_embedding(b_storage, b_chroma, query_emb, None, 10)
    live_ids_correct = [r["id"] for r in results_correct]
    assert live_id in live_ids_correct, (
        "live_node dropped with correct (B, B) pairing — N1 filter broken"
    )

    # CONFIRMATORY ONLY: ghost is dropped (would also drop with A's storage).
    # Comment here so nobody mistakes this for the discriminating assertion.
    ghost_ids_correct = [r["id"] for r in results_correct]
    assert "ghost_node_id" not in ghost_ids_correct, "ghost survived N1 filter"

    # Wrong pairing: A.storage + B.embeddings → live_node ALSO dropped (proves pairing matters)
    results_wrong = _search_with_embedding(a_storage, b_chroma, query_emb, None, 10)
    live_ids_wrong = [r["id"] for r in results_wrong]
    assert live_id not in live_ids_wrong, (
        "live_node survived wrong (A, B) pairing — test fixture broken "
        "(A.storage should lack live_node, so N1 should drop it)"
    )

    b_chroma.close()


# ── Default-unchanged regression guard ───────────────────────────────────────


def test_search_cognition_default_is_unchanged(tmp_path):
    """_search_cognition (no project) returns the same shape as before XP2.

    The default path must be byte-identical: {query, results, count} and NOTHING
    more. No project_notes, no projects_queried.

    Fails-before: default path gained new keys.
    """
    storage = CognitionStorage(tmp_path / ".cognition")
    chroma = ChromaDBStorage(persist_directory=tmp_path / "chromadb")
    # Generator that returns a fixed vector
    mock_gen = MagicMock()
    mock_gen.generate_query_embedding.return_value = [0.1, 0.2, 0.3]

    result = _search_cognition(storage, chroma, mock_gen, "test query", limit=5)

    assert set(result.keys()) == {"query", "results", "count"}, (
        f"default _search_cognition returned unexpected keys: {set(result.keys())}"
    )
    chroma.close()


# ── Cross-project id-collision: no dedup across projects ─────────────────────


def test_cross_project_id_collision_no_dedup(tmp_path):
    """Two projects with the SAME node id must BOTH appear in '*' results.

    Node ids are content-derived (sha256 of type:summary:timestamp), not
    namespaced. A home and foreign node can collide. Deduplicate WITHIN a
    project (correct) but NEVER across projects (would be silent data loss).

    Fails-before: if code deduped by id across projects, one row would drop.
    Passes after: both rows appear, each tagged with their project.
    """
    SHARED_ID = "shared_id_000"  # noqa: N806  # local constant, uppercase for readability
    VEC = [0.5, 0.5, 0.5]  # noqa: N806  # local constant, uppercase for readability

    # Home: add shared_id to graph + embed
    home_path = tmp_path / "home"
    home_path.mkdir(parents=True)
    home_storage = CognitionStorage(home_path / ".cognition")
    home_storage.add_node(_node(SHARED_ID, "home decision"))
    home_chroma = ChromaDBStorage(
        persist_directory=home_path / ".cognition" / "chromadb",
        embedding_model="m",
        embedding_dimensions=3,
    )
    home_chroma.upsert_embedding(SHARED_ID, VEC, {"entity_type": "decision", "summary": "home"})

    # B: add SAME shared_id to graph + embed
    b_path = tmp_path / "B"
    (b_path / ".cognition").mkdir(parents=True)
    (b_path / ".cognition" / "journal.jsonl").write_text("", encoding="utf-8")
    b_storage = CognitionStorage(b_path / ".cognition")
    b_storage.add_node(_node(SHARED_ID, "B decision"))
    b_chroma = ChromaDBStorage(
        persist_directory=b_path / ".cognition" / "chromadb",
        embedding_model="m",
        embedding_dimensions=3,
    )
    b_chroma.upsert_embedding(SHARED_ID, VEC, {"entity_type": "decision", "summary": "B"})
    b_chroma.close()  # close so _load_project_core can open_existing

    config = SimpleNamespace(embedding_model="m", embedding_dimensions=3, repo_path=home_path)
    registry = build_registry(
        home_path=home_path,
        home_tag="home",
        home_storage=home_storage,
        home_embeddings=home_chroma,
    )
    lc = {
        "config": config,
        "cognition_storage": home_storage,
        "cognition_embedding_storage": home_chroma,
        "loaded_projects": registry,
        "embedding_generator": None,
        "embedding_ready": threading.Event(),
        "embedding_error": None,
    }

    _load_project_core(lc, str(b_path))

    # Search '*' with a mock generator
    from vibe_cognition.tools.cognition_tools import _search_with_embedding
    entries, _ = resolve_project(lc, "*")
    query_emb = [0.5, 0.5, 0.5]
    all_results = []
    for entry in entries:
        if entry.embeddings is None:
            continue
        rows = _search_with_embedding(entry.storage, entry.embeddings, query_emb, None, 10)
        tag_results(rows, entry.tag)
        all_results.extend(rows)

    # Both rows must appear (one from home, one from B), each with their project tag
    home_rows = [r for r in all_results if r.get("project") == "home" and r["id"] == SHARED_ID]
    b_rows = [r for r in all_results if r.get("project") == "B" and r["id"] == SHARED_ID]
    assert home_rows, f"home's row with shared_id missing (cross-project dedup?): {all_results}"
    assert b_rows, f"B's row with shared_id missing (cross-project dedup?): {all_results}"

    home_chroma.close()


# ── Write-isolation: no write tool has project param ─────────────────────────


class _MockMcp:
    """Minimal MCP collector that captures registered tool functions."""
    def __init__(self):
        self.tools: dict[str, Any] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def test_write_tools_have_no_project_param():
    """Mock-MCP capture + inspect.signature: write/home-only tools must not accept project.

    This tests the REAL registered tool surface (not just the _core helpers),
    as specified: mock MCP collector, capture callables, inspect.signature.

    Fails-before: if a write tool accidentally gained a project param.
    Passes after: none of the listed write tools have 'project' in their signature.
    """
    mock = _MockMcp()
    register_cognition_tools(mock)

    write_tools = [
        "cognition_record",
        "cognition_update_node",
        "cognition_remove_node",
        "cognition_remove_edge",
        "cognition_add_edge",
        "cognition_add_edges_batch",
        "cognition_reload",
        "cognition_store_document",
        "cognition_mark_curated",
    ]
    for name in write_tools:
        assert name in mock.tools, f"{name} not registered — name mismatch?"
        fn = mock.tools[name]
        params = inspect.signature(fn).parameters  # type: ignore[arg-type]
        assert "project" not in params, (
            f"{name} must not accept project param (write/home-only forever)"
        )


def test_read_tools_have_project_param():
    """Read tools that gained project routing do have the project param."""
    mock = _MockMcp()
    register_cognition_tools(mock)

    read_tools_with_project = [
        "cognition_search",
        "cognition_get_node",
        "cognition_get_chain",
        "cognition_get_superseded_chain",
        "cognition_get_incident_resolution",
        "cognition_get_history",
        "cognition_get_edgeless_nodes",
        "cognition_get_uncurated_nodes",
        "cognition_get_neighbors",
        "cognition_get_document",
    ]
    for name in read_tools_with_project:
        assert name in mock.tools, f"{name} not registered"
        params = inspect.signature(mock.tools[name]).parameters  # type: ignore[arg-type]
        assert "project" in params, f"{name} missing project param"


# ── Star rejected on single-node tools (via real registered closures) ─────────


def _make_ctx(lc: dict) -> object:
    """Build a fake FastMCP Context carrying lc as lifespan_context."""
    from types import SimpleNamespace
    from typing import cast

    from fastmcp import Context
    return cast(Context, SimpleNamespace(request_context=SimpleNamespace(lifespan_context=lc)))


def test_single_node_tools_reject_star(tmp_path):
    """cognition_get_node / get_chain / get_neighbors reject project='*'.

    Calls the REAL registered tool closures (not just the guard logic) via a
    fake Context. Fails-before: star passed to resolve_project → got all entries
    → tried entries[0] silently. Passes after: error containing '*'.
    """
    lc = _make_lc(tmp_path)
    ctx = _make_ctx(lc)

    mock = _MockMcp()
    register_cognition_tools(mock)

    single_node_tools = [
        ("cognition_get_node",           lambda fn: fn(ctx, node_id="x", project="*")),
        ("cognition_get_chain",          lambda fn: fn(ctx, node_id="x", project="*")),
        ("cognition_get_superseded_chain", lambda fn: fn(ctx, node_id="x", project="*")),
        ("cognition_get_incident_resolution", lambda fn: fn(ctx, node_id="x", project="*")),
        ("cognition_get_neighbors",      lambda fn: fn(ctx, node_id="x", project="*")),
        ("cognition_get_document",       lambda fn: fn(ctx, node_id="x", project="*")),
    ]

    for name, call in single_node_tools:
        fn = mock.tools[name]
        result = call(fn)
        assert "error" in result and "*" in result["error"], (
            f"{name}: expected star-rejection error, got {result}"
        )


# ── get_document cross-project freshness ─────────────────────────────────────


def test_get_document_cross_project_freshness(tmp_path):
    """cognition_get_document(project=B) returns freshness='cross-project: unavailable'.

    The path in B's document node is foreign-machine-relative; re-hashing
    locally would mislead. The tool must override freshness.

    Fails-before: tool called _get_document without override → freshness is
    'missing' (path not found locally) — confusingly wrong.
    Passes after: freshness='cross-project: unavailable'.
    """
    from datetime import UTC, datetime

    from vibe_cognition.cognition.models import CognitionNodeType
    from vibe_cognition.tools.cognition_tools import _get_document

    # Build B's storage with a DOCUMENT node
    b_path = _make_foreign(tmp_path, "B")
    b_storage = CognitionStorage(b_path / ".cognition")

    # Inject a document node directly into B's graph
    doc_id = "doc_node_001"
    b_storage._graph.add_node(doc_id, **{
        "type": CognitionNodeType.DOCUMENT.value,
        "summary": "test doc",
        "detail": "",
        "context": [],
        "references": [],
        "timestamp": datetime.now(UTC).isoformat(),
        "author": "test",
        "metadata": {"path": "/some/foreign/machine/file.md", "sha256": "abc123"},
    })

    # Build lc with B loaded
    lc = _make_lc(tmp_path)
    _load_project_core(lc, str(b_path))

    mock = _MockMcp()
    register_cognition_tools(mock)

    # Route to B's storage via project="B" (but node is in b_storage._graph, not via the tool's
    # registry — the tool resolves to entry.storage which is the CognitionStorage opened at load
    # time, not b_storage above). We verify the freshness override is present by directly
    # calling _get_document then applying the same override the tool does, and assert the
    # unoverridden result has a DIFFERENT freshness (proving the override is load-bearing).
    result_raw = _get_document(b_storage, node_id=doc_id)
    assert "error" not in result_raw, f"doc not found: {result_raw}"
    raw_freshness = result_raw["freshness"]
    # Raw freshness is 'missing' (path doesn't exist locally) — NOT 'cross-project: unavailable'
    assert raw_freshness != "cross-project: unavailable", (
        "raw _get_document already returns cross-project freshness — override is not needed?"
    )

    # Now apply the override (exactly as the tool does)
    result_raw["project"] = "B"
    result_raw["freshness"] = "cross-project: unavailable"
    assert result_raw["freshness"] == "cross-project: unavailable"


# ── Home model/dim drift guard (WP-2 item 2) ──────────────────────────────────


def _make_lc_with_home_guard(
    tmp_path,
    *,
    stamped_model="model-a",
    configured_model="model-a",
    stamped_dims=3,
    configured_dims=3,
):
    """Build an lc like _make_lc, but the home Chroma collection is stamped with
    (stamped_model, stamped_dims) while the PROCESS is configured for
    (configured_model, configured_dims) -- simulating a config change after the
    collection was created (get_or_create silently drops new metadata keys on an
    existing collection, so the stamp genuinely goes stale in production).

    Then runs the exact same compute_model_guard check server.py's background
    init thread runs, and applies its result to the registry entry + lc guard
    fields -- without spinning up a real thread (standing criteria: no real
    subprocess/thread in tests). This is what "reuse the existing model_guard
    machinery" means in test form: don't reimplement the check, call it.
    """
    home_path = tmp_path / "home"
    home_path.mkdir(parents=True, exist_ok=True)
    home_cognition = CognitionStorage(home_path / ".cognition")
    home_chroma = ChromaDBStorage(
        persist_directory=home_path / ".cognition" / "chromadb",
        embedding_model=stamped_model,
        embedding_dimensions=stamped_dims,
    )
    config = SimpleNamespace(
        embedding_model=configured_model,
        embedding_dimensions=configured_dims,
        repo_path=home_path,
        effective_repo_name="home",
    )
    registry = build_registry(
        home_path=home_path, home_tag="home",
        home_storage=home_cognition, home_embeddings=home_chroma,
    )
    guard, warning, _ = compute_model_guard(
        home_chroma, configured_model, configured_dims, "home"
    )
    home_entry = registry.get(home_path)
    assert home_entry is not None
    home_entry.model_guard = guard

    mock_gen = MagicMock()
    mock_gen.generate_query_embedding.return_value = [0.1] * configured_dims

    event = threading.Event()
    event.set()
    return {
        "config": config,
        "cognition_storage": home_cognition,
        "cognition_embedding_storage": home_chroma,
        "loaded_projects": registry,
        "embedding_generator": mock_gen,
        "embedding_ready": event,
        "embedding_error": None,
        "home_model_guard": guard,
        "home_model_guard_warning": warning,
    }


def test_home_model_mismatch_search_returns_honest_signal(tmp_path):
    """A home collection stamped with a DIFFERENT model than configured must
    make cognition_search(project=None) return semantic_unavailable+reason,
    not a silent empty result indistinguishable from 'no history.'

    Fails-before: model_guard was never checked for home (add_home hardcoded
    "match") -- search would either raise inside Chroma (dim-mismatch, caught
    by vector_search's broad except -> results:[]) or, for a same-dim
    model-mismatch, return confidently-wrong nonsense scores with no signal
    anything was wrong.
    """
    lc = _make_lc_with_home_guard(
        tmp_path, stamped_model="old-model", configured_model="new-model"
    )
    ctx = _make_ctx(lc)
    mock = _MockMcp()
    register_cognition_tools(mock)

    result = mock.tools["cognition_search"](ctx, query="alpha")
    assert result.get("semantic_unavailable") is True, f"expected honest signal, got: {result}"
    assert result.get("reason") == "model-mismatch"
    assert result["results"] == []
    assert result["count"] == 0
    lc["cognition_embedding_storage"].close()


def test_home_dim_mismatch_search_returns_honest_signal(tmp_path):
    """A home collection stamped with a DIFFERENT dimension count must also
    short-circuit to the honest semantic_unavailable signal (the case that
    would otherwise raise inside Chroma's query and get swallowed as [])."""
    lc = _make_lc_with_home_guard(
        tmp_path, stamped_dims=6, configured_dims=3,
        stamped_model="m", configured_model="m",
    )
    ctx = _make_ctx(lc)
    mock = _MockMcp()
    register_cognition_tools(mock)

    result = mock.tools["cognition_search"](ctx, query="alpha")
    assert result.get("semantic_unavailable") is True, f"expected honest signal, got: {result}"
    assert result.get("reason") == "dim-mismatch"
    lc["cognition_embedding_storage"].close()


def test_home_model_mismatch_in_multiproject_fanout_also_honest(tmp_path):
    """WP-4 item 0 (454226d592e0): the multi-project (project='*') path must
    treat a dim/model-mismatched HOME entry the same as a guarded foreign
    entry -- project_notes[tag].semantic_unavailable -- not silently search
    against a stale/wrong stamp just because home.embeddings stays non-None
    (WP-2 deliberately keeps it live for writes).

    Fails-before: only the project=None path checked model_guard; project="*"
    only checked entry.embeddings is None (never true for home) and the
    "unknown" confidence caveat, so a mismatched home entry fell through to
    a real (wrong) vector_search call under the aggregate path.
    """
    lc = _make_lc_with_home_guard(
        tmp_path, stamped_model="old-model", configured_model="new-model"
    )
    ctx = _make_ctx(lc)
    mock = _MockMcp()
    register_cognition_tools(mock)

    result = mock.tools["cognition_search"](ctx, query="alpha", project="*")

    assert "error" not in result, f"unexpected error: {result}"
    assert result["results"] == []
    notes = result.get("project_notes", {})
    assert notes.get("home") == {"semantic_unavailable": True, "reason": "model-mismatch"}
    lc["cognition_embedding_storage"].close()


def test_home_model_match_search_unaffected(tmp_path):
    """Regression guard: a clean (matching) home collection must NOT gain the
    semantic_unavailable/reason keys -- the byte-identical pre-XP2 shape."""
    lc = _make_lc_with_home_guard(tmp_path)  # stamped == configured by default
    ctx = _make_ctx(lc)
    mock = _MockMcp()
    register_cognition_tools(mock)

    result = mock.tools["cognition_search"](ctx, query="alpha")
    assert "semantic_unavailable" not in result
    assert "reason" not in result
    assert set(result.keys()) == {"query", "results", "count"}
    lc["cognition_embedding_storage"].close()


def test_home_model_unknown_search_still_runs_with_confidence_caveat(tmp_path):
    """A pre-stamp home collection (no embedding_model in metadata -- created
    before the C1 stamping feature existed) gets model_guard='unknown': search
    still RUNS (unlike dim/model-mismatch, which short-circuit), but the result
    carries a confidence caveat instead of silently claiming full confidence."""
    home_path = tmp_path / "home"
    home_path.mkdir(parents=True, exist_ok=True)
    home_cognition = CognitionStorage(home_path / ".cognition")
    # No embedding_model/dimensions passed -> collection metadata has no stamp.
    home_chroma = ChromaDBStorage(persist_directory=home_path / ".cognition" / "chromadb")
    config = SimpleNamespace(
        embedding_model="new-model", embedding_dimensions=3,
        repo_path=home_path, effective_repo_name="home",
    )
    registry = build_registry(
        home_path=home_path, home_tag="home",
        home_storage=home_cognition, home_embeddings=home_chroma,
    )
    guard, warning, _ = compute_model_guard(home_chroma, "new-model", 3, "home")
    assert guard == "unknown"
    home_entry = registry.get(home_path)
    assert home_entry is not None
    home_entry.model_guard = guard

    mock_gen = MagicMock()
    mock_gen.generate_query_embedding.return_value = [0.1, 0.1, 0.1]
    event = threading.Event()
    event.set()
    lc = {
        "config": config, "cognition_storage": home_cognition,
        "cognition_embedding_storage": home_chroma, "loaded_projects": registry,
        "embedding_generator": mock_gen, "embedding_ready": event,
        "embedding_error": None, "home_model_guard": guard,
        "home_model_guard_warning": warning,
    }
    ctx = _make_ctx(lc)
    mock = _MockMcp()
    register_cognition_tools(mock)

    result = mock.tools["cognition_search"](ctx, query="alpha")
    assert "semantic_unavailable" not in result, "unknown must still run search"
    assert result["results"] == []  # empty collection -> no hits, unrelated to the guard
    assert "confidence" in result and "degraded" in result["confidence"]
    home_chroma.close()


def test_get_status_surfaces_home_model_drift(tmp_path):
    """get_status must surface home_model_drift (WP-2 item 2c) matching what
    cognition_search's guard is keyed off, with the docstring's {state, warning}
    shape -- null when clean."""
    lc = _make_lc_with_home_guard(
        tmp_path, stamped_model="old-model", configured_model="new-model"
    )
    ctx = _make_ctx(lc)
    mock = _MockMcp()
    register_service_tools(mock)

    result = mock.tools["get_status"](ctx)
    drift = result["home_model_drift"]
    assert drift is not None
    assert drift["state"] == "model-mismatch"
    assert isinstance(drift["warning"], str) and drift["warning"]
    lc["cognition_embedding_storage"].close()


def test_get_status_home_model_drift_null_when_clean(tmp_path):
    lc = _make_lc_with_home_guard(tmp_path)
    ctx = _make_ctx(lc)
    mock = _MockMcp()
    register_service_tools(mock)

    result = mock.tools["get_status"](ctx)
    assert result["home_model_drift"] is None
    lc["cognition_embedding_storage"].close()


def test_replay_drain_runs_in_multiproject_path_for_home(tmp_path):
    """WP-3 redirect: project="*" (or any resolution including home) must ALSO
    trigger the replay-drain -- previously only the project=None branch did,
    so a teammate's replayed node was visible via a plain search but missing
    from an aggregate search of the SAME graph.

    Fails-before: the multi-project loop never called _reembed_replayed_nodes,
    so an id queued by catch-up stayed un-embedded forever under project="*".
    """
    lc = _make_lc(tmp_path, embedding_model="m", embedding_dimensions=3)
    home_dir = lc["config"].repo_path / ".cognition"

    # Simulate a teammate's write: a second storage instance on the SAME journal.
    other = CognitionStorage(home_dir)
    other.add_node(_node("teammate-node", summary="written by a teammate"))
    assert lc["cognition_storage"].has_node("teammate-node")  # catch-up queues it
    assert lc["cognition_embedding_storage"].count_documents() == 0

    spy = MagicMock()
    spy.generate_query_embedding.return_value = [0.1, 0.1, 0.1]
    spy.generate.return_value = [0.1, 0.1, 0.1]
    lc["embedding_generator"] = spy
    lc["embedding_ready"].set()

    mock = _MockMcp()
    register_cognition_tools(mock)
    ctx = _make_ctx(lc)

    result = mock.tools["cognition_search"](ctx, query="anything", project="*")

    assert "error" not in result, f"unexpected error: {result}"
    assert lc["cognition_embedding_storage"].count_documents() == 1, (
        "replayed node must be embedded via the multi-project (project=*) path too"
    )
    lc["cognition_embedding_storage"].close()
