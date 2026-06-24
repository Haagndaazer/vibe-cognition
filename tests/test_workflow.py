"""WP-Workflow-Node: workflow first-class node type tests.

Covers the acceptance criteria from docs/wp-workflow-node-plan.md:
- workflow is a valid cognition_record node_type; detail round-trips via get_node
- long (>1000-word) procedure is chunked and the chunks land in ChromaDB
- update_node is blocked on workflow nodes (B5) with the supersession error message
- supersession chain: v2 + supersedes edge -> get_superseded_chain returns [v2, v1]
- get_workflow_head resolves any version to the HEAD (from mid-chain or oldest)
- cognition_get_workflow tool resolves to HEAD regardless of which version matched
- same-summary v1/v2 at near-identical timestamps get distinct minted ids (collision regression)
- superseded workflow node's chunks survive supersession (N6)
- no auto-edge forms on workflow-involving pairs (B1) -- see also test_deterministic_edges.py
"""



from vibe_cognition.cognition import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    CognitionStorage,
    get_workflow_head,
)
from vibe_cognition.tools.cognition_tools import (
    _embed_workflow,
    register_cognition_tools,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _long_body(marker: str = "alpha", word_count: int = 1100) -> str:
    """Generate a long procedure body that contains the given marker word."""
    words = f"{marker} step " + ("lorem ipsum " * (word_count // 2))
    return words.strip()


def _make_workflow_node(node_id: str, summary: str, detail: str) -> CognitionNode:
    from datetime import UTC, datetime
    return CognitionNode(
        id=node_id,
        type=CognitionNodeType.WORKFLOW,
        summary=summary,
        detail=detail,
        context=[],
        references=[],
        severity=None,
        timestamp=datetime.now(UTC).isoformat(),
        author="tester",
    )


# ── record + get_node round-trip ──────────────────────────────────────────────


def test_cognition_record_workflow_valid_type(build_lc, make_ctx, mock_mcp, tmp_path):
    """cognition_record: workflow node_type is accepted; detail round-trips via get_node.

    Fails-before: if WORKFLOW was absent from CognitionNodeType and _parse_node_type
    returned an error dict instead of the enum.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_record"](
        ctx,
        node_type="workflow",
        summary="how to deploy to staging",
        detail="Step 1: build. Step 2: push. Step 3: verify.",
        context="deploy,staging",
        author="tester",
    )
    assert "error" not in result, f"record failed: {result}"
    assert result["type"] == "workflow"
    node_id = result["id"]

    got = mock_mcp.tools["cognition_get_node"](ctx, node_id=node_id)
    assert got["type"] == "workflow"
    assert got["detail"] == "Step 1: build. Step 2: push. Step 3: verify."


# ── chunking ──────────────────────────────────────────────────────────────────


def test_embed_workflow_creates_node_vector_and_chunks(fake_generator, tmp_path):
    """_embed_workflow: creates one node-level vector + N chunk vectors in ChromaDB.

    Fails-before: if the function embedded only the node vector (like _embed_entity_node)
    and skipped chunking, long procedures would be unsearchable past the nomic truncation.
    """
    from vibe_cognition.embeddings import ChromaDBStorage
    chroma = ChromaDBStorage(
        persist_directory=tmp_path / "chromadb",
        embedding_model="m",
        embedding_dimensions=3,
    )
    node = _make_workflow_node("wf1", "deploy procedure", _long_body("alpha"))
    chunks_written = _embed_workflow(chroma, fake_generator, node)  # type: ignore[arg-type]
    assert chunks_written > 0

    # node-level vector exists
    total = chroma.count_documents()
    assert total >= chunks_written + 1  # at minimum: 1 node vector + N chunk vectors

    # chunk vectors tagged with node_id
    chunk_count = chroma.count_documents(filter={"node_id": "wf1"})
    assert chunk_count == chunks_written


def test_cognition_record_workflow_creates_chunks(build_lc, make_ctx, mock_mcp, tmp_path):
    """cognition_record(workflow): long body → chunks in ChromaDB when embeddings ready.

    Fails-before: if _record_node called _embed_entity_node (not _embed_workflow) for
    workflow nodes, the body would be silently truncated by nomic and unchunked.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_record"](
        ctx,
        node_type="workflow",
        summary="long deploy runbook",
        detail=_long_body("beta"),
        context="deploy",
        author="tester",
    )
    assert "error" not in result
    node_id = result["id"]

    chroma = lc["cognition_embedding_storage"]
    chunk_count = chroma.count_documents(filter={"node_id": node_id})
    assert chunk_count > 0, "workflow body was not chunked"


# ── update_node block (B5) ────────────────────────────────────────────────────


def test_update_node_blocked_on_workflow(build_lc, make_ctx, mock_mcp, tmp_path):
    """cognition_update_node: workflow nodes return a supersession error, not a patch.

    Fails-before: if _update_node applied the patch and re-embedded via _embed_entity_node,
    leaving stale chunk orphans in ChromaDB (the silent search corruption B5 prevents).
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    rec = mock_mcp.tools["cognition_record"](
        ctx, node_type="workflow", summary="onboarding procedure",
        detail="Step 1: setup. Step 2: read docs.", context="onboarding", author="tester",
    )
    node_id = rec["id"]

    result = mock_mcp.tools["cognition_update_node"](
        ctx, node_id=node_id, summary="updated title",
    )
    assert "error" in result
    assert "supersession" in result["error"]


# ── get_workflow_head ─────────────────────────────────────────────────────────


def test_get_workflow_head_from_oldest_returns_head(tmp_path):
    """get_workflow_head: starting from the OLDEST version returns the HEAD (newest).

    Fails-before: if get_workflow_head walked OUTGOING supersedes (like get_superseded_chain)
    instead of INCOMING, it would return the same node it started from.
    """
    storage = CognitionStorage(tmp_path / ".cognition")
    v1 = _make_workflow_node("v1", "procedure v1", "old steps")
    v2 = _make_workflow_node("v2", "procedure v2", "updated steps")
    v3 = _make_workflow_node("v3", "procedure v3", "latest steps")
    for n in (v1, v2, v3):
        storage.add_node(n)

    from datetime import UTC, datetime
    ts = datetime.now(UTC).isoformat()
    # v2 supersedes v1, v3 supersedes v2
    storage.add_edge(CognitionEdge(from_id="v2", to_id="v1", edge_type=CognitionEdgeType.SUPERSEDES, timestamp=ts))
    storage.add_edge(CognitionEdge(from_id="v3", to_id="v2", edge_type=CognitionEdgeType.SUPERSEDES, timestamp=ts))

    # Starting from v1 (oldest) → should return v3 (head)
    assert get_workflow_head(storage, "v1") == "v3"
    # Starting from v2 (mid-chain) → should return v3 (head)
    assert get_workflow_head(storage, "v2") == "v3"
    # Starting from v3 (head) → should return v3 (no-op)
    assert get_workflow_head(storage, "v3") == "v3"


def test_get_workflow_head_cycle_safe(tmp_path):
    """get_workflow_head: a cycle in SUPERSEDES predecessors doesn't hang.

    Fails-before: if the cycle guard was absent, a malformed graph could cause an
    infinite loop (the same risk get_superseded_chain's visited-set guards against).
    """
    storage = CognitionStorage(tmp_path / ".cognition")
    from datetime import UTC, datetime
    ts = datetime.now(UTC).isoformat()
    v1 = _make_workflow_node("c1", "cyclic v1", "detail")
    v2 = _make_workflow_node("c2", "cyclic v2", "detail")
    for n in (v1, v2):
        storage.add_node(n)
    storage.add_edge(CognitionEdge(from_id="c2", to_id="c1", edge_type=CognitionEdgeType.SUPERSEDES, timestamp=ts))
    storage.add_edge(CognitionEdge(from_id="c1", to_id="c2", edge_type=CognitionEdgeType.SUPERSEDES, timestamp=ts))
    # Must terminate without hanging; which node is returned is implementation-defined for cycles
    result = get_workflow_head(storage, "c1")
    assert isinstance(result, str)


# ── supersession chain ────────────────────────────────────────────────────────


def test_supersession_chain_shape(build_lc, make_ctx, mock_mcp, tmp_path):
    """cognition_get_superseded_chain: v2 supersedes v1 → chain is [v2, v1], newest first.

    Fails-before: if the supersedes edge was recorded backwards (v1→v2 instead of v2→v1),
    the chain would be [v1] only (get_superseded_chain follows successors, i.e. older nodes).
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    r1 = mock_mcp.tools["cognition_record"](
        ctx, node_type="workflow", summary="release procedure", detail="v1 steps",
        context="release", author="tester",
    )
    r2 = mock_mcp.tools["cognition_record"](
        ctx, node_type="workflow", summary="release procedure v2", detail="v2 steps",
        context="release", author="tester",
    )
    # v2 supersedes v1
    mock_mcp.tools["cognition_add_edge"](
        ctx, from_id=r2["id"], to_id=r1["id"], edge_type="supersedes",
    )

    chain_result = mock_mcp.tools["cognition_get_superseded_chain"](ctx, node_id=r2["id"])
    chain = chain_result["chain"]
    assert len(chain) == 2
    assert chain[0]["id"] == r2["id"]  # newest first
    assert chain[1]["id"] == r1["id"]


# ── cognition_get_workflow ────────────────────────────────────────────────────


def test_cognition_get_workflow_returns_head(build_lc, make_ctx, mock_mcp, tmp_path):
    """cognition_get_workflow: resolves to HEAD even when an old version is the best match.

    Fails-before: if the tool returned the matched node directly without resolving via
    get_workflow_head, a search match on v1 (old procedure) would return the stale body.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    # Create v1 with "alpha" marker so it matches an "alpha" query
    r1 = mock_mcp.tools["cognition_record"](
        ctx, node_type="workflow", summary="alpha onboarding", detail="alpha steps v1",
        context="onboarding", author="tester",
    )
    # Create v2 (same topic) with "alpha" marker too
    r2 = mock_mcp.tools["cognition_record"](
        ctx, node_type="workflow", summary="alpha onboarding v2", detail="alpha steps v2 updated",
        context="onboarding", author="tester",
    )
    mock_mcp.tools["cognition_add_edge"](
        ctx, from_id=r2["id"], to_id=r1["id"], edge_type="supersedes",
    )

    result = mock_mcp.tools["cognition_get_workflow"](ctx, name_or_topic="alpha onboarding")
    assert "error" not in result, f"cognition_get_workflow failed: {result}"
    assert result["head"]["id"] == r2["id"], "HEAD must be v2, not v1"
    assert "chain" in result
    assert "matched" in result


def test_cognition_get_workflow_not_ready_returns_error(build_lc, make_ctx, mock_mcp, tmp_path):
    """cognition_get_workflow: returns error dict (not raise) when embeddings not ready.

    Fails-before: if require_embeddings was not called first and the tool tried to generate
    an embedding with an uninitialized generator.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=False)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_get_workflow"](ctx, name_or_topic="anything")
    assert "error" in result


def test_cognition_get_workflow_rejects_star(build_lc, make_ctx, mock_mcp, tmp_path):
    """cognition_get_workflow: project="*" returns error (single-node tools reject star).

    Fails-before: if the star check was absent, the project arg would be passed down to
    resolve_project which also rejects it — but later and with a less actionable message.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_get_workflow"](ctx, name_or_topic="anything", project="*")
    assert "error" in result
    assert '"*"' in result["error"] or "not supported" in result["error"]


# ── collision regression (WP-ID) ─────────────────────────────────────────────


def test_workflow_same_summary_collision_regression(build_lc, make_ctx, mock_mcp, tmp_path):
    """cognition_record: two workflow nodes with identical summary get distinct ids.

    Fails-before: if mint_unique_id=True was NOT used in _record_node for workflows,
    two records at the same coarse-clock tick would collide and the second would silently
    overwrite the first (data loss — the pre-WP-ID bug).
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    r1 = mock_mcp.tools["cognition_record"](
        ctx, node_type="workflow", summary="deploy procedure",
        detail="v1 body", context="deploy", author="tester",
    )
    r2 = mock_mcp.tools["cognition_record"](
        ctx, node_type="workflow", summary="deploy procedure",
        detail="v2 body", context="deploy", author="tester",
    )
    assert r1["id"] != r2["id"], "same-summary workflow records must have distinct ids"

    # Both must exist in the graph
    storage: CognitionStorage = lc["cognition_storage"]
    assert storage.get_node(r1["id"]) is not None
    assert storage.get_node(r2["id"]) is not None


# ── chunk survival after supersession (N6) ────────────────────────────────────


def test_superseded_workflow_chunks_survive_supersession(fake_generator, tmp_path):
    """_embed_workflow: creating a new (superseding) node does NOT purge the old node's chunks.

    Fails-before: if _embed_workflow purged chunks by the *old* node_id when embedding a
    NEW node, superseded history would silently disappear from ChromaDB.
    """
    from vibe_cognition.embeddings import ChromaDBStorage
    chroma = ChromaDBStorage(
        persist_directory=tmp_path / "chromadb",
        embedding_model="m",
        embedding_dimensions=3,
    )
    v1 = _make_workflow_node("wf_v1", "procedure v1", _long_body("alpha"))
    v2 = _make_workflow_node("wf_v2", "procedure v2", _long_body("beta"))

    _embed_workflow(chroma, fake_generator, v1)  # type: ignore[arg-type]
    v1_chunks = chroma.count_documents(filter={"node_id": "wf_v1"})
    assert v1_chunks > 0

    _embed_workflow(chroma, fake_generator, v2)  # type: ignore[arg-type]
    # v1's chunks must STILL exist — supersession doesn't purge historical nodes
    v1_chunks_after = chroma.count_documents(filter={"node_id": "wf_v1"})
    assert v1_chunks_after == v1_chunks, (
        "superseded workflow node's chunks were purged when v2 was embedded — "
        "supersession = new node, old node retained as history (N6)"
    )
