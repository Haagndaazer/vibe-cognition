"""Task 48359648a816: a document's source path never enters the committed journal
as an absolute machine path."""

import json

from vibe_cognition.cognition.documents import (
    SOURCES_FILENAME,
    cheap_staleness_signal,
    freshness_by_rehash,
    portable_source_path,
    source_file,
)
from vibe_cognition.cognition.local_paths import read_path
from vibe_cognition.cognition.operations import delete_cognition_node
from vibe_cognition.cognition.storage import CognitionStorage
from vibe_cognition.tools.cognition_tools import _get_document, _store_document


class _NoopEmbed:
    def delete_embedding(self, node_id):
        return True

    def delete_by_node_id(self, node_id):
        pass


def _project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root, CognitionStorage(root / ".cognition")


def _committed_text(cognition_dir):
    return "\n".join(
        f.read_text(encoding="utf-8")
        for f in cognition_dir.rglob("*")
        if f.is_file() and "local" not in f.relative_to(cognition_dir).parts
    )


def _store(s, path, text="t", **kw):
    return _store_document(s, title="d", document_text=text, context="", author="t",
                           file_path=str(path), **kw)


def test_file_inside_the_project_is_stored_project_relative(tmp_path):
    root, s = _project(tmp_path)
    doc = root / "docs" / "spec.md"
    doc.parent.mkdir()
    doc.write_bytes(b"spec")

    res = _store(s, doc)
    got = _get_document(s, node_id=res["node_id"])

    assert got["metadata"]["path"] == "docs/spec.md"
    assert got["path"] == str(doc.resolve())
    assert got["freshness"] == "unchanged"
    assert str(tmp_path) not in _committed_text(s.cognition_dir)
    assert str(tmp_path).replace("\\", "\\\\") not in _committed_text(s.cognition_dir)


def test_file_outside_the_project_leaves_no_path_in_committed_files(tmp_path):
    root, s = _project(tmp_path)
    outside = tmp_path / "elsewhere" / "client.pdf"
    outside.parent.mkdir()
    outside.write_bytes(b"client bytes")

    res = _store(s, outside)
    node = s.get_node(res["node_id"])

    assert "path" not in node["metadata"]
    committed = _committed_text(s.cognition_dir)
    assert "elsewhere" not in committed
    assert str(tmp_path).replace("\\", "\\\\") not in committed
    local = json.loads(read_path(s.cognition_dir, SOURCES_FILENAME).read_text(encoding="utf-8"))
    assert local == {res["node_id"]: str(outside.resolve())}
    assert read_path(s.cognition_dir, SOURCES_FILENAME).parent.name == "local"


def test_outside_file_still_gets_freshness_and_staleness_on_this_machine(tmp_path):
    root, s = _project(tmp_path)
    outside = tmp_path / "client.txt"
    outside.write_bytes(b"original")
    res = _store(s, outside)
    nid = res["node_id"]
    meta = s.get_node(nid)["metadata"]

    assert _get_document(s, node_id=nid)["path"] == str(outside.resolve())
    assert _get_document(s, node_id=nid)["freshness"] == "unchanged"
    outside.write_bytes(b"changed and longer")
    assert _get_document(s, node_id=nid)["freshness"] == "modified"
    assert cheap_staleness_signal(meta, s.cognition_dir, nid) == "size_changed"
    outside.unlink()
    assert _get_document(s, node_id=nid)["freshness"] == "missing"
    assert cheap_staleness_signal(meta, s.cognition_dir, nid) == "path_missing"


def test_outside_file_is_unknown_on_another_machine(tmp_path):
    root, s = _project(tmp_path)
    outside = tmp_path / "client.txt"
    outside.write_bytes(b"original")
    res = _store(s, outside)
    read_path(s.cognition_dir, SOURCES_FILENAME).unlink()

    got = _get_document(s, node_id=res["node_id"])
    assert got["path"] is None
    assert got["freshness"] == "unchanged"
    assert cheap_staleness_signal(got["metadata"], s.cognition_dir, res["node_id"]) is None


def test_restore_of_a_changed_outside_file_still_offers_the_prior_version(tmp_path):
    root, s = _project(tmp_path)
    outside = tmp_path / "client.txt"
    outside.write_bytes(b"one")
    first = _store(s, outside, text="v1")
    outside.write_bytes(b"two, different")
    second = _store(s, outside, text="v2")

    assert second["prior_version_id"] == first["node_id"]


def test_restore_over_a_legacy_absolute_path_still_offers_the_prior_version(tmp_path):
    root, s = _project(tmp_path)
    doc = root / "f.txt"
    doc.write_bytes(b"one")
    first = _store(s, doc, text="v1")
    meta = s.get_node(first["node_id"])["metadata"]
    s.update_node(first["node_id"], metadata={**meta, "path": str(doc.resolve())})
    doc.write_bytes(b"two, different")

    second = _store(s, doc, text="v2")

    assert second["prior_version_id"] == first["node_id"]
    assert s.get_node(second["node_id"])["metadata"]["path"] == "f.txt"


def test_legacy_absolute_path_is_still_checked(tmp_path):
    f = tmp_path / "old.txt"
    f.write_bytes(b"12345")
    meta = {"path": str(f), "size": 5, "sha256": "not-the-sha"}

    assert source_file(tmp_path / ".cognition", "n", meta) == f
    assert freshness_by_rehash(meta) == "modified"
    assert cheap_staleness_signal(meta) is None


def test_relative_path_that_climbs_out_of_the_project_is_ignored(tmp_path):
    root, s = _project(tmp_path)
    (tmp_path / "secret.txt").write_bytes(b"x")
    meta = {"path": "../secret.txt", "size": 1, "sha256": "abc"}

    assert source_file(s.cognition_dir, "n", meta) is None
    assert freshness_by_rehash(meta, s.cognition_dir, "n") == "unchanged"
    assert cheap_staleness_signal(meta, s.cognition_dir, "n") is None


def test_posix_absolute_path_from_another_machine_is_not_joined_to_the_project(tmp_path):
    root, s = _project(tmp_path)
    meta = {"path": "/home/someone/file.md", "size": 1, "sha256": "abc"}

    src = source_file(s.cognition_dir, "n", meta)
    assert src is not None
    assert not str(src).startswith(str(root.resolve()))


def test_portable_source_path_rejects_a_sibling_folder_with_a_shared_prefix(tmp_path):
    root, s = _project(tmp_path)
    sibling = tmp_path / "project-old" / "f.md"
    sibling.parent.mkdir()
    sibling.write_bytes(b"x")

    assert portable_source_path(s.cognition_dir, sibling) is None


def test_deleting_the_document_forgets_its_local_source(tmp_path):
    root, s = _project(tmp_path)
    outside = tmp_path / "client.txt"
    outside.write_bytes(b"original")
    res = _store(s, outside)

    delete_cognition_node(s, _NoopEmbed(), res["node_id"])

    local = json.loads(read_path(s.cognition_dir, SOURCES_FILENAME).read_text(encoding="utf-8"))
    assert local == {}
    assert outside.exists()


def test_copy_mode_outside_file_also_keeps_the_path_out(tmp_path):
    root, s = _project(tmp_path)
    outside = tmp_path / "elsewhere" / "client.pdf"
    outside.parent.mkdir()
    outside.write_bytes(b"client bytes")

    res = _store(s, outside, store_copy=True)

    assert "path" not in s.get_node(res["node_id"])["metadata"]
    assert "elsewhere" not in _committed_text(s.cognition_dir)


def test_a_filename_with_a_colon_is_not_mistaken_for_a_drive_path(tmp_path):
    root, s = _project(tmp_path)
    meta = {"path": "a:b.md", "size": 1, "sha256": "abc"}

    assert source_file(s.cognition_dir, "n", meta) == s.cognition_dir.resolve().parent / "a:b.md"


def test_a_corrupt_local_sources_file_does_not_break_the_store(tmp_path):
    root, s = _project(tmp_path)
    outside = tmp_path / "client.txt"
    outside.write_bytes(b"original")
    target = s.cognition_dir / "local" / SOURCES_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")

    res = _store(s, outside)

    assert _get_document(s, node_id=res["node_id"])["freshness"] == "unchanged"


def test_a_failed_local_write_does_not_fail_the_store(tmp_path, monkeypatch):
    import vibe_cognition.cognition.documents as documents

    def boom(*a, **k):
        raise OSError("read-only checkout")

    monkeypatch.setattr(documents, "_write_local_sources", boom)
    root, s = _project(tmp_path)
    outside = tmp_path / "client.txt"
    outside.write_bytes(b"original")

    res = _store(s, outside)

    assert "node_id" in res
    assert "path" not in s.get_node(res["node_id"])["metadata"]


def test_an_already_stored_document_gains_no_path(tmp_path):
    root, s = _project(tmp_path)
    outside = tmp_path / "client.txt"
    outside.write_bytes(b"original")
    first = _store(s, outside)

    again = _store(s, outside, store_copy=True)

    assert again["already_stored"] is True
    assert "path" not in s.get_node(first["node_id"])["metadata"]
