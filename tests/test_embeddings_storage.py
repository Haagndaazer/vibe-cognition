"""Tests for ChromaDBStorage construction (audit E-1: telemetry off)."""

from vibe_cognition.embeddings import ChromaDBStorage


def test_chromadb_telemetry_disabled(tmp_path):
    """The persistent client must be constructed with telemetry disabled.

    ChromaDB's anonymized PostHog telemetry is enabled by default and would
    phone home from every user's project on each server start. Regression test
    for audit finding E-1: fails before the fix (default True), passes after.
    """
    storage = ChromaDBStorage(persist_directory=tmp_path / "chromadb")

    assert storage._client.get_settings().anonymized_telemetry is False
