"""Embeddings module for vector storage and semantic search.

WP-Sidecar: SentenceTransformersBackend is deliberately NOT re-exported here
-- it now lives in sidecar.py, the ONE module that may import the heavy
torch/sentence_transformers chain, reachable only via
`python -m vibe_cognition.embeddings.sidecar`. This package's __init__ is
imported by the main server process (server.py's `from .embeddings import
...`), so anything it imports here becomes part of the server process's own
import graph -- re-exporting the sidecar module's class would defeat the
whole point.
"""

from ._backend import EmbeddingBackend
from .generator import EmbeddingGenerator, OllamaBackend
from .storage import ChromaDBStorage, adaptive_vector_search

__all__ = [
    "ChromaDBStorage",
    "EmbeddingBackend",
    "EmbeddingGenerator",
    "OllamaBackend",
    "adaptive_vector_search",
]
