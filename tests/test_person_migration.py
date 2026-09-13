"""Migrating legacy person nodes to committed profiles, and the tamper alert.

Phase 1 has to run automatically, because the write gate reads PROFILES: until a
person node becomes a profile, its owner is asked all five onboarding questions
again on a graph where someone already answered them. Phase 2 (removing the nodes)
stays manual so an interruption leaves harmless duplicates rather than a
half-deleted roster.
"""

import json

import pytest

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
from vibe_cognition.cognition.person_migration import (
    MIGRATION_FLAG_FILENAME,
    PERSON_MIGRATION_VERSION,
    ensure_person_migration,
    format_migration_announce,
    migrate_person_nodes,
)
from vibe_cognition.cognition.prime import (
    LAST_SEEN_FILENAME,
    PrimeConfig,
    _profile_change_alert,
    generate_prime,
)
from vibe_cognition.cognition.profiles import NO_MANAGER


def _person_node(node_id, email, name="Old Timer", role="sre", seniority="senior",
                 reports_to_email="", history=None, detail=""):
    return CognitionNode(
        id=node_id, type=CognitionNodeType.PERSON, summary=f"{name} — {role}",
        detail=detail, context=[], references=[],
        timestamp="2026-01-01T00:00:00+00:00", author=name,
        metadata={
            "person": {
                "email": email, "name": name, "role": role,
                "seniority": seniority, "reports_to_email": reports_to_email,
            },
            "profile_history": history or [],
        },
    )


@pytest.fixture
def legacy(tmp_path, graph_identity):
    """A graph that predates profiles: person nodes, no profiles."""
    graph_identity.unonboarded()
    storage = CognitionStorage(tmp_path / ".cognition")
    return storage


# ── phase 1 ──────────────────────────────────────────────────────────────────


def test_a_legacy_person_node_becomes_a_complete_profile(legacy):
    legacy.add_node(_person_node("p1", "old@example.com", detail="joined in 2019"))

    report = migrate_person_nodes(legacy)
    assert report["migrated"] == ["old@example.com"]

    profile = legacy.get_profile("old@example.com")
    assert profile is not None
    assert profile["name"] == "Old Timer"
    assert profile["role"] == "sre"
    assert profile["seniority"] == "senior"
    assert profile["detail"] == "joined in 2019"
    # The whole point: they are not asked the five questions again.
    assert legacy.profile_missing_required("old@example.com") == []


def test_an_empty_reports_to_becomes_nobody_not_an_unanswered_field(legacy):
    """A node stored "" for top-of-chain. Carried across verbatim it reads as
    "never answered", and every migrated solo owner fails the gate the moment they
    upgrade — the case the ruling calls the expected answer."""
    legacy.add_node(_person_node("p1", "solo@example.com", reports_to_email=""))

    migrate_person_nodes(legacy)
    assert legacy.get_profile("solo@example.com")["reports_to"] == NO_MANAGER
    assert legacy.profile_missing_required("solo@example.com") == []


def test_a_real_manager_email_is_carried_across(legacy):
    legacy.add_node(_person_node("p1", "ic@example.com", reports_to_email="mgr@example.com"))
    migrate_person_nodes(legacy)
    assert legacy.get_profile("ic@example.com")["reports_to"] == "mgr@example.com"


def test_migration_is_idempotent_and_never_overwrites_a_profile(legacy):
    """Run twice, and re-run with a node still present after an interrupted phase
    2: neither may resurrect the node's older values over the live profile."""
    legacy.add_node(_person_node("p1", "x@example.com", role="old role"))
    assert migrate_person_nodes(legacy)["migrated"] == ["x@example.com"]

    legacy.set_profile_fields(
        "x@example.com", {"role": "new role"},
        {"name": "X", "email": "x@example.com"},
    )
    second = migrate_person_nodes(legacy)
    assert second["migrated"] == []
    assert second["skipped_existing"] == ["x@example.com"]
    assert legacy.get_profile("x@example.com")["role"] == "new role"


def test_a_node_with_no_email_is_reported_not_invented(legacy):
    """Email IS the identity key. Inventing one would attribute someone's history
    to an address they never used, so this needs a human."""
    legacy.add_node(_person_node("pnoemail", ""))

    report = migrate_person_nodes(legacy)
    assert report["migrated"] == []
    assert report["skipped_no_email"] == ["pnoemail"]
    announce = format_migration_announce(report)
    assert "pnoemail" in announce
    assert "cognition_register_person" in announce


def test_an_unknown_seniority_is_dropped_rather_than_stored(legacy):
    """seniority is a closed set. A hand-edited value must not become a stored
    one, or it reaches search ranking as an unknown weight."""
    legacy.add_node(_person_node("p1", "x@example.com", seniority="archmage"))

    migrate_person_nodes(legacy)
    profile = legacy.get_profile("x@example.com")
    assert "seniority" not in profile
    # Reported as still missing, so the person is asked for it.
    assert legacy.profile_missing_required("x@example.com") == ["seniority"]


def test_the_migrated_profile_is_attributed_to_the_person_themselves(legacy):
    """Not to whoever happened to start the session — that would put a stranger in
    someone else's audit trail for a change they did not make."""
    legacy.add_node(_person_node("p1", "old@example.com"))
    migrate_person_nodes(legacy)

    records = legacy.profile_history("old@example.com")
    assert records
    assert all(r["by"]["email"] == "old@example.com" for r in records if "by" in r)
    assert all(r.get("from_agent") is False for r in records if r.get("action") == "profile_set")


def test_legacy_profile_history_is_preserved_but_never_folded(legacy):
    """The old shape is one entry per update CALL listing several fields, so
    replaying it as sets would need invented timestamps competing with real ones in
    the last-write-wins fold. Kept readable under its own action instead."""
    entry = {
        "changed": {"role": {"from": "junior", "to": "sre"}},
        "at": "2026-02-01T00:00:00+00:00",
        "by": {"name": "A Manager", "email": "mgr@example.com"},
    }
    legacy.add_node(_person_node("p1", "old@example.com", history=[entry]))
    migrate_person_nodes(legacy)

    records = legacy.profile_history("old@example.com")
    carried = [r for r in records if r.get("action") == "profile_history_legacy"]
    assert len(carried) == 1
    assert carried[0]["entries"] == [entry]
    assert carried[0]["from_node"] == "p1"
    # It did not become a value: role is still what the node said.
    assert legacy.get_profile("old@example.com")["role"] == "sre"


# ── the automatic pass ───────────────────────────────────────────────────────


def test_storage_construction_migrates_once_and_records_a_flag(tmp_path, graph_identity):
    graph_identity.unonboarded()
    cognition = tmp_path / ".cognition"
    seed = CognitionStorage(cognition)
    seed.add_node(_person_node("p1", "old@example.com"))

    # A fresh storage over the same dir is what a new session does.
    first = CognitionStorage(cognition)
    assert first.person_migration_report is None  # already flagged by `seed`
    # ...so migrate explicitly to prove the path, then confirm the flag blocks it.
    assert (cognition / "local" / MIGRATION_FLAG_FILENAME).read_text(
        encoding="utf-8"
    ).strip() == str(PERSON_MIGRATION_VERSION)
    assert ensure_person_migration(first) is None


def test_the_flag_is_not_written_when_something_errored(tmp_path, graph_identity, monkeypatch):
    """A half-done migration must be retried, or the people it failed on stay
    locked out of writing with nothing scheduled to fix it."""
    graph_identity.unonboarded()
    cognition = tmp_path / ".cognition"
    storage = CognitionStorage(cognition)
    storage.add_node(_person_node("p1", "old@example.com"))
    (cognition / "local" / MIGRATION_FLAG_FILENAME).unlink()

    monkeypatch.setattr(
        type(storage), "set_profile_fields",
        lambda self, *a, **kw: {"error": "disk on fire"},
    )
    report = ensure_person_migration(storage)
    assert report is not None and report["errors"]
    assert not (cognition / "local" / MIGRATION_FLAG_FILENAME).exists()


def test_a_graph_with_no_person_nodes_is_a_completed_migration(tmp_path, graph_identity):
    """"nothing to do" must flag as done, or every startup rescans forever."""
    graph_identity.unonboarded()
    cognition = tmp_path / ".cognition"
    CognitionStorage(cognition)
    assert (cognition / "local" / MIGRATION_FLAG_FILENAME).exists()


def test_prime_announces_the_migration_loudly(tmp_path, graph_identity):
    graph_identity.unonboarded()
    cognition = tmp_path / ".cognition"
    seed = CognitionStorage(cognition)
    seed.add_node(_person_node("p1", "old@example.com"))
    (cognition / "local" / MIGRATION_FLAG_FILENAME).unlink()

    storage = CognitionStorage(cognition)
    note = format_migration_announce(storage.person_migration_report or {})
    assert "Roster migrated" in note
    assert "old@example.com" in note
    assert "NOT committed" in note
    assert "cognition_remove_node" in note  # the leftover nodes need a decision


def test_the_announce_is_silent_when_nothing_happened():
    assert format_migration_announce({}) == ""
    assert format_migration_announce(
        {"migrated": [], "skipped_existing": ["a@b.com"], "skipped_no_email": [], "errors": []}
    ) == ""


# ── phase 2 is manual, and the roster tolerates the in-between state ─────────


def test_the_roster_prefers_the_profile_while_the_node_is_still_there(legacy):
    legacy.add_node(_person_node("p1", "old@example.com", role="old role"))
    migrate_person_nodes(legacy)
    legacy.set_profile_fields(
        "old@example.com", {"role": "current role"},
        {"name": "Old Timer", "email": "old@example.com"},
    )

    roster = legacy.roster()
    assert len(roster.all()) == 1
    person = roster.get("old@example.com")
    assert person.role == "current role"
    assert person.source == "profile"


# ── the tamper alert (B5) ────────────────────────────────────────────────────


def _stamp_last_seen(cognition, email, at):
    local = cognition / "local"
    local.mkdir(parents=True, exist_ok=True)
    (local / LAST_SEEN_FILENAME).write_text(json.dumps({email: at}), encoding="utf-8")


def test_a_foreign_change_to_your_profile_raises_a_loud_alert(tmp_path, graph_identity):
    graph_identity.unonboarded()
    cognition = tmp_path / ".cognition"
    storage = CognitionStorage(cognition)
    me = "me@example.com"
    storage.set_profile_fields(
        me,
        {"name": "Me", "email": me, "role": "engineer", "seniority": "mid",
         "reports_to": NO_MANAGER},
        {"name": "Me", "email": me},
    )
    _stamp_last_seen(cognition, me, "2026-01-01T00:00:00+00:00")
    storage.set_profile_fields(
        me, {"seniority": "junior", "role": "support"},
        {"name": "A Manager", "email": "mgr@example.com"},
    )

    alert = _profile_change_alert(storage, me)
    assert "Someone else changed YOUR profile" in alert
    assert "A Manager" in alert
    # Seniority leads, and says why it matters.
    first_bullet = [ln for ln in alert.splitlines() if ln.startswith("- ")][0]
    assert "seniority" in first_bullet
    assert "reweights" in first_bullet
    assert "'mid'" in alert and "'junior'" in alert  # from -> to
    assert "cognition_update_person" in alert


def test_your_own_changes_never_alert(tmp_path, graph_identity):
    graph_identity.unonboarded()
    cognition = tmp_path / ".cognition"
    storage = CognitionStorage(cognition)
    me = "me@example.com"
    storage.set_profile_fields(
        me, {"name": "Me", "email": me, "role": "engineer"}, {"name": "Me", "email": me},
    )
    _stamp_last_seen(cognition, me, "2026-01-01T00:00:00+00:00")
    storage.set_profile_fields(me, {"role": "lead"}, {"name": "Me", "email": me})

    assert _profile_change_alert(storage, me) == ""


def test_changes_from_before_you_last_looked_do_not_re_alert(tmp_path, graph_identity):
    """Otherwise the same warning reappears every session forever, and a real one
    is lost in it."""
    graph_identity.unonboarded()
    cognition = tmp_path / ".cognition"
    storage = CognitionStorage(cognition)
    me = "me@example.com"
    storage.set_profile_fields(
        me, {"name": "Me", "email": me, "seniority": "mid"}, {"name": "Me", "email": me},
    )
    storage.set_profile_fields(
        me, {"seniority": "senior"}, {"name": "Mgr", "email": "mgr@example.com"},
    )
    _stamp_last_seen(cognition, me, "2099-01-01T00:00:00+00:00")

    assert _profile_change_alert(storage, me) == ""


def test_a_first_run_with_no_marker_says_nothing(tmp_path, graph_identity):
    """With no baseline every legitimate pre-registration would read as tampering,
    and a brand-new user sees their profile in the identity header anyway."""
    graph_identity.unonboarded()
    cognition = tmp_path / ".cognition"
    storage = CognitionStorage(cognition)
    me = "me@example.com"
    storage.set_profile_fields(
        me, {"name": "Me", "email": me, "role": "engineer", "seniority": "mid",
             "reports_to": NO_MANAGER},
        {"name": "A Manager", "email": "mgr@example.com"},
    )
    assert _profile_change_alert(storage, me) == ""


def test_the_alert_is_the_first_thing_in_the_digest(tmp_path, graph_identity):
    """It is about the reader and it is the one thing they may need to dispute, so
    it must not be buried under the project context."""
    graph_identity.unonboarded()
    cognition = tmp_path / ".cognition"
    storage = CognitionStorage(cognition)
    me = "me@example.com"
    storage.add_node(CognitionNode(
        id="d1", type=CognitionNodeType.DECISION, summary="a decision", detail="d",
        context=[], references=[], timestamp="2026-01-01T00:00:00+00:00", author="a",
    ))
    storage.set_profile_fields(
        me, {"name": "Me", "email": me, "seniority": "mid"}, {"name": "Me", "email": me},
    )
    _stamp_last_seen(cognition, me, "2026-01-01T00:00:00+00:00")
    storage.set_profile_fields(
        me, {"seniority": "junior"}, {"name": "Mgr", "email": "mgr@example.com"},
    )

    out = generate_prime(storage, PrimeConfig(), current_email=me)
    header = "# Vibe Cognition — Project Context\n\n"
    assert out.startswith(header + "## ⚠ Someone else changed YOUR profile")


def test_an_empty_email_never_alerts(tmp_path, graph_identity):
    graph_identity.unonboarded()
    storage = CognitionStorage(tmp_path / ".cognition")
    assert _profile_change_alert(storage, "") == ""
