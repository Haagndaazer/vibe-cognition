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
