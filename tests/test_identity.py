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
from vibe_cognition.cognition.local_paths import read_path as local_read_path
from vibe_cognition.cognition.local_paths import write_path as local_write_path
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
    assert not local_read_path(cognition, IDENTITY_FILENAME).exists()


def test_write_confirmed_identity_casefolds_and_persists(repo):
    _, cognition = repo
    write_confirmed_identity(cognition, "  Ada  ", "  ADA@Example.com ")
    data = json.loads(local_read_path(cognition, IDENTITY_FILENAME).read_text(encoding="utf-8"))
    assert data == {"name": "Ada", "email": "ada@example.com"}
    assert read_confirmed_identity(cognition) == data


def test_corrupt_identity_file_is_ignored_not_fatal(repo):
    _, cognition = repo
    local_write_path(cognition, IDENTITY_FILENAME).write_text("{not json", encoding="utf-8")
    assert read_confirmed_identity(cognition) is None


def test_identity_file_missing_fields_is_ignored(repo):
    _, cognition = repo
    local_write_path(cognition, IDENTITY_FILENAME).write_text('{"name": "x"}', encoding="utf-8")
    assert read_confirmed_identity(cognition) is None


# ── the write gate ────────────────────────────────────────────────────────────


def test_gate_blocks_when_no_email_resolves(repo):
    root, cognition = repo
    err = require_identity(root, cognition)
    assert err is not None
    assert err["identity_required"] is True
    assert "cognition_set_identity" in err["error"]


def test_gate_refuses_unconfirmed_git_identity_and_offers_it_as_a_candidate(
    repo, monkeypatch, tmp_path
):
    """CONTRACT CHANGE (WP-Identity-Profiles): a working git email is no longer
    enough. v0.37.0 let it through so existing installs would not break on
    upgrade; that readmits the shared-build-account case this module exists to
    close -- several humans on one machine, every write stamped with whatever
    address sits in git config, indistinguishably.

    The git identity is still SUGGESTED, so confirming it is one answer away.
    """
    root, cognition = repo
    _write_git_config(tmp_path / "gitconfig", "Git Name", "git@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))

    err = require_identity(root, cognition)
    assert err is not None
    assert err["identity_required"] is True
    assert err["confirmed"] is False
    assert "NOT CONFIRMED" in err["error"]
    assert {"name": "Git Name", "email": "git@example.com", "source": SOURCE_GIT} in (
        err["suggestions"]
    )


def test_confirming_identity_satisfies_the_identity_half_of_the_gate(repo):
    """NOT the full write gate: `missing_profile_fields=None` means "not checked",
    so this exercises confirmation alone.

    Kept scoped on purpose. Every real call site computes the profile fields and
    passes them -- see the gated-tool tests below, and
    test_gate_refuses_a_confirmed_identity_whose_profile_is_incomplete for the
    other half. Read as a full-gate test this would pass even if profile
    completeness were deleted entirely.
    """
    root, cognition = repo
    assert require_identity(root, cognition) is not None
    write_confirmed_identity(cognition, "Ada", "ada@example.com")
    assert require_identity(root, cognition, None) is None
    assert require_identity(root, cognition, ["role"]) is not None


def test_gate_refuses_a_confirmed_identity_whose_profile_is_incomplete(repo):
    """Confirmation alone is not the bar -- role, seniority and reporting line are
    part of what must be answered before anything is written, so the graph can
    rank and route by them instead of carrying anonymous rows."""
    root, cognition = repo
    write_confirmed_identity(cognition, "Ada", "ada@example.com")
    missing = ["role", "seniority", "reports_to"]

    err = require_identity(root, cognition, missing)
    assert err is not None
    assert err["confirmed"] is True
    assert err["missing_profile_fields"] == missing
    assert "PROFILE INCOMPLETE" in err["error"]
    assert "ada@example.com" in err["error"]
    for field in missing:
        assert field in err["error"]
    # "nobody" must be offered, or a solo user is asked an unanswerable question.
    assert "nobody" in err["error"]


def test_gate_in_a_read_only_checkout_refuses_without_asking_anyone(repo, monkeypatch):
    """A checkout that cannot persist identity.json can never be confirmed, so the
    refusal must say so instead of asking five questions whose answer cannot be
    saved (ruling Q4)."""
    root, cognition = repo

    def _no_touch(self, *a, **kw):
        raise PermissionError("read-only")

    monkeypatch.setattr("pathlib.Path.touch", _no_touch)

    err = require_identity(root, cognition)
    assert err is not None
    assert err["read_only"] is True
    assert "CANNOT BE CONFIRMED IN THIS CHECKOUT" in err["error"]
    assert "ASK THE HUMAN" not in err["error"]


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
    nid = "n1"
    st.add_node(CognitionNode(
        id=nid, author="t", detail="", timestamp="2026-01-01T00:00:00Z",
        type=CognitionNodeType.DECISION, summary="d",
        metadata={"recorded_by": {"name": "Old", "email": "old@example.com"}},
    ))

    assert apply_remap(st, "old@example.com", "new@example.com", "New Name") == 1
    stored = st.get_node(nid)
    assert stored is not None
    rb = stored["metadata"]["recorded_by"]
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
    ("cognition_update_node", {"node_id": "whatever", "summary": "rewritten"}),
    ("cognition_remove_node", {"node_id": "whatever"}),
    ("cognition_remove_edge", {"from_id": "a", "to_id": "b", "edge_type": "led_to"}),
    ("cognition_set_env_fact", {"key": "os", "value": "w11"}),
    ("cognition_delete_env_fact", {"key": "os"}),
    ("cognition_clear_env_facts", {}),
]

#: Tools that write only AFTER a curation token is checked. The token check runs
#: first deliberately -- an agent that must not be calling these at all should be
#: told that, not told to onboard -- so they need a valid token before the
#: identity refusal is reachable.
CURATION_WRITE_TOOLS = [
    ("cognition_add_edge", {"from_id": "a", "to_id": "b", "edge_type": "led_to"}),
    ("cognition_add_edges_batch",
     {"edges": '[{"from_id": "a", "to_id": "b", "edge_type": "led_to"}]'}),
    ("cognition_mark_curated", {"node_ids": "a,b"}),
]

#: Every registered tool that does NOT write to the graph, with why. Paired with
#: the roster test below so a newly added tool cannot quietly land in neither
#: list: cognition_register_person shipped ungated in v0.37.0 precisely because
#: nothing forced that decision to be made.
UNGATED_TOOLS = {
    # The unblock path itself. Gating this would be a deadlock.
    "cognition_set_identity",
    # Mints a session token; writes nothing to the graph.
    "cognition_begin_curation",
    # Reads.
    "cognition_get_node", "cognition_get_document", "cognition_search",
    "cognition_get_chain", "cognition_get_superseded_chain", "cognition_get_workflow",
    "cognition_get_incident_resolution", "cognition_get_history",
    "cognition_get_edgeless_nodes", "cognition_get_uncurated_nodes",
    "cognition_get_neighbors", "cognition_list_tasks", "cognition_get_person",
    "cognition_list_people", "cognition_list_env_facts", "cognition_list_projects",
    "get_status", "cognition_dashboard", "cognition_readme",
    # Process/registry state, not graph content.
    "cognition_reload", "cognition_load_project", "cognition_unload_project",
}

#: Gated, but not callable in the parametrized refusal tests above: it resolves
#: the target person BEFORE the gate and returns "no person found", and seeding a
#: person first needs a write that is itself gated. Verified by reading
#: _update_person, which calls _gated_identity before appending any history.
GATED_NOT_DIRECTLY_TESTABLE = {
    "cognition_update_person", "cognition_update_task", "cognition_remove_person",
}


def test_every_registered_tool_is_classified_as_gated_or_deliberately_not(mock_mcp):
    """A new write tool must not be able to ship ungated unnoticed.

    v0.37.0 shipped cognition_register_person without the gate, and the
    single-tool test in place at the time could not see it. This asserts the
    PARTITION is total: every registered tool is either exercised by a refusal
    test, listed as gated-but-awkward-to-call, or explicitly declared
    non-writing. Adding a tool without touching one of these three lists fails
    here, forcing the decision to be made.
    """
    from vibe_cognition.tools import register_all_tools

    register_all_tools(mock_mcp)
    registered = set(mock_mcp.tools)
    classified = (
        {name for name, _ in WRITE_TOOLS}
        | {name for name, _ in CURATION_WRITE_TOOLS}
        | UNGATED_TOOLS
        | GATED_NOT_DIRECTLY_TESTABLE
    )
    assert not registered - classified, (
        "unclassified tool(s) — gate them and add to WRITE_TOOLS, or declare them "
        f"non-writing in UNGATED_TOOLS: {sorted(registered - classified)}"
    )
    assert not classified - registered, (
        f"classified but not registered (stale list): {sorted(classified - registered)}"
    )


@pytest.mark.parametrize("tool_name,kwargs", WRITE_TOOLS, ids=[t for t, _ in WRITE_TOOLS])
def test_every_write_tool_refuses_without_identity(
    tool_name, kwargs, tmp_path, mock_mcp, build_lc, make_ctx, graph_identity, monkeypatch
):
    """No write path may stamp attribution when no identity resolves.

    Parametrized deliberately: cognition_register_person shipped ungated because
    the single-tool test could not see it.

    cognition_update_person is absent by necessity, not oversight: it returns
    "no person found" before reaching the gate, and seeding a person first needs
    a write that is itself gated. No write happens on that path either way.
    """
    from vibe_cognition.tools.cognition_tools import register_cognition_tools

    graph_identity.unonboarded()
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


@pytest.mark.parametrize(
    "tool_name,kwargs", CURATION_WRITE_TOOLS, ids=[t for t, _ in CURATION_WRITE_TOOLS]
)
def test_curation_write_tools_refuse_without_identity_even_with_a_valid_token(
    tool_name, kwargs, tmp_path, mock_mcp, build_lc, make_ctx, graph_identity, monkeypatch
):
    """A curation token proves WHICH agent is calling, not WHO it belongs to.

    Edges and curation marks are graph content: they change what search returns
    and permanently remove nodes from the uncurated worklist. Before this they
    were the one mutation path with no identity requirement at all, so a checkout
    with no confirmed identity could still rewrite the graph's semantic structure.
    """
    from vibe_cognition.tools.cognition_tools import register_cognition_tools

    graph_identity.unonboarded()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "nope"))
    monkeypatch.setenv("SVN_CONFIG_DIR", str(tmp_path / "nope-svn"))
    monkeypatch.setattr("vibe_cognition.cognition.git_identity.getpass.getuser", lambda: "x")

    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    token = mock_mcp.tools["cognition_begin_curation"](ctx)["curation_token"]
    result = mock_mcp.tools[tool_name](ctx, curation_token=token, **kwargs)
    assert result.get("identity_required") is True, (tool_name, result)


def test_remove_person_is_gated_even_though_it_resolves_the_target_first(
    tmp_path, mock_mcp, build_lc, make_ctx, graph_identity, monkeypatch
):
    """The three tools in GATED_NOT_DIRECTLY_TESTABLE resolve their target before
    the gate, so the parametrized refusal test above cannot reach their gate. This
    one seeds a real target first and then unclaims the checkout.

    Taking someone off the roster changes search ranking and everyone's reporting
    chain, so an unidentified caller must not be able to do it.
    """
    from vibe_cognition.tools.cognition_tools import register_cognition_tools

    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)
    lc["cognition_storage"].set_profile_fields(
        "leaver@example.com",
        {"name": "Leaver", "email": "leaver@example.com", "role": "eng",
         "seniority": "mid", "reports_to": "nobody"},
        {"name": "Seed", "email": "seed@example.com"},
    )

    graph_identity.unresolvable("Ghost")
    result = mock_mcp.tools["cognition_remove_person"](ctx, email="leaver@example.com")
    assert result.get("identity_required") is True, result
    # Nothing was cleared: they are still on the roster.
    assert lc["cognition_storage"].get_profile("leaver@example.com") is not None


# ── round-3 regressions ───────────────────────────────────────────────────────


def test_confirm_rejects_malformed_address_a_suggestion_could_carry(repo):
    """Suggestion and confirmation must share one validator.

    An SVN username like "a b@c.com" once passed confirmation's loose "@" check,
    producing a permanently-confirmed garbage identity that stamped every write.
    """
    _, cognition = repo
    for bad in ("a b@c.com", "a@b", "a@@b.com", "a@.com", "no-at-sign"):
        assert "error" in write_confirmed_identity(cognition, "N", bad), bad
    assert read_confirmed_identity(cognition) is None


def test_suggestions_reject_malformed_svn_username(repo, monkeypatch, tmp_path):
    """A malformed SVN username is offered as a name, never as an email."""
    root, cognition = repo
    (root / ".svn").mkdir()
    cfg = tmp_path / "svnconf"
    _write_svn_credential(cfg, "a b@c.com")
    monkeypatch.setenv("SVN_CONFIG_DIR", str(cfg))

    got = identity_suggestions(root, cognition)
    assert [s["email"] for s in got] == [""], got


def test_confirmed_identity_survives_a_utf8_bom(repo):
    """A BOM must not silently defeat confirmation (some Windows editors add one)."""
    _, cognition = repo
    write_confirmed_identity(cognition, "Ada", "ada@example.com")
    path = local_read_path(cognition, IDENTITY_FILENAME)
    path.write_text("\ufeff" + path.read_text(encoding="utf-8"), encoding="utf-8")

    assert read_confirmed_identity(cognition) == {"name": "Ada", "email": "ada@example.com"}


def test_write_confirmed_identity_returns_what_landed_on_disk(repo):
    """The return value must be ground truth, not the intended payload."""
    _, cognition = repo
    got = write_confirmed_identity(cognition, "  Ada  ", "ADA@Example.com")
    assert got == read_confirmed_identity(cognition)
    assert not list(local_write_path(cognition, "x").parent.glob("identity.json.*.tmp")), "temp file left behind"
