"""Embedding generator with pluggable backends for local embedding generation."""

import logging
from typing import TYPE_CHECKING

from ._backend import EmbeddingBackend
from .sidecar_client import SidecarBackend, get_or_create_standalone_supervisor

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)

# WP-13 (9cb745be2570): nomic-family task-specific prefixes. Module-level (not a
# backend-class attribute) because they are NOMIC-specific, not sentence-
# transformers-specific -- OllamaBackend needs the SAME two strings when its
# configured model is also nomic (the default for both backends), not a
# second, independently-drifting copy (embeddings/sidecar.py's
# SentenceTransformersBackend imports these from here too). Neither backend
# actually gates on model name today (both apply these unconditionally to
# whatever model is configured) -- a pre-existing simplification this mirrors
# exactly, not a new rule invented here.
NOMIC_DOCUMENT_PREFIX = "search_document: "
NOMIC_QUERY_PREFIX = "search_query: "


class OllamaBackend(EmbeddingBackend):
    """Embedding backend using Ollama server."""

    def __init__(self, model: str, base_url: str):
        """Initialize the Ollama backend.

        Args:
            model: Name of the Ollama model to use
            base_url: Ollama server base URL
        """
        import ollama

        self._model = model
        self._client = ollama.Client(host=base_url)
        logger.info(f"Ollama client initialized with model: {model} at {base_url}")

    def encode(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        """Encode texts into embeddings.

        Args:
            texts: List of texts to encode
            is_query: Whether the texts are queries (vs documents) — applies the
                same nomic search_query:/search_document: prefix SentenceTransformersBackend
                does (WP-13, 9cb745be2570). Previously accepted but ignored, silently
                degrading retrieval quality for the default nomic-embed-text Ollama model
                the SAME way an un-prefixed ST call would.

        Returns:
            List of embedding vectors
        """
        if not texts:
            return []

        prefix = NOMIC_QUERY_PREFIX if is_query else NOMIC_DOCUMENT_PREFIX
        embeddings = []
        for text in texts:
            response = self._client.embeddings(model=self._model, prompt=prefix + text)
            embeddings.append(response["embedding"])

        return embeddings


class EmbeddingGenerator:
    """Generate embeddings using pluggable backends."""

    # Batch limits
    MAX_BATCH_SIZE = 128

    def __init__(self, backend: EmbeddingBackend):
        """Initialize the embedding generator.

        Args:
            backend: Embedding backend to use
        """
        self._backend = backend

    @classmethod
    def from_config(cls, config: "Settings") -> "EmbeddingGenerator":
        """Create an EmbeddingGenerator from configuration.

        Args:
            config: Application settings

        Returns:
            Configured EmbeddingGenerator instance
        """
        if config.embedding_backend == "ollama":
            backend = OllamaBackend(
                model=config.ollama_model,
                base_url=config.ollama_base_url,
            )
        else:
            # WP-Sidecar: the sentence-transformers backend is now a thin
            # proxy over the sidecar client -- the actual model/torch import
            # lives entirely in the sidecar subprocess (embeddings/sidecar.py),
            # never here. Blocks (bounded by the in-budget retry window),
            # same shape as the old direct SentenceTransformersBackend()
            # construction this replaces. Uses a STANDALONE supervisor (no
            # request-scoped context to attach to here) -- the real MCP
            # server never reaches this branch; lifespan()/_load_embeddings_
            # and_sync drive a context-attached supervisor directly instead,
            # so its lazy-recovery updates the real, live server state.
            supervisor = get_or_create_standalone_supervisor(config)
            supervisor.ensure_ready()
            backend = SidecarBackend(supervisor)

        return cls(backend)

    def generate(self, text: str, input_type: str = "document") -> list[float]:
        """Generate embedding for a single text.

        Args:
            text: Text to embed
            input_type: Type of input ("document" or "query")

        Returns:
            Embedding vector as list of floats
        """
        is_query = input_type == "query"
        result = self._backend.encode([text], is_query=is_query)
        return result[0] if result else []

    def generate_query_embedding(self, query: str) -> list[float]:
        """Generate embedding for a search query.

        Args:
            query: Search query text

        Returns:
            Embedding vector
        """
        return self.generate(query, input_type="query")
