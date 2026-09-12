"""Identity resolution, the write gate, SVN credential reading, and the remap CLI."""

import json
from pathlib import Path

import pytest

from vibe_cognition.cognition.identity import (
    IDENTITY_FILENAME,
    SOURCE_CONFIRMED,
    SOURCE_GIT,
    SOURCE_OS_USER,
    SOURCE_SVN,
    identity_suggestions,
    read_confirmed_identity,
    require_identity,
    resolve_identity,
    write_confirmed_identity,
)
from vibe_cognition.cognition.svn_identity import (
    _looks_like_email,
    _parse_hash_dump,
    is_svn_working_copy,
    resolve_svn_identity,
    svn_username_candidates,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A project with no git and no SVN identity reachable."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-such-gitconfig"))
    monkeypatch.setenv("SVN_CONFIG_DIR", str(tmp_path / "no-such-svn"))
    cognition = tmp_path / ".cognition"
    cognition.mkdir()
    return tmp_path, cognition


def _write_git_config(path: Path, name: str, email: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"[user]\n\tname = {name}\n\temail = {email}\n", encoding="utf-8")


def _write_svn_credential(config_dir: Path, username: str, realm: str = "<https://x> R") -> Path:
    d = config_dir / "auth" / "svn.simple"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "deadbeef"
    p.write_text(
        f"K 15\nsvn:realmstring\nV {len(realm)}\n{realm}\n"
        f"K 8\nusername\nV {len(username)}\n{username}\nEND\n",
        encoding="utf-8",
    )
    return p


# ── svn_identity ──────────────────────────────────────────────────────────────


def test_parse_hash_dump_extracts_username_and_realm():
    text = "K 8\nusername\nV 6\njsmith\nK 15\nsvn:realmstring\nV 3\nfoo\nEND\n"
    assert _parse_hash_dump(text) == {"username": "jsmith", "svn:realmstring": "foo"}


def test_parse_hash_dump_tolerates_garbage():
    assert _parse_hash_dump("nonsense\nK 4\nonly-a-key\n") == {}
    assert _parse_hash_dump("") == {}


def test_parse_hash_dump_stops_at_end_marker():
    text = "K 8\nusername\nV 6\njsmith\nEND\nK 8\nusername\nV 6\nlater!\n"
    assert _parse_hash_dump(text)["username"] == "jsmith"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a@b.com", True),
        ("first.last@sub.example.co.uk", True),
        ("jsmith", False),
        ("a@b", False),
        ("a b@c.com", False),
        ("a@@b.com", False),
        ("@b.com", False),
        ("a@.com", False),
        ("a@b.", False),
    ],
)
def test_looks_like_email(value, expected):
    assert _looks_like_email(value) is expected


def test_svn_candidates_empty_when_no_config(repo):
    assert svn_username_candidates() == []


def test_svn_candidates_reads_cached_username(repo, monkeypatch, tmp_path):
    cfg = tmp_path / "svnconf"
    _write_svn_credential(cfg, "jsmith@example.com")
    monkeypatch.setenv("SVN_CONFIG_DIR", str(cfg))
    got = svn_username_candidates()
    assert [c["username"] for c in got] == ["jsmith@example.com"]


def test_resolve_svn_identity_requires_a_working_copy(repo, monkeypatch, tmp_path):
    root, _ = repo
    cfg = tmp_path / "svnconf"
    _write_svn_credential(cfg, "jsmith@example.com")
    monkeypatch.setenv("SVN_CONFIG_DIR", str(cfg))
    # Credential exists, but this dir is not an SVN working copy.
    assert resolve_svn_identity(root) == {"name": "", "email": ""}
    (root / ".svn").mkdir()
    assert resolve_svn_identity(root)["email"] == "jsmith@example.com"


def test_resolve_svn_identity_bare_login_yields_no_email(repo, monkeypatch, tmp_path):
    """A non-email SVN login must NOT be turned into an address."""
    root, _ = repo
    (root / ".svn").mkdir()
    cfg = tmp_path / "svnconf"
    _write_svn_credential(cfg, "jsmith")
    monkeypatch.setenv("SVN_CONFIG_DIR", str(cfg))
    assert resolve_svn_identity(root) == {"name": "jsmith", "email": ""}


def test_is_svn_working_copy(repo):
    root, _ = repo
    assert not is_svn_working_copy(root)
    (root / ".svn").mkdir()
    assert is_svn_working_copy(root)


# ── resolution order ──────────────────────────────────────────────────────────


def test_resolve_falls_back_to_os_user_with_no_email(repo):
    root, cognition = repo
    ident = resolve_identity(root, cognition)
    assert ident["email"] == ""
    assert ident["source"] == SOURCE_OS_USER
    assert ident["confirmed"] is False


def test_resolve_prefers_git_over_svn(repo, monkeypatch, tmp_path):
    root, cognition = repo
    _write_git_config(tmp_path / "gitconfig", "Git Name", "git@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (root / ".svn").mkdir()
    cfg = tmp_path / "svnconf"
    _write_svn_credential(cfg, "svn@example.com")
    monkeypatch.setenv("SVN_CONFIG_DIR", str(cfg))

    ident = resolve_identity(root, cognition)
    assert ident["email"] == "git@example.com"
    assert ident["source"] == SOURCE_GIT


def test_svn_is_suggestion_only_and_never_stamps_a_write(repo, monkeypatch, tmp_path):
    """SVN credentials must NOT become the acting identity.

    The auth cache is machine-wide and realm-keyed, and no realm-to-working-copy
    correlation is attempted, so trusting it would let another project's
    credential silently attribute this repo's history. The address is offered as
    a suggestion and the write stays blocked until a human confirms.
    """
    root, cognition = repo
    (root / ".svn").mkdir()
    cfg = tmp_path / "svnconf"
    _write_svn_credential(cfg, "svn@example.com")
    monkeypatch.setenv("SVN_CONFIG_DIR", str(cfg))

    ident = resolve_identity(root, cognition)
    assert ident["email"] == "", "an SVN credential must never stamp a write"
    assert ident["source"] == SOURCE_OS_USER

    assert require_identity(root, cognition) is not None
    assert [s["email"] for s in identity_suggestions(root, cognition)] == ["svn@example.com"]


def test_confirmed_identity_beats_every_vcs_source(repo, monkeypatch, tmp_path):
    root, cognition = repo
    _write_git_config(tmp_path / "gitconfig", "Git Name", "git@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))

    write_confirmed_identity(cognition, "Real Person", "Real@Example.COM")
    ident = resolve_identity(root, cognition)
    assert ident == {
        "name": "Real Person",
        "email": "real@example.com",
        "source": SOURCE_CONFIRMED,
        "confirmed": True,
    }


# ── confirmed file ────────────────────────────────────────────────────────────


def test_write_confirmed_identity_rejects_bad_input(repo):
    _, cognition = repo
    assert "error" in write_confirmed_identity(cognition, "", "a@b.com")
    assert "error" in write_confirmed_identity(cognition, "Name", "not-an-email")
    assert not (cognition / IDENTITY_FILENAME).exists()


def test_write_confirmed_identity_casefolds_and_persists(repo):
    _, cognition = repo
    write_confirmed_identity(cognition, "  Ada  ", "  ADA@Example.com ")
    data = json.loads((cognition / IDENTITY_FILENAME).read_text(encoding="utf-8"))
    assert data == {"name": "Ada", "email": "ada@example.com"}
    assert read_confirmed_identity(cognition) == data


def test_corrupt_identity_file_is_ignored_not_fatal(repo):
    _, cognition = repo
    (cognition / IDENTITY_FILENAME).write_text("{not json", encoding="utf-8")
    assert read_confirmed_identity(cognition) is None


def test_identity_file_missing_fields_is_ignored(repo):
    _, cognition = repo
    (cognition / IDENTITY_FILENAME).write_text('{"name": "x"}', encoding="utf-8")
    assert read_confirmed_identity(cognition) is None


# ── the write gate ────────────────────────────────────────────────────────────


def test_gate_blocks_when_no_email_resolves(repo):
    root, cognition = repo
    err = require_identity(root, cognition)
    assert err is not None
    assert err["identity_required"] is True
    assert "cognition_set_identity" in err["error"]


def test_gate_allows_unconfirmed_git_identity(repo, monkeypatch, tmp_path):
    """An existing install with a working git email must NOT break on upgrade."""
    root, cognition = repo
    _write_git_config(tmp_path / "gitconfig", "Git Name", "git@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    assert require_identity(root, cognition) is None


def test_gate_allows_after_set_identity(repo):
    root, cognition = repo
    assert require_identity(root, cognition) is not None
    write_confirmed_identity(cognition, "Ada", "ada@example.com")
    assert require_identity(root, cognition) is None


def test_gate_error_offers_svn_candidate_without_assuming_it(repo, monkeypatch, tmp_path):
    root, cognition = repo
    (root / ".svn").mkdir()
    cfg = tmp_path / "svnconf"
    _write_svn_credential(cfg, "jsmith")  # bare login: not a usable email
    monkeypatch.setenv("SVN_CONFIG_DIR", str(cfg))

    err = require_identity(root, cognition)
    assert err is not None, "a bare SVN login is not an email and must still block"
    assert any(s["source"] == SOURCE_SVN for s in err["suggestions"])
    assert "jsmith" in err["error"]


def test_suggestions_are_deduped_and_sourced(repo, monkeypatch, tmp_path):
    root, cognition = repo
    _write_git_config(tmp_path / "gitconfig", "Git Name", "shared@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (root / ".svn").mkdir()
    cfg = tmp_path / "svnconf"
    _write_svn_credential(cfg, "shared@example.com")
    monkeypatch.setenv("SVN_CONFIG_DIR", str(cfg))

    got = identity_suggestions(root, cognition)
    assert [s["email"] for s in got] == ["shared@example.com"]
    assert got[0]["source"] == SOURCE_GIT


# ── remap CLI ─────────────────────────────────────────────────────────────────


def _storage(cognition):
    from vibe_cognition.cognition.storage import CognitionStorage

    return CognitionStorage(cognition)


def test_remap_plan_finds_only_matching_attribution(repo):
    from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
    from vibe_cognition.remap_identity import plan_remap

    _, cognition = repo
    st = _storage(cognition)
    for i, email in enumerate(("old@example.com", "other@example.com")):
        st.add_node(CognitionNode(
            id=f"node{i}", author="t", detail="", timestamp="2026-01-01T00:00:00Z",
            type=CognitionNodeType.DISCOVERY, summary=f"n {email}",
            metadata={"recorded_by": {"name": "X", "email": email}},
        ))
    planned = plan_remap(st, "old@example.com", "new@example.com")
    assert len(planned) == 1
    assert planned[0]["keys"] == ["recorded_by"]


def test_remap_apply_rewrites_and_preserves_original(repo):
    from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
    from vibe_cognition.remap_identity import apply_remap

    _, cognition = repo
    st = _storage(cognition)
    node = st.add_node(CognitionNode(
        id="n1", author="t", detail="", timestamp="2026-01-01T00:00:00Z", type=CognitionNodeType.DECISION, summary="d",
        metadata={"recorded_by": {"name": "Old", "email": "old@example.com"}},
    ))
    nid = node["id"] if isinstance(node, dict) else node

    assert apply_remap(st, "old@example.com", "new@example.com", "New Name") == 1
    rb = st.get_node(nid)["metadata"]["recorded_by"]
    assert rb["email"] == "new@example.com"
    assert rb["name"] == "New Name"
    assert rb["remapped_from"] == "old@example.com"


def test_remap_is_case_insensitive_on_the_source_address(repo):
    from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
    from vibe_cognition.remap_identity import plan_remap

    _, cognition = repo
    st = _storage(cognition)
    st.add_node(CognitionNode(
        id="n1", author="t", detail="", timestamp="2026-01-01T00:00:00Z", type=CognitionNodeType.PATTERN, summary="p",
        metadata={"recorded_by": {"name": "X", "email": "Mixed@Example.COM"}},
    ))
    assert len(plan_remap(st, "mixed@example.com", "new@example.com")) == 1


def test_remap_cli_dry_run_writes_nothing(repo, capsys):
    from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
    from vibe_cognition.remap_identity import main

    root, cognition = repo
    st = _storage(cognition)
    st.add_node(CognitionNode(
        id="n1", author="t", detail="", timestamp="2026-01-01T00:00:00Z", type=CognitionNodeType.DISCOVERY, summary="d",
        metadata={"recorded_by": {"name": "X", "email": "old@example.com"}},
    ))
    before = (cognition / "journal.jsonl").read_bytes()

    rc = main([str(root), "--from", "old@example.com", "--to", "new@example.com"])
    assert rc == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert (cognition / "journal.jsonl").read_bytes() == before


def test_remap_cli_rejects_identical_addresses(repo):
    from vibe_cognition.remap_identity import main

    root, _ = repo
    assert main([str(root), "--from", "a@b.com", "--to", "A@B.com"]) == 2


# ── gate integration across every write tool ─────────────────────────────────


WRITE_TOOLS = [
    ("cognition_record", {"node_type": "decision", "summary": "s", "detail": "d", "context": "", "author": "t"}),
    ("cognition_add_task", {"summary": "s", "detail": "d", "context": ""}),
    ("cognition_store_document",
     {"title": "t", "document_text": "c", "content_text": "c", "context": "", "author": "a"}),
    ("cognition_register_person",
     {"name": "n", "role": "r", "seniority": "mid", "email": "someone@example.com"}),
]


@pytest.mark.parametrize("tool_name,kwargs", WRITE_TOOLS, ids=[t for t, _ in WRITE_TOOLS])
def test_every_write_tool_refuses_without_identity(
    tool_name, kwargs, tmp_path, mock_mcp, build_lc, make_ctx, monkeypatch
):
    """No write path may stamp attribution when no identity resolves.

    Parametrized deliberately: cognition_register_person shipped ungated because
    the single-tool test could not see it.

    cognition_update_person is absent by necessity, not oversight: it returns
    "no person found" before reaching the gate, and seeding a person first needs
    a write that is itself gated. No write happens on that path either way.
    """
    from vibe_cognition.tools.cognition_tools import register_cognition_tools

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "nope"))
    monkeypatch.setenv("SVN_CONFIG_DIR", str(tmp_path / "nope-svn"))
    monkeypatch.setattr("vibe_cognition.cognition.git_identity.getpass.getuser", lambda: "x")
    # NB: patching git_identity.resolve_git_identity would be dead code here --
    # identity.py did `from .git_identity import resolve_git_identity`, binding its
    # own name at import. The env redirection above is what makes the email empty.

    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    result = mock_mcp.tools[tool_name](ctx, **kwargs)
    assert result.get("identity_required") is True, (tool_name, result)
    assert lc["cognition_storage"].get_all_nodes() == [], tool_name
