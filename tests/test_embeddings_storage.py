"""Tests for ChromaDBStorage construction (audit E-1: telemetry off)."""

from vibe_cognition.embeddings import ChromaDBStorage


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
