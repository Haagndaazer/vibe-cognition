"""Embeddings module for vector storage and semantic search."""

from .generator import (
    EmbeddingBackend,
    EmbeddingGenerator,
    OllamaBackend,
    SentenceTransformersBackend,
)
from .storage import ChromaDBStorage, adaptive_vector_search

__all__ = [
    "ChromaDBStorage",
    "EmbeddingBackend",
    "EmbeddingGenerator",
    "OllamaBackend",
    "SentenceTransformersBackend",
    "adaptive_vector_search",
]
