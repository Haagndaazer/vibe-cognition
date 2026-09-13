"""WP-EnvFacts-A Stage 2: env-fact MCP tool surface.

Covers the brief's (doc:35d9206676e0) tool-layer acceptance:
- self-only by construction: NO email parameter exists on any write tool
  (passing one is a TypeError, not an ACL check); target is always the
  server-resolved git identity; unresolvable identity -> retryable error,
  nothing written
- machine defaulting: casefolded hostname; explicit machine override
  casefolded; unresolvable hostname -> retryable error
- disclosure: EVERY successful write's response carries the disclosure string
  (schema-level assertion); noop and clear variants covered
- machine cap honored from config (env_fact_machine_cap), error names remedy
- clear: no-arg means ALL machines (never "this machine"); cleared_keys
  uniform machine/key shape
- list: self default, open reads for others, registered:false join for a
  facts-without-person-node identity, current_machine surfaced
- get_person joins environment from the facts registry
- from_agent lands in the journaled delta line
"""

from __future__ import annotations

import json

from tests.conftest import identity_stamp
from vibe_cognition.cognition.people_facts import email_slug
from vibe_cognition.tools.cognition_tools import register_cognition_tools

SELF = {"name": "Colton Dyck", "email": "Colton@Example.com"}  # resolver output
SELF_FOLDED = "colton@example.com"


def _setup(
    build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch,
    hostname="DESKTOP-Abc",
):
    graph_identity.acting_as(SELF["name"], SELF["email"])
    monkeypatch.setattr(
        "vibe_cognition.tools.cognition_tools.platform.node", lambda: hostname
    )
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    return lc, make_ctx(lc)


def _person_file(tmp_path, email):
    return tmp_path / "home" / ".cognition" / "people" / f"{email_slug(email)}.jsonl"


# ── self-only by construction ───────────────────────────────────────────────


def test_write_tools_have_no_email_parameter(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch):
    """The brief's impersonation-resistance: there is no email parameter AT ALL
    on write tools — a caller trying to target someone else gets a TypeError
    from the signature, not an ACL decision."""
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    for tool in ("cognition_set_env_fact", "cognition_delete_env_fact", "cognition_clear_env_facts"):
        fn = mock_mcp.tools[tool]
        try:
            if tool == "cognition_set_env_fact":
                fn(ctx, key="k", value="v", email="mallory@x.com")
            elif tool == "cognition_delete_env_fact":
                fn(ctx, key="k", email="mallory@x.com")
            else:
                fn(ctx, email="mallory@x.com")
            raise AssertionError(f"{tool} accepted an email parameter")
        except TypeError:
            pass


def test_set_targets_server_resolved_identity(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    r = mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="Windows 11")
    assert "error" not in r, r
    assert r["email"] == SELF_FOLDED  # casefolded resolver email, no override possible
    assert r["machine"] == "desktop-abc"  # casefolded hostname default
    assert r["written"] is True
    assert _person_file(tmp_path, SELF_FOLDED).exists()


def test_unresolvable_identity_is_retryable_error_nothing_written(
    build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch
):
    graph_identity.unresolvable("Ghost")
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    for call in (
        lambda: mock_mcp.tools["cognition_set_env_fact"](ctx, key="k", value=1),
        lambda: mock_mcp.tools["cognition_delete_env_fact"](ctx, key="k"),
        lambda: mock_mcp.tools["cognition_clear_env_facts"](ctx),
        lambda: mock_mcp.tools["cognition_list_env_facts"](ctx),
    ):
        r = call()
        # Writes AND the self-defaulting read refuse the same way: identity
        # first, with the retry path named, never a silent empty-address write.
        assert r.get("identity_required") is True, r
        assert "cognition_set_identity" in r["error"]
    assert not (tmp_path / "home" / ".cognition" / "people").exists()


# ── machine defaulting ──────────────────────────────────────────────────────


def test_explicit_machine_override_casefolded(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    r = mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="macOS", machine="My-Laptop")
    assert r["machine"] == "my-laptop"
    listing = mock_mcp.tools["cognition_list_env_facts"](ctx)
    assert listing["environment"] == {"my-laptop": {"os": "macOS"}}


def test_unresolvable_hostname_is_retryable_error(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch, hostname="")
    r = mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="w11")
    assert "error" in r and "machine=" in r["error"]
    # Explicit machine unblocks (the retry the error names).
    r = mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="w11", machine="desk")
    assert r["written"] is True


def test_explicit_blank_machine_is_retryable_error(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch):
    """machine='' / whitespace-only errors on every write tool (final-gate LOW:
    the error text must cover the blank-argument case, not just an
    unresolvable hostname), and nothing is written."""
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    for call in (
        lambda: mock_mcp.tools["cognition_set_env_fact"](ctx, key="k", value=1, machine="   "),
        lambda: mock_mcp.tools["cognition_delete_env_fact"](ctx, key="k", machine=""),
        lambda: mock_mcp.tools["cognition_clear_env_facts"](ctx, machine="  "),
    ):
        r = call()
        assert "error" in r and "blank" in r["error"]
    # people/ exists (the acting identity has a committed profile in it); what
    # must not exist is an env-fact file for them.
    assert not _person_file(tmp_path, SELF_FOLDED).exists()


def test_person_removal_leaves_fact_file_registered_false(
    build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch
):
    """Final-gate regression: taking someone off the roster does NOT touch their
    env-fact file — facts persist and surface as registered:false (the documented
    orphaned-file KNOWN LIMIT). Removal is deliberately not a cascade: their facts
    and everything they authored are history, not the leaver's property.
    """
    lc, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    storage = lc["cognition_storage"]
    leaver = "leaver@example.com"
    mock_mcp.tools["cognition_register_person"](
        ctx, name="Leaver", role="eng", seniority="mid", email=leaver,
        reports_to="nobody",
    )
    # Their own facts, as they arrive by merge from their machine (writes are
    # self-only, so this is the only way they can exist here).
    storage.set_env_fact(leaver, "their-box", "os", "linux", dict(SELF), False)
    assert mock_mcp.tools["cognition_list_env_facts"](
        ctx, email_or_id=leaver,
    )["registered"] is True

    removed = mock_mcp.tools["cognition_remove_person"](ctx, email=leaver)
    assert removed.get("removed") is True, removed
    assert removed["orphaned_reports"] == []

    r = mock_mcp.tools["cognition_list_env_facts"](ctx, email_or_id=leaver)
    assert r["registered"] is False  # off the roster, facts remain
    assert r["environment"] == {"their-box": {"os": "linux"}}
    assert _person_file(tmp_path, leaver).exists()


def test_removing_your_own_profile_is_refused(
    build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch
):
    """Clearing the profile of the identity driving this checkout would close the
    write gate immediately, and the NEXT tool call would be refused with no obvious
    cause. The refusal names the two things the user probably meant instead."""
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)

    result = mock_mcp.tools["cognition_remove_person"](ctx, email=SELF["email"])
    assert "error" in result
    assert "cognition_update_person" in result["error"]
    assert "cognition_set_identity" in result["error"]
    assert mock_mcp.tools["cognition_list_env_facts"](ctx)["registered"] is True


def test_removing_a_legacy_node_person_refuses_instead_of_claiming_success(
    build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch
):
    """Clearing profile fields does nothing for a roster row that comes from a
    legacy person NODE — the node fallback rebuilds it identically on the next
    read. Reporting removed=True with six cleared fields would be a confident lie,
    and the caller would move on believing the person was gone."""
    from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType

    lc, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    lc["cognition_storage"].add_node(CognitionNode(
        id="legacyperson", type=CognitionNodeType.PERSON, summary="Old — sre",
        detail="", context=[], references=[], timestamp="2026-01-01T00:00:00+00:00",
        author="Old",
        metadata={"person": {"email": "old@example.com", "name": "Old", "role": "sre",
                             "seniority": "senior", "reports_to_email": ""}},
    ))

    result = mock_mcp.tools["cognition_remove_person"](ctx, email="old@example.com")
    assert result["removed"] is False
    assert result["cleared_fields"] == []
    assert "cognition_remove_node" in result["error"]
    assert "legacyperson" in result["error"]
    # Still there, exactly as before.
    assert mock_mcp.tools["cognition_get_person"](ctx, email_or_id="old@example.com")[
        "seniority"
    ] == "senior"


def test_removing_a_manager_names_the_people_left_dangling(
    build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch
):
    """Silently orphaning someone's reporting line would quietly change their prime
    digest (no manager, no rollup) with nothing anywhere saying why."""
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    mock_mcp.tools["cognition_register_person"](
        ctx, name="Boss", role="lead", seniority="senior", email="boss@example.com",
        reports_to="nobody",
    )
    mock_mcp.tools["cognition_register_person"](
        ctx, name="Report", role="eng", seniority="mid", email="rep@example.com",
        reports_to="boss@example.com",
    )

    removed = mock_mcp.tools["cognition_remove_person"](ctx, email="boss@example.com")
    assert removed["orphaned_reports"] == ["rep@example.com"]
    assert "rep@example.com" in removed["warning"]
    assert "cognition_update_person" in removed["warning"]
    # The report is still on the roster, now with a dangling manager.
    rep = mock_mcp.tools["cognition_get_person"](ctx, email_or_id="rep@example.com")
    assert rep["reports_to"] == "boss@example.com"
    assert rep["reports_to_registered"] is False


# ── disclosure contract ─────────────────────────────────────────────────────


def test_every_successful_write_carries_disclosure(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch):
    """Schema-level: written=True responses ALWAYS include a non-empty
    disclosure naming the identity; noop responses disclose the no-change."""
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    set_r = mock_mcp.tools["cognition_set_env_fact"](ctx, key="shell", value="pwsh")
    assert set_r["written"] and SELF_FOLDED in set_r["disclosure"]
    assert "removed" in set_r["disclosure"].lower()  # removal-on-request named

    noop_r = mock_mcp.tools["cognition_set_env_fact"](ctx, key="shell", value="pwsh")
    assert noop_r["noop"] and "no change" in noop_r["disclosure"].lower()

    del_r = mock_mcp.tools["cognition_delete_env_fact"](ctx, key="shell")
    assert del_r["written"] and SELF_FOLDED in del_r["disclosure"]

    mock_mcp.tools["cognition_set_env_fact"](ctx, key="a", value=1)
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="b", value=2)
    clear_r = mock_mcp.tools["cognition_clear_env_facts"](ctx)
    assert clear_r["written"] and "2 fact(s)" in clear_r["disclosure"]
    assert "desktop-abc/a" in clear_r["disclosure"]  # enumerated, never opaque


# ── machine cap via config ──────────────────────────────────────────────────


def test_machine_cap_read_from_config(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch):
    lc, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    lc["config"].env_fact_machine_cap = 2
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value=1, machine="m1")
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value=1, machine="m2")
    r = mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value=1, machine="m3")
    assert "error" in r and "machine cap" in r["error"]
    assert "cognition_clear_env_facts" in r["error"]  # names the remedy


# ── clear scope policy ──────────────────────────────────────────────────────


def test_clear_no_arg_means_all_machines_not_current(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch):
    """The deliberate non-default: no-arg clear is "forget everything", NOT
    "forget this machine" — a hostname-defaulted clear would silently leave
    other machines' facts behind on a removal request."""
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="w11")  # current machine
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="macos", machine="laptop")
    r = mock_mcp.tools["cognition_clear_env_facts"](ctx)
    assert r["machine"] is None
    assert sorted(r["cleared_keys"]) == ["desktop-abc/os", "laptop/os"]
    assert mock_mcp.tools["cognition_list_env_facts"](ctx)["environment"] == {}


def test_clear_machine_scope(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="w11")
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="macos", machine="laptop")
    r = mock_mcp.tools["cognition_clear_env_facts"](ctx, machine="LAPTOP")
    assert r["cleared_keys"] == ["laptop/os"]
    env = mock_mcp.tools["cognition_list_env_facts"](ctx)["environment"]
    assert "laptop" not in env and env["desktop-abc"]["os"] == "w11"


# ── list / reads open ───────────────────────────────────────────────────────


def test_list_defaults_to_self_and_surfaces_current_machine(
    build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch
):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="w11")
    r = mock_mcp.tools["cognition_list_env_facts"](ctx)
    assert r["email"] == SELF_FOLDED
    assert r["current_machine"] == "desktop-abc"
    assert r["machine_count"] == 1


def test_list_open_reads_and_registered_flag(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch):
    """Facts for an identity with NO person node: first-class legal,
    registered:false — never an error. After registration: registered:true."""
    lc, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    storage = lc["cognition_storage"]
    storage.set_env_fact("teammate@x.com", "their-box", "os", "linux", {"name": "T", "email": "teammate@x.com"})

    r = mock_mcp.tools["cognition_list_env_facts"](ctx, email_or_id="Teammate@X.com")
    assert r["email"] == "teammate@x.com"
    assert r["registered"] is False
    assert r["environment"] == {"their-box": {"os": "linux"}}

    reg = mock_mcp.tools["cognition_register_person"](
        ctx, name="Teammate", role="dev", seniority="mid", email="teammate@x.com"
    )
    assert "error" not in reg, reg
    r = mock_mcp.tools["cognition_list_env_facts"](ctx, email_or_id="teammate@x.com")
    assert r["registered"] is True


def test_list_resolves_person_node_id(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    reg = mock_mcp.tools["cognition_register_person"](ctx, name="Colton", role="owner", seniority="owner")
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="w11")
    r = mock_mcp.tools["cognition_list_env_facts"](ctx, email_or_id=reg["id"])
    assert r["email"] == SELF_FOLDED and r["environment"]["desktop-abc"]["os"] == "w11"


def test_get_person_joins_environment(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    mock_mcp.tools["cognition_register_person"](ctx, name="Colton", role="owner", seniority="owner")
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="project_root", value="C:/proj")
    r = mock_mcp.tools["cognition_get_person"](ctx, email_or_id=SELF_FOLDED)
    assert r["environment"] == {"desktop-abc": {"project_root": "C:/proj"}}
    # A person with no facts gets an empty dict, not a missing key.
    mock_mcp.tools["cognition_register_person"](
        ctx, name="Bare", role="dev", seniority="junior", email="bare@x.com"
    )
    r = mock_mcp.tools["cognition_get_person"](ctx, email_or_id="bare@x.com")
    assert r["environment"] == {}


# ── provenance ──────────────────────────────────────────────────────────────


def test_settings_knob_default_and_env_override(graph_identity, monkeypatch):
    from vibe_cognition.config import Settings

    monkeypatch.delenv("ENV_FACT_MACHINE_CAP", raising=False)
    assert Settings().env_fact_machine_cap == 10
    monkeypatch.setenv("ENV_FACT_MACHINE_CAP", "3")
    assert Settings().env_fact_machine_cap == 3


def test_from_agent_lands_in_delta_line(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch)
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="a", value=1)  # default true
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="b", value=2, from_agent=False)
    lines = [
        json.loads(ln)
        for ln in _person_file(tmp_path, SELF_FOLDED).read_text(encoding="utf-8").splitlines()
    ]
    assert lines[0]["from_agent"] is True and lines[1]["from_agent"] is False
    stamp = identity_stamp(SELF["name"], SELF["email"])
    assert all(ln["by"] == stamp for ln in lines)  # server-resolved stamp, verbatim
