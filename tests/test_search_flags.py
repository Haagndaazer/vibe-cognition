"""WP-SearchFlags: conflict/supersession provenance flags (`conflicted`,
`superseded_by`) on every cognition_search result. Each test names the specific
failure mode it guards; several are written to fail before their fix exists."""

import threading
from types import SimpleNamespace
from typing import cast

from fastmcp import Context

from vibe_cognition.cognition import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    CognitionStorage,
)
from vibe_cognition.cognition.queries import conflict_details
from vibe_cognition.embeddings import ChromaDBStorage, EmbeddingGenerator
from vibe_cognition.tools.cognition_tools import (
    _load_project_core,
    _search_cognition,
    register_cognition_tools,
)
from vibe_cognition.tools.project_registry import build_registry

# ── Helpers ───────────────────────────────────────────────────────────────────


def _entity(
    node_id: str,
    node_type: CognitionNodeType = CognitionNodeType.DECISION,
    *,
    summary: str = "s",
    timestamp: str = "2026-07-15T00:00:00+00:00",
) -> CognitionNode:
    return CognitionNode(
        id=node_id, type=node_type, summary=summary, detail="d",
        context=[], references=[], severity=None,
        timestamp=timestamp, author="t", metadata={},
    )


def _upsert(
    embed: ChromaDBStorage,
    node_id: str,
    vec: list[float],
    node_type: CognitionNodeType = CognitionNodeType.DECISION,
    summary: str = "s",
) -> None:
    embed.upsert_embedding(node_id, vec, {"entity_type": node_type.value, "summary": summary})


class _FixedGen:
    """Every query embeds to the SAME fixed vector -- these tests control document
    vectors directly (same convention as WP-TC9/TC10's fixtures)."""

    def __init__(self, vec: list[float]) -> None:
        self._vec = vec

    def generate(self, text: str, input_type: str = "document") -> list[float]:
        return self._vec

    def generate_query_embedding(self, text: str) -> list[float]:
        return self._vec


def _make_ctx(lc: dict) -> Context:
    return cast(Context, SimpleNamespace(request_context=SimpleNamespace(lifespan_context=lc)))


class _MockMcp:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self):
        def decorator(fn):
            import asyncio
            import functools
            import inspect
            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                def sync_shim(*args, **kwargs):
                    return asyncio.run(fn(*args, **kwargs))
                self.tools[fn.__name__] = sync_shim
            else:
                self.tools[fn.__name__] = fn
            return fn
        return decorator


def _add_edge(s: CognitionStorage, from_id: str, to_id: str, edge_type: CognitionEdgeType,
              timestamp: str = "2026-07-15T00:00:00+00:00") -> None:
    s.add_edge(CognitionEdge(from_id=from_id, to_id=to_id, edge_type=edge_type,
                              timestamp=timestamp, source="test"))


# ── conflicted: bidirectional contradicts ──────────────────────────────────────


def test_conflicted_true_for_incoming_contradicts(tmp_path):
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("a"))
    s.add_node(_entity("b"))
    _add_edge(s, "b", "a", CognitionEdgeType.CONTRADICTS)
    _upsert(embed, "a", [1.0, 0.0, 0.0])
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=5)
    hit = next(r for r in res["results"] if r["id"] == "a")
    assert hit["conflicted"] is True
    embed.close()


def test_conflicted_true_for_outgoing_contradicts_fails_before(tmp_path):
    """Fails-before: an incoming-only membership check misses this -- 'a' is the
    SOURCE (outgoing) side of the contradicts edge, the exact V1-banner bug class
    (pattern 6ed494680fb3)."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("a"))
    s.add_node(_entity("b"))
    _add_edge(s, "a", "b", CognitionEdgeType.CONTRADICTS)
    _upsert(embed, "a", [1.0, 0.0, 0.0])
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=5)
    hit = next(r for r in res["results"] if r["id"] == "a")
    assert hit["conflicted"] is True
    embed.close()


def test_conflicted_and_superseded_by_always_present_and_clean_on_isolated_hit(tmp_path):
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("clean"))
    _upsert(embed, "clean", [1.0, 0.0, 0.0])
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=5)
    hit = next(r for r in res["results"] if r["id"] == "clean")
    assert "conflicted" in hit and hit["conflicted"] is False
    assert "superseded_by" in hit and hit["superseded_by"] is None
    embed.close()


# ── superseded_by: incoming-only, id not bool ──────────────────────────────────


def test_superseded_by_points_to_newer_node_id_not_a_bool(tmp_path):
    """Fails-before: a bool-only implementation would give True/False instead of
    the newer node's id; a backwards-direction implementation would leave 'old'
    unmarked and/or mark 'new' instead."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("old", summary="old"))
    s.add_node(_entity("new", summary="new"))
    _add_edge(s, "new", "old", CognitionEdgeType.SUPERSEDES)
    _upsert(embed, "old", [1.0, 0.0, 0.0], summary="old")
    _upsert(embed, "new", [0.99, 0.01, 0.0], summary="new")
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=5)
    rows = {r["id"]: r for r in res["results"]}
    assert rows["old"]["superseded_by"] == "new"
    assert rows["new"]["superseded_by"] is None  # the resolution, not itself superseded
    embed.close()


def test_superseded_by_branch_case_uses_node_timestamp_not_edge_timestamp(tmp_path):
    """B2 trap (peer-review corrected): two incoming SUPERSEDES edges on 'old'.
    newer1's NODE is authored LATER than newer2's, but its EDGE was minted
    EARLIER (as if backfilled) -- the tie-break must read
    storage.get_node(source_id)['timestamp'] (authorship time), never the edge's
    own 'timestamp' (mint time), or this picks newer2 instead of newer1."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("old", summary="old", timestamp="2026-01-01T00:00:00+00:00"))
    s.add_node(_entity("newer1", summary="newer1", timestamp="2026-06-01T00:00:00+00:00"))
    s.add_node(_entity("newer2", summary="newer2", timestamp="2026-02-01T00:00:00+00:00"))
    # newer1: later node authorship, EARLIER edge mint time
    _add_edge(s, "newer1", "old", CognitionEdgeType.SUPERSEDES, timestamp="2026-01-05T00:00:00+00:00")
    # newer2: earlier node authorship, LATER edge mint time (as if backfilled)
    _add_edge(s, "newer2", "old", CognitionEdgeType.SUPERSEDES, timestamp="2026-12-01T00:00:00+00:00")
    _upsert(embed, "old", [1.0, 0.0, 0.0], summary="old")
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=5)
    hit = next(r for r in res["results"] if r["id"] == "old")
    assert hit["superseded_by"] == "newer1"
    embed.close()


# ── Gate D S4 replay shape ──────────────────────────────────────────────────────


def test_gate_d_s4_replay_shape_junior_find_flagged_by_senior_revision(tmp_path):
    """Closes the Gate D S4 audit finding: a junior's original finding, a senior's
    later revision, and the SUPERSEDES edge between them (same shape as the audit
    scenario) -- search must return the junior hit WITH superseded_by pointing at
    the senior node, so a consumer reading only the junior hit still learns it has
    been revised."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("junior_find", CognitionNodeType.DISCOVERY, summary="junior's original finding"))
    s.add_node(_entity("senior_revision", CognitionNodeType.DISCOVERY, summary="senior's revision"))
    _add_edge(s, "senior_revision", "junior_find", CognitionEdgeType.SUPERSEDES)
    _upsert(embed, "junior_find", [1.0, 0.0, 0.0],
            CognitionNodeType.DISCOVERY, summary="junior's original finding")
    _upsert(embed, "senior_revision", [0.95, 0.05, 0.0],
            CognitionNodeType.DISCOVERY, summary="senior's revision")
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=5)
    rows = {r["id"]: r for r in res["results"]}
    assert rows["junior_find"]["superseded_by"] == "senior_revision"
    embed.close()


# ── No ranking change (pinned decision) ─────────────────────────────────────────


def test_ranking_unaffected_by_conflicted_or_superseded_flags(tmp_path):
    """No ranking change (pinned decision): a hit that is BOTH superseded and
    conflicted keeps the exact weighted_score/score its raw similarity earns --
    identical to a clean hit with the same raw score. Proves the new flags carry
    no silent down-weight."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("flagged", summary="flagged hit"))
    s.add_node(_entity("newer", summary="newer"))
    s.add_node(_entity("clean", summary="clean hit"))
    _add_edge(s, "newer", "flagged", CognitionEdgeType.SUPERSEDES)
    _add_edge(s, "flagged", "clean", CognitionEdgeType.CONTRADICTS)
    same_vec = [1.0, 0.0, 0.0]
    _upsert(embed, "flagged", same_vec, summary="flagged hit")
    _upsert(embed, "clean", same_vec, summary="clean hit")
    _upsert(embed, "newer", [0.5, 0.5, 0.0], summary="newer")
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=5)
    rows = {r["id"]: r for r in res["results"]}
    assert rows["flagged"]["conflicted"] is True
    assert rows["flagged"]["superseded_by"] == "newer"
    assert rows["flagged"]["score"] == rows["clean"]["score"]
    assert rows["flagged"]["weighted_score"] == rows["clean"]["weighted_score"]
    embed.close()


# ── conflict_details: sibling to conflict_flags, names counterparties ──────────


def test_conflict_details_unit_names_both_directions_with_author_and_reason(tmp_path):
    """conflict_details is bidirectional (incoming AND outgoing CONTRADICTS) and
    resolves each counterparty's author from its recorded_by stamp when present,
    falling back to free-text author otherwise; reason comes from the edge."""
    s = CognitionStorage(tmp_path / "cog")
    s.add_node(_entity("a", summary="claim a"))
    s.add_node(CognitionNode(
        id="b", type=CognitionNodeType.DECISION, summary="claim b", detail="d",
        context=[], references=[], timestamp="2026-07-15T00:00:00+00:00",
        author="free text author", metadata={"recorded_by": {"name": "Stamped Name", "email": "s@x.com"}},
    ))
    s.add_node(CognitionNode(
        id="c", type=CognitionNodeType.DECISION, summary="claim c", detail="d",
        context=[], references=[], timestamp="2026-07-15T00:00:00+00:00",
        author="free text only",
    ))
    _add_edge(s, "b", "a", CognitionEdgeType.CONTRADICTS)  # incoming on "a"
    s.add_edge(CognitionEdge(
        from_id="a", to_id="c", edge_type=CognitionEdgeType.CONTRADICTS,
        timestamp="2026-07-15T00:00:00+00:00", source="test", reason="scope disagreement",
    ))  # outgoing on "a"

    details = conflict_details(s, "a")
    by_id = {d["id"]: d for d in details}
    assert set(by_id) == {"b", "c"}
    assert by_id["b"]["author"] == "Stamped Name"  # recorded_by name wins over free-text author
    assert by_id["b"]["reason"] is None  # edge minted with no reason
    assert by_id["c"]["author"] == "free text only"  # falls back when no recorded_by
    assert by_id["c"]["reason"] == "scope disagreement"


def test_conflict_details_unit_clean_node_returns_empty(tmp_path):
    s = CognitionStorage(tmp_path / "cog")
    s.add_node(_entity("clean"))

    assert conflict_details(s, "clean") == []


def test_conflict_details_unit_skips_dangling_edge_target(tmp_path):
    """A CONTRADICTS edge pointing at a node id the graph no longer has a node
    for (deleted, edge not yet cleaned up) is silently skipped -- never a row
    for a node that doesn't exist. add_edge() itself refuses this, so the
    dangling state is reproduced by writing directly to the underlying graph,
    the same transient shape a deleted-but-not-yet-reconciled edge would leave."""
    s = CognitionStorage(tmp_path / "cog")
    s.add_node(_entity("a"))
    s._graph.add_edge(
        "ghost", "a", key=CognitionEdgeType.CONTRADICTS.value,
        type=CognitionEdgeType.CONTRADICTS.value,
        timestamp="2026-07-15T00:00:00+00:00", source="test", reason=None,
    )

    assert conflict_details(s, "a") == []


# ── conflicted_with wired into cognition_search ─────────────────────────────────


def test_search_conflicted_with_present_only_when_conflicted(tmp_path):
    """conflicted_with names the counterparty ONLY for conflicted hits -- a clean
    hit never pays the conflict_details cost and never carries the key."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("a", summary="claim a"))
    s.add_node(CognitionNode(
        id="b", type=CognitionNodeType.DECISION, summary="claim b", detail="d",
        context=[], references=[], timestamp="2026-07-15T00:00:00+00:00",
        author="t", metadata={"recorded_by": {"name": "Author B", "email": "b@x.com"}},
    ))
    s.add_node(_entity("clean", summary="clean hit"))
    s.add_edge(CognitionEdge(
        from_id="b", to_id="a", edge_type=CognitionEdgeType.CONTRADICTS,
        timestamp="2026-07-15T00:00:00+00:00", source="test", reason="conflicting scope",
    ))
    _upsert(embed, "a", [1.0, 0.0, 0.0], summary="claim a")
    _upsert(embed, "clean", [1.0, 0.0, 0.0], summary="clean hit")
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=5)
    rows = {r["id"]: r for r in res["results"]}
    assert rows["a"]["conflicted_with"] == [
        {"id": "b", "summary": "claim b", "author": "Author B", "reason": "conflicting scope"}
    ]
    assert "conflicted_with" not in rows["clean"]
    embed.close()


# ── Multi-project: flags computed against each entry's OWN graph ───────────────


def test_multiproject_foreign_graph_flags_computed_against_its_own_graph(tmp_path):
    """A foreign-graph hit with a supersedes edge is flagged from ITS graph (the
    fan-out already threads each entry's own storage into the formatter -- no
    extra work, but worth locking in with a test)."""
    home_path = tmp_path / "home"
    home_path.mkdir(parents=True)
    home_storage = CognitionStorage(home_path / ".cognition")
    home_storage.add_node(_entity("home1", summary="home hit"))
    home_chroma = ChromaDBStorage(
        persist_directory=home_path / ".cognition" / "chromadb",
        embedding_model="m", embedding_dimensions=3,
    )
    _upsert(home_chroma, "home1", [1.0, 0.0, 0.0], summary="home hit")

    b_path = tmp_path / "B"
    (b_path / ".cognition").mkdir(parents=True)
    (b_path / ".cognition" / "journal.jsonl").write_text("", encoding="utf-8")
    b_storage = CognitionStorage(b_path / ".cognition")
    b_storage.add_node(_entity("b_old", summary="b old"))
    b_storage.add_node(_entity("b_new", summary="b new"))
    b_storage.add_edge(CognitionEdge(
        from_id="b_new", to_id="b_old", edge_type=CognitionEdgeType.SUPERSEDES,
        timestamp="2026-07-15T00:00:00+00:00", source="test",
    ))
    b_chroma = ChromaDBStorage(
        persist_directory=b_path / ".cognition" / "chromadb",
        embedding_model="m", embedding_dimensions=3,
    )
    _upsert(b_chroma, "b_old", [1.0, 0.0, 0.0], summary="b old")
    _upsert(b_chroma, "b_new", [0.99, 0.01, 0.0], summary="b new")
    b_chroma.close()

    config = SimpleNamespace(embedding_model="m", embedding_dimensions=3, repo_path=home_path)
    registry = build_registry(
        home_path=home_path, home_tag="home",
        home_storage=home_storage, home_embeddings=home_chroma,
    )
    ready = threading.Event()
    ready.set()
    lc = {
        "config": config, "cognition_storage": home_storage,
        "cognition_embedding_storage": home_chroma, "loaded_projects": registry,
        "embedding_generator": cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0])),
        "embedding_ready": ready, "embedding_error": None,
    }
    _load_project_core(lc, str(b_path))

    mock = _MockMcp()
    register_cognition_tools(mock)
    ctx = _make_ctx(lc)

    result = mock.tools["cognition_search"](ctx, query="q", project="*", limit=10)
    rows = {(r["project"], r["id"]): r for r in result["results"]}
    assert rows[("B", "b_old")]["superseded_by"] == "b_new"
    assert rows[("home", "home1")]["superseded_by"] is None
    home_chroma.close()
