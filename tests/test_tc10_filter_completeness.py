"""WP-TC10: per-author exclude_people filter (cognition_search, cognition_list_tasks,
addendum) + "returned N of M" search/get_history completeness (total_found/exhaustive,
M4). Each test names the specific failure mode it guards and is written to fail before
its fix exists."""

import threading
from types import SimpleNamespace
from typing import cast

from vibe_cognition.cognition import CognitionNode, CognitionNodeType, CognitionStorage
from vibe_cognition.embeddings import ChromaDBStorage, EmbeddingGenerator
from vibe_cognition.tools.cognition_tools import (
    _list_tasks,
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
    severity: str | None = None,
    recorded_by: dict | None = None,
    author: str = "t",
) -> CognitionNode:
    metadata: dict = {}
    if recorded_by is not None:
        metadata["recorded_by"] = recorded_by
    return CognitionNode(
        id=node_id, type=node_type, summary=summary, detail="d",
        context=[], references=[], severity=severity,
        timestamp="2026-07-15T00:00:00+00:00", author=author, metadata=metadata,
    )


def _task(node_id: str, *, summary: str = "t", created_by: dict | None = None) -> CognitionNode:
    metadata: dict = {"status": "open"}
    if created_by is not None:
        metadata["created_by"] = created_by
    return CognitionNode(
        id=node_id, type=CognitionNodeType.TASK, summary=summary, detail="d",
        context=[], references=[], severity="normal",
        timestamp="2026-07-15T00:00:00+00:00", author="t", metadata=metadata,
    )


class _FixedGen:
    """Every text (document or query) embeds to the SAME fixed vector — deliberate:
    these tests care about which nodes are found/excluded/counted, not score
    differentiation (unlike the re-embed tests, which need a text-keyed embedder)."""

    def __init__(self, vec):
        self._vec = vec

    def generate(self, text, input_type="document"):
        return self._vec

    def generate_query_embedding(self, text):
        return self._vec


ALICE = {"name": "Alice", "email": "alice@x.com"}
BOB = {"name": "Bob", "email": "bob@x.com"}


# ── exclude_people: cognition_search core (_search_cognition) ─────────────────


def test_exclude_people_absent_by_default(tmp_path):
    """No exclude_people passed -> excluded_count/excluded_for are ABSENT, not
    present-with-zero. Fails-before: the keys were unconditionally emitted."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("n1", recorded_by=ALICE))
    embed.upsert_embedding("n1", [1.0, 0.0, 0.0], {"entity_type": "decision"})
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=10)
    assert "excluded_count" not in res
    assert "excluded_for" not in res
    embed.close()


def test_exclude_people_widens_past_excluded_flood_and_reports_terminating_round(tmp_path):
    """The core exclude_people behavior: excluded hits are dropped BEFORE the limit
    fill, so adaptive widening refills with non-excluded candidates -- and
    excluded_count reflects only the TERMINATING round (never accumulated across
    widening rounds, since each round's dedupe recomputes from scratch).

    limit=2 -> n starts at 10. 15 alice-authored (excluded) nodes all score 1.0
    (rank ahead of bob's); 2 bob-authored nodes score 0.99. Round 1 (n=10): top 10
    are all alice -> 0 survive exclusion, len(results)==n so widen. Round 2 (n=20):
    all 17 live nodes returned -> 15 alice excluded (this round's count), 2 bob
    survive and fill the limit. total_found must be 2 (post-exclusion, terminating
    round), NOT 17 or 15.

    Fails-before: capping _format_search_results to `limit` before returning would
    silently drop the refill candidates found in round 2, or excluded_count would
    read 25 (10+15, wrongly accumulated) instead of 15."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    for i in range(15):
        nid = f"alice{i:02d}"
        s.add_node(_entity(nid, recorded_by=ALICE))
        embed.upsert_embedding(nid, [1.0, 0.0, 0.0], {"entity_type": "decision"})
    for i in range(2):
        nid = f"bob{i:02d}"
        s.add_node(_entity(nid, recorded_by=BOB))
        embed.upsert_embedding(nid, [0.99, 0.01, 0.0], {"entity_type": "decision"})
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=2, exclude_people="alice@x.com")

    ids = {r["id"] for r in res["results"]}
    assert ids == {"bob00", "bob01"}, f"widening did not refill past the excluded flood: {ids}"
    assert res["count"] == 2
    assert res["total_found"] == 2, "total_found must be the terminating round's post-exclusion count"
    assert res["excluded_count"] == 15, "excluded_count must be the terminating round's count, not accumulated"
    assert res["excluded_for"] == ["alice@x.com"]
    embed.close()


def test_exclude_people_unstamped_node_never_excluded(tmp_path):
    """A node whose free-text `author` field happens to equal the excluded email
    but carries NO server-resolved recorded_by stamp is NEVER excluded (unstamped
    != matches) -- and does not count toward excluded_count. Fails-before: matching
    against the free-text author field would wrongly drop it."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("n1", author="alice@x.com"))  # no recorded_by stamp at all
    embed.upsert_embedding("n1", [1.0, 0.0, 0.0], {"entity_type": "decision"})
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=10, exclude_people="alice@x.com")

    assert [r["id"] for r in res["results"]] == ["n1"], "unstamped node was wrongly excluded"
    assert "excluded_count" not in res, "nothing was actually excluded -- keys must stay absent"


def test_exclude_people_constraint_and_incident_never_excluded(tmp_path):
    """Never-wipe carve-out (same doctrine as TC9): a constraint/incident hit from
    an excluded author is NEVER dropped, and passing it through does not increment
    excluded_count -- but an ordinary decision by the SAME author IS excluded,
    proving this is a type-specific carve-out, not a blanket exemption."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("con1", CognitionNodeType.CONSTRAINT, recorded_by=ALICE))
    s.add_node(_entity("inc1", CognitionNodeType.INCIDENT, recorded_by=ALICE))
    s.add_node(_entity("dec1", CognitionNodeType.DECISION, recorded_by=ALICE))
    for nid, et in (("con1", "constraint"), ("inc1", "incident"), ("dec1", "decision")):
        embed.upsert_embedding(nid, [1.0, 0.0, 0.0], {"entity_type": et})
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=10, exclude_people="alice@x.com")

    ids = {r["id"] for r in res["results"]}
    assert ids == {"con1", "inc1"}, f"constraint/incident must survive, decision must be dropped: {ids}"
    assert res["excluded_count"] == 1, "only the decision counts as excluded, not the exempt pair"
    embed.close()


def test_exclude_people_casefold_and_key_absence_when_nothing_matches(tmp_path):
    """excluded_for is casefolded (input mixed-case, node stamp lowercase, both
    normalize and match); a param that matches NOTHING leaves both keys absent
    (never a silent present-with-zero)."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("n1", recorded_by=ALICE))
    embed.upsert_embedding("n1", [1.0, 0.0, 0.0], {"entity_type": "decision"})
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    matches = _search_cognition(s, embed, gen, "q", limit=10, exclude_people="ALICE@X.COM")
    assert matches["results"] == []
    assert matches["excluded_count"] == 1
    assert matches["excluded_for"] == ["alice@x.com"], "excluded_for must be casefolded"

    nothing = _search_cognition(s, embed, gen, "q", limit=10, exclude_people="nobody@nowhere.com")
    assert [r["id"] for r in nothing["results"]] == ["n1"]
    assert "excluded_count" not in nothing
    assert "excluded_for" not in nothing
    embed.close()


# ── total_found / exhaustive (M4): cognition_search core ──────────────────────


def test_total_found_exhaustive_true_under_limit(tmp_path):
    """Fewer live matches than `limit` -> Chroma runs dry on round 1 -> exhaustive
    True, total_found == count exactly."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    for i in range(3):
        nid = f"n{i}"
        s.add_node(_entity(nid))
        embed.upsert_embedding(nid, [1.0, 0.0, 0.0], {"entity_type": "decision"})
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=10)
    assert res["count"] == 3
    assert res["total_found"] == 3
    assert res["exhaustive"] is True
    embed.close()


def test_total_found_exceeds_count_when_capped_at_limit(tmp_path):
    """More live matches than `limit` within a single (non-widening) round ->
    stopped because the limit was hit, NOT because Chroma ran dry -> exhaustive
    False, and total_found reports the FULL round's count (10), honestly
    exceeding `count` (2) -- the whole point of M4. Fails-before: the old
    _format_search_results capped its own return to `limit`, so a caller could
    never see this gap."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    for i in range(20):
        nid = f"n{i:02d}"
        s.add_node(_entity(nid))
        embed.upsert_embedding(nid, [1.0, 0.0, 0.0], {"entity_type": "decision"})
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=2)
    assert res["count"] == 2
    assert res["total_found"] == 10, "n starts at limit*5=10; round 1 alone already fills the limit"
    assert res["exhaustive"] is False
    assert res["count"] < res["total_found"]
    embed.close()


# ── exclude_people: cognition_list_tasks core (_list_tasks, binding addendum) ──


def test_list_tasks_exclude_people_drops_matching_created_by(tmp_path):
    s = CognitionStorage(tmp_path / "cog")
    s.add_node(_task("t-alice-1", created_by=ALICE))
    s.add_node(_task("t-alice-2", created_by=ALICE))
    s.add_node(_task("t-bob-1", created_by=BOB))

    out = _list_tasks(s, exclude_people="alice@x.com")

    ids = {t["id"] for t in out["tasks"]}
    assert ids == {"t-bob-1"}
    assert out["excluded_count"] == 2
    assert out["excluded_for"] == ["alice@x.com"]


def test_list_tasks_exclude_people_unstamped_task_never_excluded(tmp_path):
    """A task with no created_by stamp at all survives -- unstamped is never
    excludable, same doctrine as search."""
    s = CognitionStorage(tmp_path / "cog")
    s.add_node(_task("t-nostamp", created_by=None))

    out = _list_tasks(s, exclude_people="alice@x.com")

    assert {t["id"] for t in out["tasks"]} == {"t-nostamp"}
    assert "excluded_count" not in out


def test_list_tasks_exclude_people_absent_keys_by_default(tmp_path):
    s = CognitionStorage(tmp_path / "cog")
    s.add_node(_task("t1", created_by=ALICE))

    out = _list_tasks(s)
    assert "excluded_count" not in out
    assert "excluded_for" not in out


# ── get_recent_nodes(with_total=True) additive contract ───────────────────────


def test_get_recent_nodes_with_total_is_additive(tmp_path):
    """with_total=False (default, every pre-existing caller) returns a plain list,
    UNCHANGED. with_total=True returns (sliced_list, exact_total) -- total is the
    full matching count BEFORE the limit slice."""
    s = CognitionStorage(tmp_path / "cog")
    for i in range(5):
        s.add_node(_entity(f"n{i}"))

    default = s.get_recent_nodes(limit=2)
    assert isinstance(default, list) and len(default) == 2

    sliced, total = s.get_recent_nodes(limit=2, with_total=True)
    assert len(sliced) == 2
    assert total == 5


# ── total_found/exhaustive on cognition_get_history (both branches) ───────────


def test_get_history_recency_branch_total_found_and_exhaustive(tmp_path):
    """Recency branch (no context_term): total_found is the exact pre-slice count
    (always exact -- a full structural scan, never a floor); exhaustive is always
    True (unlike cognition_search's adaptive vector search)."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    for i in range(5):
        s.add_node(_entity(f"n{i}", CognitionNodeType.PATTERN))
    config = SimpleNamespace(embedding_model="m", embedding_dimensions=3, repo_path=tmp_path)
    registry = build_registry(home_path=tmp_path, home_tag="home", home_storage=s, home_embeddings=embed)
    lc = {
        "config": config, "cognition_storage": s, "cognition_embedding_storage": embed,
        "loaded_projects": registry, "embedding_generator": None,
        "embedding_ready": threading.Event(), "embedding_error": None,
    }
    mock = _MockMcpLocal()
    register_cognition_tools(mock)
    ctx = _ctx(lc)

    result = mock.tools["cognition_get_history"](ctx, node_type="pattern", limit=2)

    assert result["count"] == 2
    assert result["total_found"] == 5
    assert result["exhaustive"] is True
    embed.close()


def test_get_history_context_term_branch_total_found_and_exhaustive(tmp_path):
    """context_term branch: total_found is len(full match list) before the limit
    slice -- "free" since get_history_for_context already returns the unsliced
    list, we just grab its length before slicing."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    for i in range(4):
        s.add_node(CognitionNode(
            id=f"n{i}", type=CognitionNodeType.DECISION, summary="s", detail="d",
            context=["shared-tag"], references=[], severity=None,
            timestamp="2026-07-15T00:00:00+00:00", author="t",
        ))
    config = SimpleNamespace(embedding_model="m", embedding_dimensions=3, repo_path=tmp_path)
    registry = build_registry(home_path=tmp_path, home_tag="home", home_storage=s, home_embeddings=embed)
    lc = {
        "config": config, "cognition_storage": s, "cognition_embedding_storage": embed,
        "loaded_projects": registry, "embedding_generator": None,
        "embedding_ready": threading.Event(), "embedding_error": None,
    }
    mock = _MockMcpLocal()
    register_cognition_tools(mock)
    ctx = _ctx(lc)

    result = mock.tools["cognition_get_history"](ctx, context_term="shared-tag", limit=2)

    assert result["count"] == 2
    assert result["total_found"] == 4
    assert result["exhaustive"] is True
    embed.close()


# ── Multi-project combination: exclude_people + total_found/exhaustive ────────


def _multi_project_lc(tmp_path):
    home_path = tmp_path / "home"
    home_path.mkdir(parents=True)
    home_storage = CognitionStorage(home_path / ".cognition")
    home_chroma = ChromaDBStorage(
        persist_directory=home_path / ".cognition" / "chromadb",
        embedding_model="m", embedding_dimensions=3,
    )
    config = SimpleNamespace(embedding_model="m", embedding_dimensions=3, repo_path=home_path)
    registry = build_registry(
        home_path=home_path, home_tag="home",
        home_storage=home_storage, home_embeddings=home_chroma,
    )
    lc = {
        "config": config, "cognition_storage": home_storage,
        "cognition_embedding_storage": home_chroma, "loaded_projects": registry,
        "embedding_generator": cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0])),
        "embedding_ready": threading.Event(), "embedding_error": None,
    }
    lc["embedding_ready"].set()
    return lc, home_path, home_storage, home_chroma


def _make_foreign_with_data(tmp_path, name, node_id, recorded_by):
    b_path = tmp_path / name
    (b_path / ".cognition").mkdir(parents=True)
    b_storage = CognitionStorage(b_path / ".cognition")
    b_storage.add_node(_entity(node_id, recorded_by=recorded_by))
    b_chroma = ChromaDBStorage(
        persist_directory=b_path / ".cognition" / "chromadb",
        embedding_model="m", embedding_dimensions=3,
    )
    b_chroma.upsert_embedding(node_id, [1.0, 0.0, 0.0], {"entity_type": "decision"})
    b_chroma.close()  # so _load_project_core can open_existing
    return b_path


def test_search_multi_project_combines_exclude_and_completeness_per_entry(tmp_path):
    """WP-TC10 scope: exclude_people applies per entry; total_found is SUMMED,
    exhaustive AND-reduced, excluded_count/excluded_for combine across entries."""
    lc, home_path, home_storage, home_chroma = _multi_project_lc(tmp_path)
    home_storage.add_node(_entity("home-alice", recorded_by=ALICE))
    home_storage.add_node(_entity("home-bob", recorded_by=BOB))
    home_chroma.upsert_embedding("home-alice", [1.0, 0.0, 0.0], {"entity_type": "decision"})
    home_chroma.upsert_embedding("home-bob", [1.0, 0.0, 0.0], {"entity_type": "decision"})

    b_path = _make_foreign_with_data(tmp_path, "B", "b-alice", ALICE)
    _load_project_core(lc, str(b_path))

    mock = _MockMcpLocal()
    register_cognition_tools(mock)
    ctx = _ctx(lc)

    result = mock.tools["cognition_search"](
        ctx, query="q", project="*", limit=10, exclude_people="alice@x.com"
    )

    ids = {r["id"] for r in result["results"]}
    assert ids == {"home-bob"}, f"exclude_people must apply per-entry across the fan-out: {ids}"
    assert result["exhaustive"] is True, "both entries exhaust well under limit=10"
    assert result["total_found"] == 1, "home's 1 non-excluded + B's 0 non-excluded"
    assert result["excluded_count"] == 2, "1 excluded at home + 1 excluded at B"
    assert result["excluded_for"] == ["alice@x.com"]
    home_chroma.close()


def test_get_history_multi_project_total_found_summed(tmp_path):
    lc, home_path, home_storage, home_chroma = _multi_project_lc(tmp_path)
    home_storage.add_node(_entity("home1", CognitionNodeType.PATTERN))
    home_storage.add_node(_entity("home2", CognitionNodeType.PATTERN))

    b_path = tmp_path / "B"
    (b_path / ".cognition").mkdir(parents=True)
    b_storage = CognitionStorage(b_path / ".cognition")
    b_storage.add_node(_entity("b1", CognitionNodeType.PATTERN))
    b_chroma = ChromaDBStorage(
        persist_directory=b_path / ".cognition" / "chromadb", embedding_model="m", embedding_dimensions=3,
    )
    b_chroma.close()
    _load_project_core(lc, str(b_path))

    mock = _MockMcpLocal()
    register_cognition_tools(mock)
    ctx = _ctx(lc)

    result = mock.tools["cognition_get_history"](ctx, node_type="pattern", project="*", limit=10)

    assert result["total_found"] == 3, "2 home + 1 B, summed"
    assert result["exhaustive"] is True
    assert set(result["projects_queried"]) == {"home", "B"}
    home_chroma.close()


# ── Minimal local MCP/ctx shims (mirrors tests/conftest.py's _MockMcp/make_ctx;
#    kept local here so this file's multi-project lc dicts -- built by hand, not
#    via build_lc -- can drive the real tool wrappers without pulling in the
#    single-project-only build_lc fixture) ──────────────────────────────────────


class _MockMcpLocal:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            import asyncio
            import functools
            import inspect as _inspect

            if _inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                def sync_shim(*args, **kwargs):
                    return asyncio.run(fn(*args, **kwargs))
                self.tools[fn.__name__] = sync_shim
            else:
                self.tools[fn.__name__] = fn
            return fn
        return decorator


def _ctx(lc):
    from fastmcp import Context
    return cast(Context, SimpleNamespace(request_context=SimpleNamespace(lifespan_context=lc)))
