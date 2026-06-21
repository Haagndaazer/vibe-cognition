"""WP-ID: global node-id collision (data-loss minter) — the mint lives in add_node,
fires only at generation (never replay), and the embedding uses the minted id."""

import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

from fastmcp import Context

import vibe_cognition.tools.cognition_tools as ct
from vibe_cognition.cognition import CognitionNode, CognitionNodeType, CognitionStorage
from vibe_cognition.tools.cognition_tools import _record_node
from vibe_cognition.tools.utils import get_lifespan

_FROZEN = datetime(2026, 6, 13, 0, 0, 0, tzinfo=UTC)


class _FrozenClock:
    @staticmethod
    def now(tz=None):
        return _FROZEN


class _RecordingEmbed:
    """Records the id each embedding is upserted under (to catch a stale-id upsert)."""

    def __init__(self):
        self.upserts: list[str] = []

    def upsert_embedding(self, entity_id, embedding, metadata, document=None):
        self.upserts.append(entity_id)

    def delete_by_node_id(self, node_id):
        pass


class _Gen:
    def generate(self, text, input_type="document"):
        return [0.1, 0.2, 0.3]

    def generate_query_embedding(self, text):
        return self.generate(text, input_type="query")


def _ctx(storage, *, ready):
    ev = threading.Event()
    if ready:
        ev.set()
    lc = {
        "cognition_storage": storage,
        "cognition_embedding_storage": _RecordingEmbed(),
        "embedding_generator": _Gen(),
        "embedding_ready": ev,
        "embedding_error": None,
    }
    return cast(Context, SimpleNamespace(request_context=SimpleNamespace(lifespan_context=lc)))


def _node(node_id, detail="d"):
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DECISION, summary="s", detail=detail,
        context=[], references=[], timestamp="2026-06-13T00:00:00+00:00", author="t",
    )


def test_record_node_same_tick_collision_both_survive(tmp_path, monkeypatch):
    """The global bug (was document-only): two same-type+summary nodes recorded in one
    frozen clock tick get DISTINCT ids and BOTH survive. Fails-before: identical ids,
    the second silently overwrites the first → one node, one detail lost."""
    monkeypatch.setattr(ct, "datetime", _FrozenClock)
    s = CognitionStorage(tmp_path / ".cognition")
    ctx = _ctx(s, ready=False)  # embeddings off → embed block skipped
    r1 = _record_node(ctx, CognitionNodeType.DECISION, "same summary", "detail A", "c", "t")
    r2 = _record_node(ctx, CognitionNodeType.DECISION, "same summary", "detail B", "c", "t")

    assert r1["id"] != r2["id"], "same-summary same-tick records collided (data loss)"
    n1, n2 = s.get_node(r1["id"]), s.get_node(r2["id"])
    assert n1 is not None and n2 is not None, "a node was overwritten"
    assert n1["detail"] == "detail A"
    assert n2["detail"] == "detail B"


def test_record_node_embedding_uses_minted_id_not_stale(tmp_path, monkeypatch):
    """A1: on a salted collision the embedding must be upserted under the MINTED id.
    The graph node-id set must equal the embedded id set — else a node is in the graph
    with no embedding (silently unsearchable). Fails-before: the second record's vector
    lands under the stale (pre-mint) id, so the salted node has no embedding."""
    monkeypatch.setattr(ct, "datetime", _FrozenClock)
    s = CognitionStorage(tmp_path / ".cognition")
    ctx = _ctx(s, ready=True)
    embed = get_lifespan(ctx)["cognition_embedding_storage"]
    _record_node(ctx, CognitionNodeType.DECISION, "dup", "A", "c", "t")
    _record_node(ctx, CognitionNodeType.DECISION, "dup", "B", "c", "t")  # collides → salted

    graph_ids = {n["id"] for n in s.get_all_nodes()}
    assert len(graph_ids) == 2, "collision lost a node"
    assert graph_ids == set(embed.upserts), (
        f"embedded ids diverged from graph ids (stale-id upsert): graph={graph_ids} embed={set(embed.upserts)}"
    )


def test_replay_converges_without_resalting(tmp_path, monkeypatch):
    """The replay seam: a second storage hydrating the SAME journal converges to the
    SAME ids and count — replay re-applies journaled add_nodes directly (never the
    minting public add_node), so it must NOT re-salt or duplicate."""
    monkeypatch.setattr(ct, "datetime", _FrozenClock)
    cog = tmp_path / ".cognition"
    s1 = CognitionStorage(cog)
    ctx = _ctx(s1, ready=False)
    _record_node(ctx, CognitionNodeType.DECISION, "dup", "A", "c", "t")
    _record_node(ctx, CognitionNodeType.DECISION, "dup", "B", "c", "t")  # salted
    ids1 = {n["id"] for n in s1.get_all_nodes()}

    s2 = CognitionStorage(cog)  # fresh replay of the same journal
    ids2 = {n["id"] for n in s2.get_all_nodes()}
    assert ids2 == ids1, "replay did not converge to the same ids (re-salted on replay)"
    assert len(ids2) == 2, "replay duplicated or dropped a node"


def test_cross_process_mint_sees_caught_up_node(tmp_path, monkeypatch):
    """Composition / TOCTOU-shrink: the mint runs under _synced (catch-up first), so a
    second process that already journaled a same-summary node in the same tick is VISIBLE
    when this process mints → it salts to a distinct id. Both survive a third fresh replay.
    Fails-before (no mint): B writes A's id, and the replay collapses them to one node."""
    monkeypatch.setattr(ct, "datetime", _FrozenClock)
    cog = tmp_path / ".cognition"
    s_a = CognitionStorage(cog)
    r_a = _record_node(_ctx(s_a, ready=False), CognitionNodeType.DECISION, "dup", "A", "c", "t")

    s_b = CognitionStorage(cog)  # hydrates A's journaled node
    r_b = _record_node(_ctx(s_b, ready=False), CognitionNodeType.DECISION, "dup", "B", "c", "t")
    assert r_a["id"] != r_b["id"], "B's mint did not see A's journaled node (cross-process collision)"

    s_c = CognitionStorage(cog)  # third instance replays both
    ids = {n["id"] for n in s_c.get_all_nodes()}
    assert {r_a["id"], r_b["id"]} <= ids and len(ids) == 2, "cross-process nodes collapsed on replay"


def test_add_node_default_preserves_exact_id_and_overwrites(tmp_path):
    """Opt-out default (mint_unique_id=False) is non-breaking: the exact id is kept and
    a repeated id overwrites (the prior contract), so hand-chosen-id callers are safe."""
    s = CognitionStorage(tmp_path / ".cognition")
    assert s.add_node(_node("fixed", "a")) == "fixed"
    assert s.add_node(_node("fixed", "b")) == "fixed", "default add_node must keep the exact id"
    fixed = s.get_node("fixed")
    assert fixed is not None and fixed["detail"] == "b", "overwrite semantics preserved"
    assert sum(1 for n in s.get_all_nodes() if n["id"] == "fixed") == 1
