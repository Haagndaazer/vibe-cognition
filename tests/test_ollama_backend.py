"""WP-13 (9cb745be2570): first OllamaBackend tests + nomic prefix parity.

OllamaBackend.encode() accepted is_query but ignored it -- SentenceTransformersBackend's
search_query:/search_document: prefixes are load-bearing for nomic retrieval quality, and
both backends default to a nomic model family. Fixed to apply the SAME prefixes.

ollama.Client(host=...) construction itself never makes a network call (verified: it just
builds an httpx client, connects lazily per-request) -- so OllamaBackend() is safe to
construct directly; only the per-call ._client.embeddings(...) is mocked, never a real
socket/daemon.
"""

from vibe_cognition.embeddings.generator import (
    NOMIC_DOCUMENT_PREFIX,
    NOMIC_QUERY_PREFIX,
    OllamaBackend,
)


class _FakeOllamaClient:
    """Records every embeddings() call's (model, prompt); returns a fixed vector."""

    def __init__(self):
        self.calls: list[dict] = []

    def embeddings(self, model, prompt):
        self.calls.append({"model": model, "prompt": prompt})
        return {"embedding": [0.1, 0.2, 0.3]}


def _backend_with_fake_client() -> tuple[OllamaBackend, _FakeOllamaClient]:
    backend = OllamaBackend(model="nomic-embed-text", base_url="http://localhost:1")
    fake = _FakeOllamaClient()
    backend._client = fake  # type: ignore[assignment]  # replace the real (lazily-connecting) client
    return backend, fake


def test_encode_document_mode_applies_document_prefix():
    """Fails-before: is_query was accepted but ignored -- prompt would have
    been the raw, un-prefixed text regardless of is_query."""
    backend, fake = _backend_with_fake_client()
    backend.encode(["hello world"], is_query=False)
    assert fake.calls[0]["prompt"] == f"{NOMIC_DOCUMENT_PREFIX}hello world"


def test_encode_query_mode_applies_query_prefix():
    backend, fake = _backend_with_fake_client()
    backend.encode(["hello world"], is_query=True)
    assert fake.calls[0]["prompt"] == f"{NOMIC_QUERY_PREFIX}hello world"


def test_encode_default_is_document_mode():
    """is_query defaults to False -- matches SentenceTransformersBackend's default."""
    backend, fake = _backend_with_fake_client()
    backend.encode(["hello world"])
    assert fake.calls[0]["prompt"] == f"{NOMIC_DOCUMENT_PREFIX}hello world"


def test_encode_multiple_texts_each_prefixed_independently():
    backend, fake = _backend_with_fake_client()
    backend.encode(["first", "second", "third"], is_query=True)
    assert [c["prompt"] for c in fake.calls] == [
        f"{NOMIC_QUERY_PREFIX}first",
        f"{NOMIC_QUERY_PREFIX}second",
        f"{NOMIC_QUERY_PREFIX}third",
    ]


def test_encode_passes_configured_model_name():
    backend, fake = _backend_with_fake_client()
    backend.encode(["x"])
    assert fake.calls[0]["model"] == "nomic-embed-text"


def test_encode_empty_texts_returns_empty_list_no_client_call():
    backend, fake = _backend_with_fake_client()
    result = backend.encode([])
    assert result == []
    assert fake.calls == []


def test_encode_returns_embeddings_in_order():
    backend, fake = _backend_with_fake_client()
    result = backend.encode(["a", "b"])
    assert result == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]


def test_prefix_constants_match_sentence_transformers_backend():
    """Same source of truth, not a second independently-drifting copy."""
    from vibe_cognition.embeddings.sidecar import SentenceTransformersBackend

    assert NOMIC_DOCUMENT_PREFIX == SentenceTransformersBackend.DOCUMENT_PREFIX
    assert NOMIC_QUERY_PREFIX == SentenceTransformersBackend.QUERY_PREFIX
