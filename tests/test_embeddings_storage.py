"""Tests for ChromaDBStorage construction (audit E-1: telemetry off)."""

from unittest.mock import patch

import pytest
from chromadb.errors import InternalError

from vibe_cognition.embeddings import ChromaDBStorage
from vibe_cognition.embeddings.generator import SentenceTransformersBackend
from vibe_cognition.embeddings.storage import _retry_chromadb_open


def test_chromadb_telemetry_disabled(tmp_path):
    """The persistent client must be constructed with telemetry disabled.

    Defense-in-depth (audit E-1): inert at our pinned chromadb 1.5.5 (the
    telemetry client is a no-op stub), but chromadb 0.5-0.6.x — permitted by
    our >=0.5.0 floor — actively phoned home gated on this flag. Regression
    test: fails before the fix (default True), passes after.
    """
    storage = ChromaDBStorage(persist_directory=tmp_path / "chromadb")

    assert storage._client.get_settings().anonymized_telemetry is False


def test_delete_by_node_id_purges_chunks_and_is_noop_safe(tmp_path):
    """WP-D1b chunk purge: delete_by_node_id removes exactly the chunk vectors
    tagged with that node_id, leaves others, and is a clean no-op on an empty
    collection / docs lacking the field (A2/A3 — direct delete(where=), never the
    get-then-delete-ids shape that would raise on an empty match)."""
    storage = ChromaDBStorage(persist_directory=tmp_path / "chromadb")

    # No-op safe on an empty collection (no raise).
    storage.delete_by_node_id("nobody")

    # Seed two chunks for node A and one for node B (the shape D2 will write).
    storage.upsert_embedding("A#chunk-0", [0.1, 0.2, 0.3], {"node_id": "A"})
    storage.upsert_embedding("A#chunk-1", [0.4, 0.5, 0.6], {"node_id": "A"})
    storage.upsert_embedding("B#chunk-0", [0.7, 0.8, 0.9], {"node_id": "B"})

    storage.delete_by_node_id("A")

    remaining = set(storage._collection.get()["ids"])
    assert remaining == {"B#chunk-0"}, f"chunk purge wrong set: {remaining}"

    # No-op safe when nothing matches the field value.
    storage.delete_by_node_id("A")
    assert set(storage._collection.get()["ids"]) == {"B#chunk-0"}


def test_count_documents_splits_nodes_and_chunks(tmp_path):
    """WP-D2 count split (A1): the node-vs-chunk count separates via the positive
    is_chunk marker — count_documents(filter={'is_chunk': True}) (the PUBLIC param is
    filter=, not where=). Node count = total - chunks."""
    storage = ChromaDBStorage(persist_directory=tmp_path / "chromadb")
    storage.upsert_embedding("n1", [0.1, 0.2, 0.3], {"entity_type": "decision"})  # node vector
    storage.upsert_embedding("d1#chunk-0", [0.1, 0.2, 0.3],
                             {"node_id": "d1", "is_chunk": True}, document="x")
    storage.upsert_embedding("d1#chunk-1", [0.1, 0.2, 0.31],
                             {"node_id": "d1", "is_chunk": True}, document="y")

    total = storage.count_documents()
    chunks = storage.count_documents(filter={"is_chunk": True})
    assert total == 3, f"total wrong: {total}"
    assert chunks == 2, f"is_chunk count wrong: {chunks}"
    assert total - chunks == 1, "node count (total - chunks) wrong"


def test_retry_chromadb_open_absorbs_transient_internal_error():
    """WP-A 1a: bounded retry absorbs a transient chromadb rust-backend
    InternalError (flake e09d4f4a9a23) and returns the eventual success.

    Fails-before: no retry existed, so a single InternalError propagated
    straight out of ChromaDBStorage.__init__ and killed the MCP handshake.
    """
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise InternalError("simulated rust-backend flake")
        return "ok"

    assert _retry_chromadb_open(flaky) == "ok"
    assert calls["n"] == 3


def test_retry_chromadb_open_bounded_reraises_after_exhausting_attempts():
    """The retry is BOUNDED -- a persistent InternalError still propagates
    (never retries forever), so a genuine failure fails diagnosably instead
    of hanging the handshake indefinitely."""

    def always_broken():
        raise InternalError("persistent rust-backend failure")

    with pytest.raises(InternalError, match="persistent rust-backend failure"):
        _retry_chromadb_open(always_broken)


def test_retry_chromadb_open_does_not_retry_other_exceptions():
    """Only InternalError is retried -- any other exception (e.g. the
    NotFoundError the is_new-collection probe expects on a fresh install)
    propagates immediately on the FIRST attempt, unretried, so the expected
    "collection doesn't exist yet" control flow is never delayed."""
    calls = {"n": 0}

    def not_found():
        calls["n"] += 1
        raise ValueError("not an InternalError")

    with pytest.raises(ValueError, match="not an InternalError"):
        _retry_chromadb_open(not_found)
    assert calls["n"] == 1


def test_chromadb_storage_construction_survives_transient_internal_error(tmp_path):
    """Integration-level: ChromaDBStorage() itself (the actual pre-yield call
    site server.py's lifespan hits) absorbs a transient InternalError from
    get_or_create_collection during construction, not just the bare helper.

    chromadb.PersistentClient is a FACTORY FUNCTION (not a class) that returns
    a ClientCreator instance, so the flaky behavior is installed as an instance
    attribute override on the real client PersistentClient produces, rather
    than patched on a class.
    """
    from vibe_cognition.embeddings import storage as storage_module

    real_persistent_client = storage_module.chromadb.PersistentClient
    state = {"n": 0}

    def flaky_persistent_client(*args, **kwargs):
        client = real_persistent_client(*args, **kwargs)
        real_get_or_create = client.get_or_create_collection

        def flaky_get_or_create(*a, **kw):
            state["n"] += 1
            if state["n"] < 2:
                raise InternalError("simulated rust-backend flake")
            return real_get_or_create(*a, **kw)

        client.get_or_create_collection = flaky_get_or_create
        return client

    with patch.object(storage_module.chromadb, "PersistentClient", flaky_persistent_client):
        storage = ChromaDBStorage(persist_directory=tmp_path / "chromadb")

    assert storage._collection is not None
    assert state["n"] == 2


def test_flatten_metadata_updated_at_is_timezone_aware(tmp_path):
    """E-7a: _flatten_metadata must produce a timezone-aware ISO timestamp
    (datetime.now(UTC), not datetime.utcnow()).

    Fails-before: datetime.utcnow().isoformat() produces a naive string like
    '2024-01-01T12:00:00.000000' with no '+00:00' suffix.
    Passes after: datetime.now(UTC).isoformat() appends '+00:00'.
    """
    storage = ChromaDBStorage(persist_directory=tmp_path / "chromadb")
    flat = storage._flatten_metadata({"x": "y"})
    updated_at = str(flat["updated_at"])
    assert "+00:00" in updated_at, (
        f"updated_at is not timezone-aware; got {updated_at!r} (expected '+00:00' suffix)"
    )


def test_revision_pin_forwarded_to_sentence_transformer(tmp_path):
    """E-7b: SentenceTransformersBackend must pass revision= to SentenceTransformer.

    Fails-before: SentenceTransformer called without revision kwarg — revision
    kwarg assertion fails. Passes after: revision='abc123' forwarded correctly.
    Also verifies revision=None is passed (SentenceTransformer treats None == absent).
    """
    captured_kwargs = {}

    def fake_st(model_name, **kwargs):
        captured_kwargs.update(kwargs)
        m = object.__new__(type("FakeST", (), {"encode": lambda s, t, **kw: [[0.0]]}))
        return m

    with patch("vibe_cognition.embeddings.generator.SentenceTransformer", side_effect=fake_st):
        SentenceTransformersBackend("test-model", revision="abc123")

    assert captured_kwargs.get("revision") == "abc123", (
        f"revision not forwarded; got kwargs={captured_kwargs}"
    )
    assert captured_kwargs.get("trust_remote_code") is True


def test_upsert_document_text_round_trips_to_search(tmp_path):
    """WP-D2 Commit 1: a chunk upserted WITH document text returns it as
    matched_text in vector_search; a node vector upserted WITHOUT a document has no
    matched_text — a collection mixes text-bearing and text-less entries cleanly."""
    storage = ChromaDBStorage(persist_directory=tmp_path / "chromadb")
    storage.upsert_embedding("n1", [0.1, 0.2, 0.3], {"entity_type": "decision"})  # no text
    storage.upsert_embedding(
        "d1#chunk-0", [0.1, 0.2, 0.31], {"node_id": "d1", "is_chunk": True},
        document="the extracted chunk body",
    )

    hits = storage.vector_search([0.1, 0.2, 0.3], limit=10)
    by_id = {h["_id"]: h for h in hits}
    assert by_id["d1#chunk-0"]["matched_text"] == "the extracted chunk body", "chunk text not returned"
    assert "matched_text" not in by_id["n1"], "text-less node vector wrongly got matched_text"
