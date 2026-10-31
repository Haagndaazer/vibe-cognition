"""WP-11 (38a5914e6dc6): lifespan startup failure diagnosability.

CognitionStorage/VectorStore init failures (corrupted journal, .cognition/
permission errors) must log the failing component + path at ERROR, then
re-raise -- FastMCP still fails the server (that's correct); the point is
being able to find WHY, matching Settings()'s existing log-and-reraise floor.
"""

import logging
import threading

import pytest

from vibe_cognition.embeddings import VectorStore
from vibe_cognition.server import _load_embeddings_and_sync, lifespan


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
async def test_damaged_vector_store_never_fails_startup(tmp_path, monkeypatch):
    """A store that is not a database at all (the corruption class that used to
    crash chromadb natively) must not stop the lifespan: it yields, the damaged
    generation is marked and a fresh one opened, and the next session start gets
    a one-time notice. Fails against the chromadb build, whose open raised."""
    from vibe_cognition.cognition.loss_notices import VECTOR_STORE_REBUILT_KIND, read_notices
    from vibe_cognition.cognition.storage import REHYDRATE_FLAG_FILENAME
    from vibe_cognition.config import Settings

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")
    store_dir = Settings().cognition_vectors_path
    store_dir.mkdir(parents=True)
    (store_dir / "vectors-1.sqlite3").write_bytes(b"this is not a sqlite database" * 64)

    async with lifespan(None) as context:  # type: ignore[arg-type]
        store = context["cognition_embedding_storage"]
        assert store.available, store.status()
        assert store.generation == 2
        assert (store_dir / "vectors-1.corrupt").exists()

    notices = read_notices(tmp_path / ".cognition" / "local" / REHYDRATE_FLAG_FILENAME)
    assert [n["kind"] for n in notices] == [VECTOR_STORE_REBUILT_KIND]


# ── WP-C: broken embedding backend degrades gracefully (background thread) ──


def test_broken_embedding_backend_degrades_gracefully_not_a_handshake_crash(tmp_path, monkeypatch):
    """WP-C: a broken torch/sentence_transformers import (or any embedding-
    backend construction failure) must degrade gracefully -- the background
    thread's existing except-Exception clause catches it, sets embedding_error,
    and still sets embedding_ready (so tools don't hang forever) -- never a
    handshake crash, which by construction has ALREADY yielded by the time
    this background work runs.

    Fails-before: N/A (the graceful-degradation clause predates WP-C) -- this
    PINS that lazy-importing sentence_transformers (WP-C) doesn't escape that
    existing safety net; a broken import now surfaces exactly where a broken
    model load always did, not earlier on the handshake path.
    """
    from vibe_cognition.config import Settings

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    # WP-Wedge: backend=ollama skips the §3a subprocess import probe (irrelevant
    # here -- it only guards the sentence-transformers backend's heavy import),
    # keeping this test focused and fast. EmbeddingGenerator.from_config is
    # mocked wholesale below regardless of backend, so the boom still fires.
    monkeypatch.setenv("EMBEDDING_BACKEND", "ollama")
    config = Settings()

    def _boom(*args, **kwargs):
        raise ImportError("simulated: DLL load failed (broken torch install)")

    monkeypatch.setattr("vibe_cognition.server.EmbeddingGenerator.from_config", _boom)

    context = {
        "cognition_storage": None,
        "cognition_embedding_storage": VectorStore(persist_directory=tmp_path / "chromadb"),
        "loaded_projects": None,
        "embedding_ready": threading.Event(),
        "embedding_sync_done": threading.Event(),
        "embedding_error": None,
    }

    _load_embeddings_and_sync(config, context)  # must not raise

    assert context["embedding_error"] is not None
    assert "simulated" in context["embedding_error"]
    assert context["embedding_ready"].is_set(), "tools must not hang forever on a broken backend"
    assert context["embedding_sync_done"].is_set()
