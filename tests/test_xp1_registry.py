"""WP-XP1 tests: model stamp (C1), close (C2), open_existing, LoadedProjects registry."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.embeddings import ChromaDBStorage
from vibe_cognition.tools.cognition_tools import (
    _list_projects_core,
    _load_project_core,
    _unload_project_core,
)
from vibe_cognition.tools.project_registry import (
    LoadedProjects,
    ModelGuard,
    ProjectEntry,
    build_registry,
)

# ── C1: model stamp ──────────────────────────────────────────────────────────


def test_c1_fresh_collection_gets_model_stamp(tmp_path):
    """C1: a freshly created collection has embedding_model and embedding_dimensions
    in its metadata.

    Fails-before: __init__ with no model params → metadata lacks both keys.
    Passes after: both keys present when params provided.
    """
    storage = ChromaDBStorage(
        persist_directory=tmp_path / "chromadb",
        embedding_model="test-model",
        embedding_dimensions=128,
    )
    meta = storage._collection.metadata or {}
    assert meta.get("embedding_model") == "test-model", (
        f"embedding_model not stamped; got metadata={meta}"
    )
    assert meta.get("embedding_dimensions") == 128, (
        f"embedding_dimensions not stamped; got metadata={meta}"
    )


def test_c1_existing_collection_hnsw_space_survives(tmp_path):
    """C1: re-opening an existing collection with stamp params must NOT drop hnsw:space.

    chromadb 1.5.5 silently ignores new metadata on get_or_create for existing
    collections. collection.modify would drop hnsw:space (raises if present).
    The safe path is: stamp only at NEW collection creation, leave existing alone.

    Fails-before: if code called collection.modify → ValueError (hnsw:space present).
    Passes after: second open leaves hnsw:space="cosine" intact, no exception raised.
    """
    # First open: creates collection with hnsw:space
    ChromaDBStorage(persist_directory=tmp_path / "chromadb")

    # Second open with stamp params against existing collection: must not raise
    storage2 = ChromaDBStorage(
        persist_directory=tmp_path / "chromadb",
        embedding_model="test-model",
        embedding_dimensions=128,
    )
    meta = storage2._collection.metadata or {}
    assert meta.get("hnsw:space") == "cosine", (
        f"hnsw:space dropped on re-open; got metadata={meta}"
    )


# ── C2: close ────────────────────────────────────────────────────────────────


def test_c2_close_calls_client_close(tmp_path):
    """C2: storage.close() must delegate to self._client.close().

    Fails-before: close() was `pass` → _client.close never called.
    Passes after: _client.close called exactly once.
    """
    storage = ChromaDBStorage(persist_directory=tmp_path / "chromadb")
    storage._client.close = MagicMock()  # type: ignore[attr-defined]
    storage.close()
    storage._client.close.assert_called_once()  # type: ignore[attr-defined]


def test_c2_close_integration_reopen(tmp_path):
    """C2 integration: upsert → close → open_existing → data readable.

    Verifies that close() properly releases the handle so open_existing can
    attach (critical on Windows, where an open handle blocks file access).
    """
    chroma_dir = tmp_path / "chromadb"
    storage = ChromaDBStorage(persist_directory=chroma_dir)
    storage.upsert_embedding("n1", [0.1, 0.2, 0.3], {"entity_type": "decision"})
    storage.close()

    reopened = ChromaDBStorage.open_existing(chroma_dir)
    assert reopened is not None, "open_existing returned None after close"
    assert reopened.count_documents() == 1, (
        f"expected 1 document after reopen, got {reopened.count_documents()}"
    )
    reopened.close()


# ── open_existing read-only invariants ───────────────────────────────────────


def test_open_existing_absent_dir_returns_none(tmp_path):
    """open_existing on a non-existent directory must return None (no index case).

    This is the foundation of structural-only degrade: B has no vector index
    → open_existing returns None → model_guard="no-index".
    """
    absent = tmp_path / "no-such-chromadb"
    result = ChromaDBStorage.open_existing(absent)
    assert result is None


def test_open_existing_absent_dir_does_not_create_it(tmp_path):
    """open_existing must NOT create the directory or chroma.sqlite3.

    The read-only invariant: loading a foreign project with no vector index
    must leave B's directory completely untouched.

    Fails-before: if open_existing called __init__ → chroma.sqlite3 would appear.
    Passes after: directory still absent after the call.
    """
    absent = tmp_path / "B" / ".cognition" / "chromadb"
    ChromaDBStorage.open_existing(absent)
    assert not absent.exists(), (
        f"open_existing created the directory at {absent}"
    )


def test_open_existing_absent_collection_returns_none(tmp_path):
    """open_existing returns None when the directory exists but the collection doesn't.

    Simulates B having a chroma dir from a DIFFERENT collection name.
    """
    chroma_dir = tmp_path / "chromadb"
    # Create a collection under a different name
    from chromadb import PersistentClient
    from chromadb.config import Settings as ChromaSettings
    client = PersistentClient(path=str(chroma_dir), settings=ChromaSettings(anonymized_telemetry=False))
    client.get_or_create_collection("some_other_collection")
    client.close()  # type: ignore[attr-defined]

    result = ChromaDBStorage.open_existing(chroma_dir, collection_name="cognition_embeddings")
    assert result is None


def test_open_existing_present_collection_returns_storage(tmp_path):
    """open_existing returns a usable ChromaDBStorage when the collection exists."""
    chroma_dir = tmp_path / "chromadb"
    # Write-path: create via __init__
    s = ChromaDBStorage(persist_directory=chroma_dir)
    s.upsert_embedding("n1", [0.1, 0.2, 0.3], {"entity_type": "decision"})
    s.close()

    result = ChromaDBStorage.open_existing(chroma_dir)
    assert result is not None
    assert result.count_documents() == 1
    result.close()


# ── LoadedProjects registry ───────────────────────────────────────────────────


def _make_entry(path, tag, pinned=False, model_guard: ModelGuard = "match"):
    """Build a minimal ProjectEntry without real storage (registry-only tests)."""
    return ProjectEntry(
        path=path,
        tag=tag,
        storage=MagicMock(),
        embeddings=None,
        pinned=pinned,
        model_guard=model_guard,
    )


def test_home_pin_is_set_and_remove_is_refused(tmp_path):
    """Home project is always pinned; registry.is_home returns True for it.

    Indirectly verifies the tool's home-pin guard (which checks entry.pinned).
    """
    home_path = tmp_path / "home"
    registry = LoadedProjects(_home_path=home_path)
    registry.add_home(
        path=home_path,
        tag="myproject",
        storage=MagicMock(),
        embeddings=None,
    )
    assert registry.is_home(home_path)
    entry = registry.get(home_path)
    assert entry is not None and entry.pinned, "home entry must be pinned"
    assert registry.foreign_count() == 0


def test_unique_tag_collision_suffix(tmp_path):
    """unique_tag appends -2, -3, ... to avoid collisions.

    Fails-before: unique_tag not implemented → AttributeError or wrong tag returned.
    Passes after: base, base-2, base-3 assigned for three projects with the same name.
    """
    home_path = tmp_path / "home"
    registry = LoadedProjects(_home_path=home_path)
    registry.add_home(path=home_path, tag="myproject", storage=MagicMock(), embeddings=None)

    t1 = registry.unique_tag("foreign")
    assert t1 == "foreign"
    registry.add_foreign(_make_entry(tmp_path / "A", t1))

    t2 = registry.unique_tag("foreign")
    assert t2 == "foreign-2"
    registry.add_foreign(_make_entry(tmp_path / "B", t2))

    t3 = registry.unique_tag("foreign")
    assert t3 == "foreign-3"


def test_resolve_tag_by_tag_and_by_path(tmp_path):
    """resolve_tag finds entries by tag string and by path string."""
    home_path = tmp_path / "home"
    registry = LoadedProjects(_home_path=home_path)
    registry.add_home(path=home_path, tag="myproject", storage=MagicMock(), embeddings=None)

    foreign_path = tmp_path / "foreign"
    registry.add_foreign(_make_entry(foreign_path, "foreign"))

    assert registry.resolve_tag("foreign") is not None
    assert registry.resolve_tag(str(foreign_path)) is not None
    assert registry.resolve_tag("nonexistent") is None


def test_remove_foreign_decrements_count(tmp_path):
    """remove() decrements foreign_count correctly."""
    home_path = tmp_path / "home"
    foreign_path = tmp_path / "B"
    registry = LoadedProjects(_home_path=home_path)
    registry.add_home(path=home_path, tag="myproject", storage=MagicMock(), embeddings=None)
    registry.add_foreign(_make_entry(foreign_path, "B"))

    assert registry.foreign_count() == 1
    registry.remove(foreign_path)
    assert registry.foreign_count() == 0


# ── Write-isolation: CognitionStorage init does not write journal ──────────────


def test_foreign_cognition_storage_init_does_not_write_journal(tmp_path):
    """Loading B's CognitionStorage must not write to B's journal.

    The write-isolation invariant: attaching a foreign project may read the
    journal (for get_statistics) but must never append to it.

    Fails-before: if CognitionStorage.__init__ wrote a header or index line.
    Passes after: journal mtime_ns unchanged after CognitionStorage(b_dir).
    """
    b_cognition = tmp_path / "B" / ".cognition"
    b_cognition.mkdir(parents=True)
    journal = b_cognition / "journal.jsonl"
    journal.write_text('{"id": "n1", "type": "decision", "summary": "x"}\n', encoding="utf-8")

    before_mtime = journal.stat().st_mtime_ns
    CognitionStorage(b_cognition)
    after_mtime = journal.stat().st_mtime_ns

    assert before_mtime == after_mtime, (
        f"CognitionStorage.__init__ wrote to B's journal "
        f"(mtime changed: {before_mtime} → {after_mtime})"
    )


# ── Acceptance-criteria proofs via _load_project_core / _unload_project_core / _list_projects_core ──


def _make_lc(tmp_path, home_tag="myproject", embedding_model="test-model", embedding_dimensions=3):
    """Build a minimal lifespan context for the XP1 core functions."""
    import threading
    home_path = tmp_path / "home"
    home_path.mkdir(parents=True, exist_ok=True)
    home_cognition = CognitionStorage(home_path / ".cognition")
    home_chroma = ChromaDBStorage(
        persist_directory=home_path / ".cognition" / "chromadb",
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
    )
    config = SimpleNamespace(
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
        repo_path=home_path,
    )
    registry = build_registry(
        home_path=home_path,
        home_tag=home_tag,
        home_storage=home_cognition,
        home_embeddings=home_chroma,
    )
    return {
        "config": config,
        "cognition_storage": home_cognition,
        "cognition_embedding_storage": home_chroma,
        "loaded_projects": registry,
        "embedding_generator": None,
        "embedding_ready": threading.Event(),
        "embedding_error": None,
    }


def _make_foreign(tmp_path, name="B", with_journal=True):
    """Create a minimal foreign project directory."""
    b_path = tmp_path / name
    cognition_dir = b_path / ".cognition"
    cognition_dir.mkdir(parents=True)
    if with_journal:
        (cognition_dir / "journal.jsonl").write_text(
            '{"id": "n1", "type": "decision", "summary": "s"}\n', encoding="utf-8"
        )
    return b_path


# ── Home-pin guard with path variants ────────────────────────────────────────


def test_load_project_refuses_home_by_exact_path(tmp_path):
    """cognition_load_project refuses to load the home project by exact path.

    The home-pin guard runs AFTER resolve(), so trailing slashes, . components,
    and mixed separators all normalise to the same canonical path.

    Fails-before: no home-pin check → home registered twice as foreign.
    Passes after: {"error": "already loaded as home project"}.
    """
    lc = _make_lc(tmp_path)
    home_path = lc["config"].repo_path

    # Exact path
    res = _load_project_core(lc, str(home_path))
    assert "error" in res and "home" in res["error"], f"exact path not refused: {res}"

    # Trailing slash variant
    res2 = _load_project_core(lc, str(home_path) + "/")
    assert "error" in res2 and "home" in res2["error"], f"trailing-slash path not refused: {res2}"

    # Dot-join variant (path / "." resolves to same dir)
    res3 = _load_project_core(lc, str(home_path / "."))
    assert "error" in res3 and "home" in res3["error"], f"dot-join path not refused: {res3}"


def test_load_project_reload_same_path_error_carries_existing_tag(tmp_path):
    """WP-8 (c0e6afeddaf9 audit sweep): re-loading an already-loaded foreign
    project's error dict ALSO carries "tag" (the EXISTING project's tag) —
    documented now in cognition_load_project's docstring, since a caller
    checking truthy "tag" instead of "error" first would misread this as a
    successful (re-)load rather than a no-op collision.

    Fails-before: no test previously pinned this second key existing at all.
    """
    lc = _make_lc(tmp_path)
    b_path = _make_foreign(tmp_path, "B", with_journal=True)

    first = _load_project_core(lc, str(b_path))
    assert "error" not in first
    tag = first["tag"]

    second = _load_project_core(lc, str(b_path))
    assert "error" in second
    assert second["tag"] == tag


def test_unload_project_refuses_home_by_tag(tmp_path):
    """cognition_unload_project refuses to unload home even by tag."""
    lc = _make_lc(tmp_path)
    res = _unload_project_core(lc, "myproject")
    assert "error" in res and "home project" in res["error"], (
        f"home unload by tag not refused: {res}"
    )


def test_unload_project_refuses_home_by_path(tmp_path):
    """cognition_unload_project refuses to unload home by path string."""
    lc = _make_lc(tmp_path)
    home_path = lc["config"].repo_path
    res = _unload_project_core(lc, str(home_path))
    assert "error" in res and "home project" in res["error"], (
        f"home unload by path not refused: {res}"
    )


# ── Structural-only degrade end-to-end ───────────────────────────────────────


def test_structural_only_degrade_end_to_end(tmp_path):
    """End-to-end: load a B that has journal.jsonl but NO chromadb dir.

    Expected behaviour:
    - Returns model_guard="no-index"
    - Does NOT create B's chromadb directory
    - list_projects shows B with model_guard="no-index" and vector_count="n/a"
    - unload_project succeeds (null-guard: embeddings=None should not crash)

    Fails-before: if _load_project_core called __init__ instead of open_existing
    → chroma.sqlite3 would be created at B's chromadb dir.
    """
    lc = _make_lc(tmp_path)
    b_path = _make_foreign(tmp_path, "B", with_journal=True)
    b_chroma_dir = b_path / ".cognition" / "chromadb"

    # Load
    res = _load_project_core(lc, str(b_path))
    assert res.get("model_guard") == "no-index", f"expected no-index, got: {res}"
    assert "error" not in res, f"unexpected error on load: {res}"
    assert not b_chroma_dir.exists(), (
        f"load created B's chroma dir at {b_chroma_dir} (read-only violation)"
    )

    # list_projects shows B
    listing = _list_projects_core(lc)
    tags = [p["tag"] for p in listing["projects"]]
    assert "B" in tags, f"B not in listing: {listing}"
    b_entry = next(p for p in listing["projects"] if p["tag"] == "B")
    assert b_entry["model_guard"] == "no-index"
    assert b_entry["vector_count"] == "n/a"

    # unload succeeds without crashing on null embeddings
    res2 = _unload_project_core(lc, "B")
    assert "unloaded" in res2, f"unload failed: {res2}"
    assert _list_projects_core(lc)["foreign_count"] == 0


# ── Write-isolation: tool-level ───────────────────────────────────────────────


def test_load_project_does_not_write_b_journal(tmp_path):
    """Full _load_project_core on a no-index B must not change B's journal mtime.

    Fails-before: if core wrote a registration record, sync header, or anything
    to B's journal.
    """
    lc = _make_lc(tmp_path)
    b_path = _make_foreign(tmp_path, "B", with_journal=True)
    journal = b_path / ".cognition" / "journal.jsonl"

    before = journal.stat().st_mtime_ns
    _load_project_core(lc, str(b_path))
    after = journal.stat().st_mtime_ns

    assert before == after, (
        f"_load_project_core modified B's journal (mtime changed: {before} → {after})"
    )


# ── Dim-mismatch and model-mismatch branches ─────────────────────────────────


def test_load_project_dim_mismatch_structural_only(tmp_path):
    """A B whose stored dim differs from home dim → model_guard="dim-mismatch",
    structural attach still succeeds (registered in registry), embeddings=None.

    Fails-before: guard missing → model mismatch silently attaches wrong-dim collection.
    """
    lc = _make_lc(tmp_path, embedding_model="model-a", embedding_dimensions=3)
    b_path = _make_foreign(tmp_path, "B", with_journal=True)

    # Create B's chroma with DIM=6 (different from home's 3)
    b_chroma = ChromaDBStorage(
        persist_directory=b_path / ".cognition" / "chromadb",
        embedding_model="model-a",
        embedding_dimensions=6,
    )
    b_chroma.upsert_embedding("n1", [0.1, 0.2, 0.3, 0.4, 0.5, 0.6], {"entity_type": "decision"})
    b_chroma.close()

    res = _load_project_core(lc, str(b_path))
    assert res.get("model_guard") == "dim-mismatch", f"expected dim-mismatch, got: {res}"
    assert "warning" in res
    assert res.get("vector_count") == "n/a"

    # Registry holds the entry but with embeddings=None (structural-only)
    registry = lc["loaded_projects"]
    entry = registry.resolve_tag("B")
    assert entry is not None
    assert entry.embeddings is None
    assert entry.model_guard == "dim-mismatch"


def test_load_project_model_mismatch_structural_only(tmp_path):
    """A B whose stored model differs from home model → model_guard="model-mismatch"."""
    lc = _make_lc(tmp_path, embedding_model="model-a", embedding_dimensions=3)
    b_path = _make_foreign(tmp_path, "B", with_journal=True)

    # Create B's chroma with a DIFFERENT model stamp, same dim
    b_chroma = ChromaDBStorage(
        persist_directory=b_path / ".cognition" / "chromadb",
        embedding_model="model-b",  # different
        embedding_dimensions=3,
    )
    b_chroma.upsert_embedding("n1", [0.1, 0.2, 0.3], {"entity_type": "decision"})
    b_chroma.close()

    res = _load_project_core(lc, str(b_path))
    assert res.get("model_guard") == "model-mismatch", f"expected model-mismatch, got: {res}"
    assert "warning" in res
    assert res.get("vector_count") == "n/a"

    entry = lc["loaded_projects"].resolve_tag("B")
    assert entry is not None and entry.embeddings is None


def test_load_project_match_guard(tmp_path):
    """A B whose stored model + dim match home → model_guard="match", semantic enabled."""
    lc = _make_lc(tmp_path, embedding_model="model-a", embedding_dimensions=3)
    b_path = _make_foreign(tmp_path, "B", with_journal=True)

    # Create B's chroma matching home stamp
    b_chroma = ChromaDBStorage(
        persist_directory=b_path / ".cognition" / "chromadb",
        embedding_model="model-a",
        embedding_dimensions=3,
    )
    b_chroma.upsert_embedding("n1", [0.1, 0.2, 0.3], {"entity_type": "decision"})
    b_chroma.close()

    res = _load_project_core(lc, str(b_path))
    assert res.get("model_guard") == "match", f"expected match, got: {res}"
    assert "warning" not in res
    assert res.get("vector_count") == 1

    entry = lc["loaded_projects"].resolve_tag("B")
    assert entry is not None and entry.embeddings is not None
    entry.embeddings.close()
