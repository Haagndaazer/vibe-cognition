"""LoadedProjects registry for cross-project cognition (XP1)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ..cognition import CognitionStorage
    from ..embeddings import ChromaDBStorage

logger = logging.getLogger(__name__)

ModelGuard = Literal["match", "unknown", "no-index", "dim-mismatch", "model-mismatch"]


def compute_model_guard(
    embeddings: ChromaDBStorage | None,
    configured_model: str,
    configured_dims: int,
    label: str,
) -> tuple[ModelGuard, str | None, int | str]:
    """THE single embedding model/dimension drift check (XP1's foreign-attach
    load-time guard, reused for the HOME collection at startup — WP-2, so a
    home config change can't silently degrade search to an empty result
    indistinguishable from "no history"). Compares a Chroma collection's
    stamped (or, for pre-stamp collections, live-probed) embedding_model /
    embedding_dimensions against what THIS process is currently configured to
    run. Never raises.

    Does NOT mutate or close ``embeddings`` — that's caller-specific: the
    foreign-attach path detaches its embeddings handle on a mismatch (that
    collection is read-only and otherwise unused); the home path must NOT,
    since home's collection is the actively-written index and new writes
    still need to land in it regardless of a stale stamp.

    Returns ``(model_guard, warning_or_None, vector_count_or_"n/a")``.
    """
    if embeddings is None:
        return "no-index", f"semantic search unavailable for {label} (no vector index)", "n/a"

    meta = embeddings._collection.metadata or {}
    stored_model = meta.get("embedding_model")
    stored_dims_meta = meta.get("embedding_dimensions")

    if stored_dims_meta is not None:
        stored_dims: int | None = int(stored_dims_meta)
    else:
        # Pre-stamp collection: fall back to probing a sample vector. len()
        # works for both plain lists and numpy arrays (chromadb Rust backend
        # may return either). Exception -> None (unknown dim, skip dim check).
        try:
            probe = embeddings._collection.get(limit=1, include=["embeddings"])
            embs = probe.get("embeddings") or []
            stored_dims = int(len(embs[0])) if embs else None
        except Exception:
            stored_dims = None

    if stored_dims is not None and stored_dims != configured_dims:
        return (
            "dim-mismatch",
            f"semantic search disabled for {label}: stored dim={stored_dims}, "
            f"configured dim={configured_dims} (structural-only)",
            "n/a",
        )
    if stored_model is not None and stored_model != configured_model:
        return (
            "model-mismatch",
            f"semantic search disabled for {label}: model '{stored_model}' != "
            f"configured '{configured_model}' (structural-only)",
            "n/a",
        )
    if stored_model is not None and stored_model == configured_model:
        return "match", None, embeddings.count_documents()

    # model absent (pre-stamp collection) OR empty collection
    return (
        "unknown",
        f"semantic search for {label} is degraded-confidence: no model "
        f"provenance in collection metadata (pre-stamp)",
        embeddings.count_documents(),
    )


@dataclass
class ProjectEntry:
    path: Path
    tag: str
    storage: CognitionStorage
    embeddings: ChromaDBStorage | None
    pinned: bool
    model_guard: ModelGuard


@dataclass
class LoadedProjects:
    """Registry of loaded cognition projects (home + foreign).

    Home is always the first entry, pinned=True, keyed by its resolved path.
    Foreign projects are keyed by their resolved canonical path.
    """

    _home_path: Path
    _entries: dict[Path, ProjectEntry] = field(default_factory=dict)

    def add_home(
        self,
        path: Path,
        tag: str,
        storage: CognitionStorage,
        embeddings: ChromaDBStorage | None,
    ) -> None:
        """Register the home project (pinned, un-unloadable)."""
        self._entries[path] = ProjectEntry(
            path=path,
            tag=tag,
            storage=storage,
            embeddings=embeddings,
            pinned=True,
            model_guard="match",
        )

    def add_foreign(self, entry: ProjectEntry) -> None:
        self._entries[entry.path] = entry

    def get(self, path: Path) -> ProjectEntry | None:
        return self._entries.get(path)

    def remove(self, path: Path) -> None:
        self._entries.pop(path, None)

    def is_home(self, path: Path) -> bool:
        return path == self._home_path

    def all_entries(self) -> list[ProjectEntry]:
        return list(self._entries.values())

    def foreign_count(self) -> int:
        return sum(1 for e in self._entries.values() if not e.pinned)

    def resolve_tag(self, tag_or_path: str) -> ProjectEntry | None:
        """Find entry by tag or path string."""
        for entry in self._entries.values():
            if entry.tag == tag_or_path:
                return entry
        try:
            resolved = Path(tag_or_path).resolve()
            return self._entries.get(resolved)
        except Exception:
            return None

    def unique_tag(self, base: str) -> str:
        """Return base tag or base-2, base-3, etc. to avoid collision."""
        existing = {e.tag for e in self._entries.values()}
        if base not in existing:
            return base
        for i in range(2, 1000):
            candidate = f"{base}-{i}"
            if candidate not in existing:
                return candidate
        return f"{base}-{id(self)}"


def build_registry(
    home_path: Path,
    home_tag: str,
    home_storage: CognitionStorage,
    home_embeddings: ChromaDBStorage | None,
) -> LoadedProjects:
    """Build the registry and register the home project."""
    registry = LoadedProjects(_home_path=home_path)
    registry.add_home(
        path=home_path,
        tag=home_tag,
        storage=home_storage,
        embeddings=home_embeddings,
    )
    return registry


def resolve_project(
    lc: dict[str, Any], project: str | None = None
) -> tuple[list[ProjectEntry], dict[str, Any] | None]:
    """Resolve a project specifier to a list of entries.

    Args:
        lc: Lifespan context dict.
        project: None → [home]; "*" → all entries; tag/path str → [matching entry].

    Returns:
        (entries, None) on success, ([], error_dict) on failure.
    """
    registry: LoadedProjects = lc["loaded_projects"]
    if project is None:
        home = registry.get(registry._home_path)
        assert home is not None
        return ([home], None)
    if project == "*":
        return (registry.all_entries(), None)
    entry = registry.resolve_tag(project)
    if entry is None:
        return ([], {"error": f"no loaded project matching '{project}'"})
    return ([entry], None)


def tag_results(results: list[dict[str, Any]], tag: str) -> list[dict[str, Any]]:
    """Add 'project': tag to each result dict in-place (additive — no copy)."""
    for r in results:
        r["project"] = tag
    return results
