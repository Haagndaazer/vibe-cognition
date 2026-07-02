"""WP-E-3 tests: document prefix fix (C1 four-site) + marker-gated recreate (C2).

LOAD-BEARING PROOF (test_all_four_storage_paths_use_document_prefix):
  All four storage embed sites must call generate(input_type="document"), NOT
  generate_query_embedding.  Missing any one site makes this test red.

C2 tests verify the marker-gated recreate flow: no-marker → recreate → sync
rebuilds document-prefixed; marker-present → skip; idempotent; count preserved.
"""

from __future__ import annotations

import pytest

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType, generate_node_id
from vibe_cognition.embeddings import ChromaDBStorage
from vibe_cognition.server import _sync_cognition_embeddings
from vibe_cognition.tools.cognition_tools import (
    _embed_document,
    _embed_entity_node,
    _search_cognition,
)

# ── Spy generator ─────────────────────────────────────────────────────────────


class _PrefixSpy:
    """Records every embed call: (method, input_type_or_None, text)."""

    DIM = 3

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str]] = []

    def generate(self, text: str, input_type: str = "document") -> list[float]:
        self.calls.append(("generate", input_type, text))
        return [0.1] * self.DIM

    def generate_query_embedding(self, text: str) -> list[float]:
        self.calls.append(("generate_query_embedding", None, text))
        return [0.2] * self.DIM

    def generate_batch(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        for t in texts:
            self.calls.append(("generate_batch", input_type, t))
        return [[0.1] * self.DIM for _ in texts]

    def storage_calls(self) -> list[tuple[str, str | None, str]]:
        """Return only the calls that were not generate_query_embedding (i.e., correct doc-prefix calls)."""
        return [c for c in self.calls if c[0] == "generate" and c[1] == "document"]

    def query_calls(self) -> list[tuple[str, str | None, str]]:
        return [c for c in self.calls if c[0] == "generate_query_embedding"]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_node(summary: str = "summary", detail: str = "detail") -> CognitionNode:
    ts = "2026-06-21T00:00:00+00:00"
    return CognitionNode(
        id=generate_node_id(CognitionNodeType.DECISION, summary, ts),
        type=CognitionNodeType.DECISION,
        summary=summary,
        detail=detail,
        context=[],
        references=[],
        timestamp=ts,
        author="test",
    )


def _make_chroma(tmp_path, *, name: str = "col") -> ChromaDBStorage:
    return ChromaDBStorage(
        persist_directory=tmp_path / "chroma",
        collection_name=name,
        embedding_model="test-model",
        embedding_dimensions=_PrefixSpy.DIM,
    )


def _make_legacy_chroma(tmp_path, *, name: str = "col") -> ChromaDBStorage:
    """A collection that predates the embed_scheme-at-creation stamp (WP-3,
    b35e15766c6b): it EXISTS already but was never migrated. Built via a
    throwaway raw chromadb client (closed before handoff, for Windows handle
    safety) so the subsequent ChromaDBStorage(...) sees an EXISTING, unstamped
    collection and correctly does NOT add the stamp (get_or_create_collection
    preserves an existing collection's metadata as-is) -- this is exactly the
    real legacy-collection scenario the E-3 migration exists to handle. A
    plain _make_chroma(...) collection is now pre-stamped at creation and can
    no longer stand in for "needs migration."
    """
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    persist_dir = tmp_path / "chroma"
    persist_dir.mkdir(parents=True, exist_ok=True)
    raw_client = chromadb.PersistentClient(
        path=str(persist_dir), settings=ChromaSettings(anonymized_telemetry=False)
    )
    raw_client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})
    raw_client.close()  # type: ignore[attr-defined]

    return ChromaDBStorage(
        persist_directory=persist_dir,
        collection_name=name,
        embedding_model="test-model",
        embedding_dimensions=_PrefixSpy.DIM,
    )


def _make_cognition(tmp_path) -> CognitionStorage:
    return CognitionStorage(tmp_path / ".cognition")


# ── C1: four-site prefix proof ────────────────────────────────────────────────


class TestAllStoragePathsUseDocumentPrefix:
    """Load-bearing proof: all four embed-storage sites must use input_type="document".

    A missed site makes the assertion for that site fail (spy records the wrong method).
    The search path is also asserted to REMAIN query-prefixed (asymmetry is the fix).
    """

    def test_site1_embed_entity_node(self, tmp_path):
        """Site 1: _embed_entity_node (cognition_tools.py)."""
        spy = _PrefixSpy()
        chroma = _make_chroma(tmp_path)
        node = _make_node()

        _embed_entity_node(chroma, spy, node)  # type: ignore[arg-type]

        assert spy.query_calls() == [], "site 1 must NOT use generate_query_embedding"
        assert len(spy.storage_calls()) == 1, "site 1 must call generate(input_type='document')"

    def test_site2_embed_document_node_vector(self, tmp_path):
        """Site 2: _embed_document node-level vector (cognition_tools.py)."""
        spy = _PrefixSpy()
        chroma = _make_chroma(tmp_path)

        _embed_document(chroma, spy, "node1", "title", "detail", "")  # type: ignore[arg-type]

        doc_calls = spy.storage_calls()
        query_calls = spy.query_calls()
        assert query_calls == [], "site 2 node vector must NOT use generate_query_embedding"
        # With empty sidecar: exactly 1 call (node vector only, zero chunks)
        assert len(doc_calls) >= 1, "site 2 must call generate(input_type='document') for node"

    def test_site3_embed_document_chunk_loop(self, tmp_path):
        """Site 3: _embed_document chunk loop (cognition_tools.py) — SEPARATE call from site 2."""
        spy = _PrefixSpy()
        chroma = _make_chroma(tmp_path)

        _embed_document(chroma, spy, "node1", "title", "detail", "chunk text here")  # type: ignore[arg-type]

        doc_calls = spy.storage_calls()
        query_calls = spy.query_calls()
        assert query_calls == [], "site 3 chunk loop must NOT use generate_query_embedding"
        # With non-empty sidecar: >=2 calls (node vector + >=1 chunk)
        assert len(doc_calls) >= 2, "site 3: chunk loop must also call generate(input_type='document')"

    def test_site4_sync_nondoc_inline(self, tmp_path):
        """Site 4: _sync_cognition_embeddings non-doc inline (server.py line 112).

        This is the inline that bypasses _embed_entity_node — the most likely to be
        missed.  Assert it goes through generate(input_type='document'), NOT
        generate_query_embedding.
        """
        spy = _PrefixSpy()
        cognition = _make_cognition(tmp_path)
        chroma = _make_chroma(tmp_path)

        # Record a non-doc node directly into the JSONL (bypassing embed)
        node = _make_node(summary="sync-test")
        cognition.add_node(node)

        _sync_cognition_embeddings(cognition, chroma, spy)  # type: ignore[arg-type]

        # The inline at server.py:112 (non-doc path) must have called generate(document)
        query_calls = spy.query_calls()
        doc_calls = spy.storage_calls()
        assert query_calls == [], "site 4 non-doc inline must NOT use generate_query_embedding"
        assert len(doc_calls) >= 1, "site 4 must call generate(input_type='document')"

    def test_search_path_stays_query_prefixed(self, tmp_path):
        """Search must remain query-prefixed — the asymmetry IS the fix."""
        spy = _PrefixSpy()
        cognition = _make_cognition(tmp_path)
        chroma = _make_chroma(tmp_path)

        _search_cognition(cognition, chroma, spy, "my query", None, 5)  # type: ignore[arg-type]

        query_calls = spy.query_calls()
        assert len(query_calls) >= 1, "search must call generate_query_embedding (query prefix)"


# ── C2: recreate_collection ───────────────────────────────────────────────────


class TestRecreateCollection:
    def test_drops_vectors_and_stamps_marker(self, tmp_path):
        """recreate_collection() empties the collection and stamps embed_scheme."""
        spy = _PrefixSpy()
        chroma = _make_legacy_chroma(tmp_path)

        # Seed a vector with (wrong) query prefix
        chroma.upsert_embedding("n1", spy.generate_query_embedding("text"), {"entity_type": "decision"})
        assert chroma._collection.count() == 1
        assert chroma._collection.metadata.get("embed_scheme") is None

        chroma.recreate_collection()

        assert chroma._collection.count() == 0, "recreate must empty the collection"
        assert chroma._collection.metadata.get("embed_scheme") == "doc-prefix-v1"

    def test_preserves_model_stamp(self, tmp_path):
        """recreate_collection() preserves embedding_model and embedding_dimensions in metadata."""
        chroma = ChromaDBStorage(
            persist_directory=tmp_path / "chroma",
            collection_name="col",
            embedding_model="nomic-v1",
            embedding_dimensions=768,
        )
        chroma.recreate_collection()

        meta = chroma._collection.metadata
        assert meta.get("embedding_model") == "nomic-v1"
        assert meta.get("embedding_dimensions") == 768
        assert meta.get("embed_scheme") == "doc-prefix-v1"

    def test_defensive_on_absent_collection(self, tmp_path):
        """recreate_collection() must not raise even if delete fails (brand-new install path)."""
        chroma = _make_chroma(tmp_path)
        # Manually delete before calling recreate — simulates absence
        chroma._client.delete_collection(chroma._collection_name)
        chroma.recreate_collection()  # must not raise

        assert chroma._collection.metadata.get("embed_scheme") == "doc-prefix-v1"


# ── C2: marker-gated migration flow ──────────────────────────────────────────


class TestMarkerGatedMigration:
    def test_full_migration_flow_node_count_preserved(self, tmp_path):
        """Full flow: no-marker collection → recreate → sync rebuilds → marker stamped.

        Node count must be PRESERVED (no data loss — journal is source of truth).
        The query-prefixed vector is replaced by a document-prefixed one.
        """
        spy = _PrefixSpy()
        cognition = _make_cognition(tmp_path)
        chroma = _make_legacy_chroma(tmp_path)

        # Seed two non-doc nodes into cognition (bypassing embed — simulates pre-E3 state)
        n1 = _make_node(summary="alpha")
        n2 = _make_node(summary="beta")
        cognition.add_node(n1)
        cognition.add_node(n2)

        # Pre-seed chroma with query-prefixed vectors (wrong)
        chroma.upsert_embedding(n1.id, spy.generate_query_embedding("x"), {"entity_type": "decision"})
        chroma.upsert_embedding(n2.id, spy.generate_query_embedding("x"), {"entity_type": "decision"})
        assert chroma._collection.count() == 2
        assert chroma._collection.metadata.get("embed_scheme") is None

        # Simulate the server bg-thread gate (WP-3: live_embed_scheme(), not a
        # process-cached metadata snapshot — see server.py's real check)
        if chroma.live_embed_scheme() != "doc-prefix-v1":
            chroma.recreate_collection()

        # Collection emptied, marker stamped
        assert chroma._collection.count() == 0
        assert chroma._collection.metadata.get("embed_scheme") == "doc-prefix-v1"

        # Sync rebuilds — all calls must be document-prefixed now
        spy2 = _PrefixSpy()
        _sync_cognition_embeddings(cognition, chroma, spy2)  # type: ignore[arg-type]

        assert spy2.query_calls() == [], "sync after recreate must use document prefix only"
        assert chroma._collection.count() == 2, "node count must be preserved after rebuild"

    def test_marker_present_skips_recreate(self, tmp_path):
        """If embed_scheme=doc-prefix-v1 already present, recreate_collection is NOT called."""
        spy = _PrefixSpy()
        cognition = _make_cognition(tmp_path)
        chroma = _make_chroma(tmp_path)

        # Set marker and seed a node document-prefixed
        chroma.recreate_collection()
        n = _make_node(summary="gamma")
        cognition.add_node(n)
        chroma.upsert_embedding(n.id, spy.generate("text", input_type="document"), {"entity_type": "decision"})
        assert chroma._collection.count() == 1

        # Simulate bg-thread gate: marker present -> skip (live_embed_scheme(),
        # not a process-cached metadata snapshot — see server.py's real check)
        recreate_called = False
        if chroma.live_embed_scheme() != "doc-prefix-v1":
            chroma.recreate_collection()
            recreate_called = True

        assert not recreate_called, "marker present: recreate_collection must NOT be called"
        assert chroma._collection.count() == 1, "existing vectors must be untouched"

    # ── E-8 dead-method proof ─────────────────────────────────────────────────

    def test_generator_has_no_generate_batch(self):
        """E-8: generate_batch was dead (zero callers); assert it stays removed."""
        from vibe_cognition.embeddings.generator import EmbeddingGenerator

        assert not hasattr(EmbeddingGenerator, "generate_batch"), (
            "generate_batch was pruned as dead code (WP-LP-A); do not re-add without callers"
        )

    def test_idempotent_second_server_start(self, tmp_path):
        """Second server start with marker already set: collection unchanged."""
        spy = _PrefixSpy()
        cognition = _make_cognition(tmp_path)
        chroma = _make_chroma(tmp_path)

        # First start: migrate + sync
        col_meta = chroma._collection.metadata or {}
        if col_meta.get("embed_scheme") != "doc-prefix-v1":
            chroma.recreate_collection()

        n = _make_node(summary="delta")
        cognition.add_node(n)
        _sync_cognition_embeddings(cognition, chroma, spy)  # type: ignore[arg-type]
        count_after_first = chroma._collection.count()

        # Second start: marker already present, sync finds nothing missing
        spy2 = _PrefixSpy()
        col_meta2 = chroma._collection.metadata or {}
        if col_meta2.get("embed_scheme") != "doc-prefix-v1":
            chroma.recreate_collection()  # must NOT fire

        _sync_cognition_embeddings(cognition, chroma, spy2)  # type: ignore[arg-type]
        assert chroma._collection.count() == count_after_first, "second start must not change count"
        assert spy2.query_calls() == [], "second-start sync must not use query prefix"


# ── WP-3 (b35e15766c6b): recreate_collection file-lock guard ──────────────────


class TestRecreateCollectionLockGuard:
    """Two same-project processes racing recreate_collection() in the model-load
    window must not both drop+recreate — the second would silently wipe the
    first's freshly-synced vectors (the exact failure this task fixes)."""

    def test_two_instance_contention_does_not_double_wipe(self, tmp_path):
        """SAME-PROCESS two-instance lock test (per the plan pin — no real
        subprocess/thread needed to prove the guard): instance A holds the
        recreate lock (simulating it mid-migration); instance B, racing the
        SAME legacy collection, must see the lock held and NOT delete the
        collection out from under A's in-flight work.

        Fails-before: no lock existed, so B's recreate_collection() would
        unconditionally delete_collection() -> wipe whatever A had already
        written, even though A's own migration was still in progress.
        """
        legacy = _make_legacy_chroma(tmp_path)
        legacy.upsert_embedding("pre-existing", [0.1, 0.1, 0.1], {"entity_type": "decision"})
        assert legacy._collection.count() == 1
        lock_path = legacy._persist_directory / ".recreate-embed-scheme.lock"
        assert not lock_path.exists()

        # Instance B attaches to the SAME on-disk collection.
        instance_b = ChromaDBStorage(
            persist_directory=legacy._persist_directory,
            collection_name=legacy._collection_name,
            embedding_model="test-model",
            embedding_dimensions=_PrefixSpy.DIM,
        )

        # Simulate instance A being mid-migration: it holds the lock file.
        lock_path.write_text("", encoding="utf-8")
        try:
            instance_b.recreate_collection()  # must NOT wipe -- lock is held
            assert instance_b._collection.count() == 1, (
                "contended recreate_collection() deleted the collection -- "
                "the exact double-delete-recreate race this guard prevents"
            )
        finally:
            lock_path.unlink(missing_ok=True)

        # Once the lock is free, recreate_collection() proceeds normally and
        # actually migrates (proving the guard doesn't permanently wedge it).
        instance_b.recreate_collection()
        assert instance_b._collection.count() == 0, "uncontended recreate must still migrate"
        assert instance_b.live_embed_scheme() == "doc-prefix-v1"

    def test_lock_released_after_recreate(self, tmp_path):
        """The lock must not leak -- a clean recreate_collection() releases it,
        so it never permanently blocks future migrations/attaches."""
        legacy = _make_legacy_chroma(tmp_path)
        lock_path = legacy._persist_directory / ".recreate-embed-scheme.lock"

        legacy.recreate_collection()

        assert not lock_path.exists(), "lock file must be released after recreate_collection()"

    def test_double_check_under_lock_skips_redundant_wipe(self, tmp_path):
        """If another process finishes the migration while THIS process was
        waiting for the lock, re-acquiring must NOT re-wipe an already-migrated
        (and possibly already-resynced) collection -- the double-checked-lock
        re-read of live_embed_scheme() inside the lock must catch this."""
        legacy = _make_legacy_chroma(tmp_path)
        legacy.recreate_collection()  # "someone else" already migrated it
        legacy.upsert_embedding("resynced", [0.1, 0.1, 0.1], {"entity_type": "decision"})
        assert legacy._collection.count() == 1
        assert legacy.live_embed_scheme() == "doc-prefix-v1"

        legacy.recreate_collection()  # this instance's OWN stale gate check fires again

        assert legacy._collection.count() == 1, (
            "recreate_collection() re-wiped an already-migrated collection instead "
            "of no-op'ing on the live (re-checked) embed_scheme"
        )


@pytest.mark.skip(
    reason=(
        "True cross-process repro (real OS processes racing recreate_collection() "
        "against the same on-disk collection) -- standing test criteria forbid "
        "real subprocesses in the default suite. Kept as a documented, runnable-"
        "on-demand stub (plan pin b35e15766c6b): spawn two `python -c` "
        "subprocesses that both construct ChromaDBStorage against the same "
        "legacy (unstamped) persist_directory and call recreate_collection() as "
        "close together as possible; assert the surviving collection has "
        "embed_scheme=doc-prefix-v1 and non-zero count after both re-sync, i.e. "
        "neither process's sync work was silently wiped by the other's delete."
    )
)
def test_true_multiprocess_recreate_race_repro(tmp_path):
    pass
