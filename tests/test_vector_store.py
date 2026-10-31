"""WP-VectorStore: the SQLite + numpy vector store that replaced chromadb.

Each test names what it fails on against the reverted (chromadb) code or a
broken variant of this one (ledger 12).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
from vibe_cognition.embeddings import VectorStore, adaptive_vector_search
from vibe_cognition.embeddings import vector_store as vs
from vibe_cognition.embeddings.vector_store import (
    STATE_NEWER_SCHEMA,
    STATE_OK,
    STATE_UNAVAILABLE,
    VectorStoreCorruptionError,
    active_generation,
    db_path,
    marker_path,
)

REPO = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).with_name("fixtures") / "vector_store_golden.json"


def _store(tmp_path: Path, name: str = "vectors", **kw: Any) -> VectorStore:
    return VectorStore(tmp_path / name, embedding_model=kw.pop("model", "m"),
                       embedding_dimensions=kw.pop("dims", 3), **kw)


def _peer_conn(store_or_path: VectorStore | Path) -> sqlite3.Connection:
    path = store_or_path.path if isinstance(store_or_path, VectorStore) else store_or_path
    return sqlite3.connect(path, timeout=0.1, isolation_level=None)


def _node(node_id: str, summary: str = "s") -> CognitionNode:
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DECISION, summary=summary, detail="d",
        context=[], references=[], timestamp="2026-10-31T00:00:00+00:00", author="t",
    )


def _child_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra)
    return env


# ── 1. golden parity against the recorded chromadb outputs ──────────────────


def _hit(h: dict) -> dict:
    out = {k: v for k, v in h.items() if k not in ("updated_at", "score")}
    out["score"] = round(float(h["score"]), 5)
    out["has_updated_at"] = "updated_at" in h
    return out


def test_golden_parity_with_chromadb(tmp_path):
    """Every search, count and delete result matches what ChromaDBStorage produced
    on the same fixture: ids, order, scores to 2e-5, metadata types, matched_text.
    Fails against any implementation with a different score formula, metadata
    round-trip or filter semantics."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    fx, exp = golden["fixture"], golden["expected"]
    store = VectorStore(tmp_path / "v", embedding_model="m", embedding_dimensions=fx["dim"])
    for e in fx["entries"]:
        store.upsert_embedding(e["id"], e["embedding"], e["metadata"], document=e["document"])
    e3 = fx["entries"][3]
    store.upsert_embedding("node03", e3["embedding"], {**e3["metadata"], "summary": "overwritten"})

    i = 0
    for q in fx["queries"]:
        for limit in (1, 3, 10, 100):
            for et in (None, "decision", "document"):
                got = [_hit(h) for h in store.vector_search(q, limit=limit, entity_type=et)]
                want = exp["searches"][i]["hits"]
                i += 1
                assert [h["_id"] for h in got] == [h["_id"] for h in want], (limit, et)
                for a, b in zip(got, want, strict=True):
                    assert abs(a["score"] - b["score"]) <= 2e-5
                    assert {k: v for k, v in a.items() if k != "score"} == {
                        k: v for k, v in b.items() if k != "score"
                    }
    assert {
        "all": store.count_documents(),
        "is_chunk": store.count_documents(filter={"is_chunk": True}),
        "node_id_doc1": store.count_documents(filter={"node_id": "doc1"}),
        "entity_decision": store.count_documents(filter={"entity_type": "decision"}),
    } == exp["counts"]
    store.delete_embedding("node00")
    store.delete_by_node_id("doc2")
    assert store.count_documents() == exp["after_deletes"]["count"]
    assert [h["_id"] for h in store.vector_search(fx["queries"][0], limit=100)] == exp["after_deletes"]["ids"]


# ── 2. search edge cases ─────────────────────────────────────────────────────


def test_search_on_an_empty_store_returns_nothing(tmp_path):
    store = _store(tmp_path)
    assert store.vector_search([0.1, 0.2, 0.3], limit=5) == []
    assert store.last_error() is None


@pytest.mark.parametrize("limit", [2, 3, 50])
def test_search_limit_at_or_past_the_store_size(tmp_path, limit):
    """k = min(limit, N): argpartition raises for kth >= N, so limit == N and
    limit > N need the full-pool branch."""
    store = _store(tmp_path)
    store.upsert_embedding("a", [1, 0, 0], {"entity_type": "x"})
    store.upsert_embedding("b", [0, 1, 0], {"entity_type": "x"})
    store.upsert_embedding("c", [0, 0, 1], {"entity_type": "y"})
    hits = store.vector_search([1, 0.1, 0], limit=limit)
    assert len(hits) == min(limit, 3)
    assert hits[0]["_id"] == "a"
    assert [h["_id"] for h in store.vector_search([1, 0, 0], limit=limit, entity_type="y")] == ["c"]


@pytest.mark.parametrize("query", [[0, 0, 0], [float("nan"), 1, 1], [1, 2], [], ["a", "b", "c"]])
def test_unusable_query_returns_nothing_and_says_why(tmp_path, query):
    store = _store(tmp_path)
    store.upsert_embedding("a", [1, 0, 0], {})
    assert store.vector_search(query, limit=5) == []
    assert store.last_error(), "an unusable query must record why it returned nothing"


def test_adaptive_search_reports_exhaustive_when_the_store_runs_dry(tmp_path):
    store = _store(tmp_path)
    for i in range(4):
        store.upsert_embedding(f"n{i}", [1, i, 0], {})
    out = adaptive_vector_search(store, [1, 0, 0], limit=10, dedupe=lambda hits, lim: (hits, 0))
    assert out["exhaustive"] is True
    assert out["total_found"] == 4


# ── 3. writes never raise ────────────────────────────────────────────────────


def test_a_wrong_dimension_write_is_skipped_not_raised(tmp_path, caplog):
    """Chroma raised on a dimension mismatch, out of cognition_record after the node
    was already in the graph. The store now logs and skips."""
    store = _store(tmp_path)
    store.upsert_embedding("a", [1, 0, 0], {})
    with caplog.at_level(logging.ERROR):
        store.upsert_embedding("b", [1, 0, 0, 0], {})
        store.replace_chunks("n", [("n#chunk-0", [1, 0, 0, 0], {"node_id": "n"}, "t")])
    assert store.all_ids() == ["a"]
    assert any("dimension" in r.message for r in caplog.records)


@pytest.mark.parametrize("vector", [[0, 0, 0], [float("inf"), 0, 0], [], "abc"])
def test_an_unusable_vector_is_skipped(tmp_path, vector):
    store = _store(tmp_path)
    store.upsert_embedding("a", vector, {})
    assert store.all_ids() == []


def test_a_locked_store_skips_the_write_and_never_marks_it(tmp_path):
    """SQLITE_BUSY after the busy timeout is contention, not damage: the write is
    skipped with a recorded reason, nothing is marked corrupt, and the store works
    again once the lock is released. Fails against a design that quarantines on any
    DatabaseError (the reviewed rev 1 of the shelved plan)."""
    store = _store(tmp_path)
    store.upsert_embedding("a", [1, 0, 0], {})
    peer = _peer_conn(store)
    peer.execute("BEGIN IMMEDIATE")
    try:
        store._conn.execute("PRAGMA busy_timeout=50")
        store.upsert_embedding("b", [0, 1, 0], {})
        assert "busy" in (store.last_error() or "")
    finally:
        peer.execute("ROLLBACK")
        peer.close()
    assert not list((tmp_path / "vectors").glob("*.corrupt"))
    store._busy_until = 0.0
    store.upsert_embedding("b", [0, 1, 0], {})
    assert sorted(store.all_ids()) == ["a", "b"]


def test_a_failed_transaction_never_blocks_later_writes(tmp_path, monkeypatch):
    """A failure after BEGIN IMMEDIATE must roll back; otherwise every later write
    fails with 'cannot start a transaction within a transaction' (review 5)."""
    store = _store(tmp_path)
    real = VectorStore._write_rows
    calls = {"n": 0}

    def flaky(self, conn, rows, dim):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected mid-transaction failure")
        return real(self, conn, rows, dim)

    monkeypatch.setattr(VectorStore, "_write_rows", flaky)
    store.upsert_embedding("a", [1, 0, 0], {})
    assert store._conn is not None and not store._conn.in_transaction
    store.upsert_embedding("b", [0, 1, 0], {})
    assert store.all_ids() == ["b"]


def test_a_failed_call_never_freezes_the_view_of_peer_writes(tmp_path, monkeypatch):
    store = _store(tmp_path)
    peer_store = _store(tmp_path)
    store.upsert_embedding("a", [1, 0, 0], {})
    assert [h["_id"] for h in store.vector_search([1, 0, 0], limit=5)] == ["a"]
    monkeypatch.setattr(VectorStore, "_write_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    store.upsert_embedding("z", [0, 0, 1], {})
    monkeypatch.undo()
    peer_store.upsert_embedding("b", [0.9, 0.1, 0], {})
    assert {h["_id"] for h in store.vector_search([1, 0, 0], limit=5)} == {"a", "b"}


def test_surrogates_in_chunk_text_are_stored_not_retried_forever(tmp_path):
    store = _store(tmp_path)
    store.replace_chunks("n", [("n#chunk-0", [1, 0, 0], {"node_id": "n", "is_chunk": True}, "bad \ud800 text")])
    [hit] = store.vector_search([1, 0, 0], limit=1)
    assert hit["matched_text"] == "bad ? text"


def test_replace_chunks_swaps_the_whole_set_atomically(tmp_path):
    store = _store(tmp_path)
    rows = [(f"n#chunk-{i}", [1, i, 0], {"node_id": "n", "is_chunk": True}, f"c{i}") for i in range(4)]
    store.replace_chunks("n", rows)
    store.replace_chunks("n", rows[:2])
    assert sorted(store.all_ids()) == ["n#chunk-0", "n#chunk-1"]
    store.replace_chunks("n", [])
    assert store.all_ids() == []


# ── 4. cross-process convergence with a barrier ──────────────────────────────

_WORKER = r'''
import sys, time
from pathlib import Path
from vibe_cognition.embeddings import VectorStore
store_dir, me, n, barrier = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])
store = VectorStore(store_dir, embedding_model="m", embedding_dimensions=4)
store.vector_search([1, 0, 0, 0], limit=5)
(barrier / f"ready-{me}").write_text("x")
deadline = time.monotonic() + 60
while len(list(barrier.glob("ready-*"))) < n:
    if time.monotonic() > deadline:
        sys.exit("barrier timeout")
    time.sleep(0.01)
for k in range(20):
    store.upsert_embedding(f"w{me}-{k}", [1 + me, k, 1, 0], {"entity_type": f"w{me}"})
store.delete_embedding(f"w{me}-0")
(barrier / f"done-{me}").write_text("x")
want = {f"w{j}-19" for j in range(n)}
while time.monotonic() < deadline:
    seen = {h["_id"] for h in store.vector_search([1, 1, 1, 0], limit=500)}
    if want <= seen and not any(f"w{j}-0" in seen for j in range(n)) and len(list(barrier.glob("done-*"))) == n:
        print("converged")
        sys.exit(0)
    time.sleep(0.05)
sys.exit("never saw the other workers' commits")
'''


def test_processes_sharing_a_store_converge(tmp_path):
    """Warm caches + a barrier: each process must see the others' commits in search.
    Fails against a cache that never reloads on a peer commit (data_version)."""
    n = 3
    worker = tmp_path / "worker.py"
    worker.write_text(_WORKER, encoding="utf-8")
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    store_dir = tmp_path / "shared"
    _store(tmp_path, "shared", dims=4).close()
    procs = [
        subprocess.Popen(
            [sys.executable, "-I", str(worker), str(store_dir), str(i), str(n), str(barrier)],
            env=_child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for i in range(n)
    ]
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err[-2000:]
        assert "converged" in out
    store = VectorStore(store_dir)
    assert set(store.all_ids()) == {f"w{i}-{k}" for i in range(n) for k in range(1, 20)}
    conn = _peer_conn(store)
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    conn.close()


# ── 5-7. corruption is detected and set aside; contention is not ─────────────


def _write_some(store: VectorStore, n: int = 50) -> None:
    for i in range(n):
        store.upsert_embedding(f"n{i}", [1, i, 2], {"entity_type": "decision", "summary": "x" * 200})


def test_a_store_that_is_not_a_database_is_set_aside(tmp_path):
    """The corruption class that crashed chromadb natively: here it is a catchable
    NOTADB, the generation is marked, and a fresh one is opened."""
    d = tmp_path / "vectors"
    d.mkdir()
    db_path(d, 1).write_bytes(b"not a database " * 100)
    events: list[dict] = []
    store = VectorStore.open_home(d, "m", 3, on_quarantine=events.append)
    assert store.available and store.generation == 2
    assert marker_path(d, 1).exists()
    assert db_path(d, 1).exists(), "the damaged file must stay for diagnosis"
    assert events and "not a database" in events[0]["reason"]
    store.upsert_embedding("a", [1, 0, 0], {})
    assert store.all_ids() == ["a"]


def test_a_truncated_store_is_set_aside(tmp_path):
    d = tmp_path / "vectors"
    store = _store(tmp_path)
    _write_some(store, 200)
    store.close()
    data = db_path(d, 1).read_bytes()
    for suffix in ("-wal", "-shm"):
        Path(str(db_path(d, 1)) + suffix).unlink(missing_ok=True)
    db_path(d, 1).write_bytes(data[: len(data) // 3])
    store = VectorStore(d, "m", 3)
    assert store.health_check() is True
    assert store.generation == 2 and marker_path(d, 1).exists()


@pytest.mark.parametrize("damage", ["nan", "short"])
def test_damaged_vector_payloads_are_caught_by_row_validation(tmp_path, damage):
    """quick_check passes on a well-formed page holding a bad blob; the reload's
    row validation is what catches it, and its reason names the check."""
    d = tmp_path / "vectors"
    store = _store(tmp_path)
    _write_some(store, 5)
    peer = _peer_conn(store)
    blob = b"\x00\x00\xc0\x7f" * 3 if damage == "nan" else b"\x00" * 7
    peer.execute("UPDATE vectors SET embedding = ? WHERE id = 'n3'", (blob,))
    peer.close()
    events: list[dict] = []
    store.on_quarantine = events.append
    assert store.health_check() is True
    assert marker_path(d, 1).exists()
    expected = "non-finite" if damage == "nan" else "bytes, expected"
    assert expected in events[0]["reason"]
    assert store.generation == 2 and store.all_ids() == []


def test_page_corruption_is_caught(tmp_path):
    d = tmp_path / "vectors"
    store = _store(tmp_path)
    _write_some(store, 300)
    conn = _peer_conn(store)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    page = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    store.close()
    path = db_path(d, 1)
    data = bytearray(path.read_bytes())
    for offset in range(page * 3, min(len(data), page * 9)):
        data[offset] = 0xA5
    path.write_bytes(bytes(data))
    events: list[dict] = []
    store = VectorStore.open_home(d, "m", 3, on_quarantine=events.append)
    store.health_check()
    assert marker_path(d, 1).exists(), "page damage must be caught by quick_check or the reload"
    reason = events[0]["reason"]
    caught_by_quick_check = "(confirmed: quick_check:" in reason
    caught_by_sqlite_read = "database disk image is malformed" in reason
    assert caught_by_quick_check or caught_by_sqlite_read, reason
    assert store.available


# ── 8-9. quarantine mechanics, the confirm step and its limits ───────────────


def test_two_stores_quarantining_together_share_one_new_generation(tmp_path):
    d = tmp_path / "vectors"
    a = _store(tmp_path)
    b = _store(tmp_path)
    _write_some(a, 3)
    peer = _peer_conn(a)
    peer.execute("UPDATE vectors SET embedding = x'00' WHERE id = 'n1'")
    peer.close()
    changed = threading.Event()
    b.on_resync_needed = changed.set
    a.health_check()
    assert a.generation == 2
    b.upsert_embedding("after", [1, 0, 0], {})
    assert b.generation == 2 and changed.is_set(), "the peer must follow and trigger a re-sync"
    assert a.all_ids() == ["after"]
    assert sorted(p.name for p in d.glob("*.corrupt")) == ["vectors-1.corrupt"]
    assert db_path(d, 1).exists()


def test_an_unconfirmed_failure_is_never_quarantined(tmp_path):
    """A corruption-looking error the fresh connection cannot reproduce is treated as
    transient: no marker, and the confirm step is not re-run on every call."""
    store = _store(tmp_path)
    _write_some(store, 3)

    def op(conn):
        raise VectorStoreCorruptionError("looked corrupt once")

    assert store._run(op, "default", "probe") == "default"
    assert not marker_path(tmp_path / "vectors", 1).exists()
    assert store._confirm_cooldown_until > time.monotonic()
    assert store.count_documents() == 3, "an unconfirmed error must not take the store offline"


def test_repeated_damage_stops_rebuilding(tmp_path, monkeypatch):
    """A fault that recurs on every fresh generation (a network or sync folder) must
    not mark generations forever (review blocker 1)."""
    d = tmp_path / "vectors"
    d.mkdir()

    def always_corrupt(self, generation, busy_ms):
        db_path(self._dir, generation).write_bytes(b"x")
        raise VectorStoreCorruptionError("deterministic damage")

    monkeypatch.setattr(VectorStore, "_connect", always_corrupt)
    monkeypatch.setattr(VectorStore, "_confirm_damage", lambda self, g: "confirmed")
    store = VectorStore.open_home(d, "m", 3)
    for _ in range(5):
        store._last_open_attempt = 0.0
        store.upsert_embedding("a", [1, 0, 0], {})
    assert len(list(d.glob("*.corrupt"))) == 3
    assert store.state == STATE_UNAVAILABLE
    assert "repeated corruption" in (store.status()["reason"] or "")


def test_deleting_old_generation_files_never_moves_the_active_generation(tmp_path):
    """Review 2: with 'smallest unmarked generation', deleting generation 1's files
    and marker would make new sessions start an empty generation 1 while peers stay
    on 2 (split brain)."""
    d = tmp_path / "vectors"
    d.mkdir()
    db_path(d, 1).write_bytes(b"junk")
    store = VectorStore.open_home(d, "m", 3)
    store.upsert_embedding("a", [1, 0, 0], {})
    store.close()
    assert active_generation(d) == 2
    for p in d.glob("vectors-1*"):
        p.unlink()
    assert active_generation(d) == 2
    assert VectorStore(d, "m", 3).all_ids() == ["a"]


def test_quarantine_writes_a_complete_marker(tmp_path):
    d = tmp_path / "vectors"
    d.mkdir()
    db_path(d, 1).write_bytes(b"junk" * 50)
    VectorStore.open_home(d, "m", 3).close()
    info = json.loads(marker_path(d, 1).read_text(encoding="utf-8"))
    assert info["generation"] == 1 and info["at"] and info["reason"]
    assert vs.quarantined_generations(d)[0]["reason"] == info["reason"]
    assert not list(d.glob("*.tmp"))


# ── 10. a newer or unknown format is left untouched ──────────────────────────


@pytest.mark.parametrize("change", ["user_version", "format"])
def test_a_newer_store_is_never_quarantined_or_written(tmp_path, change):
    d = tmp_path / "vectors"
    store = _store(tmp_path)
    store.upsert_embedding("a", [1, 0, 0], {})
    store.close()
    conn = _peer_conn(db_path(d, 1))
    if change == "user_version":
        conn.execute("PRAGMA user_version=2")
    else:
        conn.execute("UPDATE store_meta SET value = 'f16-v9' WHERE key = 'format'")
    conn.close()
    store = VectorStore(d, "m", 3)
    assert store.state == STATE_NEWER_SCHEMA
    store.upsert_embedding("b", [0, 1, 0], {})
    assert store.vector_search([1, 0, 0], limit=5) == []
    assert not list(d.glob("*.corrupt"))
    conn = _peer_conn(db_path(d, 1))
    assert [r[0] for r in conn.execute("SELECT id FROM vectors").fetchall()] == ["a"]
    conn.close()


def test_search_reports_an_unusable_store_instead_of_no_history(tmp_path, build_lc, make_ctx, mock_mcp):
    """Review 4: a newer-schema or failing store must make cognition_search say
    semantic_unavailable, never return a bare [] that reads as 'no history'."""
    from vibe_cognition.tools.cognition_tools import register_cognition_tools

    lc = build_lc(tmp_path, embeddings_ready=True)
    register_cognition_tools(mock_mcp)
    search = mock_mcp.tools["cognition_search"]
    store: VectorStore = lc["cognition_embedding_storage"]

    store._state = STATE_NEWER_SCHEMA
    store._close_conn()
    out = search(make_ctx(lc), query="anything")
    assert out["semantic_unavailable"] is True and out["reason"] == "newer-schema"

    store._state = STATE_OK
    store._open_active()

    def broken(conn):
        raise sqlite3.OperationalError("disk I/O error")

    store._refresh_cache = broken  # type: ignore[method-assign]
    out = search(make_ctx(lc), query="anything")
    assert out["semantic_unavailable"] is True and out["reason"] == "vector-store-unavailable"
    assert "disk I/O" in out["detail"]


def test_search_flags_a_rebuild_in_progress(tmp_path, build_lc, make_ctx, mock_mcp):
    from vibe_cognition.tools.cognition_tools import register_cognition_tools

    lc = build_lc(tmp_path, embeddings_ready=True)
    lc["embedding_sync_done"] = threading.Event()
    register_cognition_tools(mock_mcp)
    out = mock_mcp.tools["cognition_search"](make_ctx(lc), query="anything")
    assert out.get("index_syncing") is True
    lc["embedding_sync_done"].set()
    out = mock_mcp.tools["cognition_search"](make_ctx(lc), query="anything")
    assert "index_syncing" not in out


# ── 6 (review). a pre-yield open never stalls behind a peer's lock ───────────


def test_startup_open_does_not_wait_on_a_peer_lock(tmp_path):
    d = tmp_path / "vectors"
    d.mkdir()
    peer = sqlite3.connect(db_path(d, 1), timeout=0.1, isolation_level=None)
    peer.execute("BEGIN IMMEDIATE")
    peer.execute("CREATE TABLE IF NOT EXISTS _hold (x)")
    try:
        started = time.monotonic()
        store = VectorStore.open_home(d, "m", 3, startup=True)
        assert store.count_documents() == 0
        assert time.monotonic() - started < 2.0, "the pre-yield path must not wait out the busy timeout"
        assert store.state == STATE_UNAVAILABLE
    finally:
        peer.execute("ROLLBACK")
        peer.close()
    assert store.ensure_open() is True
    store.upsert_embedding("a", [1, 0, 0], {})
    assert store.all_ids() == ["a"]
    assert not list(d.glob("*.corrupt"))


# ── 13. open modes ───────────────────────────────────────────────────────────


def test_read_only_and_viewer_opens_never_create(tmp_path):
    d = tmp_path / "absent"
    assert VectorStore.open_existing(d) is None
    assert VectorStore.open_viewer(d) is None
    assert not d.exists()


def test_viewer_and_read_only_opens_never_quarantine(tmp_path):
    d = tmp_path / "vectors"
    d.mkdir()
    db_path(d, 1).write_bytes(b"junk" * 100)
    assert VectorStore.open_existing(d) is None
    assert VectorStore.open_viewer(d) is None
    assert not list(d.glob("*.corrupt"))


@pytest.mark.parametrize("keep_shm", [True, False])
def test_read_only_open_of_a_crashed_wal_store(tmp_path, keep_shm):
    """A server killed mid-write leaves -wal (and maybe no -shm); a foreign read-only
    attach must still read it, including the rows only in the WAL (review 10)."""
    live = _store(tmp_path, "live")
    live._conn.execute("PRAGMA wal_autocheckpoint=0")
    _write_some(live, 10)
    copy = tmp_path / "copy"
    copy.mkdir()
    for suffix in ("", "-wal") + (("-shm",) if keep_shm else ()):
        shutil.copy(str(live.path) + suffix, str(db_path(copy, 1)) + suffix)
    live.close()
    foreign = VectorStore.open_existing(copy)
    assert foreign is not None
    assert foreign.count_documents() == 10
    foreign.upsert_embedding("never", [1, 0, 0], {})
    assert foreign.count_documents() == 10
    foreign.close()


# ── 14. sessions syncing one project split the work ──────────────────────────


class _SlowGen:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def generate(self, text: str, input_type: str = "document") -> list[float]:
        with self._lock:
            self.calls += 1
        time.sleep(0.004)
        return [1.0, float(len(text) % 7), 0.5]


def _two_concurrent_syncs(tmp_path: Path) -> tuple[int, int, set[str]]:
    from vibe_cognition.server import _sync_cognition_embeddings

    cognition = CognitionStorage(tmp_path / ".cognition")
    n = 60
    for i in range(n):
        cognition.add_node(_node(f"node{i:03d}", f"summary {i}"))
    stores = [_store(tmp_path), _store(tmp_path)]
    gens = [_SlowGen(), _SlowGen()]
    barrier = threading.Barrier(2)

    def run(i: int) -> None:
        barrier.wait()
        _sync_cognition_embeddings(cognition, stores[i], gens[i])  # type: ignore[arg-type]

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    return n, gens[0].calls + gens[1].calls, set(stores[0].all_ids())


def test_concurrent_syncs_split_the_work(tmp_path):
    n, total, ids = _two_concurrent_syncs(tmp_path)
    assert ids == {f"node{i:03d}" for i in range(n)}
    assert total < 1.5 * n, f"{total} embeds for {n} nodes: the sessions repeated each other's work"


def test_the_split_test_is_sensitive_to_the_recheck(tmp_path, monkeypatch):
    """Proves test_concurrent_syncs_split_the_work does not pass trivially: with the
    per-node recheck disabled, both sessions embed everything."""
    monkeypatch.setattr(VectorStore, "existing_ids", lambda self, ids: set())
    n, total, _ = _two_concurrent_syncs(tmp_path)
    assert total >= 1.5 * n


# ── 15. the maintenance loop re-syncs after a generation change ──────────────


def test_generation_change_triggers_a_resync(tmp_path, monkeypatch):
    from vibe_cognition import server

    passes: list[int] = []
    monkeypatch.setattr(server, "_sync_cognition_embeddings", lambda *a: passes.append(1) or {})
    context: dict[str, Any] = {"vector_resync": threading.Event(), "embedding_sync_done": threading.Event()}
    t = threading.Thread(target=server._maintain_vector_store, args=(context, None, None, None), daemon=True)
    t.start()
    context["vector_resync"].set()
    for _ in range(200):
        if passes:
            break
        time.sleep(0.01)
    assert passes == [1]
    assert context["embedding_sync_done"].is_set()
    context["_vector_maintenance_stop"] = True
    context["vector_resync"].set()
    t.join(5)
    assert not t.is_alive()


def test_model_drift_skips_the_startup_sync(tmp_path, monkeypatch):
    """Review 15: with a different model stamped on the store, every node would be
    re-embedded on every start and every write rejected; the sync is skipped."""
    from vibe_cognition import server

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")
    monkeypatch.setattr(server, "run_stale_sweep", lambda: {})
    monkeypatch.setattr(server.EmbeddingGenerator, "from_config", lambda cfg: SimpleNamespace())
    synced: list[int] = []
    monkeypatch.setattr(server, "_sync_cognition_embeddings", lambda *a: synced.append(1) or {})
    config = server.Settings()
    cognition = CognitionStorage(tmp_path / ".cognition")
    store = VectorStore(tmp_path / "v", embedding_model="some-other-model",
                        embedding_dimensions=config.embedding_dimensions)
    context = {
        "cognition_storage": cognition, "cognition_embedding_storage": store,
        "loaded_projects": None, "embedding_ready": threading.Event(),
        "embedding_sync_done": threading.Event(), "embedding_error": None,
    }
    server._load_embeddings_and_sync(config, context)
    assert context["home_model_guard"] == "model-mismatch"
    assert synced == []
    assert context["embedding_sync_done"].is_set()


# ── prime notice ─────────────────────────────────────────────────────────────


def test_rebuilt_notice_is_shown_once_at_the_next_session_start(tmp_path):
    from vibe_cognition.cognition.prime import _consume_rehydrate_flag
    from vibe_cognition.server import _record_vector_store_rebuilt

    cognition_dir = tmp_path / ".cognition"
    (cognition_dir / "local").mkdir(parents=True)
    _record_vector_store_rebuilt(cognition_dir, {"generation": 1, "at": "2026-10-31T00:00:00+00:00", "reason": "x"})
    first = _consume_rehydrate_flag(cognition_dir)
    assert "search index was found damaged" in first and "no memories were lost" in first
    assert _consume_rehydrate_flag(cognition_dir) == ""


# ── 11-12. the real server starts on a broken store, without chromadb ────────


def _lines(stream, sink: list[str]) -> None:
    for raw in stream:
        sink.append(raw.decode("utf-8", "replace").rstrip())


def _wait_json(out: list[str], wanted_id: int, err: list[str], timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in out:
            if line.startswith("{"):
                msg = json.loads(line)
                if msg.get("id") == wanted_id:
                    return msg
        time.sleep(0.05)
    tail = "\n".join(err[-40:])
    raise AssertionError(f"no response {wanted_id}; server stderr tail:\n{tail}")


def test_server_entrypoint_starts_on_a_broken_store(tmp_path):
    """The real server, given a store that is not a database, still answers
    initialize and tools/list, with the damaged generation set aside. Against the
    chromadb build the equivalent damage killed the process with 0xC0000005."""
    from vibe_cognition.config import project_store_key

    repo = tmp_path / "repo"
    repo.mkdir()
    data = tmp_path / "data"
    env = _child_env(REPO_PATH=str(repo), VIBE_DATA_DIR=str(data), EMBEDDING_BACKEND="ollama",
                     OLLAMA_BASE_URL="http://127.0.0.1:9")
    env.pop("VIBE_VECTORS_DIR", None)
    store_dir = data / "vectors" / project_store_key(repo)
    store_dir.mkdir(parents=True)
    db_path(store_dir, 1).write_bytes(b"\x00garbage, not sqlite" * 200)
    proc = subprocess.Popen(
        [sys.executable, "-m", "vibe_cognition.server"], cwd=str(repo), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out: list[str] = []
    err: list[str] = []
    threading.Thread(target=_lines, args=(proc.stdout, out), daemon=True).start()
    threading.Thread(target=_lines, args=(proc.stderr, err), daemon=True).start()

    def send(msg: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(msg).encode() + b"\n")
        proc.stdin.flush()

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}})
        assert "result" in _wait_json(out, 1, err)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert len(_wait_json(out, 2, err)["result"]["tools"]) > 20
        assert marker_path(store_dir, 1).exists()
    finally:
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    assert proc.returncode != 3221225477, "native crash"


def test_the_server_never_imports_chromadb(tmp_path):
    code = "import sys, vibe_cognition.server; print('chromadb' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], env=_child_env(REPO_PATH=str(tmp_path)),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "False"


def test_no_source_file_imports_chromadb():
    import ast

    offenders = []
    for path in (REPO / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n == "chromadb" or n.startswith("chromadb.") for n in names):
                offenders.append(str(path.relative_to(REPO)))
    assert offenders == []


# ── 16. tests never write into the real plugin data dir ──────────────────────

_RESOLVE = (
    "from vibe_cognition.config import Settings\n"
    "from vibe_cognition.embeddings import VectorStore\n"
    "p = Settings().cognition_vectors_path\n"
    "VectorStore.open_home(p, 'm', 3).close()\n"
    "print(p)\n"
)


def _sentinel_home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    sentinel = home / ".claude" / "plugins" / "data" / "vibe-cognition-sentinel"
    sentinel.mkdir(parents=True)
    return home, sentinel


def test_subprocesses_inherit_an_isolated_data_root(tmp_path):
    """Task 46822ab83433: a deleted VIBE_DATA_DIR was inherited as absent, and the
    child's discovery glob found the REAL plugin data dir (246 stray stores)."""
    home, sentinel = _sentinel_home(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _child_env(REPO_PATH=str(repo), HOME=str(home), USERPROFILE=str(home))
    out = subprocess.run([sys.executable, "-c", _RESOLVE], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert list(sentinel.iterdir()) == []
    assert Path(out.stdout.strip().splitlines()[-1]).is_relative_to(Path(os.environ["VIBE_DATA_DIR"]))


def test_the_isolation_guard_is_not_vacuous(tmp_path):
    """Without VIBE_DATA_DIR the same child really does write into the sentinel, so the
    test above would catch a regression."""
    home, sentinel = _sentinel_home(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _child_env(REPO_PATH=str(repo), HOME=str(home), USERPROFILE=str(home))
    env.pop("VIBE_DATA_DIR", None)
    out = subprocess.run([sys.executable, "-c", _RESOLVE], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert list(sentinel.iterdir()) != []


# ── code-review findings (0.47.0) ────────────────────────────────────────────


def test_a_retried_chunk_write_never_stamps_dimension_zero(tmp_path, monkeypatch):
    """Review H1: the chunk write's dimension was popped from a captured set, so the
    automatic retry after one I/O error stamped dim=0 and the healthy store then
    rejected every write and was quarantined."""
    store = _store(tmp_path)
    real = VectorStore._write_rows
    calls = {"n": 0}

    def flaky(self, conn, rows, dim):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real(self, conn, rows, dim)

    monkeypatch.setattr(VectorStore, "_write_rows", flaky)
    assert store.replace_chunks("n", [("n#chunk-0", [1, 0, 0], {"node_id": "n", "is_chunk": True}, "t")])
    monkeypatch.undo()
    assert store.sample_dim() == 3
    assert store.upsert_embedding("after", [0, 1, 0], {})
    assert store.health_check() and store.generation == 1


def _cognition_with_nodes(tmp_path: Path, n: int = 5) -> CognitionStorage:
    cognition = CognitionStorage(tmp_path / ".cognition")
    for i in range(n):
        cognition.add_node(_node(f"node{i}", f"summary {i}"))
    return cognition


def test_sync_never_reads_an_unusable_store_as_empty(tmp_path):
    """Review H2: a newer-schema (or unavailable) store returned {} for every read, so
    the sync embedded the whole graph on every start and every write was dropped."""
    from vibe_cognition.server import _sync_cognition_embeddings

    d = tmp_path / "vectors"
    _store(tmp_path).close()
    conn = _peer_conn(db_path(d, 1))
    conn.execute("PRAGMA user_version=2")
    conn.close()
    store = VectorStore(d, "m", 3)
    gen = _SlowGen()
    _sync_cognition_embeddings(_cognition_with_nodes(tmp_path), store, gen)  # type: ignore[arg-type]
    assert gen.calls == 0


def test_sync_stops_when_writes_fail_and_resumes_when_the_store_recovers(tmp_path, monkeypatch):
    from vibe_cognition.server import _sync_cognition_embeddings

    store = _store(tmp_path)
    resyncs: list[int] = []
    store.on_resync_needed = lambda: resyncs.append(1)
    real = VectorStore._write_rows
    monkeypatch.setattr(VectorStore, "_write_rows",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    gen = _SlowGen()
    _sync_cognition_embeddings(_cognition_with_nodes(tmp_path, 8), store, gen)  # type: ignore[arg-type]
    assert gen.calls == 1, "the pass must stop at the first write the store refuses"
    assert store.status()["failed_writes"] >= 1
    monkeypatch.setattr(VectorStore, "_write_rows", real)
    store.count_documents()
    assert resyncs, "the first call that works must ask for the re-sync"


def test_replayed_nodes_are_requeued_when_the_store_cannot_take_them(tmp_path, monkeypatch):
    """Review H2: the replay drain popped the ids, then dropped them when the store was
    busy, so a teammate's nodes stayed unsearchable for the whole session."""
    from vibe_cognition.tools.cognition_tools import _reembed_replayed_nodes

    store = _store(tmp_path)
    cognition = _cognition_with_nodes(tmp_path, 3)
    cognition.requeue_replayed_node_ids(["node0", "node1", "node2"])
    monkeypatch.setattr(VectorStore, "_write_rows",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    assert _reembed_replayed_nodes(cognition, store, _SlowGen()) == 0  # type: ignore[arg-type]
    assert sorted(cognition.pop_replayed_node_ids()) == ["node0", "node1", "node2"]


def test_a_busy_write_does_not_block_reads(tmp_path):
    """Review M1: the busy breaker used to gate reads too, so one blocked write made
    every search report the index unavailable for 5 s although WAL reads work."""
    store = _store(tmp_path)
    store.upsert_embedding("a", [1, 0, 0], {})
    peer = _peer_conn(store)
    peer.execute("BEGIN IMMEDIATE")
    try:
        store._conn.execute("PRAGMA busy_timeout=50")
        assert store.upsert_embedding("b", [0, 1, 0], {}) is False
        assert [h["_id"] for h in store.vector_search([1, 0, 0], limit=5)] == ["a"]
        assert store.last_error() is None
    finally:
        peer.execute("ROLLBACK")
        peer.close()


def test_a_raising_callback_never_breaks_the_store(tmp_path):
    d = tmp_path / "vectors"
    d.mkdir()
    db_path(d, 1).write_bytes(b"junk" * 64)

    def boom(*args):
        raise RuntimeError("callback failed")

    store = VectorStore.open_home(d, "m", 3, on_quarantine=boom)
    store.on_resync_needed = boom
    assert store.available and store.generation == 2
    assert store.upsert_embedding("a", [1, 0, 0], {})


def test_adaptive_search_stops_at_its_cap_on_a_real_store(tmp_path):
    store = _store(tmp_path)
    for i in range(600):
        store.upsert_embedding(f"n{i}", [1, i % 17, i % 5], {})
    out = adaptive_vector_search(store, [1, 0, 0], limit=10, dedupe=lambda hits, lim: ([], 0))
    assert out["exhaustive"] is False
    assert out["results"] == []


def test_a_store_kept_busy_by_peers_never_makes_the_resync_loop_spin(tmp_path, monkeypatch):
    """Code re-review HIGH: with every write failing, each successful read re-armed a
    resync and passes ran back to back. Passes are now spaced and the sync stops
    before spending an embed it cannot store."""
    from vibe_cognition import server

    store = _store(tmp_path)
    cognition = _cognition_with_nodes(tmp_path, 20)
    passes: list[int] = []
    real_sync = server._sync_cognition_embeddings

    def counting(*args):
        passes.append(1)
        return real_sync(*args)

    monkeypatch.setattr(server, "_sync_cognition_embeddings", counting)
    context: dict[str, Any] = {"vector_resync": threading.Event(), "embedding_sync_done": threading.Event()}
    store.on_resync_needed = context["vector_resync"].set
    gen = _SlowGen()
    peer = _peer_conn(store)
    peer.execute("BEGIN IMMEDIATE")
    store._conn.execute("PRAGMA busy_timeout=20")
    t = threading.Thread(target=server._maintain_vector_store, args=(context, cognition, store, gen), daemon=True)
    try:
        t.start()
        context["vector_resync"].set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            store.vector_search([1, 0, 0], limit=3)
            time.sleep(0.02)
    finally:
        context["_vector_maintenance_stop"] = True
        context["vector_resync"].set()
        peer.execute("ROLLBACK")
        peer.close()
        t.join(10)
    assert len(passes) <= 2, f"{len(passes)} passes in 3 s"
    assert gen.calls <= 2, f"{gen.calls} embeds spent on a store that could not take them"


def test_a_search_never_embeds_the_whole_graph_inline(tmp_path):
    """Upgrade simulation finding: the start-up replay queues EVERY node for the
    re-embed drain, so on a fresh store the first search embedded the whole graph
    inline (about 18 minutes for Ling Lang) and the search never answered. While the
    index is syncing the drain waits; otherwise it embeds at most a few nodes and
    hands the rest to the background maintenance loop."""
    from vibe_cognition.tools.cognition_tools import _INLINE_REEMBED_CAP, _reembed_replayed_nodes

    store = _store(tmp_path)
    cognition = _cognition_with_nodes(tmp_path, 40)
    asked: list[int] = []
    store.on_resync_needed = lambda: asked.append(1)
    gen = _SlowGen()
    assert _reembed_replayed_nodes(cognition, store, gen, index_syncing=True) == 0  # type: ignore[arg-type]
    assert gen.calls == 0
    assert _reembed_replayed_nodes(cognition, store, gen) == _INLINE_REEMBED_CAP  # type: ignore[arg-type]
    assert gen.calls == _INLINE_REEMBED_CAP
    assert asked, "the rest must be handed to the maintenance loop"
    not_embedded = {f"node{i}" for i in range(40)} - set(store.all_ids())
    assert len(not_embedded) == 40 - _INLINE_REEMBED_CAP
    assert not_embedded <= set(cognition.pop_replayed_node_ids()), "unembedded nodes must stay queued"
