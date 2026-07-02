"""WP-11 (38a5914e6dc6): lifespan startup failure diagnosability.

CognitionStorage/ChromaDBStorage init failures (corrupted journal, .cognition/
permission errors) must log the failing component + path at ERROR, then
re-raise -- FastMCP still fails the server (that's correct); the point is
being able to find WHY, matching Settings()'s existing log-and-reraise floor.
"""

import logging

import pytest

from vibe_cognition.server import lifespan


@pytest.mark.asyncio
async def test_cognition_storage_init_failure_logs_component_and_reraises(
    tmp_path, monkeypatch, caplog
):
    """Fails-before: CognitionStorage(...) was unguarded -- the exception still
    propagated (Python does that regardless), but with no diagnosable log line
    naming which component failed or at what path."""
    monkeypatch.setenv("REPO_PATH", str(tmp_path))

    def _boom(cognition_dir):
        raise RuntimeError("simulated corrupted journal")

    monkeypatch.setattr("vibe_cognition.server.CognitionStorage", _boom)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="simulated corrupted journal"):
        async with lifespan(None):  # type: ignore[arg-type]
            pass

    assert any(
        "cognition graph" in r.message and "simulated corrupted journal" in r.message
        for r in caplog.records
    ), f"no diagnosable ERROR log found: {[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_chromadb_init_failure_logs_component_and_reraises(tmp_path, monkeypatch, caplog):
    """Same guard on the second construction site (ChromaDBStorage)."""
    monkeypatch.setenv("REPO_PATH", str(tmp_path))

    def _boom(**kwargs):
        raise RuntimeError("simulated corrupted sqlite")

    monkeypatch.setattr("vibe_cognition.server.ChromaDBStorage", _boom)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="simulated corrupted sqlite"):
        async with lifespan(None):  # type: ignore[arg-type]
            pass

    assert any(
        "ChromaDB" in r.message and "simulated corrupted sqlite" in r.message
        for r in caplog.records
    ), f"no diagnosable ERROR log found: {[r.message for r in caplog.records]}"
