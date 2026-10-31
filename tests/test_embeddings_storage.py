"""Tests for VectorStore's basic contract and the sentence-transformers revision pin."""

from unittest.mock import patch

from vibe_cognition.embeddings import VectorStore
from vibe_cognition.embeddings.sidecar import SentenceTransformersBackend
from vibe_cognition.embeddings.vector_store import flatten_metadata


def test_delete_by_node_id_purges_chunks_and_is_noop_safe(tmp_path):
    """WP-D1b chunk purge: delete_by_node_id removes exactly the chunk vectors
    tagged with that node_id, leaves others, and is a clean no-op on an empty
    store and when nothing matches."""
    storage = VectorStore(persist_directory=tmp_path / "vectors")

    storage.delete_by_node_id("nobody")

    storage.upsert_embedding("A#chunk-0", [0.1, 0.2, 0.3], {"node_id": "A"})
    storage.upsert_embedding("A#chunk-1", [0.4, 0.5, 0.6], {"node_id": "A"})
    storage.upsert_embedding("B#chunk-0", [0.7, 0.8, 0.9], {"node_id": "B"})

    storage.delete_by_node_id("A")
    assert set(storage.all_ids()) == {"B#chunk-0"}

    storage.delete_by_node_id("A")
    assert set(storage.all_ids()) == {"B#chunk-0"}


def test_count_documents_splits_nodes_and_chunks(tmp_path):
    """WP-D2 count split (A1): the node-vs-chunk count separates via the positive
    is_chunk marker — count_documents(filter={'is_chunk': True}). Node count =
    total - chunks."""
    storage = VectorStore(persist_directory=tmp_path / "vectors")
    storage.upsert_embedding("n1", [0.1, 0.2, 0.3], {"entity_type": "decision"})
    storage.upsert_embedding("d1#chunk-0", [0.1, 0.2, 0.3],
                             {"node_id": "d1", "is_chunk": True}, document="x")
    storage.upsert_embedding("d1#chunk-1", [0.1, 0.2, 0.31],
                             {"node_id": "d1", "is_chunk": True}, document="y")

    total = storage.count_documents()
    chunks = storage.count_documents(filter={"is_chunk": True})
    assert total == 3
    assert chunks == 2
    assert total - chunks == 1


def test_flatten_metadata_updated_at_is_timezone_aware():
    """E-7a: flatten_metadata produces a timezone-aware ISO timestamp
    (datetime.now(UTC), not datetime.utcnow())."""
    flat = flatten_metadata({"x": "y"})
    assert "+00:00" in str(flat["updated_at"])


def test_revision_pin_forwarded_to_sentence_transformer():
    """E-7b: SentenceTransformersBackend passes revision= to SentenceTransformer."""
    captured_kwargs = {}

    def fake_st(model_name, **kwargs):
        captured_kwargs.update(kwargs)
        return object.__new__(type("FakeST", (), {"encode": lambda s, t, **kw: [[0.0]]}))

    with patch("sentence_transformers.SentenceTransformer", side_effect=fake_st):
        SentenceTransformersBackend("test-model", revision="abc123")

    assert captured_kwargs.get("revision") == "abc123"
    assert captured_kwargs.get("trust_remote_code") is True


def test_upsert_document_text_round_trips_to_search(tmp_path):
    """WP-D2: a chunk upserted WITH document text returns it as matched_text; a node
    vector upserted WITHOUT a document has no matched_text."""
    storage = VectorStore(persist_directory=tmp_path / "vectors")
    storage.upsert_embedding("n1", [0.1, 0.2, 0.3], {"entity_type": "decision"})
    storage.upsert_embedding(
        "d1#chunk-0", [0.1, 0.2, 0.31], {"node_id": "d1", "is_chunk": True},
        document="the extracted chunk body",
    )

    by_id = {h["_id"]: h for h in storage.vector_search([0.1, 0.2, 0.3], limit=10)}
    assert by_id["d1#chunk-0"]["matched_text"] == "the extracted chunk body"
    assert "matched_text" not in by_id["n1"]
