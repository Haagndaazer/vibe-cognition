"""WP-D1a: DOCUMENT node type, graph-inert guard, store/get tools, sidecar, sync guard."""

from typing import cast

from vibe_cognition.cognition.documents import text_sidecar_path
from vibe_cognition.cognition.models import CognitionEdgeType, CognitionNode, CognitionNodeType
from vibe_cognition.cognition.operations import delete_cognition_node
from vibe_cognition.cognition.storage import CognitionStorage
from vibe_cognition.embeddings import ChromaDBStorage, EmbeddingGenerator
from vibe_cognition.server import _sync_cognition_embeddings
from vibe_cognition.tools.cognition_tools import _get_document, _store_document


class _NoopEmbed:
    def delete_embedding(self, node_id):
        return True


def _node(node_id, node_type, refs=None, summary="s", detail="d"):
    return CognitionNode(
        id=node_id, type=node_type, summary=summary, detail=detail,
        context=[], references=refs or [], severity=None,
        timestamp="2026-06-13T00:00:00+00:00", author="t",
    )


def test_document_is_graph_inert_no_part_of_from_citing_episode(tmp_path):
    """D1a: a document is graph-inert. An episode citing the document's doc:<hash>
    ref must NOT mint a part_of edge — the existing matcher would otherwise treat
    the document as an entity and link it (the wrong edge fires from the EPISODE's
    record call, which is why the guard is pair-level). Asserts the SPECIFIC edge
    is absent in BOTH directions, not just an edge count."""
    s = CognitionStorage(tmp_path)
    doc_ref = "doc:abc123def456"
    s.add_node(_node("doc00001", CognitionNodeType.DOCUMENT, refs=[doc_ref]))
    s.add_node(_node("ep000001", CognitionNodeType.EPISODE, refs=[doc_ref]))

    s.create_deterministic_edges("ep000001")  # the edge would fire here
    s.create_deterministic_edges("doc00001")

    g = s.graph
    assert not g.has_edge("ep000001", "doc00001"), "episode→document edge minted (inert guard failed)"
    assert not g.has_edge("doc00001", "ep000001"), "document→episode part_of minted (inert guard failed)"


def test_entity_episode_matcher_still_links(tmp_path):
    """Positive control: the document guard did not over-reach — a normal entity
    and episode sharing a commit ref still get their entity→episode part_of."""
    s = CognitionStorage(tmp_path)
    s.add_node(_node("ep000002", CognitionNodeType.EPISODE, refs=["commit:deadbeef1234"]))
    s.add_node(_node("dec00001", CognitionNodeType.DECISION, refs=["commit:deadbeef1234"]))
    s.create_deterministic_edges("dec00001")

    g = s.graph
    assert g.has_edge("dec00001", "ep000002"), "entity→episode part_of missing (guard over-reached)"
    assert CognitionEdgeType.PART_OF.value in g["dec00001"]["ep000002"]


def test_store_reference_and_get_roundtrip(tmp_path):
    """Reference mode stores path+metadata+sha; get returns sidecar text + unchanged."""
    s = CognitionStorage(tmp_path / "cog")
    doc = tmp_path / "client.txt"
    doc.write_bytes(b"raw document bytes")
    res = _store_document(
        s, title="Client spec", document_text="extracted text here",
        context="legal, contract", author="t", file_path=str(doc),
    )
    assert res["mode"] == "reference", f"expected reference mode, got {res!r}"
    assert res["indexed_text_chars"] == len("extracted text here"), "indexed_chars not reported (S5)"
    assert res["doc_ref"].startswith("doc:")

    got = _get_document(s, node_id=res["node_id"])
    assert got["text"] == "extracted text here", "sidecar text not returned"
    assert got["metadata"]["sha256"] == got["metadata"]["sha256"]
    assert got["freshness"] == "unchanged", f"freshness should be unchanged, got {got['freshness']}"
    assert got["metadata"]["mode"] == "reference"


def test_store_content_text_and_get_by_doc_ref(tmp_path):
    """content_text path: hashed directly; resolvable by doc_ref."""
    s = CognitionStorage(tmp_path / "cog")
    res = _store_document(s, title="Inline note", document_text="the note body",
                          context="", author="t", content_text="the note body")
    got = _get_document(s, doc_ref_arg=res["doc_ref"])
    assert got["node_id"] == res["node_id"], "doc_ref did not resolve to the stored node"
    assert got["text"] == "the note body"


def test_dedup_returns_existing_unless_force_new(tmp_path):
    """Same content stored twice returns the SAME node (already_stored), not a twin;
    force_new overrides."""
    s = CognitionStorage(tmp_path / "cog")
    a = _store_document(s, title="d", document_text="x", context="", author="t", content_text="same bytes")
    b = _store_document(s, title="d", document_text="x", context="", author="t", content_text="same bytes")
    assert b.get("already_stored") is True, "dedup did not trigger (would create a twin)"
    assert b["node_id"] == a["node_id"], "dedup returned a different node id"
    c = _store_document(s, title="d", document_text="x", context="", author="t",
                        content_text="same bytes", force_new=True)
    assert c["node_id"] != a["node_id"], "force_new did not create a new node"


def test_agent_refs_go_to_context_not_node_references(tmp_path):
    """S4/N3: the document node's OWN references are restricted to doc:<hash>;
    agent-supplied refs land in context (so old matchers can't link on issue:/commit:)."""
    s = CognitionStorage(tmp_path / "cog")
    res = _store_document(s, title="d", document_text="x", context="topic",
                          author="t", content_text="bytes", references="issue:LL-1,commit:abc123")
    node = s.get_node(res["node_id"])
    assert node is not None
    assert node["references"] == [res["doc_ref"]], (
        f"document references must be ONLY its doc: key, got {node['references']}"
    )
    assert "issue:LL-1" in node["context"], "agent ref not redirected to context"


class _FakeCollection:
    def get(self, ids):
        return {"ids": []}  # nothing synced yet -> everything looks "missing"


class _FakeEmbeddingStorage:
    def __init__(self):
        self._collection = _FakeCollection()
        self.upserted: list[str] = []

    def upsert_embedding(self, entity_id, embedding, metadata):
        self.upserted.append(entity_id)


class _FakeGenerator:
    def generate_query_embedding(self, text):
        return [0.0, 0.1, 0.2]


def test_sync_skips_document_nodes_never_embeds_them(tmp_path):
    """N1-class guard: _sync_cognition_embeddings is the cross-process path that
    re-embeds JSONL nodes ChromaDB is missing. A document node must be SKIPPED
    there — otherwise every server start would re-embed documents into semantic
    search. Asserts the document id is never upserted while a normal node is
    (fails-before: without the type filter the document id appears in .upserted)."""
    s = CognitionStorage(tmp_path / "cog")
    s.add_node(_node("doc00009", CognitionNodeType.DOCUMENT, refs=["doc:abc123abc123"]))
    s.add_node(_node("dec00009", CognitionNodeType.DECISION, refs=["commit:abc123abc123"]))

    embed = _FakeEmbeddingStorage()
    _sync_cognition_embeddings(
        s, cast(ChromaDBStorage, embed), cast(EmbeddingGenerator, _FakeGenerator())
    )

    assert "doc00009" not in embed.upserted, "document node was embedded (N1 sync guard failed)"
    assert "dec00009" in embed.upserted, "non-document node was not embedded (guard over-reached)"


def test_get_document_freshness_modified_and_missing(tmp_path):
    """Reference-mode re-hash reports modified when the file changes and missing
    when it's gone — and never raises on a missing path."""
    s = CognitionStorage(tmp_path / "cog")
    doc = tmp_path / "f.txt"
    doc.write_bytes(b"original")
    res = _store_document(s, title="d", document_text="t", context="", author="t", file_path=str(doc))
    doc.write_bytes(b"changed contents")
    assert _get_document(s, node_id=res["node_id"])["freshness"] == "modified", "modified not detected"
    doc.unlink()
    assert _get_document(s, node_id=res["node_id"])["freshness"] == "missing", "missing not detected"


def test_delete_document_removes_sidecar_not_the_original(tmp_path):
    """Deleting a reference-mode document purges its managed text sidecar but
    NEVER the referenced original file (reference-mode deletion reclaims only what
    the server wrote)."""
    s = CognitionStorage(tmp_path / "cog")
    original = tmp_path / "orig.txt"
    original.write_bytes(b"the real file stays")
    res = _store_document(s, title="d", document_text="extracted", context="", author="t",
                          file_path=str(original))
    node = s.get_node(res["node_id"])
    assert node is not None
    sha = node["metadata"]["sha256"]
    sidecar = text_sidecar_path(s.cognition_dir, sha)
    assert sidecar.exists(), "sidecar not written on store"

    out = delete_cognition_node(s, _NoopEmbed(), res["node_id"])
    assert out is not None and out["id"] == res["node_id"]
    assert not sidecar.exists(), "sidecar not purged on delete"
    assert original.exists(), "delete touched the referenced original file (must never happen)"


def test_delete_one_twin_keeps_shared_sidecar_until_last_gone(tmp_path):
    """force_new can mint two document nodes over identical bytes -> one shared,
    content-addressed sidecar. Deleting one twin must NOT orphan the other's
    sidecar; only the last deletion removes it."""
    s = CognitionStorage(tmp_path / "cog")
    a = _store_document(s, title="d", document_text="x", context="", author="t", content_text="dup bytes")
    b = _store_document(s, title="d", document_text="x", context="", author="t",
                        content_text="dup bytes", force_new=True)
    assert a["node_id"] != b["node_id"], "force_new did not create a twin"
    node = s.get_node(a["node_id"])
    assert node is not None
    sha = node["metadata"]["sha256"]
    sidecar = text_sidecar_path(s.cognition_dir, sha)
    assert sidecar.exists()

    delete_cognition_node(s, _NoopEmbed(), a["node_id"])
    assert sidecar.exists(), "shared sidecar purged while a twin still references it (orphaned the twin)"
    delete_cognition_node(s, _NoopEmbed(), b["node_id"])
    assert not sidecar.exists(), "sidecar not purged after the last twin was deleted"
