"""Per-person committed profiles, and the shared JSONL directory engine."""

import json

import pytest

from vibe_cognition.cognition.people_facts import PeopleFactsRegistry
from vibe_cognition.cognition.profiles import (
    NO_MANAGER,
    PROFILE_FIELDS,
    SENIORITY_LEVELS,
    ProfileRegistry,
    profile_filename,
)

BY = {"name": "Tester", "email": "tester@example.com"}


@pytest.fixture
def registry(tmp_path):
    cognition = tmp_path / ".cognition"
    cognition.mkdir()
    return ProfileRegistry(cognition)


def _complete(reg, email="ada@example.com", **over):
    fields = {
        "name": "Ada", "email": email, "role": "engineer",
        "seniority": "senior", "reports_to": NO_MANAGER,
    }
    fields.update(over)
    reg.set_fields(email, fields, BY)
    reg.catch_up()
    return email


# ── completeness, which is what the write gate turns on ──────────────────────


def test_missing_required_lists_every_absent_field(registry):
    assert registry.missing_required("nobody@example.com") == [
        "name", "email", "role", "seniority", "reports_to",
    ]


def test_profile_is_complete_only_when_every_field_is_set(registry):
    registry.set_fields("ada@example.com", {"name": "Ada", "email": "ada@example.com"}, BY)
    registry.catch_up()
    assert not registry.is_complete("ada@example.com")
    assert registry.missing_required("ada@example.com") == ["role", "seniority", "reports_to"]

    _complete(registry)
    assert registry.is_complete("ada@example.com")


def test_nobody_is_a_real_reports_to_value_not_an_absence(registry):
    """Solo projects answer "nobody"; an empty string must NOT satisfy the gate."""
    _complete(registry, reports_to=NO_MANAGER)
    assert registry.get("ada@example.com")["reports_to"] == NO_MANAGER
    assert registry.is_complete("ada@example.com")

    registry.set_fields("bob@example.com", {
        "name": "Bob", "email": "bob@example.com", "role": "eng",
        "seniority": "mid", "reports_to": "   ",
    }, BY)
    registry.catch_up()
    assert "reports_to" in registry.missing_required("bob@example.com")


def test_unset_is_distinguishable_from_never_written(registry):
    _complete(registry)
    registry.unset_field("ada@example.com", "role", BY)
    registry.catch_up()
    assert registry.missing_required("ada@example.com") == ["role"]


# ── the blocker: multi-writer overlap ────────────────────────────────────────


def test_rewriting_one_file_keeps_another_files_fields(registry, tmp_path):
    """Profiles are trust-based multi-writer, so two files contributing to one
    email is NORMAL (a manager pre-registers, the person then confirms).

    Dropping one file's contribution must re-fold the other, or its fields are
    silently and permanently lost.
    """
    people = tmp_path / ".cognition" / "people"
    people.mkdir(parents=True, exist_ok=True)
    email = "bob@example.com"

    def rec(field, value, at):
        return json.dumps({
            "action": "profile_set", "email": email, "field": field,
            "value": value, "by": BY, "at": at,
        })

    manager_file = people / "manager-side.profile.jsonl"
    self_file = people / profile_filename(email)
    manager_file.write_text(rec("role", "engineer", "2026-01-01T00:00:00+00:00") + "\n", encoding="utf-8")
    self_file.write_text(rec("name", "Bob", "2026-01-02T00:00:00+00:00") + "\n", encoding="utf-8")

    registry.catch_up()
    assert registry.get(email)["role"] == "engineer"
    assert registry.get(email)["name"] == "Bob"

    # Rewrite ONE file divergently — the truncation/rewrite re-fold path.
    self_file.write_text(rec("name", "Robert", "2026-01-03T00:00:00+00:00") + "\n", encoding="utf-8")
    registry.catch_up()
    registry.catch_up()  # second pass completes the re-fold of the other file

    profile = registry.get(email)
    assert profile["name"] == "Robert"
    assert profile.get("role") == "engineer", "the other file's field was lost"


# ── fold order must not depend on merge direction ────────────────────────────


@pytest.mark.parametrize("order", [("old", "new"), ("new", "old")])
def test_fold_is_last_write_wins_by_timestamp_not_file_position(registry, tmp_path, order):
    """`merge=union` does not preserve chronological order, and the interleaving
    depends on which side merged. Position-ordered folding would let the same two
    commits produce different values on different machines."""
    people = tmp_path / ".cognition" / "people"
    people.mkdir(parents=True, exist_ok=True)
    recs = {
        "old": {"action": "profile_set", "email": "a@x.com", "field": "seniority",
                "value": "mid", "at": "2026-01-01T00:00:00+00:00"},
        "new": {"action": "profile_set", "email": "a@x.com", "field": "seniority",
                "value": "senior", "at": "2026-06-01T00:00:00+00:00"},
    }
    (people / profile_filename("a@x.com")).write_text(
        "\n".join(json.dumps(recs[k]) for k in order) + "\n", encoding="utf-8"
    )
    registry.catch_up()
    assert registry.get("a@x.com")["seniority"] == "senior"


# ── write surface ────────────────────────────────────────────────────────────


def test_unchanged_value_journals_nothing(registry):
    _complete(registry)
    result = registry.set_fields("ada@example.com", {"role": "engineer"}, BY)
    assert result == {"written": [], "skipped": ["role"]}


def test_seniority_is_validated_on_write(registry):
    result = registry.set_fields("ada@example.com", {"seniority": "archmage"}, BY)
    assert "error" in result
    registry.catch_up()
    assert registry.get("ada@example.com") is None

    for level in SENIORITY_LEVELS:
        assert "error" not in registry.set_fields(f"{level}@x.com", {"seniority": level}, BY)


def test_blank_email_is_rejected_by_both_write_paths(registry, tmp_path):
    assert "error" in registry.set_fields("   ", {"name": "X"}, BY)
    assert "error" in registry.unset_field("   ", "role", BY)
    people = tmp_path / ".cognition" / "people"
    assert not people.exists() or not list(people.glob("*.profile.jsonl"))


def test_direct_reports_are_indexed_without_rescanning(registry):
    _complete(registry, email="boss@example.com")
    _complete(registry, email="rep1@example.com", reports_to="boss@example.com")
    _complete(registry, email="rep2@example.com", reports_to="boss@example.com")
    assert registry.direct_reports("boss@example.com") == ["rep1@example.com", "rep2@example.com"]
    assert registry.direct_reports("rep1@example.com") == []


# ── two registries, one directory ────────────────────────────────────────────


def test_profile_and_fact_registries_do_not_see_each_others_files(tmp_path):
    """`"x.profile.jsonl".endswith(".jsonl")` is True, so the facts registry
    would otherwise read, hash and track every profile file — and a profile
    rewrite could trigger the FACTS registry's truncation re-fold."""
    cognition = tmp_path / ".cognition"
    cognition.mkdir()
    profiles = ProfileRegistry(cognition)
    facts = PeopleFactsRegistry(cognition)

    profiles.set_fields("ada@example.com", {"name": "Ada"}, BY)
    facts.set_fact("ada@example.com", "desk", "os", "Windows", BY, False)
    profiles.catch_up()
    facts.catch_up()

    assert all(n.endswith(".profile.jsonl") for n in profiles._files)
    assert not any(n.endswith(".profile.jsonl") for n in facts._files)
    ada = profiles.get("ada@example.com")
    assert ada is not None and ada["name"] == "Ada"
    assert facts.facts_for("ada@example.com") == {"desk": {"os": "Windows"}}


def test_fact_registry_excludes_profiles_even_if_profiles_never_imported(tmp_path):
    """The exclusion must not depend on import order.

    A self-registering subclass only claims its suffix once its module is
    imported, so anything importing people_facts alone would silently get the
    un-excluded behaviour back. The reserved set is therefore a literal in the
    engine, and this asserts it — using only the facts registry's own import.
    """
    people = tmp_path / ".cognition" / "people"
    people.mkdir(parents=True)
    (people / "someone.profile.jsonl").write_text(
        json.dumps({"action": "profile_set", "email": "a@x.com",
                    "field": "name", "value": "Ada", "at": "t"}) + "\n",
        encoding="utf-8",
    )
    (people / "someone.jsonl").write_text(
        json.dumps({"action": "fact_set", "email": "a@x.com",
                    "machine": "m", "key": "k", "value": "v"}) + "\n",
        encoding="utf-8",
    )

    facts = PeopleFactsRegistry(tmp_path / ".cognition")
    facts.catch_up()

    assert not any(n.endswith(".profile.jsonl") for n in facts._files)
    assert facts.facts_for("a@x.com") == {"m": {"k": "v"}}


# ── a wrong clock on ONE machine must not poison a field forever ────────────


def test_a_future_dated_record_cannot_lock_a_field_forever(registry):
    """The fold is last-write-wins by TIMESTAMP and these files merge across
    machines, so one teammate whose clock is a year ahead (dead CMOS battery,
    broken NTP, a hand-edited line) could write a record no later write can beat.
    Every correction after it would be appended, reported as written, and silently
    lose the fold — the worst shape available: a successful-looking no-op.
    """
    email = "victim@example.com"
    registry.set_fields(email, {"seniority": "mid"}, BY)
    registry.catch_up()

    people = registry._dir
    people.mkdir(parents=True, exist_ok=True)
    (people / profile_filename(email)).open("a", encoding="utf-8").write(
        json.dumps({
            "action": "profile_set", "email": email, "field": "seniority",
            "value": "owner", "by": {"name": "Skewed", "email": "skew@example.com"},
            "at": "2099-01-01T00:00:00+00:00",
        }) + "\n"
    )
    registry.catch_up()
    assert registry.get(email)["seniority"] == "owner"

    result = registry.set_fields(email, {"seniority": "senior"}, BY)
    registry.catch_up()
    assert registry.get(email)["seniority"] == "senior", "the correction lost the fold"
    assert "clock_skew" in result, "the skew must be surfaced, not silently absorbed"
    assert "seniority" in result["clock_skew"]


def test_a_normal_write_is_stamped_now_and_says_nothing_about_skew(registry):
    result = registry.set_fields("a@x.com", {"role": "eng"}, BY)
    assert "clock_skew" not in result
    registry.catch_up()
    assert registry.get("a@x.com")["role"] == "eng"


def test_unset_also_beats_a_future_dated_record(registry):
    """Otherwise cognition_remove_person silently fails on a skew-poisoned field
    while reporting the field cleared."""
    email = "victim@example.com"
    registry.set_fields(email, {"role": "eng"}, BY)
    registry.catch_up()
    (registry._dir / profile_filename(email)).open("a", encoding="utf-8").write(
        json.dumps({
            "action": "profile_set", "email": email, "field": "role",
            "value": "poisoned", "by": {"name": "S", "email": "s@x.com"},
            "at": "2099-01-01T00:00:00+00:00",
        }) + "\n"
    )
    registry.catch_up()

    registry.unset_field(email, "role", BY)
    registry.catch_up()
    assert (registry.get(email) or {}).get("role") is None


# ── the identity key must not merge two different people ────────────────────


def test_two_distinct_addresses_are_two_people_not_one(registry):
    """`str.casefold()` folds ß to ss and ligatures to their letters, so
    `groß@x.com` and `gross@x.com` — both valid, deliverable, and plausible for a
    German surname — became ONE identity: one profile file, one write-gate state,
    each silently overwriting the other's role and seniority.
    """
    a, b = "gross@x.com", "gro\u00df@x.com"
    assert profile_filename(a) != profile_filename(b)

    registry.set_fields(a, {"name": "Gross", "role": "eng"}, BY)
    registry.set_fields(b, {"name": "Gro\u00df", "role": "design"}, BY)
    registry.catch_up()

    assert registry.get(a)["role"] == "eng"
    assert registry.get(b)["role"] == "design"
    assert sorted(registry.all_emails()) == sorted([a, b])


def test_ascii_case_still_folds_to_one_person(registry):
    """The case-insensitivity guarantee is the whole reason folding exists; it
    must survive the switch from casefold() to lower()."""
    registry.set_fields("Alice@Example.COM", {"name": "Alice", "role": "eng"}, BY)
    registry.set_fields("alice@example.com", {"role": "lead"}, BY)
    registry.catch_up()

    assert registry.all_emails() == ["alice@example.com"]
    assert registry.get("ALICE@EXAMPLE.COM")["role"] == "lead"


def test_a_decomposed_accent_is_the_same_person_as_a_composed_one(registry):
    """NFC first, or one person splits in two depending on how their mail client
    encoded the accent — the opposite failure, and just as silent."""
    composed, decomposed = "caf\u00e9@x.com", "cafe\u0301@x.com"
    registry.set_fields(composed, {"name": "Cafe", "role": "eng"}, BY)
    registry.catch_up()
    assert registry.get(decomposed) is not None
    assert profile_filename(composed) == profile_filename(decomposed)


# ── removal has to survive an unrelated concurrent edit ─────────────────────


def _remove(reg, email):
    for field in PROFILE_FIELDS:
        reg.unset_field(email, field, BY)
    reg.mark_removed(email, BY)
    reg.catch_up()


def test_an_unrelated_single_field_edit_does_not_un_remove_someone(registry):
    """Two clones merge via `merge=union`: one removes a departed teammate, the
    other -- not yet aware -- edits that person's role. Under a newest-field-wins
    rule whichever landed later resurrected them with ONLY that field set: a roster
    row with a blank name and an incomplete profile, and no error anywhere. No
    malice, no unusual setup, just two people with some merge latency between them.
    """
    email = _complete(registry, email="leaver@x.com")
    _remove(registry, email)
    assert registry.is_removed(email)

    registry.set_fields(email, {"role": "staff engineer"}, {"name": "Peer", "email": "p@x.com"})
    registry.catch_up()
    assert registry.is_removed(email), "a single unrelated field must not resurrect"
    assert email in registry.removed_emails()


def test_a_deliberate_full_re_registration_brings_someone_back(registry):
    """The flip side: coming back must actually work, and carry the whole trail."""
    email = _complete(registry, email="boom@x.com")
    _remove(registry, email)
    assert registry.is_removed(email)

    _complete(registry, email=email, role="sre", seniority="mid")
    assert not registry.is_removed(email)
    assert registry.get(email)["role"] == "sre"
    assert registry.is_complete(email)
    # Nothing was lost: the removal is still in the trail.
    actions = [r.get("action") for r in registry.history_for(email)]
    assert "profile_removed" in actions


def test_a_tie_between_the_tombstone_and_a_field_favours_removal(registry, tmp_path):
    """Equal timestamps must resolve the same way on every machine, and the safe
    direction is staying removed -- a resurrection should be deliberate."""
    people = tmp_path / ".cognition" / "people"
    people.mkdir(parents=True, exist_ok=True)
    email = "tie@x.com"
    at = "2026-05-05T00:00:00+00:00"
    lines = [
        json.dumps({"action": "profile_set", "email": email, "field": f,
                    "value": "v", "by": BY, "at": at})
        for f in ("name", "email", "role", "seniority", "reports_to")
    ]
    lines.append(json.dumps({"action": "profile_removed", "email": email,
                             "by": BY, "at": at}))
    (people / profile_filename(email)).write_text("\n".join(lines) + "\n", encoding="utf-8")
    registry.catch_up()
    assert registry.is_removed(email)


def test_the_tombstone_survives_a_rewrite_of_another_file_for_the_same_email(
    registry, tmp_path
):
    """The removal and the fields can legitimately live in different files -- a
    manager's and the person's own. A divergent rewrite of one must not drop the
    other's tombstone."""
    people = tmp_path / ".cognition" / "people"
    people.mkdir(parents=True, exist_ok=True)
    email = "split@x.com"
    own = people / profile_filename(email)
    mgr = people / "manager-side.profile.jsonl"

    own.write_text("\n".join(
        json.dumps({"action": "profile_set", "email": email, "field": f, "value": "v",
                    "by": BY, "at": "2026-01-01T00:00:00+00:00"})
        for f in ("name", "email", "role", "seniority", "reports_to")
    ) + "\n", encoding="utf-8")
    mgr.write_text(json.dumps({"action": "profile_removed", "email": email,
                               "by": BY, "at": "2026-02-01T00:00:00+00:00"}) + "\n",
                   encoding="utf-8")
    registry.catch_up()
    assert registry.is_removed(email)

    # Rewrite the person's own file divergently (a merge would do this).
    own.write_text(json.dumps({"action": "profile_set", "email": email, "field": "role",
                               "value": "rewritten", "by": BY,
                               "at": "2026-01-15T00:00:00+00:00"}) + "\n", encoding="utf-8")
    registry.catch_up()
    registry.catch_up()  # the engine's re-fold of the overlapping file
    assert registry.is_removed(email), "the other file's tombstone was lost"
