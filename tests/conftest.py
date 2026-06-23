"""Shared pytest fixtures for the vibe-cognition test suite.

Provides the three infrastructure fixtures used across the wrapper and support-module
tests (T-1a/b spec). The split between build_lc and make_ctx is intentional (B1):
build_lc owns the storage/threading state; make_ctx owns only the Context shim.
"""

import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.embeddings import ChromaDBStorage, EmbeddingGenerator
from vibe_cognition.tools.project_registry import build_registry

# ── _TextKeyedGen ─────────────────────────────────────────────────────────────
#
# Orthogonal-unit-vector embedder keyed on marker words — promoted from
# test_wp_cap.py:117 (decision: constant-vector fake is tautological because it
# can't distinguish a re-embed from a stale vector, discovery 986687c1ed27).

class _TextKeyedGen:
    """Text-KEYED fake embedder: distinct marker words → distinct orthogonal unit
    vectors, so a re-embed genuinely moves the stored vector. Never loads a model."""

    _MARKERS = {
        "alpha": [1.0, 0.0, 0.0],
        "beta": [0.0, 1.0, 0.0],
        "gamma": [0.0, 0.0, 1.0],
    }

    def generate(self, text: str, input_type: str = "document") -> list[float]:
        low = text.lower()
        for marker, vec in self._MARKERS.items():
            if marker in low:
                return list(vec)
        return [0.0, 0.0, 1.0]

    def generate_query_embedding(self, text: str) -> list[float]:
        return self.generate(text, input_type="query")


# ── _MockMcp ──────────────────────────────────────────────────────────────────
#
# Promoted from test_xp2_routing.py:297. Captures registered closures by name
# via the @mcp.tool() decorator shim without depending on FastMCP internals.

class _MockMcp:
    """Minimal MCP stub: .tool() captures registered closures into self.tools."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):  # type: ignore[override]
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_generator() -> EmbeddingGenerator:
    """A text-keyed fake embedder (3-D, no model load)."""
    return cast(EmbeddingGenerator, _TextKeyedGen())


@pytest.fixture
def build_lc(fake_generator: EmbeddingGenerator):  # type: ignore[type-arg]
    """Factory fixture: call with (tmp_path, embeddings_ready=False) → lc dict.

    Mirrors _make_lc in test_xp2_routing.py:33 plus the embedding_ready knob
    (B4): an unset threading.Event makes require_embeddings return an error dict,
    so happy-path embedding assertions on an unset event are vacuous. Pass
    embeddings_ready=True for the ready/happy path; leave False to test the
    not-ready error path.
    """
    def _factory(tmp_path, *, embeddings_ready: bool = False) -> dict[str, Any]:
        home_path = tmp_path / "home"
        home_path.mkdir(parents=True, exist_ok=True)
        cognition_storage = CognitionStorage(home_path / ".cognition")
        chroma = ChromaDBStorage(
            persist_directory=home_path / ".cognition" / "chromadb",
            embedding_model="m",
            embedding_dimensions=3,
        )
        config = SimpleNamespace(
            embedding_model="m",
            embedding_dimensions=3,
            repo_path=home_path,
            effective_repo_name="home",
        )
        registry = build_registry(
            home_path=home_path,
            home_tag="home",
            home_storage=cognition_storage,
            home_embeddings=chroma,
        )
        event = threading.Event()
        if embeddings_ready:
            event.set()
        return {
            "config": config,
            "cognition_storage": cognition_storage,
            "cognition_embedding_storage": chroma,
            "loaded_projects": registry,
            "embedding_generator": fake_generator,
            "embedding_ready": event,
            "embedding_error": None,
        }

    return _factory


@pytest.fixture
def make_ctx():
    """Factory fixture: call with an lc dict → fake FastMCP Context.

    Mirrors _make_ctx in test_xp2_routing.py:367. Kept separate from build_lc
    (B1: do NOT conflate lifespan-dict construction with Context wrapping).
    """
    from fastmcp import Context

    def _factory(lc: dict[str, Any]) -> Context:
        return cast(
            Context,
            SimpleNamespace(request_context=SimpleNamespace(lifespan_context=lc)),
        )

    return _factory


@pytest.fixture
def mock_mcp() -> _MockMcp:
    """Fresh _MockMcp collector per test."""
    return _MockMcp()
