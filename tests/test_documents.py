"""WP-D1a/D1b: DOCUMENT type, matcher pair rules, store/get, sidecar, copy mode."""

from datetime import UTC, datetime
from typing import cast

import vibe_cognition.tools.cognition_tools as ct
from vibe_cognition.cognition.documents import (
    blob_path,
    blob_rel_path,
    cheap_staleness_signal,
    documents_dir,
    find_orphaned_document_artifacts,
    gitignore_has_entry,
    sanitize_extension,
    sha256_bytes,
    text_sidecar_path,
)
from vibe_cognition.cognition.models import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
)
from vibe_cognition.cognition.operations import delete_cognition_node
from vibe_cognition.cognition.storage import CognitionStorage
from vibe_cognition.embeddings import ChromaDBStorage, EmbeddingGenerator
from vibe_cognition.server import _reconcile_orphan_embeddings, _sync_cognition_embeddings
from vibe_cognition.tools.cognition_tools import (
    _format_search_results,
    _get_document,
    _search_cognition,
    _store_document,
)


class _NoopEmbed:
    def delete_embedding(self, node_id):
        return True

    def delete_by_node_id(self, node_id):
        pass


def _node(node_id, node_type, refs=None, summary="s", detail="d"):
    return CognitionNode(
        id=node_id, type=node_type, summary=summary, detail=detail,
        context=[], references=refs or [], severity=None,
        timestamp="2026-06-13T00:00:00+00:00", author="t",
    )


def _meta_sha(s, node_id):
    node = s.get_node(node_id)
    assert node is not None
    return node["metadata"]["sha256"]


# --- WP-D1b matcher pair rules (supersede D1a's inert guard) -----------------
# D1a's test_document_is_graph_inert_no_part_of_from_citing_episode is intentionally
# REMOVED: the inert guard was temporary. Documents are now first-class hubs with
# the §1/§9 S4 truth table below — an episode citing a doc ref now gets a
# document→episode relates_to (not "no edge").


def test_entity_document_part_of_on_doc_ref(tmp_path):
    """entity↔document → part_of (direction entity→document), doc:-gated."""
    s = CognitionStorage(tmp_path)
    ref = "doc:abc123def456"
    s.add_node(_node("doc00001", CognitionNodeType.DOCUMENT, refs=[ref]))
    s.add_node(_node("dec00001", CognitionNodeType.DECISION, refs=[ref]))
    s.create_deterministic_edges("dec00001")

    g = s.graph
    assert g.has_edge("dec00001", "doc00001"), "entity→document part_of not minted"
    assert CognitionEdgeType.PART_OF.value in g["dec00001"]["doc00001"]
    assert not g.has_edge("doc00001", "dec00001"), "wrong-direction edge minted"


def test_document_episode_relates_to_on_doc_ref(tmp_path):
    """document↔episode → relates_to (direction document→episode), doc:-gated.
    Supersedes D1a's inert guarantee: the episode's record call now mints the
    relates_to. Asserts the SPECIFIC type+direction, not a count."""
    s = CognitionStorage(tmp_path)
    ref = "doc:abc123def456"
    s.add_node(_node("doc00001", CognitionNodeType.DOCUMENT, refs=[ref]))
    s.add_node(_node("ep000001", CognitionNodeType.EPISODE, refs=[ref]))
    s.create_deterministic_edges("ep000001")  # fires from the episode's call

    g = s.graph
    assert g.has_edge("doc00001", "ep000001"), "document→episode relates_to not minted"
    assert CognitionEdgeType.RELATES_TO.value in g["doc00001"]["ep000001"]
    assert CognitionEdgeType.PART_OF.value not in g["doc00001"]["ep000001"], (
        "document→episode must be relates_to, not part_of (§8(c))"
    )
    assert not g.has_edge("ep000001", "doc00001"), "wrong-direction edge minted"

    # Per-(from,to,TYPE) idempotency: re-running must NOT re-mint the relates_to.
    # (A hardcoded part_of-only existing-edge check would never skip a relates_to
    # and would re-mint on every run — the A5 keying bug.)
    assert s.create_deterministic_edges("ep000001") == 0, "relates_to re-minted (not idempotent)"
    assert s.create_deterministic_edges("doc00001") == 0, "relates_to re-minted from the doc side"


def test_s4_vacuum_no_document_link_on_nondoc_shared_ref(tmp_path):
    """§9 S4 vacuum defense (the key fails-before): a document and an episode
    sharing ONLY a non-doc ref (issue:X) must get ZERO deterministic edges — a
    document link fires only on a shared doc: key, never on a popular issue:/
    commit: ref. Without the doc-gate this would wrongly mint relates_to."""
    s = CognitionStorage(tmp_path)
    s.add_node(_node("doc00002", CognitionNodeType.DOCUMENT, refs=["issue:LL-9"]))
    s.add_node(_node("ep000002", CognitionNodeType.EPISODE, refs=["issue:LL-9"]))
    s.create_deterministic_edges("ep000002")
    s.create_deterministic_edges("doc00002")

    g = s.graph
    assert not g.has_edge("doc00002", "ep000002"), "document linked on a non-doc shared ref (vacuum)"
    assert not g.has_edge("ep000002", "doc00002"), "document linked on a non-doc shared ref (vacuum)"


def test_doc_doc_and_episode_episode_pairs_skip(tmp_path):
    """document↔document and episode↔episode share a doc: ref but get NO edge
    (versioning uses explicit supersedes; episodes don't nest deterministically)."""
    s = CognitionStorage(tmp_path)
    ref = "doc:abc123def456"
    s.add_node(_node("doc00003", CognitionNodeType.DOCUMENT, refs=[ref]))
    s.add_node(_node("doc00004", CognitionNodeType.DOCUMENT, refs=[ref]))
    s.add_node(_node("ep000003", CognitionNodeType.EPISODE, refs=[ref]))
    s.add_node(_node("ep000004", CognitionNodeType.EPISODE, refs=[ref]))
    for nid in ("doc00003", "doc00004", "ep000003", "ep000004"):
        s.create_deterministic_edges(nid)

    g = s.graph
    assert not g.has_edge("doc00003", "doc00004") and not g.has_edge("doc00004", "doc00003"), (
        "doc↔doc edge minted"
    )
    # episode↔episode (note: each still links to the documents via relates_to, but not to each other)
    assert not g.has_edge("ep000003", "ep000004") and not g.has_edge("ep000004", "ep000003"), (
        "episode↔episode edge minted"
    )


def test_manual_edge_coexistence_and_idempotency(tmp_path):
    """Per-(from,to,type) idempotency: a manual relates_to does NOT block a
    deterministic part_of (different type); a manual part_of is NOT clobbered by a
    re-mint (same type, any source skips); re-running the matcher adds no duplicate."""
    s = CognitionStorage(tmp_path)
    ref = "doc:abc123def456"
    s.add_node(_node("doc00005", CognitionNodeType.DOCUMENT, refs=[ref]))
    s.add_node(_node("dec00005", CognitionNodeType.DECISION, refs=[ref]))

    # A manual relates_to on the same pair the matcher will mint part_of on.
    s.add_edge(CognitionEdge(
        from_id="dec00005", to_id="doc00005", edge_type=CognitionEdgeType.RELATES_TO,
        timestamp="2026-06-13T00:00:00+00:00", source="manual",
    ))
    s.create_deterministic_edges("dec00005")
    g = s.graph
    assert CognitionEdgeType.PART_OF.value in g["dec00005"]["doc00005"], (
        "deterministic part_of blocked by a different-type manual edge"
    )
    assert g["dec00005"]["doc00005"][CognitionEdgeType.RELATES_TO.value]["source"] == "manual", (
        "manual relates_to clobbered"
    )

    # Re-run: no duplicate part_of, and a pre-existing manual part_of survives untouched.
    created_second = s.create_deterministic_edges("dec00005")
    assert created_second == 0, "matcher re-minted an existing edge (not idempotent)"


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
    assert got["metadata"]["sha256"] == sha256_bytes(b"raw document bytes"), "stored sha != file sha"
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


def test_distinct_docs_get_distinct_ids_under_a_frozen_clock(tmp_path, monkeypatch):
    """Windows-surfaced (CI flake), real-everywhere bug: node ids hash
    type:summary:timestamp, and the Windows clock is ~15 ms coarse, so two stores
    of the same title in one tick hashed to the SAME id and add_node SILENTLY
    OVERWROTE the first. Freeze the clock to make the collision deterministic:
    two DIFFERENT documents sharing a title must still get distinct ids (and both
    must survive). This also covers the force_new twin case that flaked CI."""
    import vibe_cognition.tools.cognition_tools as ct

    frozen = datetime(2026, 6, 13, 0, 0, 0, tzinfo=UTC)

    class _FrozenClock:
        @staticmethod
        def now(tz=None):
            return frozen

    monkeypatch.setattr(ct, "datetime", _FrozenClock)

    s = CognitionStorage(tmp_path / "cog")
    a = _store_document(s, title="same title", document_text="x", context="",
                        author="t", content_text="bytes A")
    b = _store_document(s, title="same title", document_text="y", context="",
                        author="t", content_text="bytes B")
    assert a["node_id"] != b["node_id"], "two distinct docs collided on one id (silent overwrite)"
    assert s.has_node(a["node_id"]) and s.has_node(b["node_id"]), "a colliding store overwrote the other"


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


# WP-D2 REPLACES D1a's test_sync_skips_document_nodes_never_embeds_them: documents
# are no longer skipped — the sync backfills documents stored without embeddings
# (the D1a/D1b interim, or a model-not-ready defer) as node vector + sidecar chunks.


def test_sync_backfills_document_node_and_chunks(tmp_path):
    """_sync embeds documents that were stored without embeddings (deferred): node
    vector + sidecar chunks. Idempotent — a second sync does ZERO embedding work."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    gen_obj = _FixedGen([0.1, 0.2, 0.3])
    gen = cast(EmbeddingGenerator, gen_obj)
    text = " ".join(str(i) for i in range(1500))  # multi-chunk
    res = _store_document(s, title="d", document_text=text, context="", author="t",
                          content_text=text)  # no embedding deps -> deferred
    assert embed._collection.get()["ids"] == [], "precondition: deferred (no vectors yet)"
    nid = res["node_id"]

    _sync_cognition_embeddings(s, embed, gen)
    ids = set(embed._collection.get()["ids"])
    assert nid in ids, "document node not backfilled by sync"
    assert any(i.startswith(f"{nid}#chunk-") for i in ids), "document chunks not backfilled"

    gen_obj.calls = 0
    _sync_cognition_embeddings(s, embed, gen)
    assert gen_obj.calls == 0, "already-synced document re-embedded (not idempotent — re-embed loop)"


def test_sync_does_not_re_embed_empty_text_document(tmp_path):
    """A4 loop guard: a text-less document (empty sidecar) is fully synced after its
    NODE vector lands — no chunks, and NOT re-embedded every boot."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    gen_obj = _FixedGen([0.1, 0.2, 0.3])
    gen = cast(EmbeddingGenerator, gen_obj)
    res = _store_document(s, title="empty", document_text="", context="", author="t",
                          content_text="x")  # empty sidecar text
    nid = res["node_id"]

    _sync_cognition_embeddings(s, embed, gen)
    ids = set(embed._collection.get()["ids"])
    assert nid in ids, "empty-text document node not embedded"
    assert not any(i.startswith(f"{nid}#chunk-") for i in ids), "empty-text doc should have no chunks"

    gen_obj.calls = 0
    _sync_cognition_embeddings(s, embed, gen)
    assert gen_obj.calls == 0, "empty-text document re-embedded every sync (A4 loop)"


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


def test_restore_changed_file_offers_supersedes_link(tmp_path):
    """WP-12 (db65f1568fa5): re-storing a file at the SAME path with DIFFERENT
    content (new sha) offers a supersedes link -- prior_version_id + a
    consider_supersedes note -- WITHOUT creating the edge itself (manual-only
    is the recorded design).

    Fails-before: no such offer existed; the two document nodes at the same
    path sat completely unlinked with no signal a curator could act on.
    """
    s = CognitionStorage(tmp_path / "cog")
    doc = tmp_path / "f.txt"
    doc.write_bytes(b"version one")
    first = _store_document(s, title="d", document_text="v1", context="", author="t",
                            file_path=str(doc))
    assert "prior_version_id" not in first  # nothing prior on the first store

    doc.write_bytes(b"version two, genuinely different content")
    second = _store_document(s, title="d", document_text="v2", context="", author="t",
                             file_path=str(doc))

    assert second["prior_version_id"] == first["node_id"]
    assert "consider_supersedes" in second
    assert first["node_id"] in second["consider_supersedes"]
    assert second["node_id"] in second["consider_supersedes"]
    # Never auto-created -- the two nodes must have NO edge between them yet.
    assert not any(
        tid == first["node_id"] for tid, _ in s.get_successors(second["node_id"])
    )


def test_first_store_at_path_has_no_supersedes_offer(tmp_path):
    """A brand-new path with no prior document node gets no offer at all."""
    s = CognitionStorage(tmp_path / "cog")
    doc = tmp_path / "f.txt"
    doc.write_bytes(b"only version")
    res = _store_document(s, title="d", document_text="v1", context="", author="t",
                          file_path=str(doc))
    assert "prior_version_id" not in res
    assert "consider_supersedes" not in res


def test_content_text_store_never_offers_supersedes(tmp_path):
    """content_text-sourced stores have no path at all -- the check must not
    run (and must not crash) for them."""
    s = CognitionStorage(tmp_path / "cog")
    first = _store_document(s, title="d", document_text="v1", context="", author="t",
                            content_text="version one")
    second = _store_document(s, title="d", document_text="v2", context="", author="t",
                             content_text="version two")
    assert "prior_version_id" not in first
    assert "prior_version_id" not in second


def test_document_metadata_survives_journal_replay(tmp_path):
    """Cross-process seam: the metadata dict (sha256, path, mode) must round-trip
    through the JSONL journal — a SECOND storage instance replaying the same
    journal must see it, or freshness re-hash and sidecar deletion break for any
    teammate who didn't create the node. Asserts sha256 specifically (not just
    'metadata is non-empty')."""
    cog = tmp_path / "cog"
    s1 = CognitionStorage(cog)
    res = _store_document(s1, title="d", document_text="t", context="", author="t",
                          content_text="round-trip me")
    n1 = s1.get_node(res["node_id"])
    assert n1 is not None
    sha1 = n1["metadata"]["sha256"]

    s2 = CognitionStorage(cog)  # fresh instance -> replays the journal from disk
    replayed = s2.get_node(res["node_id"])
    assert replayed is not None, "node did not replay"
    assert replayed["metadata"].get("sha256") == sha1, "sha256 lost across journal replay"
    assert replayed["metadata"].get("mode") == "reference", "mode lost across journal replay"


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


def test_delete_document_purges_sidecar_even_when_an_entity_cites_it(tmp_path):
    """F1 regression: per DESIGN S4 a descriptor ENTITY cites the document's
    doc:<hash> in its OWN references. Deleting the sole document node must still
    purge the sidecar — a citing entity is NOT a twin (only another document with
    the same sha is). The old twin-check used find_nodes_by_ref (any citer) and so
    leaked the sidecar permanently: no document node remained to ever re-trigger
    the purge. Asserts sidecar purged, original untouched, entity still present."""
    s = CognitionStorage(tmp_path / "cog")
    original = tmp_path / "spec.txt"
    original.write_bytes(b"contract bytes")
    res = _store_document(s, title="Spec", document_text="extracted spec text",
                          context="", author="t", file_path=str(original))
    doc_ref = res["doc_ref"]
    # A descriptor entity that cites the document's doc: ref (the intended S4 pattern).
    s.add_node(_node("dec00100", CognitionNodeType.DECISION, refs=[doc_ref]))

    node = s.get_node(res["node_id"])
    assert node is not None
    sidecar = text_sidecar_path(s.cognition_dir, node["metadata"]["sha256"])
    assert sidecar.exists()

    delete_cognition_node(s, _NoopEmbed(), res["node_id"])
    assert not sidecar.exists(), "sidecar leaked: a citing entity was wrongly treated as a twin (F1)"
    assert original.exists(), "delete touched the referenced original file"
    assert s.has_node("dec00100"), "deleting the document wrongly removed the citing entity"


# --- WP-D1b copy mode: blob store, sanitization, size policy, .gitignore, delete -


def test_sanitize_extension_whitelist_or_drop():
    """The sole agent-controlled path component: keep a leading-dot alnum run
    (<=10), DROP everything else (never fail the store)."""
    assert sanitize_extension(".pdf") == ".pdf"
    assert sanitize_extension(".TXT") == ".TXT"
    assert sanitize_extension(".tar3") == ".tar3"
    for bad in ["", ".", "noleadingdot", ".a/b", "../x", ".has.dot", ".pdf.exe",
                ".with space", ".toolongextension", ".x\\y"]:
        assert sanitize_extension(bad) == "", f"{bad!r} not dropped"
    # blob_rel_path applies it: a hostile ext collapses to a bare-sha name.
    assert blob_rel_path("a" * 64, "../evil") == f"aa/{'a' * 64}"


def test_copy_mode_writes_blob_and_reports(tmp_path):
    """store_copy=True writes the content-addressed blob and reports mode/bytes/path;
    committed by default (no .gitignore line)."""
    s = CognitionStorage(tmp_path / "cog")
    res = _store_document(s, title="c.txt", document_text="x", context="", author="t",
                          content_text="blob bytes here", store_copy=True)
    assert res["mode"] == "copy", f"expected copy mode, got {res!r}"
    assert res["blob_bytes"] == len(b"blob bytes here")
    assert res["local_only"] is False
    bp = documents_dir(s.cognition_dir) / res["blob_path"]
    assert bp.exists() and bp.read_bytes() == b"blob bytes here", "blob not written with exact bytes"
    assert bp.name.endswith(".txt"), "sanitized .txt ext not carried into the blob name"
    assert not gitignore_has_entry(s.cognition_dir, res["blob_path"]), "committed blob wrongly gitignored"


def test_copy_mode_blob_write_once(tmp_path):
    """force_new copy twins over identical bytes share ONE blob file (write-once)."""
    s = CognitionStorage(tmp_path / "cog")
    a = _store_document(s, title="d", document_text="x", context="", author="t",
                        content_text="same", store_copy=True)
    b = _store_document(s, title="d", document_text="x", context="", author="t",
                        content_text="same", store_copy=True, force_new=True)
    assert a["node_id"] != b["node_id"], "force_new did not create a twin"
    assert a["blob_path"] == b["blob_path"], "same content produced different blob paths"
    bp = blob_path(s.cognition_dir, _meta_sha(s, a["node_id"]), "")
    assert bp.exists()


def test_copy_mode_local_only_writes_gitignore(tmp_path):
    """local_only=True ignores the blob via the LOCAL self-ignoring documents/.gitignore."""
    s = CognitionStorage(tmp_path / "cog")
    res = _store_document(s, title="d", document_text="x", context="", author="t",
                          content_text="secret", store_copy=True, local_only=True)
    assert res["local_only"] is True
    assert gitignore_has_entry(s.cognition_dir, res["blob_path"]), "local_only blob not gitignored"
    assert gitignore_has_entry(s.cognition_dir, ".gitignore"), ".gitignore is not self-ignoring"


def test_local_only_gitignore_entry_lands_before_blob_write(tmp_path, monkeypatch):
    """WP-12 (07fdfe725e7f): the .gitignore entry for a local_only blob must be
    written BEFORE the blob file itself lands on disk -- otherwise there is a
    window where the privacy-intended blob sits unignored, and a coincident
    `git add`/commit in that window could publish it.

    Fails-before: write_blob ran first, then add_gitignore_entry -- monkeypatching
    write_blob to check gitignore state at call-time would have observed the
    entry ABSENT, failing this assertion.
    """
    s = CognitionStorage(tmp_path / "cog")
    seen_ignored_at_write_time = {}

    real_write_blob = ct.write_blob

    def _spy_write_blob(cognition_dir, sha, ext, **kwargs):
        blob_rel = blob_rel_path(sha, ext)
        seen_ignored_at_write_time["value"] = gitignore_has_entry(cognition_dir, blob_rel)
        return real_write_blob(cognition_dir, sha, ext, **kwargs)

    monkeypatch.setattr(ct, "write_blob", _spy_write_blob)

    res = _store_document(s, title="d", document_text="x", context="", author="t",
                          content_text="secret", store_copy=True, local_only=True)

    assert res["local_only"] is True
    assert seen_ignored_at_write_time.get("value") is True, (
        "gitignore entry was not yet present when write_blob ran -- ordering regression"
    )


def test_copy_mode_size_policy_forces_local_only(tmp_path, monkeypatch):
    """§9 S1: a blob >= the WARN threshold is auto-forced to local_only with a
    warning (no hard cap). Thresholds monkeypatched small to avoid a 50MB fixture."""
    monkeypatch.setattr(ct, "BLOB_WARN_BYTES", 4)
    monkeypatch.setattr(ct, "BLOB_REFUSE_BYTES", 1000)
    s = CognitionStorage(tmp_path / "cog")
    res = _store_document(s, title="d", document_text="x", context="", author="t",
                          content_text="way over four bytes", store_copy=True)  # default commit
    assert res["local_only"] is True, "large blob not auto-forced to local_only"
    assert any("local_only" in w for w in res.get("warnings", [])), "no size warning reported"
    assert gitignore_has_entry(s.cognition_dir, res["blob_path"])


def test_copy_mode_s3_promote_local_only_to_default(tmp_path):
    """S3: re-storing a local_only blob as default PROMOTES it (removes the
    .gitignore line, reports promoted)."""
    s = CognitionStorage(tmp_path / "cog")
    a = _store_document(s, title="d", document_text="x", context="", author="t",
                        content_text="same", store_copy=True, local_only=True)
    assert gitignore_has_entry(s.cognition_dir, a["blob_path"])
    b = _store_document(s, title="d", document_text="x", context="", author="t",
                        content_text="same", store_copy=True, local_only=False)
    assert b.get("already_stored") is True and b["node_id"] == a["node_id"], "dedup did not hit"
    assert b.get("promoted") is True, "local_only->default did not report promoted"
    assert not gitignore_has_entry(s.cognition_dir, a["blob_path"]), "promote did not de-gitignore"


def test_copy_mode_s3_demote_default_to_local_only_cannot_unpublish(tmp_path):
    """S3: re-storing a committed blob as local_only cannot un-publish it — reports
    already_committed (git history retains it)."""
    s = CognitionStorage(tmp_path / "cog")
    a = _store_document(s, title="d", document_text="x", context="", author="t",
                        content_text="same", store_copy=True)  # committed
    b = _store_document(s, title="d", document_text="x", context="", author="t",
                        content_text="same", store_copy=True, local_only=True)
    assert b["node_id"] == a["node_id"], "dedup did not hit"
    assert b.get("already_committed") is True, "default->local_only did not warn already_committed"


def test_copy_blob_refcount_delete_uses_shared_predicate(tmp_path):
    """Blob refcount delete: two copy twins share one blob -> delete one keeps it,
    delete the last unlinks it + its .gitignore line. The 'still referenced?' check
    asserts via storage.documents_with_sha (the SAME predicate the code uses) — not
    a re-encoded inline filter (Vince/ledger 11: a 4th drifting reader = F1 reborn)."""
    s = CognitionStorage(tmp_path / "cog")
    a = _store_document(s, title="d", document_text="x", context="", author="t",
                        content_text="dup", store_copy=True, local_only=True)
    b = _store_document(s, title="d", document_text="x", context="", author="t",
                        content_text="dup", store_copy=True, local_only=True, force_new=True)
    sha = _meta_sha(s, a["node_id"])
    bp = blob_path(s.cognition_dir, sha, "")
    rel = a["blob_path"]
    assert bp.exists() and gitignore_has_entry(s.cognition_dir, rel)
    assert len(s.documents_with_sha(sha)) == 2  # shared-predicate assertion (not inline)

    delete_cognition_node(s, _NoopEmbed(), a["node_id"])
    assert s.documents_with_sha(sha) == [b["node_id"]], "predicate should report the surviving twin"
    assert bp.exists(), "blob unlinked while a copy twin still owns it"
    assert gitignore_has_entry(s.cognition_dir, rel), ".gitignore line removed prematurely"

    out = delete_cognition_node(s, _NoopEmbed(), b["node_id"])
    assert out is not None
    assert s.documents_with_sha(sha) == [], "predicate should report no documents left"
    assert not bp.exists(), "blob not unlinked after the last copy twin deleted"
    assert not gitignore_has_entry(s.cognition_dir, rel), ".gitignore line not reclaimed"
    assert rel in out.get("unlinked_artifacts", []), "unlinked blob not reported"


def test_reference_twin_has_no_blob_stake(tmp_path):
    """B3: a reference-mode twin shares the sha but NOT the blob — deleting the
    copy-mode node unlinks the blob even though the reference twin remains (blob
    refcount filters the sha cohort to mode=='copy', sidecar refcount does not)."""
    s = CognitionStorage(tmp_path / "cog")
    ref_node = _store_document(s, title="d", document_text="x", context="", author="t",
                               content_text="shared")  # reference mode
    copy_node = _store_document(s, title="d", document_text="x", context="", author="t",
                                content_text="shared", store_copy=True, force_new=True)
    sha = _meta_sha(s, copy_node["node_id"])
    bp = blob_path(s.cognition_dir, sha, "")
    sidecar = text_sidecar_path(s.cognition_dir, sha)
    assert bp.exists() and sidecar.exists()

    delete_cognition_node(s, _NoopEmbed(), copy_node["node_id"])
    assert not bp.exists(), "blob not unlinked: a reference twin (no blob stake) wrongly blocked it"
    assert sidecar.exists(), "sidecar wrongly purged while the reference twin still holds the sha"
    assert s.has_node(ref_node["node_id"]), "reference twin wrongly removed"


# --- WP-D1b N1 ghost-search fix (general; pre-dates documents) -----------------


def test_search_drops_hits_for_graph_absent_nodes(tmp_path):
    """N1 (§9) + D2 dedupe: a ghost id (node deleted cross-process) is dropped; a
    present node and its #chunk- hit collapse to ONE result keyed on the NODE id (not
    the chunk id). Fails-before: without the has_node filter the ghost is served."""
    s = CognitionStorage(tmp_path / "cog")
    s.add_node(_node("live0001", CognitionNodeType.DECISION))
    hits = [
        {"_id": "live0001", "entity_type": "decision", "summary": "kept"},
        {"_id": "ghost001", "entity_type": "decision", "summary": "DELETED — must not surface"},
        {"_id": "live0001#chunk-3", "entity_type": "decision", "summary": "chunk of a live node"},
    ]
    out, _ = _format_search_results(hits, s, limit=10)
    ids = [h["id"] for h in out]
    assert ids == ["live0001"], f"expected one deduped node id, got {ids}"
    assert "ghost001" not in ids, "ghost (graph-absent) hit served — N1 fix failed"


def test_reconcile_orphan_sweep_removes_only_graph_absent(tmp_path):
    """N1 startup reclamation (§9 N1b): the sweep deletes Chroma ids absent from the
    graph (incl. #chunk-*), KEEPS present ones (the ordering guard — a present node
    is never swept), and is a no-op on an empty collection (the ids=[] raise guard)."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")

    # No-op safe on an empty collection (the all_ids guard; never raises).
    _reconcile_orphan_embeddings(s, embed)

    s.add_node(_node("live0002", CognitionNodeType.DECISION))
    embed.upsert_embedding("live0002", [0.1, 0.2, 0.3], {"entity_type": "decision"})
    embed.upsert_embedding("live0002#chunk-0", [0.4, 0.5, 0.6],
                           {"node_id": "live0002", "entity_type": "decision", "is_chunk": True})
    embed.upsert_embedding("ghost002", [0.7, 0.8, 0.9], {"entity_type": "decision"})

    _reconcile_orphan_embeddings(s, embed)

    remaining = set(embed._collection.get()["ids"])
    assert "ghost002" not in remaining, "orphan vector not swept"
    assert remaining == {"live0002", "live0002#chunk-0"}, (
        f"sweep removed a present node or its chunk (ordering guard failed): {remaining}"
    )


# --- WP-D1b composition review (rule 11): matcher x dedup x deletion -----------


def test_composition_copy_twins_matcher_dedup_deletion(tmp_path):
    """force_new copy twins share ONE blob AND one doc: ref (same content → same
    sha → same doc_ref). A citing entity links part_of BOTH twins; deleting one
    cascades only its own edge and keeps the shared blob/sidecar; deleting the last
    reclaims them. Composes the matcher pair rules, force_new dedup, and per-blob
    refcount delete in one scenario."""
    s = CognitionStorage(tmp_path / "cog")
    a = _store_document(s, title="d", document_text="x", context="", author="t",
                        content_text="dup", store_copy=True, local_only=True)
    b = _store_document(s, title="d", document_text="x", context="", author="t",
                        content_text="dup", store_copy=True, local_only=True, force_new=True)
    sha = _meta_sha(s, a["node_id"])
    bp = blob_path(s.cognition_dir, sha, "")
    sidecar = text_sidecar_path(s.cognition_dir, sha)
    # An entity citing the shared doc_ref links part_of BOTH document twins.
    s.add_node(_node("dec00300", CognitionNodeType.DECISION, refs=[a["doc_ref"]]))
    s.create_deterministic_edges("dec00300")
    g = s.graph
    assert g.has_edge("dec00300", a["node_id"]) and g.has_edge("dec00300", b["node_id"]), (
        "entity did not link part_of both content-identical twins"
    )

    delete_cognition_node(s, _NoopEmbed(), a["node_id"])
    assert not g.has_edge("dec00300", a["node_id"]), "deleted twin's edge not cascaded"
    assert g.has_edge("dec00300", b["node_id"]), "surviving twin's edge wrongly removed"
    assert bp.exists() and sidecar.exists(), "shared blob/sidecar reclaimed while a twin remains"

    delete_cognition_node(s, _NoopEmbed(), b["node_id"])
    assert not bp.exists() and not sidecar.exists(), "blob/sidecar not reclaimed after last twin"
    assert not gitignore_has_entry(s.cognition_dir, a["blob_path"])
    assert s.has_node("dec00300"), "citing entity wrongly removed"


def test_all_artifact_classes_share_one_delete_path(tmp_path):
    """Vince/Reginald aspiration (ledger 11): every server-written artifact class —
    text sidecar, content-addressed blob, chunk/node vectors — is reclaimed by the
    ONE delete_cognition_node path, each keyed on the creator's identity predicate
    (sidecar+blob via documents_with_sha; vectors via node_id). Deleting the sole
    node leaves NO managed artifact of any class behind."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    res = _store_document(s, title="d", document_text="extracted", context="", author="t",
                          content_text="bytes", store_copy=True, local_only=True)
    sha = _meta_sha(s, res["node_id"])
    sidecar = text_sidecar_path(s.cognition_dir, sha)
    bp = documents_dir(s.cognition_dir) / res["blob_path"]
    # Seed the vectors D2 will write: a node vector + a node_id-tagged chunk.
    embed.upsert_embedding(res["node_id"], [0.1, 0.2, 0.3], {"entity_type": "document"})
    embed.upsert_embedding(f"{res['node_id']}#chunk-0", [0.4, 0.5, 0.6],
                           {"node_id": res["node_id"], "entity_type": "document", "is_chunk": True},
                           document="chunk body")
    assert sidecar.exists() and bp.exists()
    assert set(embed._collection.get()["ids"]) == {res["node_id"], f"{res['node_id']}#chunk-0"}

    delete_cognition_node(s, embed, res["node_id"])
    assert not sidecar.exists(), "sidecar (artifact class 1) not reclaimed"
    assert not bp.exists(), "blob (artifact class 2) not reclaimed"
    assert not gitignore_has_entry(s.cognition_dir, res["blob_path"]), ".gitignore line not reclaimed"
    assert embed._collection.get()["ids"] == [], "node/chunk vectors (artifact class 3) not reclaimed"


class _FixedGen:
    """Generator stub returning a fixed query embedding (so a real ChromaDB
    vector_search deterministically returns the seeded vector). Counts calls so a
    test can prove re-sync is idempotent (no re-embedding work)."""

    def __init__(self, vec):
        self._vec = vec
        self.calls = 0

    def generate(self, text, input_type="document"):
        self.calls += 1
        return self._vec

    def generate_query_embedding(self, text):
        return self.generate(text, input_type="query")


def test_n1_cross_process_ghost_filtered_end_to_end(tmp_path):
    """N1 END-TO-END (§9, the don't-serve-deleted-content guarantee): a node deleted
    on machine A replays as a remove_node tombstone into B's graph but is NEVER
    un-embedded in B's Chroma. The REAL search path (_search_cognition: vector_search
    -> has_node filter, the same core cognition_search calls) must return NOTHING for
    it. Two CognitionStorage over ONE journal + INDEPENDENT real ChromaDB + real
    cross-process replay — not a hand-built hits list."""
    cog = tmp_path / "cog"
    vec = [0.11, 0.22, 0.33]
    gen = _FixedGen(vec)
    embed_b = ChromaDBStorage(persist_directory=tmp_path / "chromaB")

    # A records a normal node into the shared journal.
    stor_a = CognitionStorage(cog)
    stor_a.add_node(_node("ghostnode", CognitionNodeType.DECISION, summary="secret deleted decision"))

    # B replays the add and embeds it into B's OWN Chroma (the embedding lands).
    stor_b = CognitionStorage(cog)
    embed_b.upsert_embedding("ghostnode", vec, {"entity_type": "decision", "summary": "x"})
    assert stor_b.has_node("ghostnode")
    gen_c = cast(EmbeddingGenerator, gen)
    assert _search_cognition(stor_b, embed_b, gen_c, "find it")["count"] == 1, (
        "precondition: a live node's embedding is searchable"
    )

    # A deletes the node (journal tombstone). B replays it (graph-only) — B's Chroma
    # STILL holds the vector (remove_node never un-embeds; the sync only ADDS).
    stor_a.remove_node("ghostnode")
    stor_b2 = CognitionStorage(cog)  # fresh replay picks up the tombstone
    assert not stor_b2.has_node("ghostnode"), "tombstone did not replay into B's graph"

    res = _search_cognition(stor_b2, embed_b, gen_c, "find it")
    assert res["count"] == 0, "cross-process ghost served by the real search path (N1 fix not wired)"


def test_store_document_embeds_node_and_chunks(tmp_path):
    """WP-D2 Commit 3: storing a document (with embedding deps) writes ONE node
    vector (no is_chunk) + N chunk vectors (is_chunk True, node_id set, chunk text
    stored). The node-vs-chunk marker is the count-split discriminator (A1)."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    gen = cast(EmbeddingGenerator, _FixedGen([0.1, 0.2, 0.3]))
    text = " ".join(str(i) for i in range(2500))  # > 1000 words -> multiple chunks
    res = _store_document(s, title="big doc", document_text=text, context="", author="t",
                          content_text=text, embedding_storage=embed, generator=gen)
    nid = res["node_id"]
    ids = set(embed._collection.get()["ids"])
    assert nid in ids, "document node vector not embedded"
    chunk_ids = {i for i in ids if i.startswith(f"{nid}#chunk-")}
    assert len(chunk_ids) >= 2, f"sidecar not chunked into multiple chunks: {chunk_ids}"

    node_meta = (embed._collection.get(ids=[nid], include=["metadatas"])["metadatas"] or [{}])[0]
    assert "is_chunk" not in node_meta, "node vector wrongly marked is_chunk"
    chunk = embed._collection.get(ids=[f"{nid}#chunk-0"], include=["metadatas", "documents"])
    cmeta = (chunk["metadatas"] or [{}])[0]
    cdocs = chunk["documents"] or [""]
    assert cmeta["is_chunk"] is True, "chunk missing is_chunk marker (count-split breaks)"
    assert cmeta["node_id"] == nid
    assert cdocs[0], "chunk text not stored as a Chroma document"


def test_store_document_defers_embedding_when_deps_missing(tmp_path):
    """The skip-if-None guard / deferred path: with a generator absent, the store
    writes the node+sidecar but NO vectors (the next sync backfills) — never errors."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    res = _store_document(s, title="d", document_text="body text", context="", author="t",
                          content_text="body text", embedding_storage=embed, generator=None)
    assert s.has_node(res["node_id"]), "node not stored"
    assert embed._collection.get()["ids"] == [], "embedded despite missing generator (should defer)"


def test_copy_blob_refcount_is_per_blob_path_not_per_sha(tmp_path):
    """The deviation's motivating case (Vince should-fix #3): two copy nodes with the
    SAME bytes but DIFFERENT ext own DIFFERENT blob files. Deleting one unlinks ITS
    blob and leaves the sibling's. Fails-before a per-SHA refcount (which, seeing the
    sibling shares the sha, would skip the unlink and leak the deleted node's blob)."""
    s = CognitionStorage(tmp_path / "cog")
    a = _store_document(s, title="doc.pdf", document_text="x", context="", author="t",
                        content_text="dup", store_copy=True)
    b = _store_document(s, title="doc.txt", document_text="x", context="", author="t",
                        content_text="dup", store_copy=True, force_new=True)
    assert a["blob_path"] != b["blob_path"], "different ext did not yield different blob paths"
    bpa = documents_dir(s.cognition_dir) / a["blob_path"]
    bpb = documents_dir(s.cognition_dir) / b["blob_path"]
    assert bpa.exists() and bpb.exists()

    delete_cognition_node(s, _NoopEmbed(), a["node_id"])
    assert not bpa.exists(), "deleted node's own-ext blob not unlinked"
    assert bpb.exists(), "sibling's distinct-ext blob wrongly unlinked (per-sha refcount leak)"


def test_copy_promotion_survives_journal_replay(tmp_path):
    """Should-fix #4: a teammate's blob-refcount integrity depends on the promote
    (reference -> copy via update_node) round-tripping. Store reference, then
    store_copy the same bytes (dedup -> promote), replay in a 2nd instance, assert
    mode=='copy' + blob_path survived (else a puller can't refcount the blob)."""
    cog = tmp_path / "cog"
    s1 = CognitionStorage(cog)
    ref = _store_document(s1, title="d", document_text="x", context="", author="t",
                          content_text="bytes")
    promoted = _store_document(s1, title="d", document_text="x", context="", author="t",
                               content_text="bytes", store_copy=True)
    assert promoted["node_id"] == ref["node_id"] and promoted["mode"] == "copy", "promote did not hit"

    s2 = CognitionStorage(cog)  # fresh replay of add_node + update_node
    node = s2.get_node(ref["node_id"])
    assert node is not None
    assert node["metadata"]["mode"] == "copy", "promotion to copy lost across journal replay"
    assert node["metadata"]["blob_path"] == promoted["blob_path"], "blob_path lost across replay"


# --- WP-D2 Commit 5: search over-query + dedupe-to-best-hit-per-node + excerpt ---


def test_search_dedupes_chunks_to_one_node_with_excerpt(tmp_path):
    """Two chunks of one document + another node → the document comes back ONCE
    (best chunk) keyed on the NODE id, with matched_excerpt; the other node too."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_node("docaaaa1", CognitionNodeType.DOCUMENT, summary="doc A"))
    s.add_node(_node("decbbbb1", CognitionNodeType.DECISION, summary="decision B"))
    q = [1.0, 0.0, 0.0]
    embed.upsert_embedding("docaaaa1#chunk-0", [1.0, 0.0, 0.0],
                           {"node_id": "docaaaa1", "entity_type": "document", "is_chunk": True},
                           document="alpha chunk body")
    embed.upsert_embedding("docaaaa1#chunk-1", [0.99, 0.01, 0.0],
                           {"node_id": "docaaaa1", "entity_type": "document", "is_chunk": True},
                           document="beta chunk body")
    embed.upsert_embedding("decbbbb1", [0.8, 0.2, 0.0], {"entity_type": "decision"})
    gen = cast(EmbeddingGenerator, _FixedGen(q))

    res = _search_cognition(s, embed, gen, "anything", limit=10)
    ids = [h["id"] for h in res["results"]]
    assert ids.count("docaaaa1") == 1, f"document chunks not deduped to one node: {ids}"
    assert "decbbbb1" in ids, "other node missing"
    doc_hit = next(h for h in res["results"] if h["id"] == "docaaaa1")
    assert doc_hit["matched_excerpt"] == "alpha chunk body", "best-chunk excerpt not carried"


def test_adaptive_overquery_returns_distinct_nodes_past_starve_boundary(tmp_path):
    """B3 at the REAL boundary: a document with MORE than limit*k chunks (12 > 2*5),
    all out-ranking another live node, must STILL return 2 distinct nodes — the
    adaptive widen keeps querying past the initial window. A fixed limit*k=10 window
    would see only that doc's chunks and starve the second node (recall miss). The
    prior 8-chunk test passed by luck (8 < 10); this exercises the starve threshold."""
    s = CognitionStorage(tmp_path / "cog")
    embed = ChromaDBStorage(persist_directory=tmp_path / "chroma")
    s.add_node(_node("docaaaa2", CognitionNodeType.DOCUMENT, summary="doc A"))
    s.add_node(_node("decbbbb2", CognitionNodeType.DECISION, summary="decision B"))
    q = [1.0, 0.0, 0.0]
    for i in range(12):  # 12 chunks (> limit*k = 10) all at q, out-ranking decB
        embed.upsert_embedding(f"docaaaa2#chunk-{i}", [1.0, 0.0, 0.0],
                               {"node_id": "docaaaa2", "entity_type": "document", "is_chunk": True},
                               document=f"chunk {i}")
    embed.upsert_embedding("decbbbb2", [0.6, 0.4, 0.0], {"entity_type": "decision"})
    gen = cast(EmbeddingGenerator, _FixedGen(q))

    res = _search_cognition(s, embed, gen, "x", limit=2)
    ids = [h["id"] for h in res["results"]]
    assert set(ids) == {"docaaaa2", "decbbbb2"}, f"chunk flood starved distinct nodes: {ids}"


def test_deleted_document_all_chunk_hits_drop(tmp_path):
    """Composition (N1 x dedupe): a document deleted cross-process leaves multiple
    chunk vectors behind; EVERY chunk hit must drop (graph-absent) — none survives to
    serve verbatim deleted client text."""
    s = CognitionStorage(tmp_path / "cog")  # graph does NOT contain the document
    hits = [
        {"_id": "gonedoc1#chunk-0", "entity_type": "document", "matched_text": "secret 0"},
        {"_id": "gonedoc1#chunk-1", "entity_type": "document", "matched_text": "secret 1"},
    ]
    out, _ = _format_search_results(hits, s, limit=10)
    assert out == [], "a deleted document's chunk hits were served (N1xdedupe compose failed)"


# ── Orphaned document artifact discovery (WP-12, d999b4e3851a) ────────────────


def test_find_orphaned_artifacts_none_after_normal_store(tmp_path):
    """A normally-stored document's sidecar (and, in copy mode, blob) are owned
    by its node -- the sweep must report zero orphans."""
    s = CognitionStorage(tmp_path / "cog")
    _store_document(s, title="d", document_text="hello world", context="", author="t",
                     content_text="hello world", store_copy=True)
    orphans = find_orphaned_document_artifacts(s.cognition_dir, s)
    assert orphans == []


def test_find_orphaned_sidecar_with_no_owning_node(tmp_path):
    """A text sidecar written directly (simulating the write-before-journal crash
    window _store_document has) with no node citing its sha must be flagged.

    Fails-before: no scanner existed at all to find this class of file.
    """
    s = CognitionStorage(tmp_path / "cog")
    sha = sha256_bytes(b"orphaned content")
    sidecar = text_sidecar_path(s.cognition_dir, sha)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("orphaned content", encoding="utf-8")

    orphans = find_orphaned_document_artifacts(s.cognition_dir, s)

    assert len(orphans) == 1
    assert orphans[0].replace("\\", "/") == f"documents/text/{sha}.txt"


def test_find_orphaned_blob_with_no_owning_node(tmp_path):
    """A copy-mode blob with no node citing its sha must be flagged."""
    from vibe_cognition.cognition.documents import write_blob

    s = CognitionStorage(tmp_path / "cog")
    sha = sha256_bytes(b"orphan blob bytes")
    write_blob(s.cognition_dir, sha, ".txt", data=b"orphan blob bytes")

    orphans = find_orphaned_document_artifacts(s.cognition_dir, s)

    assert len(orphans) == 1
    assert orphans[0].replace("\\", "/") == f"documents/{sha[:2]}/{sha}.txt"


def test_find_orphaned_artifacts_ignores_non_content_addressed_files(tmp_path):
    """documents/.gitignore and other non-sha-named files must not be
    misidentified as orphaned blobs (they're shorter than a sha256 hexdigest,
    or live outside the shard-dir layout)."""
    s = CognitionStorage(tmp_path / "cog")
    _store_document(s, title="d", document_text="x", context="", author="t",
                     content_text="x", store_copy=True, local_only=True)
    # documents/.gitignore now exists (from the local_only store above) at the
    # documents/ root, not inside a 2-char shard dir -- must not crash or flag it.
    orphans = find_orphaned_document_artifacts(s.cognition_dir, s)
    assert orphans == []


def test_find_orphaned_artifacts_empty_when_no_documents_dir(tmp_path):
    """No documents/ directory at all (never stored anything) -- empty, no error."""
    s = CognitionStorage(tmp_path / "cog")
    assert find_orphaned_document_artifacts(s.cognition_dir, s) == []


# ── Cheap search staleness signal (WP-12, db65f1568fa5) ───────────────────────


def test_cheap_staleness_signal_no_path_is_none():
    """content_text-sourced documents have no path -- nothing to check."""
    assert cheap_staleness_signal({"size": 5}) is None


def test_cheap_staleness_signal_path_missing(tmp_path):
    gone = tmp_path / "does_not_exist.txt"
    assert cheap_staleness_signal({"path": str(gone), "size": 5}) == "path_missing"


def test_cheap_staleness_signal_size_changed(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("this is now much longer than five bytes", encoding="utf-8")
    assert cheap_staleness_signal({"path": str(p), "size": 5}) == "size_changed"


def test_cheap_staleness_signal_unchanged_size_returns_none(tmp_path):
    """Same size = no cheap signal -- NOT proof of no drift (a same-size edit
    is invisible to this check by design), but the honest "nothing detected" case."""
    p = tmp_path / "f.txt"
    p.write_text("exact5", encoding="utf-8")  # 6 bytes
    assert cheap_staleness_signal({"path": str(p), "size": 6}) is None


def test_search_results_document_hit_carries_staleness_when_path_missing(tmp_path):
    """WP-12 (db65f1568fa5): a search hit for a document whose referenced path
    no longer exists carries "staleness": "path_missing" -- surfaced without a
    full re-hash (unlike cognition_get_document's freshness field).

    Fails-before: no scanner existed at all to find this class of file; search
    results never carried a staleness key.
    """
    s = CognitionStorage(tmp_path / "cog")
    gone_path = str(tmp_path / "gone.txt")
    node = CognitionNode(
        id="doc1", type=CognitionNodeType.DOCUMENT, summary="a doc", detail="d",
        context=[], references=[], timestamp="2026-06-23T00:00:00+00:00", author="t",
        metadata={"path": gone_path, "size": 10, "sha256": "abc123"},
    )
    s.add_node(node)

    hits = [{"_id": "doc1", "entity_type": "document", "summary": "a doc"}]
    out, _ = _format_search_results(hits, s, limit=10)

    assert len(out) == 1
    assert out[0]["staleness"] == "path_missing"


def test_search_results_non_document_hit_has_no_staleness_key(tmp_path):
    """A non-document hit never gets a staleness key at all (not even None) --
    keeps the common-case result shape unbloated."""
    s = CognitionStorage(tmp_path / "cog")
    s.add_node(_node("dec1", CognitionNodeType.DECISION, summary="a decision"))

    hits = [{"_id": "dec1", "entity_type": "decision", "summary": "a decision"}]
    out, _ = _format_search_results(hits, s, limit=10)

    assert len(out) == 1
    assert "staleness" not in out[0]


def test_search_results_document_hit_no_staleness_key_when_unchanged(tmp_path):
    """A document whose path is unchanged (or content_text-sourced, no path at
    all) gets no staleness key -- only a POSITIVE cheap finding is surfaced."""
    s = CognitionStorage(tmp_path / "cog")
    node = CognitionNode(
        id="doc1", type=CognitionNodeType.DOCUMENT, summary="a doc", detail="d",
        context=[], references=[], timestamp="2026-06-23T00:00:00+00:00", author="t",
        metadata={"sha256": "abc123"},  # no "path" -- content_text-sourced
    )
    s.add_node(node)

    hits = [{"_id": "doc1", "entity_type": "document", "summary": "a doc"}]
    out, _ = _format_search_results(hits, s, limit=10)

    assert len(out) == 1
    assert "staleness" not in out[0]
