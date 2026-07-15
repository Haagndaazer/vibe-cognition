"""WP-TC9: seniority + agent-origin weighting in cognition_search (visible,
never-wiping). Each test names the specific failure mode it guards and is written to
fail before its fix exists."""

import threading
from types import SimpleNamespace
from typing import cast

from fastmcp import Context

from vibe_cognition.cognition import CognitionNode, CognitionNodeType, CognitionStorage
from vibe_cognition.embeddings import ChromaDBStorage, EmbeddingGenerator
from vibe_cognition.tools.cognition_tools import (
    _AGENT_MULTIPLIER,
    _SENIORITY_MULTIPLIERS,
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
    recorded_by: dict | None = None,
    from_agent: bool | None = None,
) -> CognitionNode:
    metadata: dict = {}
    if recorded_by is not None:
        metadata["recorded_by"] = recorded_by
    if from_agent is not None:
        metadata["from_agent"] = from_agent
    return CognitionNode(
        id=node_id, type=node_type, summary=summary, detail="d",
        context=[], references=[], severity=None,
        timestamp="2026-07-15T00:00:00+00:00", author="t", metadata=metadata,
    )


def _person(node_id: str, email: str, seniority: str, name: str = "P") -> CognitionNode:
    return CognitionNode(
        id=node_id, type=CognitionNodeType.PERSON, summary=f"{name} — eng", detail="",
        context=[], references=[], timestamp="2026-07-15T00:00:00+00:00", author=name,
        metadata={
            "person": {
                "email": email, "name": name, "role": "eng",
                "seniority": seniority, "reports_to_email": "",
            },
            "profile_history": [],
            "recorded_by": {"name": name, "email": email},
            "from_agent": False,
        },
    )


def _upsert(
    embed: ChromaDBStorage,
    node_id: str,
    vec: list[float],
    node_type: CognitionNodeType = CognitionNodeType.DECISION,
    summary: str = "s",
    from_agent: bool | None = None,
) -> None:
    """Mirrors the real record path's Chroma-metadata mirroring (cognition_tools.py
    ~118-122): from_agent lives in embedding metadata, NOT derived from the node at
    search time -- so a test must upsert it explicitly to exercise agent weighting."""
    metadata: dict = {"entity_type": node_type.value, "summary": summary}
    if from_agent is not None:
        metadata["from_agent"] = from_agent
    embed.upsert_embedding(node_id, vec, metadata)


class _FixedGen:
    """Every query embeds to the SAME fixed vector -- these tests control document
    vectors directly (upsert_embedding), so score differentiation comes from the
    documents, not the query text (same convention as WP-TC10's test fixture)."""

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


def _ready_event() -> threading.Event:
    e = threading.Event()
    e.set()
    return e


# ── Ordering / limit-boundary ────────────────────────────────────────────────


def test_ordering_penalized_higher_raw_hit_sorts_below_unpenalized_lower_raw(tmp_path):
    """Junior-authored hit with the HIGHER raw score still surfaces (never wiped)
    but sorts BELOW a senior hit whose weighted_score exceeds it."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_person("psen", "senior@x.com", "senior"))
    s.add_node(_person("pjr", "junior@x.com", "junior"))
    s.add_node(_entity("jr", summary="junior hit",
                        recorded_by={"name": "J", "email": "junior@x.com"}, from_agent=False))
    s.add_node(_entity("sr", summary="senior hit",
                        recorded_by={"name": "S", "email": "senior@x.com"}, from_agent=False))
    _upsert(embed, "jr", [1.0, 0.0, 0.0], summary="junior hit")    # raw ~1.0 (best match)
    _upsert(embed, "sr", [0.99, 0.01, 0.0], summary="senior hit")  # raw ~0.99 (second-best)
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=2)
    ids = [r["id"] for r in res["results"]]
    assert set(ids) == {"jr", "sr"}  # never wiped
    assert ids[0] == "sr"  # senior (weighted ~0.99) now sorts above junior (weighted ~0.9)
    embed.close()


def test_ordering_unpenalized_hit_enters_top_n_crossing_limit_boundary(tmp_path):
    """Same setup, but limit=1: the reordering crosses the limit SLICE, not just the
    returned order -- the senior hit displaces the junior hit out of the top-N."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_person("psen", "senior@x.com", "senior"))
    s.add_node(_person("pjr", "junior@x.com", "junior"))
    s.add_node(_entity("jr", summary="junior hit",
                        recorded_by={"name": "J", "email": "junior@x.com"}, from_agent=False))
    s.add_node(_entity("sr", summary="senior hit",
                        recorded_by={"name": "S", "email": "senior@x.com"}, from_agent=False))
    _upsert(embed, "jr", [1.0, 0.0, 0.0], summary="junior hit")
    _upsert(embed, "sr", [0.99, 0.01, 0.0], summary="senior hit")
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=1)
    assert [r["id"] for r in res["results"]] == ["sr"]
    embed.close()


# ── Invariants ────────────────────────────────────────────────────────────────


def test_agent_multiplier_strictly_below_every_seniority_multiplier():
    """Programmatic check against the SHIPPED table: every agent-attributed
    multiplier is strictly below every human-attributed one (ruled: human input
    always outweighs agent input)."""
    assert min(_SENIORITY_MULTIPLIERS.values()) > _AGENT_MULTIPLIER


def test_all_shipped_multipliers_in_penalty_only_range():
    """Penalty-only: every multiplier is in (0, 1.0] -- never a boost."""
    assert all(0 < m <= 1.0 for m in _SENIORITY_MULTIPLIERS.values())
    assert 0 < _AGENT_MULTIPLIER <= 1.0


def test_no_weighted_score_ever_exceeds_raw_score(tmp_path):
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_person("psen", "senior@x.com", "senior"))
    s.add_node(_entity("agent1", recorded_by={"name": "A", "email": "agent@x.com"}, from_agent=True))
    s.add_node(_entity("sr1", recorded_by={"name": "S", "email": "senior@x.com"}, from_agent=False))
    s.add_node(_entity("unk1"))  # fully unstamped
    _upsert(embed, "agent1", [1.0, 0.0, 0.0], from_agent=True)
    _upsert(embed, "sr1", [0.9, 0.1, 0.0], from_agent=False)
    _upsert(embed, "unk1", [0.8, 0.2, 0.0])
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=10)
    assert len(res["results"]) == 3
    for r in res["results"]:
        assert r["weighted_score"] <= r["score"] + 1e-9
    embed.close()


def test_exempt_hit_rises_relative_to_a_raw_score_tied_penalized_neighbor(tmp_path):
    """A constraint (exempt) hit TIED on raw score with a junior-authored decision
    strictly outranks it after weighting -- 'never outranked by seniority' proven at
    the exact tie boundary the brief calls out (a tie requires the neighbor's raw
    score to already meet the exempt hit's -- constructed directly here)."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_person("pjr", "junior@x.com", "junior"))
    s.add_node(_entity("con1", node_type=CognitionNodeType.CONSTRAINT, summary="constraint"))
    s.add_node(_entity("jr1", summary="junior decision",
                        recorded_by={"name": "J", "email": "junior@x.com"}, from_agent=False))
    same_vec = [1.0, 0.0, 0.0]
    _upsert(embed, "con1", same_vec, node_type=CognitionNodeType.CONSTRAINT, summary="constraint")
    _upsert(embed, "jr1", same_vec, summary="junior decision")
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=2)
    ids = [r["id"] for r in res["results"]]
    assert ids == ["con1", "jr1"]
    con = next(r for r in res["results"] if r["id"] == "con1")
    assert con["weight"]["multiplier"] == 1.0
    assert con["weight"]["basis"] == "exempt:constraint"
    embed.close()


# ── Fallback fixtures a/b/c/d ─────────────────────────────────────────────────


def test_fallback_a_pre_tc6_stamped_registered_person(tmp_path):
    """(a) pre-TC6 node: stamped, no from_agent KEY at all, registered person ->
    from_agent null, no agent penalty, basis human:<seniority>."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_person("pmid", "mid@x.com", "mid"))
    s.add_node(_entity("n1", recorded_by={"name": "M", "email": "mid@x.com"}))  # no from_agent key
    _upsert(embed, "n1", [1.0, 0.0, 0.0])
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=10)
    hit = res["results"][0]
    assert hit["weight"] == {
        "multiplier": _SENIORITY_MULTIPLIERS["mid"], "seniority": "mid",
        "from_agent": None, "basis": "human:mid",
    }
    embed.close()


def test_fallback_b_stamped_unregistered_human(tmp_path):
    """(b) stamped email, from_agent explicitly False, no person node -> seniority
    null, neutral, basis human:unregistered (NOT unverified)."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("n1", recorded_by={"name": "U", "email": "unrostered@x.com"}, from_agent=False))
    _upsert(embed, "n1", [1.0, 0.0, 0.0], from_agent=False)
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=10)
    hit = res["results"][0]
    assert hit["weight"] == {
        "multiplier": 1.0, "seniority": None, "from_agent": False, "basis": "human:unregistered",
    }
    embed.close()


def test_fallback_c_fully_unstamped(tmp_path):
    """(c) fully unstamped (no recorded_by/created_by at all) -> both axes null,
    multiplier 1.0, basis unverified."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("n1"))
    _upsert(embed, "n1", [1.0, 0.0, 0.0])
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=10)
    hit = res["results"][0]
    assert hit["weight"] == {
        "multiplier": 1.0, "seniority": None, "from_agent": None, "basis": "unverified",
    }
    embed.close()


def test_fallback_d_compound_stamped_no_from_agent_key_no_person_node(tmp_path):
    """(d) COMPOUND (peer-review): stamped + no from_agent key + no person node (the
    realistic post-P13n/pre-TC6 unrostered case) -> neutral, basis human:unregistered."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("n1", recorded_by={"name": "C", "email": "compound@x.com"}))
    _upsert(embed, "n1", [1.0, 0.0, 0.0])
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=10)
    hit = res["results"][0]
    assert hit["weight"] == {
        "multiplier": 1.0, "seniority": None, "from_agent": None, "basis": "human:unregistered",
    }
    embed.close()


# ── Visibility ────────────────────────────────────────────────────────────────


def test_weight_key_present_on_every_hit_including_neutral(tmp_path):
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("n1"))
    _upsert(embed, "n1", [1.0, 0.0, 0.0])
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=10)
    assert "weight" in res["results"][0]
    embed.close()


def test_score_and_weighted_score_equal_when_multiplier_is_1_0(tmp_path):
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_entity("n1"))
    _upsert(embed, "n1", [1.0, 0.0, 0.0])
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=10)
    hit = res["results"][0]
    assert hit["weight"]["multiplier"] == 1.0
    assert hit["weighted_score"] == hit["score"]
    embed.close()


# ── get_workflow inheritance ──────────────────────────────────────────────────


def test_get_workflow_weighting_changes_which_workflow_matches(build_lc, make_ctx, mock_mcp, tmp_path):
    """WP-TC9 (peer-review): a multi-topic fixture where an agent-authored workflow
    and a senior-authored near-similar workflow TIE on raw score -- weighting must
    change WHICH one `matched`/`head` resolves to, not just its rank in a list."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]
    embed = lc["cognition_embedding_storage"]

    storage.add_node(_person("psen", "senior@x.com", "senior"))
    storage.add_node(CognitionNode(
        id="wf-agent", type=CognitionNodeType.WORKFLOW, summary="alpha workflow (agent)",
        detail="d", context=[], references=[], timestamp="2026-07-15T00:00:00+00:00",
        author="A", metadata={"recorded_by": {"name": "A", "email": "agent@x.com"}, "from_agent": True},
    ))
    storage.add_node(CognitionNode(
        id="wf-senior", type=CognitionNodeType.WORKFLOW, summary="alpha workflow (senior)",
        detail="d", context=[], references=[], timestamp="2026-07-15T00:00:00+00:00",
        author="S", metadata={"recorded_by": {"name": "S", "email": "senior@x.com"}, "from_agent": False},
    ))
    same_vec = [1.0, 0.0, 0.0]  # the "alpha" marker vector (_TextKeyedGen) -- a genuine tie
    embed.upsert_embedding(
        "wf-agent", same_vec,
        {"entity_type": "workflow", "summary": "alpha workflow (agent)", "from_agent": True},
    )
    embed.upsert_embedding(
        "wf-senior", same_vec,
        {"entity_type": "workflow", "summary": "alpha workflow (senior)", "from_agent": False},
    )

    result = mock_mcp.tools["cognition_get_workflow"](ctx, name_or_topic="alpha")
    assert "error" not in result, result
    assert result["matched"] == "wf-senior"


# ── TC10 composition ──────────────────────────────────────────────────────────


def test_exclude_people_composes_with_weighting_excluded_set_unaffected(tmp_path):
    """exclude_people drops hits BEFORE weighting ever runs on them -- the excluded
    set is unaffected by weights, and weighting still applies to survivors."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_person("psen", "senior@x.com", "senior"))
    s.add_node(_entity("alice1", recorded_by={"name": "A", "email": "alice@x.com"}, from_agent=False))
    s.add_node(_entity("sr1", recorded_by={"name": "S", "email": "senior@x.com"}, from_agent=False))
    _upsert(embed, "alice1", [1.0, 0.0, 0.0])
    _upsert(embed, "sr1", [0.99, 0.01, 0.0])
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=10, exclude_people="alice@x.com")
    ids = {r["id"] for r in res["results"]}
    assert ids == {"sr1"}
    assert res["excluded_count"] == 1
    embed.close()


def test_total_found_exhaustive_unchanged_by_weighting(tmp_path):
    """Weighting reorders within the discovered set -- total_found/exhaustive count
    the same hits regardless of weighted reordering."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_person("psen", "senior@x.com", "senior"))
    for i, email in enumerate(["agent@x.com", "senior@x.com"]):
        nid = f"n{i}"
        is_agent = email == "agent@x.com"
        s.add_node(_entity(nid, recorded_by={"name": "X", "email": email}, from_agent=is_agent))
        _upsert(embed, nid, [1.0 - i * 0.01, i * 0.01, 0.0], from_agent=is_agent)
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    res = _search_cognition(s, embed, gen, "q", limit=10)
    assert res["total_found"] == 2
    assert res["exhaustive"] is True
    embed.close()


# ── Multi-project ─────────────────────────────────────────────────────────────


def test_multiproject_resort_uses_weighted_score_not_raw(tmp_path):
    """THE safety-critical fixture: a cross-project raw-vs-weighted inversion proves
    the outer merge switched to weighted_score. Home's agent-authored hit has a
    HIGHER raw score than B's senior-authored hit, but B's hit must rank FIRST once
    weighted -- the exact silent-no-op failure mode the peer review flagged."""
    home_path = tmp_path / "home"
    home_path.mkdir(parents=True)
    home_storage = CognitionStorage(home_path / ".cognition")
    home_storage.add_node(
        _entity("home1", recorded_by={"name": "A", "email": "agent@x.com"}, from_agent=True)
    )
    home_chroma = ChromaDBStorage(
        persist_directory=home_path / ".cognition" / "chromadb",
        embedding_model="m", embedding_dimensions=3,
    )
    _upsert(home_chroma, "home1", [1.0, 0.0, 0.0], from_agent=True)

    b_path = tmp_path / "B"
    (b_path / ".cognition").mkdir(parents=True)
    (b_path / ".cognition" / "journal.jsonl").write_text("", encoding="utf-8")
    b_storage = CognitionStorage(b_path / ".cognition")
    b_storage.add_node(_person("psen", "senior@x.com", "senior"))
    b_storage.add_node(
        _entity("b1", recorded_by={"name": "S", "email": "senior@x.com"}, from_agent=False)
    )
    b_chroma = ChromaDBStorage(
        persist_directory=b_path / ".cognition" / "chromadb",
        embedding_model="m", embedding_dimensions=3,
    )
    _upsert(b_chroma, "b1", [0.99, 0.01, 0.0], from_agent=False)
    b_chroma.close()

    config = SimpleNamespace(embedding_model="m", embedding_dimensions=3, repo_path=home_path)
    registry = build_registry(
        home_path=home_path, home_tag="home",
        home_storage=home_storage, home_embeddings=home_chroma,
    )
    lc = {
        "config": config, "cognition_storage": home_storage,
        "cognition_embedding_storage": home_chroma, "loaded_projects": registry,
        "embedding_generator": cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0])),
        "embedding_ready": _ready_event(), "embedding_error": None,
    }
    _load_project_core(lc, str(b_path))

    mock = _MockMcp()
    register_cognition_tools(mock)
    ctx = _make_ctx(lc)

    result = mock.tools["cognition_search"](ctx, query="q", project="*", limit=2)
    ids = [r["id"] for r in result["results"]]
    assert ids and ids[0] == "b1", f"expected B's weighted-top hit first, got: {ids}"
    home_chroma.close()


def test_multiproject_per_entry_uses_its_own_person_registry(tmp_path):
    """Same email registered with DIFFERENT seniority in two projects -- each
    entry's hits weight by that entry's OWN registry, not a merged/shared one."""
    home_path = tmp_path / "home"
    home_path.mkdir(parents=True)
    home_storage = CognitionStorage(home_path / ".cognition")
    home_storage.add_node(_person("phome", "same@x.com", "junior"))
    home_storage.add_node(
        _entity("home1", recorded_by={"name": "X", "email": "same@x.com"}, from_agent=False)
    )
    home_chroma = ChromaDBStorage(
        persist_directory=home_path / ".cognition" / "chromadb",
        embedding_model="m", embedding_dimensions=3,
    )
    _upsert(home_chroma, "home1", [1.0, 0.0, 0.0])

    b_path = tmp_path / "B"
    (b_path / ".cognition").mkdir(parents=True)
    (b_path / ".cognition" / "journal.jsonl").write_text("", encoding="utf-8")
    b_storage = CognitionStorage(b_path / ".cognition")
    b_storage.add_node(_person("pb", "same@x.com", "owner"))
    b_storage.add_node(
        _entity("b1", recorded_by={"name": "X", "email": "same@x.com"}, from_agent=False)
    )
    b_chroma = ChromaDBStorage(
        persist_directory=b_path / ".cognition" / "chromadb",
        embedding_model="m", embedding_dimensions=3,
    )
    _upsert(b_chroma, "b1", [1.0, 0.0, 0.0])
    b_chroma.close()

    config = SimpleNamespace(embedding_model="m", embedding_dimensions=3, repo_path=home_path)
    registry = build_registry(
        home_path=home_path, home_tag="home",
        home_storage=home_storage, home_embeddings=home_chroma,
    )
    lc = {
        "config": config, "cognition_storage": home_storage,
        "cognition_embedding_storage": home_chroma, "loaded_projects": registry,
        "embedding_generator": cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0])),
        "embedding_ready": _ready_event(), "embedding_error": None,
    }
    _load_project_core(lc, str(b_path))

    mock = _MockMcp()
    register_cognition_tools(mock)
    ctx = _make_ctx(lc)

    result = mock.tools["cognition_search"](ctx, query="q", project="*", limit=10)
    rows = {(r["project"], r["id"]): r for r in result["results"]}
    assert rows[("home", "home1")]["weight"]["basis"] == "human:junior"
    assert rows[("B", "b1")]["weight"]["basis"] == "human:owner"
    home_chroma.close()


# ── Dashboard: zero behavior change ───────────────────────────────────────────
# Verified by test_dashboard.py running unmodified and passing (existing dashboard
# search `_dedupe` closure is a fully separate implementation sharing only the
# tuple contract with _format_search_results -- no shared code path to leak into).


# ── Performance shape ─────────────────────────────────────────────────────────


def test_person_scan_happens_once_per_call_across_widening_rounds(tmp_path, monkeypatch):
    """WP-TC9 perf shape: the person-registry scan happens ONCE per top-level search
    call, even when adaptive widening spans multiple rounds -- catches the
    memo-in-wrong-scope degradation (a local inside _format_search_results would
    reset, and re-scan, every round). Forces a widening round via the same
    exclusion-flood pattern as WP-TC10's fixture (15 alice hits rank above 2 senior
    hits on raw score, so round 1 -- n=10 -- surfaces zero survivors)."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_person("psen", "senior@x.com", "senior"))
    for i in range(15):
        nid = f"alice{i:02d}"
        s.add_node(_entity(nid, recorded_by={"name": "A", "email": "alice@x.com"}, from_agent=False))
        _upsert(embed, nid, [1.0, 0.0, 0.0])
    for i in range(2):
        nid = f"sr{i:02d}"
        s.add_node(_entity(nid, recorded_by={"name": "S", "email": "senior@x.com"}, from_agent=False))
        _upsert(embed, nid, [0.99, 0.01, 0.0])
    gen = cast(EmbeddingGenerator, _FixedGen([1.0, 0.0, 0.0]))

    scan_calls = {"n": 0}
    real_get_nodes_by_type = s.get_nodes_by_type

    def _counting(node_type):
        if node_type == CognitionNodeType.PERSON:
            scan_calls["n"] += 1
        return real_get_nodes_by_type(node_type)

    monkeypatch.setattr(s, "get_nodes_by_type", _counting)

    res = _search_cognition(s, embed, gen, "q", limit=2, exclude_people="alice@x.com")
    assert {r["id"] for r in res["results"]} == {"sr00", "sr01"}, res["results"]
    assert scan_calls["n"] == 1, f"expected exactly 1 person scan, got {scan_calls['n']}"
    embed.close()
