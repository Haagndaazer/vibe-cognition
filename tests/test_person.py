"""The roster tools, now backed by committed profiles instead of person nodes.

WP-Identity-Profiles moved the roster out of the graph: a person is
`.cognition/people/<email>.profile.jsonl`, append-only and committed, not a
`person` node with a vector. The four tool NAMES are unchanged, so this file still
covers the same contract questions — one profile per email, self- vs third-party
registration, the reporting line and its cycle guard, dangling managers, seniority
as a closed set, `from_agent` provenance — against the new return shape.

Three deliberate contract changes are pinned here rather than left implicit:
  * `reports_to` replaces `reports_to_email`, takes an email or the literal
    "nobody", and REJECTS a name (the chain resolves by email);
  * re-submitting values that are already current succeeds with everything in
    `profile_skipped`, where it used to be "No updatable fields provided";
  * people are no longer searchable, because a profile carries no vector.

Also retained verbatim: the WP-TC6 `from_agent` coverage for record/add_task/
store_document/search, which this WP does not touch.
"""

from __future__ import annotations

from tests.conftest import identity_stamp
from vibe_cognition.cognition.models import SENIORITY_LEVELS
from vibe_cognition.cognition.prime import PrimeConfig, generate_prime
from vibe_cognition.cognition.profiles import NO_MANAGER
from vibe_cognition.tools.cognition_tools import register_cognition_tools

ROW_KEYS = {
    "email", "name", "role", "seniority", "reports_to", "answered_reports_to",
    "reports_to_registered", "detail", "summary", "source", "id",
}


# ── cognition_register_person ───────────────────────────────────────────────


def test_register_person_self_uses_server_resolved_email(
    build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch
):
    """Omitting `email` targets the SERVER-resolved identity, never a client value.

    Note what the gate did to this path: to call any write tool you must already
    have a complete profile, so self-registration now almost always lands on the
    dedup branch. It is kept because the email-resolution rule is the thing being
    asserted -- an agent that could pass an arbitrary email here could register a
    profile in someone else's name and have it look self-authored.
    """
    graph_identity.acting_as("Vorpid", "Vorpid@Example.com", role="implementer")
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="Someone Entirely Different", role="pretend role", seniority="owner",
    )
    assert "error" not in result, result
    assert result["email"] == "vorpid@example.com"  # casefolded, server-resolved
    assert result["already_registered"] is True
    # The client-supplied fields did NOT overwrite the existing profile.
    assert result["name"] == "Vorpid"
    assert result["role"] == "implementer"


def test_register_person_writes_a_committed_profile_not_a_graph_node(
    build_lc, make_ctx, mock_mcp, tmp_path
):
    """The roster is no longer graph content: registering someone adds a file under
    .cognition/people/ and NO node. A person node in the graph would come back into
    search results and the uncurated worklist, which is what moving them out fixed.
    """
    from vibe_cognition.cognition.profiles import profile_filename

    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage = lc["cognition_storage"]
    before = len(storage.get_all_nodes())

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to=NO_MANAGER,
    )
    assert len(storage.get_all_nodes()) == before
    assert (storage.cognition_dir / "people" / profile_filename("x@example.com")).exists()


def test_register_person_explicit_email_registers_someone_else(
    build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch
):
    """An explicit `email` registers a THIRD PARTY -- allowed, trust-based; who did
    it is in the profile records' `by`, not enforced against the explicit email."""
    graph_identity.acting_as("Vince", "vince@example.com")
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="Colton Dyck", role="owner", seniority="owner",
        email="Colton.Dyck@AcrylicCode.com", reports_to=NO_MANAGER,
    )
    assert "error" not in result, result
    assert result["email"] == "colton.dyck@acryliccode.com"

    # The records name the ACTUAL caller (Vince), not the registered person.
    history = mock_mcp.tools["cognition_get_person"](
        ctx, email_or_id="colton.dyck@acryliccode.com",
    )["profile_history"]
    assert history
    assert all(r["by"]["email"] == "vince@example.com" for r in history)


def test_a_manager_can_pre_register_a_teammate_who_then_only_confirms(
    build_lc, make_ctx, mock_mcp, tmp_path, graph_identity
):
    """The multi-writer flow, end to end through the TOOLS.

    A manager fills in role, seniority and reporting line for someone who has not
    onboarded; that person then runs cognition_set_identity with just name and
    email and is immediately able to write. Without this, every teammate would
    answer all five questions even when their manager already had the answers.
    """
    graph_identity.acting_as("Manager", "mgr@example.com")
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    pre = mock_mcp.tools["cognition_register_person"](
        ctx, name="Bob", role="engineer", seniority="junior",
        email="bob@example.com", reports_to="mgr@example.com",
    )
    assert "error" not in pre, pre
    assert pre["missing_profile_fields"] == []

    graph_identity.acting_as("Bob", "bob@example.com", seed=False)
    confirmed = mock_mcp.tools["cognition_set_identity"](
        ctx, name="Bob", email="bob@example.com",
    )
    assert confirmed["write_ready"] is True, confirmed
    assert confirmed["profile"]["role"] == "engineer"
    assert confirmed["profile"]["reports_to"] == "mgr@example.com"
    assert "error" not in mock_mcp.tools["cognition_record"](
        ctx, node_type="decision", summary="s", detail="d", context="", author="Bob",
    )


def test_register_person_blank_explicit_email_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="   ",
    )
    assert "error" in result


def test_register_person_no_resolvable_email_errors(
    build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch
):
    """No explicit email AND an unresolvable identity -> clean error (WP-P13n
    empty-email edge case), never a profile keyed on a blank address."""
    graph_identity.unresolvable("unknown")
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid",
    )
    assert "error" in result


def test_register_person_invalid_seniority_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="ultra-senior", email="x@example.com",
    )
    assert "error" in result
    for level in SENIORITY_LEVELS:
        assert level in result["error"]


def test_register_person_seniority_casefolded(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="SENIOR", email="x@example.com",
    )
    assert "error" not in result, result
    assert result["seniority"] == "senior"


def test_register_person_one_profile_per_email(build_lc, make_ctx, mock_mcp, tmp_path):
    """Re-registering an email whose profile is COMPLETE returns it with
    already_registered=True and writes nothing -- never a silent overwrite of
    someone's role by a second caller who guessed."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    first = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="Dupe@Example.com",
        reports_to=NO_MANAGER,
    )
    assert first["already_registered"] is False
    second = mock_mcp.tools["cognition_register_person"](
        ctx, name="Someone Else", role="different role", seniority="senior",
        email="dupe@example.com",  # same email, different case
        reports_to=NO_MANAGER,
    )
    assert second["already_registered"] is True
    assert second["email"] == first["email"]
    assert second["name"] == "X"
    assert second["role"] == "r"


def test_register_person_fills_in_an_incomplete_profile_rather_than_refusing(
    build_lc, make_ctx, mock_mcp, tmp_path
):
    """An INCOMPLETE profile is completed, not reported as already_registered.

    Otherwise someone who confirmed an identity (name + email only) could never be
    given a role by their manager: the dedup branch would return the half-filled
    profile and write nothing, with no error to explain why.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    # As it arrives by merge from the half-onboarded person's own machine.
    lc["cognition_storage"].set_profile_fields(
        "half@example.com",
        {"name": "Half", "email": "half@example.com"},
        {"name": "Half", "email": "half@example.com"},
    )

    filled = mock_mcp.tools["cognition_register_person"](
        ctx, name="Half", role="engineer", seniority="mid", email="half@example.com",
        reports_to=NO_MANAGER,
    )
    assert filled["already_registered"] is False, filled
    assert filled["missing_profile_fields"] == []
    assert filled["role"] == "engineer"


def test_register_person_self_reporting_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to="x@example.com",
    )
    assert "error" in result


def test_register_person_dangling_reports_to_is_legal(build_lc, make_ctx, mock_mcp, tmp_path):
    """A manager who is not on the roster yet is LEGAL (they may register later) --
    surfaced as reports_to_registered=False, not an error."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to="ghost@example.com",
    )
    assert "error" not in result, result
    assert result["reports_to"] == "ghost@example.com"
    assert result["reports_to_registered"] is False
    assert result["answered_reports_to"] is True


def test_register_person_cycle_via_dangling_reports_to_rejected(
    build_lc, make_ctx, mock_mcp, tmp_path
):
    """A cycle must be rejected even when it is only completed AT REGISTRATION
    time, via a reports_to that was legally dangling when set. Fails-before:
    registration only checked self-reporting, not the transitive chain -- so (1) B
    registers with reports_to=a@example.com while a@example.com does not exist yet
    (legal dangling), then (2) A registering with reports_to=b@example.com would
    silently close an A -> B -> A loop with no guard firing."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    b = mock_mcp.tools["cognition_register_person"](
        ctx, name="B", role="r", seniority="mid", email="b@example.com",
        reports_to="a@example.com",
    )
    assert "error" not in b, b
    assert b["reports_to_registered"] is False  # a@example.com not on the roster yet

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="A", role="r", seniority="mid", email="a@example.com",
        reports_to="b@example.com",
    )
    assert "error" in result


def test_register_person_registered_reports_to_flagged_true(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="Manager", role="lead", seniority="senior", email="mgr@example.com",
        reports_to=NO_MANAGER,
    )
    report = mock_mcp.tools["cognition_register_person"](
        ctx, name="Report", role="ic", seniority="mid", email="ic@example.com",
        reports_to="mgr@example.com",
    )
    assert report["reports_to_registered"] is True


def test_nobody_is_a_real_answer_and_is_not_a_dangling_manager(
    build_lc, make_ctx, mock_mcp, tmp_path
):
    """"nobody" satisfies the gate and must not be reported as an unregistered
    manager -- a solo owner would otherwise see a permanent "manager not
    registered" flag against an answer that is correct."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    solo = mock_mcp.tools["cognition_register_person"](
        ctx, name="Solo", role="owner", seniority="owner", email="solo@example.com",
        reports_to="NOBODY",
    )
    assert "error" not in solo, solo
    assert solo["reports_to"] == NO_MANAGER
    assert solo["answered_reports_to"] is True
    assert solo["reports_to_registered"] is False
    assert solo["missing_profile_fields"] == []


def test_reports_to_rejects_a_managers_name(build_lc, make_ctx, mock_mcp, tmp_path):
    """A name here would break the reporting chain with no error anywhere: the
    chain is walked by email, so it would simply stop."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    for bad in ("Jane Manager", "jane", "", "   "):
        result = mock_mcp.tools["cognition_register_person"](
            ctx, name="X", role="r", seniority="mid", email="x@example.com",
            reports_to=bad,
        )
        assert "error" in result, (bad, result)
        assert NO_MANAGER in result["error"]


def test_register_person_has_no_created_by_parameter(build_lc, make_ctx, mock_mcp, tmp_path):
    """Mirrors cognition_add_task's contract: no client-settable creator identity."""
    import inspect
    register_cognition_tools(mock_mcp)
    params = set(inspect.signature(mock_mcp.tools["cognition_register_person"]).parameters)
    assert "created_by" not in params
    assert "recorded_by" not in params


# ── cognition_record must reject node_type="person" ─────────────────────────


def test_cognition_record_rejects_person(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_record"](
        ctx, node_type="person", summary="s", detail="d", context="c", author="client",
    )
    assert "error" in result
    assert "cognition_register_person" in result["error"]


# ── cognition_update_person ──────────────────────────────────────────────────


def test_update_person_records_one_entry_per_changed_field(
    build_lc, make_ctx, mock_mcp, tmp_path, graph_identity, monkeypatch
):
    graph_identity.acting_as("Vince", "vince@example.com")
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="junior dev", seniority="junior", email="x@example.com",
        reports_to=NO_MANAGER,
    )
    updated = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="x@example.com", role="senior dev", seniority="senior",
    )
    assert "error" not in updated, updated
    assert sorted(updated["profile_written"]) == ["role", "seniority"]
    assert updated["summary"] == "X — senior dev"  # regenerated from the new role

    history = mock_mcp.tools["cognition_get_person"](ctx, email_or_id="x@example.com")[
        "profile_history"
    ]
    changes = [(r["field"], r["value"]) for r in history if r["action"] == "profile_set"]
    assert ("role", "senior dev") in changes
    assert ("seniority", "senior") in changes
    assert history[-1]["by"] == identity_stamp("Vince", "vince@example.com")
    assert "at" in history[-1]


def test_update_person_leaves_untouched_fields_alone(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to=NO_MANAGER,
    )
    updated = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="x@example.com", detail="new bio",
    )
    assert "error" not in updated, updated
    assert updated["detail"] == "new bio"
    assert updated["summary"] == "X — r"
    assert updated["reports_to"] == NO_MANAGER


def test_update_person_reports_to_nobody_clears_the_reporting_line(
    build_lc, make_ctx, mock_mcp, tmp_path
):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="Mgr", role="lead", seniority="senior", email="mgr@example.com",
        reports_to=NO_MANAGER,
    )
    mock_mcp.tools["cognition_register_person"](
        ctx, name="IC", role="ic", seniority="mid", email="ic@example.com",
        reports_to="mgr@example.com",
    )
    cleared = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="ic@example.com", reports_to=NO_MANAGER,
    )
    assert "error" not in cleared, cleared
    assert cleared["reports_to"] == NO_MANAGER
    assert cleared["reports_to_registered"] is False
    assert mock_mcp.tools["cognition_list_people"](ctx)["count"] == 3  # incl. the test user


def test_update_person_reports_to_omitted_leaves_unchanged(build_lc, make_ctx, mock_mcp, tmp_path):
    """None (omitted) means no change -- distinct from "nobody" (top of chain)."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="Mgr", role="lead", seniority="senior", email="mgr@example.com",
        reports_to=NO_MANAGER,
    )
    mock_mcp.tools["cognition_register_person"](
        ctx, name="IC", role="ic", seniority="mid", email="ic@example.com",
        reports_to="mgr@example.com",
    )
    updated = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="ic@example.com", role="ic2",
    )
    assert updated["reports_to"] == "mgr@example.com"


def test_update_person_self_reporting_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to=NO_MANAGER,
    )
    result = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="x@example.com", reports_to="x@example.com",
    )
    assert "error" in result


def test_update_person_cycle_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    """A -> B -> A must be rejected. Fails-before: no cycle guard would silently
    create an infinite reports_to loop."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="A", role="r", seniority="mid", email="a@example.com",
        reports_to=NO_MANAGER,
    )
    mock_mcp.tools["cognition_register_person"](
        ctx, name="B", role="r", seniority="mid", email="b@example.com",
        reports_to="a@example.com",
    )
    result = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="a@example.com", reports_to="b@example.com",
    )
    assert "error" in result


def test_update_person_dangling_reports_to_still_legal(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to=NO_MANAGER,
    )
    result = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="x@example.com", reports_to="ghost@example.com",
    )
    assert "error" not in result, result
    assert result["reports_to_registered"] is False


def test_update_person_invalid_seniority_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to=NO_MANAGER,
    )
    result = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="x@example.com", seniority="P0",
    )
    assert "error" in result


def test_update_person_no_fields_errors(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to=NO_MANAGER,
    )
    result = mock_mcp.tools["cognition_update_person"](ctx, email_or_id="x@example.com")
    assert "error" in result


def test_resubmitting_current_values_is_a_no_op_not_an_error(
    build_lc, make_ctx, mock_mcp, tmp_path
):
    """CONTRACT CHANGE: this used to return "No updatable fields provided", which
    was indistinguishable from passing nothing at all. Profiles are append-only, so
    "already that value" has an honest answer -- write nothing and say so."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to=NO_MANAGER,
    )
    again = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="x@example.com", role="r", seniority="mid",
    )
    assert "error" not in again, again
    assert again["profile_written"] == []
    assert sorted(again["profile_skipped"]) == ["role", "seniority"]


def test_update_person_not_found_errors(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="nobody@example.com", role="new",
    )
    assert "error" in result


# ── cognition_get_person / cognition_list_people ─────────────────────────────


def test_get_person_is_email_keyed_and_case_insensitive(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to=NO_MANAGER,
    )
    got = mock_mcp.tools["cognition_get_person"](ctx, email_or_id="X@Example.com")
    assert got["email"] == "x@example.com"
    assert set(got) >= ROW_KEYS
    assert got["profile_history"]
    assert got["environment"] == {}
    assert got["id"] is None  # no backing node any more


def test_get_person_joins_environment_facts(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity):
    graph_identity.acting_as("X", "x@example.com")
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to=NO_MANAGER,
    )
    mock_mcp.tools["cognition_set_env_fact"](ctx, key="os", value="w11", machine="desk")
    got = mock_mcp.tools["cognition_get_person"](ctx, email_or_id="x@example.com")
    assert got["environment"] == {"desk": {"os": "w11"}}


def test_get_person_not_found(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_get_person"](ctx, email_or_id="nobody@example.com")
    assert "error" in result


def test_list_people_roster_sorted_by_name(build_lc, make_ctx, mock_mcp, tmp_path, graph_identity):
    graph_identity.acting_as("Mid", "mid@example.com")
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="Zed", role="r", seniority="mid", email="zed@example.com",
        reports_to=NO_MANAGER,
    )
    mock_mcp.tools["cognition_register_person"](
        ctx, name="Amy", role="r", seniority="mid", email="amy@example.com",
        reports_to=NO_MANAGER,
    )
    roster = mock_mcp.tools["cognition_list_people"](ctx)
    names = [p["name"] for p in roster["people"]]
    assert names == sorted(names, key=str.casefold)
    assert names.index("Amy") < names.index("Zed")
    assert roster["count"] == len(roster["people"])
    assert set(roster["people"][0]) == ROW_KEYS


def test_a_legacy_person_node_still_appears_on_the_roster(build_lc, make_ctx, mock_mcp, tmp_path):
    """A clone that predates the migration has person NODES and no profiles. Its
    roster must still work, or upgrading would silently empty the team list and
    remove every seniority weighting from search ranking."""
    from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType

    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    lc["cognition_storage"].add_node(CognitionNode(
        id="legacyperson", type=CognitionNodeType.PERSON, summary="Old Timer — sre",
        detail="from before the migration", context=[], references=[],
        timestamp="2026-01-01T00:00:00+00:00", author="Old Timer",
        metadata={"person": {"email": "old@example.com", "name": "Old Timer",
                             "role": "sre", "seniority": "senior",
                             "reports_to_email": ""},
                  "profile_history": [{"changed": {"role": {"from": "", "to": "sre"}},
                                       "at": "2026-01-02T00:00:00+00:00",
                                       "by": {"name": "A", "email": "a@example.com"}}]},
    ))

    row = mock_mcp.tools["cognition_get_person"](ctx, email_or_id="old@example.com")
    assert row["name"] == "Old Timer"
    assert row["seniority"] == "senior"
    assert row["source"] == "node"
    assert row["id"] == "legacyperson"
    # The node's own older-shaped history is returned rather than an empty list.
    assert row["profile_history"][0]["changed"]["role"]["to"] == "sre"

    emails = [p["email"] for p in mock_mcp.tools["cognition_list_people"](ctx)["people"]]
    assert "old@example.com" in emails


def test_a_profile_wins_over_a_legacy_node_for_the_same_email(
    build_lc, make_ctx, mock_mcp, tmp_path
):
    """The migration writes a profile per node and may run while stale nodes are
    still present. The profile is the newer answer, so a double-counted person or
    a resurrected old role would both be wrong."""
    from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType

    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    lc["cognition_storage"].add_node(CognitionNode(
        id="dupe", type=CognitionNodeType.PERSON, summary="Both — old role",
        detail="", context=[], references=[], timestamp="2026-01-01T00:00:00+00:00",
        author="Both",
        metadata={"person": {"email": "both@example.com", "name": "Both",
                             "role": "old role", "seniority": "junior",
                             "reports_to_email": ""}},
    ))
    mock_mcp.tools["cognition_register_person"](
        ctx, name="Both", role="new role", seniority="senior",
        email="both@example.com", reports_to=NO_MANAGER,
    )

    rows = [p for p in mock_mcp.tools["cognition_list_people"](ctx)["people"]
            if p["email"] == "both@example.com"]
    assert len(rows) == 1
    assert rows[0]["role"] == "new role"
    assert rows[0]["source"] == "profile"


# ── people are no longer searchable ──────────────────────────────────────────


def test_searching_for_people_says_where_the_roster_lives(build_lc, make_ctx, mock_mcp, tmp_path):
    """CONTRACT CHANGE: profiles carry no vector, so node_type="person" can never
    match. An empty result would read as "nobody is registered"; the error names
    the tool that does answer the question."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="Alpha Person", role="engineer alpha", seniority="mid",
        email="alpha@example.com", reports_to=NO_MANAGER,
    )
    result = mock_mcp.tools["cognition_search"](ctx, query="alpha", node_type="person")
    assert "error" in result
    assert "cognition_list_people" in result["error"]


def test_registering_someone_embeds_nothing(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)
    chroma = lc["cognition_embedding_storage"]
    before = chroma.count_documents()

    mock_mcp.tools["cognition_register_person"](
        ctx, name="P", role="alpha role", seniority="mid", email="p@example.com",
        reports_to=NO_MANAGER,
    )
    mock_mcp.tools["cognition_update_person"](ctx, email_or_id="p@example.com", role="beta role")
    assert chroma.count_documents() == before


# ── from_agent (WP-TC6) ───────────────────────────────────────────────────────


def test_register_person_from_agent_defaults_true(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to=NO_MANAGER,
    )
    assert result["from_agent"] is True


def test_register_person_from_agent_explicit_false_lands_in_the_records(
    build_lc, make_ctx, mock_mcp, tmp_path
):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to=NO_MANAGER, from_agent=False,
    )
    assert result["from_agent"] is False
    history = mock_mcp.tools["cognition_get_person"](ctx, email_or_id="x@example.com")[
        "profile_history"
    ]
    assert all(r["from_agent"] is False for r in history)


def test_update_person_from_agent_is_per_record_not_per_person(
    build_lc, make_ctx, mock_mcp, tmp_path
):
    """Provenance belongs to each CHANGE, not to the person: a human-dictated edit
    on top of an agent-written registration must not retroactively relabel the
    registration."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to=NO_MANAGER, from_agent=True,
    )
    updated = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="x@example.com", role="r2", from_agent=False,
    )
    assert updated["from_agent"] is False

    history = mock_mcp.tools["cognition_get_person"](ctx, email_or_id="x@example.com")[
        "profile_history"
    ]
    assert history[0]["from_agent"] is True
    assert history[-1]["from_agent"] is False


def test_cognition_record_from_agent_defaults_true_and_false_honored(
    build_lc, make_ctx, mock_mcp, tmp_path
):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    default = mock_mcp.tools["cognition_record"](
        ctx, node_type="decision", summary="s1", detail="d", context="c", author="a",
    )
    explicit = mock_mcp.tools["cognition_record"](
        ctx, node_type="decision", summary="s2", detail="d", context="c", author="a",
        from_agent=False,
    )
    storage = lc["cognition_storage"]
    assert storage.get_node(default["id"])["metadata"]["from_agent"] is True
    assert storage.get_node(explicit["id"])["metadata"]["from_agent"] is False


def test_cognition_add_task_from_agent_defaults_true_and_false_honored(
    build_lc, make_ctx, mock_mcp, tmp_path
):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    default = mock_mcp.tools["cognition_add_task"](ctx, summary="t1", detail="d", context="c")
    explicit = mock_mcp.tools["cognition_add_task"](
        ctx, summary="t2", detail="d", context="c", from_agent=False,
    )
    assert default["metadata"]["from_agent"] is True
    assert explicit["metadata"]["from_agent"] is False

    rows = {t["id"]: t for t in mock_mcp.tools["cognition_list_tasks"](ctx)["tasks"]}
    assert rows[default["id"]]["from_agent"] is True
    assert rows[explicit["id"]]["from_agent"] is False


def test_cognition_store_document_from_agent_defaults_true_and_false_honored(
    build_lc, make_ctx, mock_mcp, tmp_path
):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage = lc["cognition_storage"]

    default = mock_mcp.tools["cognition_store_document"](
        ctx, title="doc1", document_text="hello", context="c", author="a",
        content_text="hello world",
    )
    explicit = mock_mcp.tools["cognition_store_document"](
        ctx, title="doc2", document_text="hello", context="c", author="a",
        content_text="a different document body", from_agent=False,
    )
    assert storage.get_node(default["node_id"])["metadata"]["from_agent"] is True
    assert storage.get_node(explicit["node_id"])["metadata"]["from_agent"] is False


def test_search_result_from_agent_missing_key_is_none_not_false(
    build_lc, make_ctx, mock_mcp, tmp_path
):
    """A node embedded before from_agent existed has NO 'from_agent' key in Chroma
    metadata -- the search result must surface None ("unknown"), never coerce it
    to False (which would misrepresent unknown provenance as "known agent-written")."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)
    chroma = lc["cognition_embedding_storage"]

    storage = lc["cognition_storage"]
    from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
    node = CognitionNode(
        id="legacy1", type=CognitionNodeType.DECISION, summary="legacy alpha decision",
        detail="d", context=[], references=[], timestamp="2026-01-01T00:00:00+00:00",
        author="a", metadata={},  # no recorded_by/from_agent -- pre-existing node
    )
    storage.add_node(node)
    chroma.upsert_embedding(
        "legacy1", lc["embedding_generator"].generate("decision: legacy alpha decision\nd"),
        {"entity_type": "decision", "summary": "legacy alpha decision"},
    )

    result = mock_mcp.tools["cognition_search"](ctx, query="alpha", node_type="decision")
    assert result["count"] == 1
    assert result["results"][0]["from_agent"] is None


# ── prime.py: the roster must stay invisible to the solo digest ─────────────


def test_solo_prime_byte_identical_with_and_without_a_roster(tmp_path, graph_identity):
    """The roster is otherwise invisible to prime.py -- the solo digest is
    byte-identical whether or not people are registered, PROVIDED no current_email
    is passed (as here). WP-TC7 landed a roster-aware onboarding notice, but it is
    gated on a resolvable current_email -- both calls below pass none, so this pin
    is unaffected and intentionally retained. See test_prime.py's onboarding block
    for the TC7-specific byte-identity pins."""
    from vibe_cognition.cognition import CognitionStorage
    from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType

    graph_identity.unonboarded()

    def _make_storage(path):
        storage = CognitionStorage(path / ".cognition")
        storage.add_node(CognitionNode(
            id="dec1", type=CognitionNodeType.DECISION, summary="a decision",
            detail="d", context=[], references=[], timestamp="2026-01-01T00:00:00+00:00",
            author="a",
        ))
        return storage

    baseline = _make_storage(tmp_path / "baseline")
    baseline_out = generate_prime(baseline, PrimeConfig())

    with_people = _make_storage(tmp_path / "with_people")
    with_people.set_profile_fields(
        "a@example.com",
        {"name": "A", "email": "a@example.com", "role": "role",
         "seniority": "mid", "reports_to": NO_MANAGER},
        {"name": "A", "email": "a@example.com"},
    )
    with_people_out = generate_prime(with_people, PrimeConfig())

    assert baseline_out == with_people_out
    assert "person" not in with_people_out.lower()
