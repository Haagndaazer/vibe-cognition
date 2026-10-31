"""WP-E-3 tests: the document prefix fix (C1 four-site) and the startup reconciler.

LOAD-BEARING PROOF (test_all_four_storage_paths_use_document_prefix):
  All four storage embed sites must call generate(input_type="document"), NOT
  generate_query_embedding.  Missing any one site makes this test red.

The marker-gated E-3 migration (recreate_collection) is gone with chromadb: a new
vector store is created stamped doc-prefix-v1, so there is nothing to migrate.
"""

from __future__ import annotations

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.documents import sha256_bytes, write_text_sidecar
from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType, generate_node_id
from vibe_cognition.embeddings import VectorStore
from vibe_cognition.server import _sync_cognition_embeddings
from vibe_cognition.tools.cognition_tools import (
    _embed_document,
    _embed_entity_node,
    _embed_workflow,
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


def _make_chroma(tmp_path, *, name: str = "col") -> VectorStore:
    return VectorStore(
        persist_directory=tmp_path / name,
        embedding_model="test-model",
        embedding_dimensions=_PrefixSpy.DIM,
    )


def _get(store: VectorStore, ids: list[str], *, with_vectors: bool = False) -> dict:
    """The present ids' metadata (and raw stored vectors) in chromadb's get() shape."""
    found = store.get_existing(ids)
    present = [i for i in ids if i in found]
    out: dict = {"ids": present, "metadatas": [found[i] for i in present]}
    if with_vectors:
        conn = store._conn
        assert conn is not None
        out["embeddings"] = [
            conn.execute("SELECT embedding FROM vectors WHERE id = ?", (i,)).fetchone()[0]
            for i in present
        ]
    return out


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
        """Site 4: _sync_cognition_embeddings's non-doc path (WP-4, 3e82d4ebc004:
        now routes through the shared _embed_entity_node rather than a drifted
        inline copy — kept as a regression guard). Assert it goes through
        generate(input_type='document'), NOT generate_query_embedding.
        """
        spy = _PrefixSpy()
        cognition = _make_cognition(tmp_path)
        chroma = _make_chroma(tmp_path)

        # Record a non-doc node directly into the JSONL (bypassing embed)
        node = _make_node(summary="sync-test")
        cognition.add_node(node)

        _sync_cognition_embeddings(cognition, chroma, spy)  # type: ignore[arg-type]

        # The non-doc path (_embed_entity_node) must have called generate(document)
        query_calls = spy.query_calls()
        doc_calls = spy.storage_calls()
        assert query_calls == [], "site 4 non-doc path must NOT use generate_query_embedding"
        assert len(doc_calls) >= 1, "site 4 must call generate(input_type='document')"

    def test_search_path_stays_query_prefixed(self, tmp_path):
        """Search must remain query-prefixed — the asymmetry IS the fix."""
        spy = _PrefixSpy()
        cognition = _make_cognition(tmp_path)
        chroma = _make_chroma(tmp_path)

        _search_cognition(cognition, chroma, spy, "my query", None, 5)  # type: ignore[arg-type]

        query_calls = spy.query_calls()
        assert len(query_calls) >= 1, "search must call generate_query_embedding (query prefix)"



class TestGeneratorSurface:
    def test_generator_has_no_generate_batch(self):
        """E-8: generate_batch was dead (zero callers); assert it stays removed."""
        from vibe_cognition.embeddings.generator import EmbeddingGenerator

        assert not hasattr(EmbeddingGenerator, "generate_batch"), (
            "generate_batch was pruned as dead code (WP-LP-A); do not re-add without callers"
        )


class TestReconcilerSharedPathParity:
    """_sync_cognition_embeddings must produce the SAME output as calling the
    shared _embed_entity_node/_embed_workflow paths directly -- not a second,
    driftable copy of the embed-text/metadata-building logic."""

    def test_task_status_owner_survive_reconciler_embed(self, tmp_path):
        """Fails-before: the old inline non-doc branch never read
        node.metadata, so a task's status/owner were silently dropped from
        both the embed text and Chroma metadata when a task was only ever
        embedded by the reconciler (e.g. created during the 2-30s model-load
        window where _record_node defers embedding)."""
        spy = _PrefixSpy()
        cognition = _make_cognition(tmp_path)
        chroma = _make_chroma(tmp_path)

        ts = "2026-06-21T00:00:00+00:00"
        task = CognitionNode(
            id=generate_node_id("task", "ship the thing", ts),
            type=CognitionNodeType.TASK, summary="ship the thing", detail="d",
            context=[], references=[], timestamp=ts, author="t",
            metadata={"status": "in_progress", "owner": "alice"},
        )
        cognition.add_node(task)

        _sync_cognition_embeddings(cognition, chroma, spy)  # type: ignore[arg-type]

        got = _get(chroma, [task.id])
        assert got["metadatas"] is not None
        meta = got["metadatas"][0]
        assert meta.get("status") == "in_progress"
        assert meta.get("owner") == "alice"
        [call] = [c for c in spy.calls if c[0] == "generate"]
        assert "status: in_progress" in call[2]
        assert "owner: alice" in call[2]

    def test_reconciler_embed_matches_embed_entity_node_directly(self, tmp_path):
        """The reconciler's output for a plain node must match (embed text +
        metadata, modulo updated_at) calling _embed_entity_node directly --
        proves there's no second copy of the embed logic to drift."""
        ts = "2026-06-21T00:00:00+00:00"
        node = CognitionNode(
            id=generate_node_id("decision", "parity check", ts),
            type=CognitionNodeType.DECISION, summary="parity check", detail="d",
            context=["ctx"], references=["commit:abc"], timestamp=ts, author="t",
            severity="high",
        )

        cognition = _make_cognition(tmp_path)
        cognition.add_node(node)
        chroma_reconciler = _make_chroma(tmp_path, name="via_reconciler")
        chroma_direct = _make_chroma(tmp_path, name="via_direct")
        spy = _PrefixSpy()

        _sync_cognition_embeddings(cognition, chroma_reconciler, spy)  # type: ignore[arg-type]
        _embed_entity_node(chroma_direct, spy, node)  # type: ignore[arg-type]

        via_reconciler = _get(chroma_reconciler, [node.id], with_vectors=True)
        via_direct = _get(chroma_direct, [node.id], with_vectors=True)
        assert via_reconciler["metadatas"] is not None and via_direct["metadatas"] is not None
        assert via_reconciler["embeddings"] is not None and via_direct["embeddings"] is not None

        meta_a = {k: v for k, v in via_reconciler["metadatas"][0].items() if k != "updated_at"}
        meta_b = {k: v for k, v in via_direct["metadatas"][0].items() if k != "updated_at"}
        assert meta_a == meta_b
        assert list(via_reconciler["embeddings"][0]) == list(via_direct["embeddings"][0])

    def test_reconciler_chunks_workflow_nodes(self, tmp_path):
        """Fails-before: workflow nodes went through the generic non-doc path
        (one unchunked vector of the full, untruncated detail, no #chunk-N
        ids) -- proves the reconciler now routes them through
        _embed_workflow like _record_node does."""
        spy = _PrefixSpy()
        cognition = _make_cognition(tmp_path)
        chroma = _make_chroma(tmp_path)

        ts = "2026-06-21T00:00:00+00:00"
        long_body = "step. " * 300
        wf = CognitionNode(
            id=generate_node_id("workflow", "deploy", ts),
            type=CognitionNodeType.WORKFLOW, summary="deploy", detail=long_body,
            context=[], references=[], timestamp=ts, author="t",
        )
        cognition.add_node(wf)

        _sync_cognition_embeddings(cognition, chroma, spy)  # type: ignore[arg-type]

        ids = chroma.all_ids()
        assert wf.id in ids
        chunk_ids = [i for i in ids if i.startswith(f"{wf.id}#chunk-")]
        assert chunk_ids, "reconciler must chunk workflow nodes via _embed_workflow"


# ── WP-4 (41ced8d1fa63): reconciler verifies the FULL chunk set ───────────────


class TestReconcilerChunkCompleteness:
    def test_detects_incomplete_chunk_set_and_reembeds(self, tmp_path):
        """A crash mid-write-loop (chunk-0 survives, a later chunk vanishes)
        must be detected and repaired -- the old chunk-0-only probe would
        have read this node as fully synced and never touched it again.
        """
        spy = _PrefixSpy()
        cognition = _make_cognition(tmp_path)
        chroma = _make_chroma(tmp_path)

        ts = "2026-06-21T00:00:00+00:00"
        long_body = "step. " * 1200  # > chunk_text's 1000-word window -> >=2 chunks
        wf = CognitionNode(
            id=generate_node_id("workflow", "deploy", ts),
            type=CognitionNodeType.WORKFLOW, summary="deploy", detail=long_body,
            context=[], references=[], timestamp=ts, author="t",
        )
        cognition.add_node(wf)
        _embed_workflow(chroma, spy, wf)  # type: ignore[arg-type]  # full, correct embed

        all_ids = chroma.all_ids()
        chunk_ids = sorted(i for i in all_ids if i.startswith(f"{wf.id}#chunk-"))
        assert len(chunk_ids) >= 2, "test needs a multi-chunk workflow to prove the gap"

        # Simulate a crash mid-write-loop: chunk-0 survives, a LATER chunk vanishes.
        victim = chunk_ids[-1]
        chroma.delete_ids([victim])
        assert victim not in set(chroma.all_ids())

        spy2 = _PrefixSpy()
        _sync_cognition_embeddings(cognition, chroma, spy2)  # type: ignore[arg-type]

        healed_ids = set(chroma.all_ids())
        assert victim in healed_ids, "reconciler must detect the missing chunk and re-embed"

    def test_complete_chunk_set_is_left_alone(self, tmp_path):
        """Regression guard: a genuinely complete node must NOT be re-embedded
        (no wasted model calls every boot)."""
        spy = _PrefixSpy()
        cognition = _make_cognition(tmp_path)
        chroma = _make_chroma(tmp_path)

        ts = "2026-06-21T00:00:00+00:00"
        wf = CognitionNode(
            id=generate_node_id("workflow", "deploy2", ts),
            type=CognitionNodeType.WORKFLOW, summary="deploy2", detail="step. " * 300,
            context=[], references=[], timestamp=ts, author="t",
        )
        cognition.add_node(wf)
        _embed_workflow(chroma, spy, wf)  # type: ignore[arg-type]

        spy2 = _PrefixSpy()
        _sync_cognition_embeddings(cognition, chroma, spy2)  # type: ignore[arg-type]

        assert spy2.calls == [], "a fully-synced workflow must not be re-embedded"

    def test_legacy_vector_missing_chunk_count_is_reembedded(self, tmp_path):
        """WP-4 gate redirect: a vector written BEFORE chunk_count stamping
        existed (no chunk_count key at all) must be treated as INCOMPLETE, not
        conflated with an explicit chunk_count=0. Fails-before: `... or 0`
        treated key-absent identically to an explicit zero, so a legacy
        text-bearing document with a missing/zero chunk set read as
        permanently complete and was never healed.
        """
        cognition = _make_cognition(tmp_path)
        chroma = _make_chroma(tmp_path)

        ts = "2026-06-21T00:00:00+00:00"
        text = "real content. " * 50
        sha = sha256_bytes(text.encode("utf-8"))
        doc = CognitionNode(
            id=generate_node_id("document", "legacy doc", ts),
            type=CognitionNodeType.DOCUMENT, summary="legacy doc", detail="d",
            context=[], references=[], timestamp=ts, author="t",
            metadata={"sha256": sha},
        )
        cognition.add_node(doc)
        write_text_sidecar(cognition.cognition_dir, sha, text)

        # Simulate a LEGACY node vector: no chunk_count key, no chunk vectors
        # (upsert directly -- bypassing _embed_document, which always stamps).
        legacy_spy = _PrefixSpy()
        chroma.upsert_embedding(
            doc.id, legacy_spy.generate("document: legacy doc\nd"),
            {"entity_type": "document", "summary": "legacy doc"},
        )
        pre_meta = _get(chroma, [doc.id])["metadatas"]
        assert pre_meta is not None
        assert "chunk_count" not in pre_meta[0]

        spy = _PrefixSpy()
        _sync_cognition_embeddings(cognition, chroma, spy)  # type: ignore[arg-type]

        post = _get(chroma, [doc.id])
        assert post["metadatas"] is not None
        assert post["metadatas"][0].get("chunk_count") == 1, (
            "reconciler must re-embed the legacy vector and stamp chunk_count"
        )
        chunk_ids = [i for i in chroma.all_ids() if i.startswith(f"{doc.id}#chunk-")]
        assert chunk_ids, "legacy doc's chunks must actually land, not just the count"

    def test_empty_sidecar_doc_with_explicit_zero_chunk_count_not_reembedded(self, tmp_path):
        """Regression guard for the fix above: an explicit chunk_count=0 (a
        genuinely empty-sidecar document, fully synced by THIS WP's own
        _embed_document) must NOT be conflated with the legacy-absent case
        and must NOT be wastefully re-embedded on every boot."""
        cognition = _make_cognition(tmp_path)
        chroma = _make_chroma(tmp_path)
        spy = _PrefixSpy()

        ts = "2026-06-21T00:00:00+00:00"
        sha = sha256_bytes(b"")
        doc = CognitionNode(
            id=generate_node_id("document", "empty doc", ts),
            type=CognitionNodeType.DOCUMENT, summary="empty doc", detail="d",
            context=[], references=[], timestamp=ts, author="t",
            metadata={"sha256": sha},
        )
        cognition.add_node(doc)
        # No sidecar written -- read_text_sidecar returns None -> has_text=False.
        _embed_document(chroma, spy, doc.id, doc.summary, doc.detail, "")  # type: ignore[arg-type]

        meta = _get(chroma, [doc.id])["metadatas"]
        assert meta is not None and meta[0].get("chunk_count") == 0

        spy2 = _PrefixSpy()
        _sync_cognition_embeddings(cognition, chroma, spy2)  # type: ignore[arg-type]

        assert spy2.calls == [], "explicit chunk_count=0 must not be treated as legacy/incomplete"
