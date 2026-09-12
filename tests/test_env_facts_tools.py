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

from vibe_cognition.cognition.people_facts import email_slug
from vibe_cognition.tools.cognition_tools import register_cognition_tools

SELF = {"name": "Colton Dyck", "email": "Colton@Example.com"}  # resolver output
SELF_FOLDED = "colton@example.com"


def _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch, hostname="DESKTOP-Abc"):
    monkeypatch.setattr(
        "vibe_cognition.tools.cognition_tools._acting_identity",
        lambda repo: dict(SELF),
    )
    monkeypatch.setattr(
        "vibe_cognition.tools.cognition_tools.platform.node", lambda: hostname
    )
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    return lc, make_ctx(lc)


def _person_file(tmp_path, email):
    return tmp_path / "home" / ".cognition" / "people" / f"{email_slug(email)}.jsonl"


# ── self-only by construction ───────────────────────────────────────────────


def test_write_tools_have_no_email_parameter(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    """The brief's impersonation-resistance: there is no email parameter AT ALL
    on write tools — a caller trying to target someone else gets a TypeError
    from the signature, not an ACL decision."""
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch)
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


def test_set_targets_server_resolved_identity(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch)
    r = mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="Windows 11")
    assert "error" not in r, r
    assert r["email"] == SELF_FOLDED  # casefolded resolver email, no override possible
    assert r["machine"] == "desktop-abc"  # casefolded hostname default
    assert r["written"] is True
    assert _person_file(tmp_path, SELF_FOLDED).exists()


def test_unresolvable_identity_is_retryable_error_nothing_written(
    build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "vibe_cognition.tools.cognition_tools._acting_identity",
        lambda repo: {"name": "Ghost", "email": ""},
    )
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
        assert "error" in r and "user.email" in r["error"] or "self-only" in r.get("error", "")
    assert not (tmp_path / "home" / ".cognition" / "people").exists()


# ── machine defaulting ──────────────────────────────────────────────────────


def test_explicit_machine_override_casefolded(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch)
    r = mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="macOS", machine="My-Laptop")
    assert r["machine"] == "my-laptop"
    listing = mock_mcp.tools["cognition_list_env_facts"](ctx)
    assert listing["environment"] == {"my-laptop": {"os": "macOS"}}


def test_unresolvable_hostname_is_retryable_error(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch, hostname="")
    r = mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="w11")
    assert "error" in r and "machine=" in r["error"]
    # Explicit machine unblocks (the retry the error names).
    r = mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="w11", machine="desk")
    assert r["written"] is True


def test_explicit_blank_machine_is_retryable_error(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    """machine='' / whitespace-only errors on every write tool (final-gate LOW:
    the error text must cover the blank-argument case, not just an
    unresolvable hostname), and nothing is written."""
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch)
    for call in (
        lambda: mock_mcp.tools["cognition_set_env_fact"](ctx, key="k", value=1, machine="   "),
        lambda: mock_mcp.tools["cognition_delete_env_fact"](ctx, key="k", machine=""),
        lambda: mock_mcp.tools["cognition_clear_env_facts"](ctx, machine="  "),
    ):
        r = call()
        assert "error" in r and "blank" in r["error"]
    assert not (tmp_path / "home" / ".cognition" / "people").exists()


def test_person_removal_leaves_fact_file_registered_false(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    """Final-gate regression: cognition_remove_node on a person node does NOT
    touch their env-fact file — facts persist and surface as registered:false
    (the documented orphaned-file KNOWN LIMIT)."""
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch)
    reg = mock_mcp.tools["cognition_register_person"](ctx, name="Colton", role="owner", seniority="owner")
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="w11")
    assert mock_mcp.tools["cognition_list_env_facts"](ctx)["registered"] is True

    removed = mock_mcp.tools["cognition_remove_node"](ctx, node_id=reg["id"])
    assert removed.get("removed") is True, removed

    r = mock_mcp.tools["cognition_list_env_facts"](ctx)
    assert r["registered"] is False  # person gone, facts remain
    assert r["environment"] == {"desktop-abc": {"os": "w11"}}
    assert _person_file(tmp_path, SELF_FOLDED).exists()


# ── disclosure contract ─────────────────────────────────────────────────────


def test_every_successful_write_carries_disclosure(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    """Schema-level: written=True responses ALWAYS include a non-empty
    disclosure naming the identity; noop responses disclose the no-change."""
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch)
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


def test_machine_cap_read_from_config(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    lc, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch)
    lc["config"].env_fact_machine_cap = 2
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value=1, machine="m1")
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value=1, machine="m2")
    r = mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value=1, machine="m3")
    assert "error" in r and "machine cap" in r["error"]
    assert "cognition_clear_env_facts" in r["error"]  # names the remedy


# ── clear scope policy ──────────────────────────────────────────────────────


def test_clear_no_arg_means_all_machines_not_current(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    """The deliberate non-default: no-arg clear is "forget everything", NOT
    "forget this machine" — a hostname-defaulted clear would silently leave
    other machines' facts behind on a removal request."""
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch)
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="w11")  # current machine
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="macos", machine="laptop")
    r = mock_mcp.tools["cognition_clear_env_facts"](ctx)
    assert r["machine"] is None
    assert sorted(r["cleared_keys"]) == ["desktop-abc/os", "laptop/os"]
    assert mock_mcp.tools["cognition_list_env_facts"](ctx)["environment"] == {}


def test_clear_machine_scope(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch)
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="w11")
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="macos", machine="laptop")
    r = mock_mcp.tools["cognition_clear_env_facts"](ctx, machine="LAPTOP")
    assert r["cleared_keys"] == ["laptop/os"]
    env = mock_mcp.tools["cognition_list_env_facts"](ctx)["environment"]
    assert "laptop" not in env and env["desktop-abc"]["os"] == "w11"


# ── list / reads open ───────────────────────────────────────────────────────


def test_list_defaults_to_self_and_surfaces_current_machine(
    build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch
):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch)
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="w11")
    r = mock_mcp.tools["cognition_list_env_facts"](ctx)
    assert r["email"] == SELF_FOLDED
    assert r["current_machine"] == "desktop-abc"
    assert r["machine_count"] == 1


def test_list_open_reads_and_registered_flag(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    """Facts for an identity with NO person node: first-class legal,
    registered:false — never an error. After registration: registered:true."""
    lc, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch)
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


def test_list_resolves_person_node_id(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch)
    reg = mock_mcp.tools["cognition_register_person"](ctx, name="Colton", role="owner", seniority="owner")
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="w11")
    r = mock_mcp.tools["cognition_list_env_facts"](ctx, email_or_id=reg["id"])
    assert r["email"] == SELF_FOLDED and r["environment"]["desktop-abc"]["os"] == "w11"


def test_get_person_joins_environment(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch)
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


def test_settings_knob_default_and_env_override(monkeypatch):
    from vibe_cognition.config import Settings

    monkeypatch.delenv("ENV_FACT_MACHINE_CAP", raising=False)
    assert Settings().env_fact_machine_cap == 10
    monkeypatch.setenv("ENV_FACT_MACHINE_CAP", "3")
    assert Settings().env_fact_machine_cap == 3


def test_from_agent_lands_in_delta_line(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    _, ctx = _setup(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch)
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="a", value=1)  # default true
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="b", value=2, from_agent=False)
    lines = [
        json.loads(ln)
        for ln in _person_file(tmp_path, SELF_FOLDED).read_text(encoding="utf-8").splitlines()
    ]
    assert lines[0]["from_agent"] is True and lines[1]["from_agent"] is False
    assert all(ln["by"] == SELF for ln in lines)  # server-resolved stamp, verbatim
