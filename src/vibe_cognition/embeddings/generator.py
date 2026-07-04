"""Embedding generator with pluggable backends for local embedding generation."""

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)

# WP-13 (9cb745be2570): nomic-family task-specific prefixes. Module-level (not a
# SentenceTransformersBackend attribute) because they are NOMIC-specific, not
# sentence-transformers-specific -- OllamaBackend needs the SAME two strings when
# its configured model is also nomic (the default for both backends), not a
# second, independently-drifting copy. Neither backend actually gates on model
# name today (both apply these unconditionally to whatever model is configured)
# -- that's a pre-existing simplification for the ST backend this mirrors exactly,
# not a new rule invented here.
NOMIC_DOCUMENT_PREFIX = "search_document: "
NOMIC_QUERY_PREFIX = "search_query: "


class EmbeddingBackend(ABC):
    """Abstract base class for embedding backends."""

    @abstractmethod
    def encode(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        """Encode texts into embeddings.

        Args:
            texts: List of texts to encode
            is_query: Whether the texts are queries (vs documents)

        Returns:
            List of embedding vectors
        """
        pass


class SentenceTransformersBackend(EmbeddingBackend):
    """Embedding backend using sentence-transformers library."""

    DOCUMENT_PREFIX = NOMIC_DOCUMENT_PREFIX
    QUERY_PREFIX = NOMIC_QUERY_PREFIX

    def __init__(self, model_name: str, dimensions: int | None = None, revision: str | None = None):
        """Initialize the sentence-transformers backend.

        Args:
            model_name: Name of the model to use (e.g., 'nomic-ai/nomic-embed-text-v1.5')
            dimensions: Optional dimension truncation
            revision: HuggingFace Hub revision (branch, tag, or full commit SHA) to pin
                the remote code loaded via trust_remote_code=True. None = hub HEAD.

        WP-C (decision 9022f7de94e9): sentence_transformers (and the torch it pulls
        in transitively) is imported HERE, lazily, not at module top -- measured
        ~9.6-9.7s of the ~10-15s server-module-import cost that used to sit on the
        MCP handshake's pre-import critical path. This class is only constructed
        from EmbeddingGenerator.from_config(), which server.py's background thread
        (_load_embeddings_and_sync) calls AFTER the handshake yields -- so the cost
        moves off that path entirely without changing when the model actually
        loads (still backgrounded, same as before this WP).
        """
        from sentence_transformers import SentenceTransformer

        t0 = time.monotonic()
        logger.info(f"Loading model: {model_name}")
        self._model: SentenceTransformer = SentenceTransformer(
            model_name, trust_remote_code=True, revision=revision
        )
        elapsed = time.monotonic() - t0
        self._dimensions = dimensions
        self._lock = threading.Lock()
        logger.info(f"Model loaded successfully ({elapsed:.1f}s)")

    def encode(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        """Encode texts into embeddings.

        Args:
            texts: List of texts to encode
            is_query: Whether the texts are queries (vs documents)

        Returns:
            List of embedding vectors
        """
        if not texts:
            return []

        # Add task-specific prefixes for nomic models
        prefix = self.QUERY_PREFIX if is_query else self.DOCUMENT_PREFIX
        prefixed = [prefix + t for t in texts]

        with self._lock:
            embeddings = self._model.encode(prefixed, convert_to_numpy=True)

        # Truncate to specified dimensions if set
        if self._dimensions:
            embeddings = embeddings[:, : self._dimensions]

        return embeddings.tolist()


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
            backend = SentenceTransformersBackend(
                model_name=config.embedding_model,
                dimensions=config.embedding_dimensions,
                revision=config.embedding_revision,
            )

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
