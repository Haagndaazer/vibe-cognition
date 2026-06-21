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


@dataclass
class ProjectEntry:
    path: Path
    tag: str
    storage: "CognitionStorage"
    embeddings: "ChromaDBStorage | None"
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
        storage: "CognitionStorage",
        embeddings: "ChromaDBStorage | None",
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
    home_storage: "CognitionStorage",
    home_embeddings: "ChromaDBStorage | None",
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


def resolve_project(lc: dict[str, Any]) -> ProjectEntry:
    """Return the home project entry (XP2 extends this to accept a project arg)."""
    registry: LoadedProjects = lc["loaded_projects"]
    return registry.get(registry._home_path)  # type: ignore[return-value]
