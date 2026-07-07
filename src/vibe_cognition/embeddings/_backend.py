"""The EmbeddingBackend ABC, in its own leaf module.

WP-Sidecar: generator.py and sidecar_client.py each need this interface but
must NOT import each other at module level (generator.py's EmbeddingGenerator.
from_config constructs a SidecarBackend; sidecar_client.SidecarBackend
implements this same ABC) -- a shared leaf module breaks that cycle instead
of relying on a lazy/sanctioned import workaround.
"""

from abc import ABC, abstractmethod


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
