"""cognition_set_identity: the one call that unblocks a refused write.

It writes two things — the machine-local pointer saying who is driving this
checkout, and the committed profile saying who that person is. Both are required
before any graph write is allowed, so the interesting cases here are the partial
ones: a pointer with no profile, a profile someone else already filled in, and
inputs that must be rejected before anything lands on disk.
"""

import json

import pytest

from tests.conftest import identity_stamp
from vibe_cognition.cognition.identity import identity_write_path, read_confirmed_identity
from vibe_cognition.cognition.profiles import NO_MANAGER, profile_filename
from vibe_cognition.tools.cognition_tools import register_cognition_tools

FULL = {
    "name": "Ada Lovelace",
    "email": "ada@example.com",
    "role": "engineer",
    "seniority": "senior",
    "reports_to": NO_MANAGER,
}


@pytest.fixture
def tools(mock_mcp, build_lc, make_ctx, tmp_path, graph_identity):
    """An un-onboarded checkout: nobody has confirmed an identity here yet."""
    graph_identity.unonboarded()
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    return mock_mcp.tools, make_ctx(lc), lc


def _cognition(lc):
    return lc["cognition_storage"].cognition_dir


def _record(tools, ctx):
    return tools["cognition_record"](
        ctx, node_type="decision", summary="s", detail="d", context="", author="a",
    )


# ── the happy path is one call ───────────────────────────────────────────────


def test_one_full_call_writes_pointer_and_profile_and_opens_the_gate(tools):
    t, ctx, lc = tools
    assert _record(t, ctx).get("identity_required") is True

    r = t["cognition_set_identity"](ctx, **FULL)
    assert "error" not in r, r
    assert r["write_ready"] is True
    assert r["missing_profile_fields"] == []
    assert sorted(r["profile_written"]) == ["email", "name", "reports_to", "role", "seniority"]
    assert r["profile"]["role"] == "engineer"

    cognition = _cognition(lc)
    assert read_confirmed_identity(cognition) == {"name": "Ada Lovelace", "email": "ada@example.com"}
    assert r["path"] == str(identity_write_path(cognition))
    assert (cognition / "people" / profile_filename("ada@example.com")).exists()

    node = _record(t, ctx)
    assert "error" not in node, node
    stored = lc["cognition_storage"].get_node(node["id"])
    assert stored["metadata"]["recorded_by"] == identity_stamp("Ada Lovelace", "ada@example.com")


def test_the_pointer_alone_does_not_open_the_gate(tools):
    """v0.37.0 confirmed an identity and stopped there. Role, seniority and the
    reporting line are part of what must be answered, so a name-and-email-only
    call must report itself unfinished AND leave writes refused."""
    t, ctx, _ = tools

    r = t["cognition_set_identity"](ctx, name="Ada Lovelace", email="ada@example.com")
    assert "error" not in r, r
    assert r["write_ready"] is False
    assert r["missing_profile_fields"] == ["role", "seniority", "reports_to"]

    refused = _record(t, ctx)
    assert refused.get("identity_required") is True
    assert refused["confirmed"] is True
    assert refused["missing_profile_fields"] == ["role", "seniority", "reports_to"]
    assert "PROFILE INCOMPLETE" in refused["error"]

    done = t["cognition_set_identity"](
        ctx, name="Ada Lovelace", email="ada@example.com",
        role="engineer", seniority="senior", reports_to=NO_MANAGER,
    )
    assert done["write_ready"] is True
    assert done["profile_skipped"] == ["name", "email"]  # unchanged, so not re-journalled
    assert "error" not in _record(t, ctx)


def test_a_teammate_whose_manager_pre_registered_them_finishes_in_one_call(tools):
    """The profile is deliberately multi-writer: a manager can fill in role,
    seniority and reporting line before the person ever onboards, and then the
    person's own confirmation only has to claim the checkout."""
    t, ctx, lc = tools
    lc["cognition_storage"].set_profile_fields(
        "bob@example.com",
        {"name": "Bob", "role": "engineer", "seniority": "junior",
         "reports_to": "ada@example.com"},
        {"name": "Ada Lovelace", "email": "ada@example.com"},
    )

    r = t["cognition_set_identity"](ctx, name="Bob", email="bob@example.com")
    assert r["write_ready"] is True, r
    assert r["profile"]["reports_to"] == "ada@example.com"
    assert "error" not in _record(t, ctx)


# ── nothing may land when an argument is wrong ────────────────────────────────


def test_a_seniority_outside_the_closed_set_writes_nothing_at_all(tools):
    """Validation runs BEFORE the pointer is written. Otherwise a rejected
    seniority would still have repointed the checkout at an identity whose
    profile never landed — confirmed as someone, profiled as nobody."""
    t, ctx, lc = tools

    r = t["cognition_set_identity"](ctx, **{**FULL, "seniority": "archmage"})
    assert "error" in r
    assert "owner" in r["error"] and "junior" in r["error"]

    cognition = _cognition(lc)
    assert read_confirmed_identity(cognition) is None
    assert not (cognition / "people" / profile_filename("ada@example.com")).exists()


@pytest.mark.parametrize("bad", ["Ada Lovelace", "my manager", "ada at example.com", "a@b"])
def test_reports_to_must_be_an_email_or_nobody_never_a_name(tools, bad):
    """The reporting chain is resolved by email. A name stored here would break it
    silently — the chain would simply stop, with no error anywhere."""
    t, ctx, lc = tools

    r = t["cognition_set_identity"](ctx, **{**FULL, "reports_to": bad})
    assert "error" in r, (bad, r)
    assert NO_MANAGER in r["error"]
    assert read_confirmed_identity(_cognition(lc)) is None


@pytest.mark.parametrize("value", ["nobody", "NOBODY", "  Nobody  "])
def test_nobody_is_accepted_in_any_casing_because_solo_projects_are_normal(tools, value):
    t, ctx, _ = tools
    r = t["cognition_set_identity"](ctx, **{**FULL, "reports_to": value})
    assert "error" not in r, r
    assert r["profile"]["reports_to"] == NO_MANAGER
    assert r["write_ready"] is True


def test_a_malformed_email_is_rejected_before_the_profile_is_touched(tools):
    t, ctx, lc = tools
    r = t["cognition_set_identity"](ctx, **{**FULL, "email": "not-an-address"})
    assert "error" in r
    assert not (_cognition(lc) / "people").exists()


# ── re-pointing ──────────────────────────────────────────────────────────────


def test_repointing_to_a_new_address_reports_what_the_new_one_still_needs(tools):
    """Changing the confirmed address must not silently inherit the old
    profile's completeness — the gate is per-email."""
    t, ctx, _ = tools
    assert t["cognition_set_identity"](ctx, **FULL)["write_ready"] is True

    moved = t["cognition_set_identity"](ctx, name="Ada Lovelace", email="ada@work.example.com")
    assert moved["write_ready"] is False
    assert moved["missing_profile_fields"] == ["role", "seniority", "reports_to"]
    assert _record(t, ctx).get("identity_required") is True


def test_previous_identity_is_reported_so_a_human_can_see_what_changed(tools):
    t, ctx, _ = tools
    t["cognition_set_identity"](ctx, **FULL)
    moved = t["cognition_set_identity"](ctx, **{**FULL, "email": "ada@work.example.com"})
    assert moved["previous"]["email"] == "ada@example.com"
    assert moved["previous"]["source"] == "confirmed"


def test_profile_records_are_attributed_to_the_person_themselves(tools):
    """Self-confirmation stamps `by` with the confirming identity, so the audit
    trail distinguishes it from a manager filling the profile in."""
    t, ctx, lc = tools
    t["cognition_set_identity"](ctx, **FULL)

    path = _cognition(lc) / "people" / profile_filename("ada@example.com")
    lines = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines
    for line in lines:
        assert line["by"] == {"name": "Ada Lovelace", "email": "ada@example.com"}
        assert line["action"] == "profile_set"
