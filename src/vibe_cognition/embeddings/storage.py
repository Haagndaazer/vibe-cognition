"""ChromaDB storage for vector embeddings."""

import contextlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import chromadb
from chromadb.config import Settings
from chromadb.errors import InternalError

from ..cognition.git_hygiene import _acquire_lock, _release_lock

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Bounded retry for the two synchronous ChromaDB calls in the MCP handshake's
# pre-yield critical section (server.py lifespan -> ChromaDBStorage.__init__).
# Small on purpose: this runs BLOCKING on the handshake, so worst-case added
# latency must stay well under the connect-timeout budget (~0.15s here).
_CHROMA_RETRY_ATTEMPTS = 3
_CHROMA_RETRY_BASE_DELAY = 0.05  # seconds


def _retry_chromadb_open(fn: Callable[[], _T]) -> _T:
    """Bounded retry absorbing a transient chromadb rust-backend InternalError
    (open flake e09d4f4a9a23 — more likely under concurrent opens against the
    same persist_directory, WP-A decision 9022f7de94e9).

    Retries ONLY ``chromadb.errors.InternalError``. Every other exception
    (e.g. a genuine ``NotFoundError`` when a collection doesn't exist yet)
    propagates immediately, unretried — that is expected control flow for
    the is_new-collection probe below, not a flake, and must not be delayed.
    """
    last_exc: InternalError | None = None
    for attempt in range(_CHROMA_RETRY_ATTEMPTS):
        try:
            return fn()
        except InternalError as e:
            last_exc = e
            if attempt < _CHROMA_RETRY_ATTEMPTS - 1:
                time.sleep(_CHROMA_RETRY_BASE_DELAY * (2**attempt))
    assert last_exc is not None  # loop always executes >=1 attempt
    raise last_exc


class ChromaDBStorage:
    """Storage for code embeddings using ChromaDB with local persistence."""

    def __init__(
        self,
        persist_directory: Path,
        collection_name: str = "cognition_embeddings",
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
    ):
        """Initialize ChromaDB connection.

        Args:
            persist_directory: Directory for persistent storage
            collection_name: Collection name for embeddings
            embedding_model: Model name to stamp into NEW collection metadata for the
                model-identity guard (XP1). Ignored on existing collections — chromadb
                1.5.5 silently drops new keys on get_or_create and collection.modify
                drops hnsw:space, so stamping existing collections is unsafe. Absent
                on pre-stamp collections → model_guard="unknown" (warn-and-allow).
            embedding_dimensions: Dimension count to stamp alongside embedding_model.
        """
        persist_directory.mkdir(parents=True, exist_ok=True)
        self._persist_directory = persist_directory
        # anonymized_telemetry=False: defense-in-depth against ChromaDB's
        # PostHog telemetry (audit E-1). At our pinned chromadb 1.5.5 this is
        # inert (the telemetry client is a no-op stub), but chromadb 0.5-0.6.x —
        # which our >=0.5.0 floor permits — actively phoned home gated on
        # exactly this flag, so we set it across the allowed range.
        # WP-A/WP-C cross-process gate finding: PersistentClient() itself (not
        # just get_collection/get_or_create_collection below) can raise the
        # same rust-backend InternalError under genuine concurrent-open
        # pressure -- observed as "table collections already exists" when N
        # processes race SharedSystemClient._create_system_if_not_exists
        # against a BRAND-NEW persist_directory (reproduced under full-suite
        # system load, not in isolation; see test_chromadb_cross_process.py).
        # This happens BEFORE self._client exists, so it needs its own retry
        # wrap rather than reusing the two below (which retry calls made
        # THROUGH an already-constructed client).
        self._client = _retry_chromadb_open(
            lambda: chromadb.PersistentClient(
                path=str(persist_directory),
                settings=Settings(anonymized_telemetry=False),
            )
        )
        collection_meta: dict[str, Any] = {"hnsw:space": "cosine"}
        if embedding_model is not None:
            collection_meta["embedding_model"] = embedding_model
        if embedding_dimensions is not None:
            collection_meta["embedding_dimensions"] = embedding_dimensions

        # Stamp embed_scheme=doc-prefix-v1 ONLY when we are about to CREATE the
        # collection (WP-3, b35e15766c6b): a brand-new install writes vectors
        # document-prefixed from day one, so it needs no E-3 migration. An
        # EXISTING collection's metadata is preserved as-is by
        # get_or_create_collection (chromadb silently drops new metadata keys
        # on an existing collection — see the embedding_model note above), so a
        # legacy un-stamped collection stays un-stamped here; that absence is
        # exactly the signal recreate_collection()'s one-time migration keys
        # off. Without this, EVERY fresh install used to trigger the migration
        # unnecessarily (nothing to migrate), and two same-project sessions
        # racing it in the model-load window could both drop+recreate.
        try:
            _retry_chromadb_open(lambda: self._client.get_collection(name=collection_name))
            is_new = False
        except Exception:
            is_new = True
        if is_new:
            collection_meta["embed_scheme"] = "doc-prefix-v1"

        self._collection_name = collection_name
        self._base_meta = collection_meta
        self._collection = _retry_chromadb_open(
            lambda: self._client.get_or_create_collection(
                name=collection_name,
                metadata=collection_meta,
            )
        )
        logger.info(f"ChromaDB initialized at {persist_directory}")

    def live_embed_scheme(self) -> str | None:
        """Fresh (not process-cached) read of the collection's embed_scheme.

        NOT ``self._collection.metadata`` — that Python-side handle's metadata
        is a snapshot from whenever this object last created/attached to the
        collection, and chromadb's PersistentClient does not auto-refresh it.
        If ANOTHER process recreated the collection since, this handle's cached
        metadata is stale — exactly the "process-local startup-frozen metadata
        snapshot" bug (b35e15766c6b) that let two racing startups both decide
        "needs migration" and double-delete-recreate. Re-queries the client
        instead. Never raises (absent collection -> None).
        """
        try:
            return (self._client.get_collection(name=self._collection_name).metadata or {}).get(
                "embed_scheme"
            )
        except Exception:
            return None

    def recreate_collection(self) -> None:
        """Drop and recreate the collection, stamping embed_scheme=doc-prefix-v1.

        Used by the E-3 one-time migration: deletes all stale query-prefixed
        vectors so the startup sync can rebuild them document-prefixed. After
        recreate the stamp is permanent; the server bg-thread checks
        ``live_embed_scheme()`` before calling this method so it only ever
        runs once per data directory.

        File-locked (WP-3, b35e15766c6b — mirrors git_hygiene._acquire_lock /
        _release_lock) so two same-project processes racing this in the
        model-load window can't both drop+recreate: the second would silently
        wipe the first's freshly-synced vectors. On lock contention this
        process does NOT perform its own drop+recreate (that IS the race) — it
        just re-attaches via get_or_create_collection to whatever the lock
        holder leaves behind (existing-collection metadata is untouched by
        get_or_create, so this is an attach, not a second migration). Also
        re-checks the LIVE embed_scheme after acquiring the lock, since another
        process may have completed the migration while this one waited.
        """
        lock_path = self._persist_directory / ".recreate-embed-scheme.lock"
        if not _acquire_lock(lock_path):
            logger.info(
                "recreate_collection: lock held by another process; attaching "
                "to its result instead of racing a second drop+recreate"
            )
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata=self._base_meta,
            )
            return
        try:
            if self.live_embed_scheme() == "doc-prefix-v1":
                # Another process finished the migration while we waited for
                # the lock — just attach, don't drop a freshly-migrated collection.
                self._collection = self._client.get_or_create_collection(
                    name=self._collection_name,
                    metadata=self._base_meta,
                )
                return
            with contextlib.suppress(Exception):
                self._client.delete_collection(self._collection_name)
            stamp_meta = {**self._base_meta, "embed_scheme": "doc-prefix-v1"}
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata=stamp_meta,
            )
            logger.info("Collection recreated with doc-prefix-v1 stamp")
        finally:
            _release_lock(lock_path)

    def upsert_embedding(
        self,
        entity_id: str,
        embedding: list[float],
        metadata: dict[str, Any],
        document: str | None = None,
    ) -> None:
        """Insert or update an embedding.

        Args:
            entity_id: Unique entity ID
            embedding: Embedding vector
            metadata: Entity metadata (content_hash, entity_type, file_path, etc.)
            document: Optional source text for the entry (WP-D2: a document chunk's
                text, stored as the Chroma ``documents`` field so search can return a
                matched excerpt). Omitted when None — a collection may mix text-bearing
                chunks and text-less node vectors (no all-or-none requirement).
        """
        flat_metadata = self._flatten_metadata(metadata)
        kwargs: dict[str, Any] = {
            "ids": [entity_id],
            "embeddings": [embedding],
            "metadatas": [flat_metadata],
        }
        if document is not None:
            kwargs["documents"] = [document]
        self._collection.upsert(**kwargs)

    def _flatten_metadata(self, metadata: dict[str, Any]) -> dict[str, str | int | float | bool]:
        """Flatten metadata to ChromaDB-compatible types.

        ChromaDB only supports str, int, float, bool as metadata values.

        Args:
            metadata: Original metadata dict

        Returns:
            Flattened metadata with only primitive types
        """
        flat: dict[str, str | int | float | bool] = {}
        now = datetime.now(UTC).isoformat()

        for key, value in metadata.items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                flat[key] = value
            elif isinstance(value, list):
                # Convert lists to comma-separated strings
                flat[key] = ",".join(str(v) for v in value)
            else:
                # Convert other types to string
                flat[key] = str(value)

        flat["updated_at"] = now
        return flat

    def delete_embedding(self, entity_id: str) -> bool:
        """Delete an embedding by entity ID.

        Args:
            entity_id: Entity ID to delete

        Returns:
            True if deleted (ChromaDB doesn't report actual deletion)
        """
        try:
            self._collection.delete(ids=[entity_id])
            return True
        except Exception:
            return False

    def delete_by_node_id(self, node_id: str) -> None:
        """Delete all chunk embeddings tagged with ``node_id`` metadata.

        Uses ``delete(where=...)`` directly — no-op-safe on an empty collection
        and on docs lacking a ``node_id`` field. Forward-compatible: D1b writes no
        chunks yet (chunk-embedding is D2), so this is a no-op today, present so
        document deletion inherits a clean chunk purge."""
        try:
            self._collection.delete(where={"node_id": node_id})
        except Exception as e:
            logger.warning(f"Chunk purge failed for {node_id}: {e}")

    def vector_search(
        self,
        query_embedding: list[float],
        limit: int = 10,
        entity_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Perform vector similarity search.

        Args:
            query_embedding: Query embedding vector
            limit: Maximum number of results
            entity_type: Filter by entity type

        Returns:
            List of matching documents with similarity scores
        """
        where_filter: Any = {"entity_type": entity_type} if entity_type else None

        try:
            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=limit,
                where=where_filter,
                include=["metadatas", "distances", "documents"],
            )
        except Exception as e:
            logger.error(f"Vector search failed: {e}")
            return []

        # Process results
        output: list[dict[str, Any]] = []
        ids = results["ids"][0] if results["ids"] else []
        metadatas = results["metadatas"][0] if results["metadatas"] else []
        distances = results["distances"][0] if results["distances"] else []
        # documents is None for text-less entries (node vectors); surfaced as matched_text.
        documents = results.get("documents") or []
        documents = documents[0] if documents else []

        for i, entity_id in enumerate(ids):
            metadata = metadatas[i] if i < len(metadatas) else {}
            distance = distances[i] if i < len(distances) else 1.0
            score = 1.0 - distance

            hit: dict[str, Any] = {
                "_id": entity_id,
                **metadata,
                "score": score,
            }
            matched_text = documents[i] if i < len(documents) else None
            if matched_text is not None:
                hit["matched_text"] = matched_text
            output.append(hit)

            if len(output) >= limit:
                break

        return output

    def count_documents(self, filter: dict[str, Any] | None = None) -> int:
        """Count documents in the collection.

        Args:
            filter: Optional filter criteria

        Returns:
            Document count
        """
        if filter:
            results = self._collection.get(where=filter)
            return len(results["ids"])
        return self._collection.count()

    @classmethod
    def open_existing(
        cls,
        persist_directory: Path,
        collection_name: str = "cognition_embeddings",
    ) -> "ChromaDBStorage | None":
        """Open an existing ChromaDB collection read-only; return None if absent.

        Used for foreign-project attach (XP1). Never calls get_or_create — will NOT
        create chroma.sqlite3 or the collection if B has no vector index. Returns None
        when the directory or collection is absent → caller degrades to structural-only.

        The home project always uses __init__ (which creates the collection on first
        run). This method is for foreign reads only.
        """
        if not persist_directory.exists():
            return None
        try:
            client = chromadb.PersistentClient(
                path=str(persist_directory),
                settings=Settings(anonymized_telemetry=False),
            )
            collection = client.get_collection(name=collection_name)
        except Exception:
            return None
        instance = object.__new__(cls)
        instance._client = client
        instance._collection = collection
        return instance

    def close(self) -> None:
        """Close the ChromaDB connection and release the Windows file handle."""
        self._client.close()  # type: ignore[attr-defined]


_SEARCH_OVERQUERY_K = 5
_SEARCH_OVERQUERY_CAP = 500


def adaptive_vector_search(
    embedding_storage: Any,
    query_embedding: list[float],
    *,
    entity_type: str | None = None,
    limit: int,
    dedupe: Any,
) -> list[dict[str, Any]]:
    """Widen n_results (doubling) until `limit` distinct deduped results, Chroma
    exhausted, or the cap. `dedupe(results, limit) -> list` owns N1-drop + chunk-
    dedupe per surface (MCP and dashboard use different result shapes)."""
    n = max(limit * _SEARCH_OVERQUERY_K, limit, 1)
    while True:
        results = embedding_storage.vector_search(
            query_embedding=query_embedding, limit=n, entity_type=entity_type
        )
        formatted = dedupe(results, limit)
        if len(formatted) >= limit or len(results) < n or n >= _SEARCH_OVERQUERY_CAP:
            return formatted
        n = min(n * 2, _SEARCH_OVERQUERY_CAP)
