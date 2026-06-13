"""WP-Cap (P2 capability gaps): cognition_get_node, edge reason persistence,
cognition_update_node + re-embed, and the exposed superseded/incident queries.

Each test names the specific failure mode it guards (rule 20) and is written to
fail before its fix exists (rule 12)."""

from typing import cast

from vibe_cognition.cognition import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    CognitionStorage,
    get_incident_resolution,
    get_superseded_chain,
)
from vibe_cognition.embeddings import ChromaDBStorage, EmbeddingGenerator
from vibe_cognition.tools.cognition_tools import (
    _add_edge_core,
    _add_edges_batch_core,
    _embed_entity_node,
    _get_node,
    _search_cognition,
    _update_node,
)


def _node(node_id, *, summary="s", detail="d"):
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DECISION, summary=summary, detail=detail,
        context=["ctx"], references=["commit:abc"], severity=None,
        timestamp="2026-06-13T00:00:00+00:00", author="t",
    )


# --- Commit 1: cognition_get_node -------------------------------------------

def test_get_node_returns_full_narrative_including_detail(tmp_path):
    """get_node must return the FULL node dict — including `detail`, which search
    results and get_neighbors omit. Fails-before: no _get_node surface at all."""
    s = CognitionStorage(tmp_path / ".cognition")
    s.add_node(_node("n1", summary="the summary", detail="the long detail body"))

    out = _get_node(s, "n1")

    assert out.get("id") == "n1", "result must carry the node id (get_node omits it)"
    assert out.get("detail") == "the long detail body", "detail must be present"
    assert out.get("summary") == "the summary"
    assert out.get("type") == CognitionNodeType.DECISION.value
    assert out.get("context") == ["ctx"]
    assert out.get("references") == ["commit:abc"]


def test_get_node_missing_id_returns_error(tmp_path):
    """A missing id returns an error dict, not a raise or a None."""
    s = CognitionStorage(tmp_path / ".cognition")
    out = _get_node(s, "nope")
    assert "error" in out, "missing node must return an error dict"


# --- Commit 2: persist the edge `reason` ------------------------------------

def _edge_reason(storage, from_id, to_id):
    for tid, edata in storage.get_successors(from_id):
        if tid == to_id:
            return edata.get("reason")
    raise AssertionError(f"no edge {from_id} -> {to_id}")


def test_edge_reason_round_trips_through_replay(tmp_path):
    """An edge's `reason` must survive a journal REPLAY in a fresh storage instance —
    it rides `model_dump` into the journal and `data.get('reason')` back out on replay.
    Fails-before (no model field / no replay read): the replayed edge's reason is None,
    the curation rationale is silently lost across a reload."""
    cog = tmp_path / ".cognition"
    s1 = CognitionStorage(cog)
    s1.add_node(_node("a"))
    s1.add_node(_node("b"))
    s1.add_edge(CognitionEdge(
        from_id="a", to_id="b", edge_type=CognitionEdgeType.LED_TO,
        timestamp="2026-06-13T00:00:00+00:00", source="manual",
        reason="a forced b because of the deadline",
    ))
    assert _edge_reason(s1, "a", "b") == "a forced b because of the deadline"

    s2 = CognitionStorage(cog)  # fresh replay of the same journal
    assert _edge_reason(s2, "a", "b") == "a forced b because of the deadline", (
        "edge reason did not survive journal replay (lost on reload)"
    )


def test_add_edge_core_persists_reason(tmp_path):
    """The single-edge tool path carries the agent's reason onto the edge."""
    s = CognitionStorage(tmp_path / ".cognition")
    s.add_node(_node("a"))
    s.add_node(_node("b"))
    out = _add_edge_core(s, "a", "b", "led_to", reason="my rationale")
    assert out.get("created") is True
    assert _edge_reason(s, "a", "b") == "my rationale"


def test_add_edges_batch_persists_reason(tmp_path):
    """The batch tool path carries each edge's reason (e.get('reason'))."""
    s = CognitionStorage(tmp_path / ".cognition")
    for nid in ("a", "b"):
        s.add_node(_node(nid))
    out = _add_edges_batch_core(
        s, '[{"from_id":"a","to_id":"b","edge_type":"led_to","reason":"batch why"}]'
    )
    assert out["created"] == 1, out
    assert _edge_reason(s, "a", "b") == "batch why"


# --- Commit 3: cognition_update_node + re-embed (the gate-hard one) ----------

class _TextKeyedGen:
    """A text-KEYED fake embedder (NOT a constant): distinct marker words map to
    distinct ORTHOGONAL unit vectors, so a re-embed genuinely MOVES the stored vector
    and search can tell the new text from the old. A constant-vector fake literally
    can't distinguish 're-embedded' from 'stale' — the re-embed proof would be
    tautological with one (Vince's B4)."""

    _MARKERS = {
        "alpha": [1.0, 0.0, 0.0],
        "beta": [0.0, 1.0, 0.0],
        "gamma": [0.0, 0.0, 1.0],
    }

    def generate_query_embedding(self, text):
        low = text.lower()
        for marker, vec in self._MARKERS.items():
            if marker in low:
                return list(vec)
        return [0.0, 0.0, 1.0]  # never used here; all test texts carry a marker


def _gen() -> EmbeddingGenerator:
    return cast(EmbeddingGenerator, _TextKeyedGen())


def _score_for(results, node_id):
    return next((r["score"] for r in results if r["id"] == node_id), None)


def test_update_node_reembeds_so_search_finds_new_text(tmp_path):
    """THE crux (rule 12, fails-before): a summary edit must REFRESH the search vector,
    not just the graph. With a text-keyed embedder, the node's vector starts on ALPHA;
    after update_node(summary=beta) + re-embed it must move to BETA — so a BETA search
    scores ~1.0 and an ALPHA search collapses to ~0. Fails-before (no re-embed): the
    vector stays on ALPHA, BETA search scores ~0 — silent search-staleness."""
    s = CognitionStorage(tmp_path / ".cognition")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chromadb")
    gen = _gen()

    node = CognitionNode(
        id="n1", type=CognitionNodeType.DECISION, summary="alpha plan", detail="body",
        context=[], references=[], timestamp="2026-06-13T00:00:00+00:00", author="t",
    )
    s.add_node(node)
    _embed_entity_node(embed, gen, node)

    # baseline: the node sits on the ALPHA vector
    before = _score_for(_search_cognition(s, embed, gen, "alpha")["results"], "n1")
    assert before is not None and before > 0.99

    out = _update_node(s, embed, gen, node_id="n1", embeddings_ready=True, summary="beta plan")
    assert out.get("reembed") == "done", out
    assert out.get("summary") == "beta plan"

    # the vector moved to BETA — a BETA query now matches it strongly
    after_beta = _score_for(_search_cognition(s, embed, gen, "beta")["results"], "n1")
    assert after_beta is not None and after_beta > 0.99, (
        "node vector was not refreshed to the new text (stale search vector)"
    )
    # and the OLD text no longer matches the node's vector
    after_alpha = _score_for(_search_cognition(s, embed, gen, "alpha")["results"], "n1")
    assert after_alpha is not None and after_alpha < 0.01, (
        "stale ALPHA vector still served after the edit"
    )


def test_update_node_deferred_when_embeddings_not_ready(tmp_path):
    """If the model isn't ready, the edit still applies but the re-embed is deferred
    (reported), not silently skipped as 'done'."""
    s = CognitionStorage(tmp_path / ".cognition")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chromadb")
    s.add_node(_node("n1", summary="alpha"))
    out = _update_node(s, embed, _gen(), node_id="n1", embeddings_ready=False, summary="beta")
    assert out.get("reembed") == "deferred", out
    assert out.get("summary") == "beta"


def test_update_node_whitelist_leaves_structural_fields_intact(tmp_path):
    """The whitelist: a narrative edit must NOT touch id/type/references/metadata
    /timestamp (editing those would corrupt a document's sha/mode ref or the part_of
    index)."""
    s = CognitionStorage(tmp_path / ".cognition")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chromadb")
    n = CognitionNode(
        id="n1", type=CognitionNodeType.DECISION, summary="s", detail="d",
        context=["old"], references=["commit:abc"], severity=None,
        timestamp="2026-06-13T00:00:00+00:00", author="t", metadata={"sha256": "deadbeef"},
    )
    s.add_node(n)

    out = _update_node(s, embed, _gen(), node_id="n1", embeddings_ready=True, context="new")
    assert out.get("context") == ["new"]
    assert out.get("references") == ["commit:abc"], "references must be untouched"
    assert out.get("metadata") == {"sha256": "deadbeef"}, "metadata must be untouched"
    assert out.get("type") == CognitionNodeType.DECISION.value


def test_update_node_reembeds_metadata_on_context_or_severity_edit(tmp_path):
    """Vince's gap: context + severity are stored in the Chroma METADATA that
    _format_search_results SURFACES in every hit — so a context/severity-only edit
    must refresh that metadata, not just summary/detail. The match vector is unchanged
    (same embed text) but the upsert refreshes the displayed metadata. Fails-before
    (re-embed gated on summary/detail only): a context+severity edit leaves search
    results showing the OLD values — the WP's own search-staleness, on the metadata."""
    s = CognitionStorage(tmp_path / ".cognition")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chromadb")
    gen = _gen()
    node = CognitionNode(
        id="n1", type=CognitionNodeType.INCIDENT, summary="alpha incident", detail="body",
        context=["ctx-old"], references=[], severity="low",
        timestamp="2026-06-13T00:00:00+00:00", author="t",
    )
    s.add_node(node)
    _embed_entity_node(embed, gen, node)

    before = next(r for r in _search_cognition(s, embed, gen, "alpha")["results"] if r["id"] == "n1")
    assert before["severity"] == "low"
    assert "ctx-old" in (before.get("context") or "")

    out = _update_node(
        s, embed, gen, node_id="n1", embeddings_ready=True, severity="high", context="ctx-new"
    )
    assert out.get("reembed") == "done"

    after = next(r for r in _search_cognition(s, embed, gen, "alpha")["results"] if r["id"] == "n1")
    assert after["severity"] == "high", "stale severity surfaced after a severity edit"
    assert "ctx-new" in (after.get("context") or ""), "stale context surfaced after a context edit"


def test_update_node_missing_and_empty(tmp_path):
    """A missing node errors; an existing node with no editable field errors (no
    silent no-op success)."""
    s = CognitionStorage(tmp_path / ".cognition")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chromadb")
    assert "error" in _update_node(s, embed, _gen(), node_id="nope", embeddings_ready=False, summary="x")
    s.add_node(_node("n1"))
    assert "error" in _update_node(s, embed, _gen(), node_id="n1", embeddings_ready=False)


# --- Commit 4: expose superseded-chain + incident-resolution queries ---------

def _typed_node(node_id, ntype):
    return CognitionNode(
        id=node_id, type=ntype, summary=node_id, detail="d", context=[], references=[],
        severity=None, timestamp="2026-06-13T00:00:00+00:00", author="t",
    )


def _edge(s, a, b, et):
    s.add_edge(CognitionEdge(
        from_id=a, to_id=b, edge_type=et, timestamp="2026-06-13T00:00:00+00:00", source="manual",
    ))


def test_superseded_chain_newest_first(tmp_path):
    """A supersedes B supersedes C -> the chain walks SUPERSEDES from the newest node
    and returns [A, B, C] newest-first."""
    s = CognitionStorage(tmp_path / ".cognition")
    for nid in ("A", "B", "C"):
        s.add_node(_typed_node(nid, CognitionNodeType.DECISION))
    _edge(s, "A", "B", CognitionEdgeType.SUPERSEDES)
    _edge(s, "B", "C", CognitionEdgeType.SUPERSEDES)

    chain = get_superseded_chain(s, "A")
    assert [n["id"] for n in chain] == ["A", "B", "C"]


def test_incident_resolution_includes_resolutions_and_all_led_to(tmp_path):
    """An incident's RESOLVED_BY target lands in `resolutions`; its LED_TO targets all
    land in `discoveries` — INCLUDING non-discovery follow-ons. This guards the
    collapsed branch (the former DISCOVERY if/else appended identically; collapsing it
    must keep including non-discovery led_to targets — otherwise a decision the
    incident produced would silently vanish from the result)."""
    s = CognitionStorage(tmp_path / ".cognition")
    s.add_node(_typed_node("I", CognitionNodeType.INCIDENT))
    s.add_node(_typed_node("FIX", CognitionNodeType.DECISION))
    s.add_node(_typed_node("DISC", CognitionNodeType.DISCOVERY))
    s.add_node(_typed_node("DEC", CognitionNodeType.DECISION))
    _edge(s, "I", "FIX", CognitionEdgeType.RESOLVED_BY)
    _edge(s, "I", "DISC", CognitionEdgeType.LED_TO)
    _edge(s, "I", "DEC", CognitionEdgeType.LED_TO)

    out = get_incident_resolution(s, "I")
    assert [r["id"] for r in out["resolutions"]] == ["FIX"]
    led_to_ids = {d["id"] for d in out["discoveries"]}
    assert led_to_ids == {"DISC", "DEC"}, "a non-discovery led_to follow-on was dropped"


def test_incident_resolution_missing_node_errors(tmp_path):
    s = CognitionStorage(tmp_path / ".cognition")
    assert "error" in get_incident_resolution(s, "nope")


# --- Commit 5: composition ---------------------------------------------------

def test_update_node_preserves_id_edges_and_curation(tmp_path):
    """The whole reason update_node exists: edit a node's narrative WITHOUT the
    delete+re-record that would lose its id, its edges, and its curation marker. After
    a summary edit + re-embed: same id, the edge survives, the curated marker persists,
    and search reflects the NEW text. (This is the end-to-end composition of Commits
    1+3 — the read surface, the in-place edit, and the re-embed.)"""
    s = CognitionStorage(tmp_path / ".cognition")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chromadb")
    gen = _gen()

    a = CognitionNode(
        id="A", type=CognitionNodeType.DECISION, summary="alpha plan", detail="body",
        context=[], references=[], timestamp="2026-06-13T00:00:00+00:00", author="t",
    )
    s.add_node(a)
    _embed_entity_node(embed, gen, a)
    s.add_node(_typed_node("B", CognitionNodeType.DECISION))
    _edge(s, "A", "B", CognitionEdgeType.LED_TO)
    assert s.mark_curated_by_skill("A")

    out = _update_node(s, embed, gen, node_id="A", embeddings_ready=True, summary="beta plan")
    assert out.get("reembed") == "done"
    assert out.get("id") == "A", "the id must be preserved across an edit"

    assert any(t == "B" for t, _ in s.get_successors("A")), "edge lost across the edit"
    node = s.get_node("A")
    assert node is not None and node.get("curated_by_skill_at") is not None, (
        "curation marker lost across the edit"
    )
    score = _score_for(_search_cognition(s, embed, gen, "beta")["results"], "A")
    assert score is not None and score > 0.99, "search did not reflect the edited text"


def test_record_and_update_share_one_embed_path(tmp_path):
    """Ledger 11: _embed_entity_node is the SINGLE node-vector path. For identical
    text, the vector a fresh record produces and the vector an update_node re-embed
    produces must be the same — i.e. update routes through the same helper, not a
    re-encoded copy that could drift."""
    s = CognitionStorage(tmp_path / ".cognition")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chromadb")
    gen = _gen()

    # record a node directly on GAMMA
    rec = CognitionNode(
        id="REC", type=CognitionNodeType.DECISION, summary="gamma topic", detail="body",
        context=[], references=[], timestamp="2026-06-13T00:00:00+00:00", author="t",
    )
    s.add_node(rec)
    _embed_entity_node(embed, gen, rec)

    # a different node edited INTO the same GAMMA text via update_node
    s.add_node(_node("UPD", summary="alpha topic"))
    _embed_entity_node(embed, gen, _node("UPD", summary="alpha topic"))
    _update_node(s, embed, gen, node_id="UPD", embeddings_ready=True, summary="gamma topic")

    # both now match a GAMMA query identically (same embed path -> same vector)
    results = _search_cognition(s, embed, gen, "gamma")["results"]
    assert _score_for(results, "REC") == _score_for(results, "UPD"), (
        "record and update produced different vectors for identical text (embed path drift)"
    )
