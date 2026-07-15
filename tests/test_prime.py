"""T-1b: prime.py — generate_prime() sections and main() hook payload.

Pins: stdout IS the hook payload (SessionStart JSON), sections assembled correctly
(constraints severity-sorted + C2 gated, incidents severity+window gated, empty→
onboarding block), migration note + hygiene line ordering, PrimeConfig trim knobs
(_truncate, task cap, severity gates) and its equivalence with Settings defaults.
The onboarding branch is covered by test_readme.py; this adds the populated-graph
main() payload path.
"""

import io
import json
from dataclasses import fields
from datetime import UTC, datetime, timedelta

import pytest

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.models import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
)
from vibe_cognition.cognition.prime import (
    ONBOARD_DECLINE_FILENAME,
    ONBOARDING_NOTICE,
    PrimeConfig,
    _derive_role,
    _truncate,
    generate_prime,
    main,
)
from vibe_cognition.cognition.readme import ONBOARDING_BLOCK
from vibe_cognition.config import Settings

# ── helpers ───────────────────────────────────────────────────────────────────


def _add(storage: CognitionStorage, node_id: str, ntype: CognitionNodeType,
         summary: str, *, severity: str | None = None, timestamp: str | None = None,
         metadata: dict | None = None,
         ) -> None:
    ts = timestamp or datetime.now(UTC).isoformat()
    storage.add_node(CognitionNode(
        id=node_id, type=ntype, summary=summary, detail="d",
        context=[], references=[], severity=severity, timestamp=ts, author="t",
        metadata=metadata or {},
    ))


def _task(storage: CognitionStorage, node_id: str, summary: str, *, severity: str | None = None,
          timestamp: str | None = None, created_by: dict | None = None,
          claimed_by: dict | None = None, status: str = "open",
          assigned_to: str | None = None) -> None:
    """Seed a task node with WP-P13n-1 provenance stamps (created_by/claimed_by) and/or
    a WP-TC8 assignment, for email-matching tests. `assigned_to` is a bare casefolded
    email STRING (not a {name,email} stamp dict), matching the real tool's shape."""
    meta: dict = {"status": status}
    if created_by is not None:
        meta["created_by"] = created_by
    if claimed_by is not None:
        meta["claimed_by"] = claimed_by
    if assigned_to is not None:
        meta["assigned_to"] = assigned_to
    _add(storage, node_id, CognitionNodeType.TASK, summary, severity=severity,
         timestamp=timestamp, metadata=meta)


# ── _truncate ─────────────────────────────────────────────────────────────────


def test_truncate_hard_cuts_when_no_whitespace():
    """No whitespace before maxlen (long URL/hash) → hard-cut at maxlen, not a
    near-complete-looking string missing only its last char."""
    text = "a" * 200
    result = _truncate(text, 50)
    assert result == ("a" * 50) + "…"


def test_truncate_short_string_untouched():
    text = "short enough"
    assert _truncate(text, 110) == text


def test_truncate_maxlen_zero_is_noop():
    text = "a" * 500
    assert _truncate(text, 0) == text


# ── PrimeConfig / Settings equivalence ────────────────────────────────────────


def test_primeconfig_defaults_match_settings_defaults():
    """A Settings() build failure in main() falls back to PrimeConfig() — that
    fallback must degrade to the SAME trimmed output, never the old fat one."""
    config = PrimeConfig()
    settings = Settings()
    for f in fields(PrimeConfig):
        assert getattr(config, f.name) == getattr(settings, f.name), f.name


# ── generate_prime ────────────────────────────────────────────────────────────


def test_generate_prime_empty_graph_returns_no_history(tmp_path):
    """generate_prime: empty graph → 'No cognition history recorded yet.' body.

    Fails-before: if empty sections produced a bare header with no body,
    confusing the agent about whether the graph is new or broken.
    """
    storage = CognitionStorage(tmp_path / ".cognition")
    result = generate_prime(storage)
    assert "No cognition history recorded yet." in result


def test_generate_prime_constraints_appear_in_output(tmp_path):
    """generate_prime: constraint nodes → '## Active Constraints' section."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "c1", CognitionNodeType.CONSTRAINT, "no bare commits", severity="high")
    result = generate_prime(storage)
    assert "## Active Constraints" in result
    assert "no bare commits" in result


def test_generate_prime_constraints_sorted_by_severity(tmp_path):
    """generate_prime: constraints sorted critical→high→normal→low.

    Fails-before: if sorting used lexicographic order ('critical' < 'high' is fine
    but 'low' < 'normal' would invert the scale) or if no sort was applied.
    Uses severities that survive the C2 gate (normal/critical) so the ordering
    assertion isn't confounded with the drop-low filter (covered separately).
    """
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "c1", CognitionNodeType.CONSTRAINT, "normal-priority guard", severity="normal")
    _add(storage, "c2", CognitionNodeType.CONSTRAINT, "critical hard rule", severity="critical")
    result = generate_prime(storage)
    assert result.index("critical hard rule") < result.index("normal-priority guard")


def test_generate_prime_constraint_c2_severity_gate(tmp_path):
    """C2 (human-confirmed): constraints drop ONLY `low`; None/normal/high survive.

    None must NOT be dropped — this is a distinct filter from the incident
    severity gate (which has its own threshold and must not collide with C2).
    """
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "c1", CognitionNodeType.CONSTRAINT, "low sev constraint", severity="low")
    _add(storage, "c2", CognitionNodeType.CONSTRAINT, "normal sev constraint", severity="normal")
    _add(storage, "c3", CognitionNodeType.CONSTRAINT, "none sev constraint", severity=None)
    _add(storage, "c4", CognitionNodeType.CONSTRAINT, "high sev constraint", severity="high")
    result = generate_prime(storage)
    assert "low sev constraint" not in result
    assert "normal sev constraint" in result
    assert "none sev constraint" in result
    assert "high sev constraint" in result


def test_generate_prime_incidents_windowed_14_days(tmp_path):
    """generate_prime: incidents older than the 14-day window are excluded.

    Pins the real 14d boundary with a 10-day (kept) and 20-day (excluded)
    incident — a 35-day-old node would pass trivially against both the old
    30-day and new 14-day windows and must not stand in for this test.
    """
    storage = CognitionStorage(tmp_path / ".cognition")
    old_ts = (datetime.now(UTC) - timedelta(days=20)).isoformat()
    recent_ts = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    _add(storage, "i1", CognitionNodeType.INCIDENT, "old outage", severity="high", timestamp=old_ts)
    _add(storage, "i2", CognitionNodeType.INCIDENT, "recent outage", severity="high", timestamp=recent_ts)

    result = generate_prime(storage)
    assert "recent outage" in result
    assert "old outage" not in result


def test_generate_prime_incident_severity_gate(tmp_path):
    """Incidents: keep only severity >= prime_incident_min_severity (high+critical);
    a `normal` incident is dropped even within the window."""
    storage = CognitionStorage(tmp_path / ".cognition")
    now = datetime.now(UTC).isoformat()
    _add(storage, "i1", CognitionNodeType.INCIDENT, "normal sev incident", severity="normal", timestamp=now)
    _add(storage, "i2", CognitionNodeType.INCIDENT, "high sev incident", severity="high", timestamp=now)

    result = generate_prime(storage)
    assert "normal sev incident" not in result
    assert "high sev incident" in result


def test_generate_prime_decisions_appear_in_output(tmp_path):
    """generate_prime: decision nodes → '## Recent Decisions' section."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "d1", CognitionNodeType.DECISION, "chose sqlite over postgres")
    result = generate_prime(storage)
    assert "## Recent Decisions" in result
    assert "chose sqlite over postgres" in result


def test_generate_prime_patterns_appear_in_output(tmp_path):
    """generate_prime: pattern nodes → '## Recent Patterns' section."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "p1", CognitionNodeType.PATTERN, "always use uv run for hooks")
    result = generate_prime(storage)
    assert "## Recent Patterns" in result
    assert "always use uv run for hooks" in result


def test_generate_prime_workflow_head_appears_in_output(tmp_path):
    """generate_prime: a workflow node -> '## Workflows' section with its title."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "w1", CognitionNodeType.WORKFLOW, "deploy to production")
    result = generate_prime(storage)
    assert "## Workflows" in result
    assert "deploy to production" in result


def test_generate_prime_superseded_workflow_excluded(tmp_path):
    """A workflow with an incoming SUPERSEDES edge is an old version -- only the
    HEAD (the superseding node) is shown, matching cognition_get_workflow's own
    HEAD-resolution semantics."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "w-old", CognitionNodeType.WORKFLOW, "old deploy steps")
    _add(storage, "w-new", CognitionNodeType.WORKFLOW, "new deploy steps")
    storage.add_edge(CognitionEdge(
        from_id="w-new", to_id="w-old", edge_type=CognitionEdgeType.SUPERSEDES,
        timestamp=datetime.now(UTC).isoformat(),
    ))
    result = generate_prime(storage)
    assert "new deploy steps" in result
    assert "old deploy steps" not in result


def test_generate_prime_workflow_limit_honored_via_config(tmp_path):
    """Workflow cap is wired to PrimeConfig.prime_workflow_limit, with the same
    overflow idiom used by tasks/constraints."""
    storage = CognitionStorage(tmp_path / ".cognition")
    for i in range(4):
        _add(storage, f"w{i}", CognitionNodeType.WORKFLOW, f"workflow {i}")

    result = generate_prime(storage, PrimeConfig(prime_workflow_limit=2))
    assert result.count("[workflow]") == 2
    assert "+2 more workflows" in result


def test_generate_prime_no_workflows_omits_section(tmp_path):
    """No workflow nodes -> no '## Workflows' header (consistent with every
    other section's empty-drops-the-section rule)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "d1", CognitionNodeType.DECISION, "some decision")
    result = generate_prime(storage)
    assert "## Workflows" not in result


def test_generate_prime_document_count_appears(tmp_path):
    """Stored document nodes -> a one-line count naming the document tools."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "doc1", CognitionNodeType.DOCUMENT, "spec.pdf")
    result = generate_prime(storage)
    assert "1 stored document" in result
    assert "cognition_store_document" in result


def test_generate_prime_zero_documents_omits_count_line(tmp_path):
    """No document nodes -> no document-count line (consistent with the
    empty-drops-the-section rule; the empty-graph case is covered separately)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "d1", CognitionNodeType.DECISION, "some decision")
    result = generate_prime(storage)
    assert "stored document" not in result


def test_generate_prime_superseded_document_excluded_from_count(tmp_path):
    """Documents version via SUPERSEDES too (document->document is a legal shape,
    the prior_version_id re-store path) -- a revised document must count once
    (its HEAD), not once per revision, mirroring the workflow HEAD-filter.

    Fails-before: _format_document_count raw-counted get_nodes_by_type(DOCUMENT)
    with no supersession filter, so a document revised once reported "2 stored
    documents" instead of "1".
    """
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "doc-old", CognitionNodeType.DOCUMENT, "spec v1.pdf")
    _add(storage, "doc-new", CognitionNodeType.DOCUMENT, "spec v2.pdf")
    storage.add_edge(CognitionEdge(
        from_id="doc-new", to_id="doc-old", edge_type=CognitionEdgeType.SUPERSEDES,
        timestamp=datetime.now(UTC).isoformat(),
    ))
    result = generate_prime(storage)
    assert "1 stored document" in result
    assert "2 stored documents" not in result


def test_generate_prime_task_cap_honored_via_config(tmp_path):
    """Task cap is wired to PrimeConfig — an explicit override changes the count
    without any env monkeypatching."""
    storage = CognitionStorage(tmp_path / ".cognition")
    for i in range(4):
        _add(storage, f"t{i}", CognitionNodeType.TASK, f"task {i}")

    result = generate_prime(storage, PrimeConfig(prime_task_cap=2))
    assert result.count("- [task]") == 2
    assert "+2 more open tasks" in result


def test_generate_prime_constraints_supersession_head_filter(tmp_path):
    """WP-P13n-2 folded fix (task 0d7e84d52537): a constraint with an incoming
    SUPERSEDES edge is an old version -- only the HEAD is shown, mirroring the
    existing workflow HEAD-filter. Fails-before: the old code raw-counted
    get_nodes_by_type(CONSTRAINT) with no supersession filter, so both the old
    and new constraint appeared (duplicate/contradictory guidance at session
    start)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "c-old", CognitionNodeType.CONSTRAINT, "old install-mechanics rule", severity="high")
    _add(storage, "c-new", CognitionNodeType.CONSTRAINT, "new install-mechanics rule", severity="high")
    storage.add_edge(CognitionEdge(
        from_id="c-new", to_id="c-old", edge_type=CognitionEdgeType.SUPERSEDES,
        timestamp=datetime.now(UTC).isoformat(),
    ))
    result = generate_prime(storage)
    assert "new install-mechanics rule" in result
    assert "old install-mechanics rule" not in result


# ── WP-P13n-2: personalization ──────────────────────────────────────────────

ME = {"name": "Alice", "email": "alice@x.com"}
TEAMMATE = {"name": "Bob", "email": "bob@x.com"}


def test_generate_prime_solo_graph_stays_global_auto_mode(tmp_path):
    """Solo graph (<=1 distinct stamped email) in 'auto' mode: output is the
    unchanged global digest, byte-identical to passing current_email=None,
    even though a resolvable current_email IS passed. This is the acceptance
    criterion's "solo output byte-identical" pin.

    prime_onboard=False on both calls: this pin is about PERSONALIZATION
    sectioning specifically (task-splitting must not change shape for a solo
    graph) -- it predates and is orthogonal to WP-TC7's onboarding notice, which
    is INTENTIONALLY current_email-gated on its own (a resolvable email with no
    person node is exactly what it must fire for). Onboarding-notice-vs-no-notice
    byte-identity is covered separately in the onboarding test block below."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t1", "solo task", severity="high", created_by=ME)
    config = PrimeConfig(prime_onboard=False)

    with_email = generate_prime(storage, config, current_email=ME["email"])
    without_email = generate_prime(storage, config, current_email=None)
    assert with_email == without_email
    assert "## Your Open Tasks" not in with_email
    assert "## Open Tasks" in with_email


def test_generate_prime_multiuser_auto_detect_personalizes(tmp_path):
    """>1 distinct stamped email in the graph flips 'auto' mode to personalized:
    the single 'Open Tasks' section is replaced by 'Your Open Tasks' + 'Team
    Critical'."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-mine", "my task", severity="normal", created_by=ME)
    _task(storage, "t-theirs", "teammate task", severity="normal", created_by=TEAMMATE)

    result = generate_prime(storage, current_email=ME["email"])
    assert "## Your Open Tasks" in result
    assert "## Open Tasks\n" not in result  # the un-split global header must not also appear


def test_generate_prime_no_email_stays_global_no_crash(tmp_path):
    """Unresolvable identity (empty/None email) -> global digest, no personal
    sections, no crash -- even in a multi-user graph and even with mode 'on'."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-mine", "my task", created_by=ME)
    _task(storage, "t-theirs", "teammate task", created_by=TEAMMATE)

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=None)
    assert "## Your Open Tasks" not in result
    assert "## Open Tasks" in result


def test_generate_prime_personalize_off_forces_global_even_multiuser(tmp_path):
    """config.prime_personalize='off' always yields the global digest, even
    with a multi-user graph and a resolvable current_email."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-mine", "my task", created_by=ME)
    _task(storage, "t-theirs", "teammate task", created_by=TEAMMATE)

    result = generate_prime(storage, PrimeConfig(prime_personalize="off"), current_email=ME["email"])
    assert "## Your Open Tasks" not in result
    assert "## Open Tasks" in result


def test_generate_prime_personalize_on_forces_personalized_even_solo(tmp_path):
    """config.prime_personalize='on' personalizes even a solo graph, as long as
    current_email resolves."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t1", "solo task", created_by=ME)

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "## Your Open Tasks" in result


def test_your_open_tasks_matches_email_only_not_owner_name(tmp_path):
    """Peer-review should-fix, closed in this WP: owner is free-text display-only
    and must NEVER be matched -- only created_by.email / claimed_by.email. A task
    whose owner name happens to equal the current user's name, but whose
    created_by/claimed_by emails don't match, must NOT appear under Your Open
    Tasks."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-mine", "my task", created_by=ME)
    _task(storage, "t-theirs", "teammate task", created_by=TEAMMATE)
    _add(
        storage, "t-owner-name-only", CognitionNodeType.TASK, "owner-name-matches only",
        metadata={"status": "open", "owner": "Alice", "created_by": TEAMMATE},
    )

    result = generate_prime(storage, current_email=ME["email"])
    assert "my task" in result
    assert "owner-name-matches only" not in result


def test_your_open_tasks_matches_via_claimed_by(tmp_path):
    """A task created by a teammate but claimed by the current user (WP-P13n-1
    claimed_by) counts as "yours" too -- created_by OR claimed_by, either one.

    Uses prime_personalize="on" rather than relying on auto-detect: both tasks
    here are created_by TEAMMATE (only the claim differs), so the multi-user
    auto-detect signal -- which is recorded_by.email/created_by.email only, by
    design (see _node_email) -- would see a single distinct email and stay
    global. That's tested separately; this test isolates claimed_by-matching.

    WP-TC16 extension (brief: "own-claims mapping documented (no new section)"):
    the manager/subordinate ruling's "own claims" half is already delivered by
    this exact claimed_by branch -- no new section duplicates it. Registering ME
    as a manager (with an unrelated direct report) proves that mapping still
    holds now that '## Your Team' exists: ME's own claim stays under Your Open
    Tasks, and is not also/instead surfaced under Your Team (which lists direct
    REPORTS' claims, never the manager's own)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-claimed", "claimed by me", created_by=TEAMMATE, claimed_by=ME)
    _task(storage, "t-theirs", "not mine", created_by=TEAMMATE)
    _person(storage, "p-me", ME["email"], name="Alice")
    _person(storage, "p-report", "carol@x.com", name="Carol", reports_to_email=ME["email"])

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "claimed by me" in result
    assert "not mine" not in result
    assert "## Your Open Tasks" in result
    your_team_idx = result.find("## Your Team")
    if your_team_idx != -1:
        assert result.index("claimed by me") < your_team_idx


def test_your_open_tasks_matches_via_assigned_to(tmp_path):
    """WP-TC8: a task created AND claimed by a teammate but assigned to the current
    user counts as "yours" too -- created_by OR claimed_by OR assigned_to, any one.
    Uses prime_personalize="on" for the same auto-detect-isolation reason as the
    claimed_by test above (assigned_to never feeds _distinct_stamped_emails)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-assigned", "assigned to me", created_by=TEAMMATE, assigned_to=ME["email"])
    _task(storage, "t-theirs", "not mine", created_by=TEAMMATE)

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "assigned to me" in result
    assert "not mine" not in result


def test_your_open_tasks_matches_assigned_to_case_insensitively(tmp_path):
    """assigned_to matching is casefolded, same as every other email match in this
    module."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-assigned", "assigned to me", created_by=TEAMMATE, assigned_to="ALICE@X.COM")

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "assigned to me" in result


def test_assigned_to_does_not_flip_multiuser_auto_detect(tmp_path):
    """assigned_to must NEVER feed _distinct_stamped_emails -- a solo graph (one
    creator) with a task assigned to a SECOND email must stay global under 'auto'
    (only recorded_by/created_by drive the multi-user signal, by design)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t1", "solo task", created_by=ME, assigned_to=TEAMMATE["email"])

    result = generate_prime(storage, current_email=ME["email"])
    assert "## Your Open Tasks" not in result
    assert "## Open Tasks" in result


def test_team_critical_excludes_tasks_already_shown_under_your_open_tasks(tmp_path):
    """A high/critical task that's already 'yours' appears once, under Your Open
    Tasks -- never duplicated under Team Critical."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-mine-critical", "my critical task", severity="critical", created_by=ME)
    _task(storage, "t-theirs-normal", "teammate normal task", severity="normal", created_by=TEAMMATE)

    result = generate_prime(storage, current_email=ME["email"])
    assert result.count("my critical task") == 1
    assert "## Team Critical" not in result  # no OTHER critical/high task exists


def test_team_critical_shows_other_high_severity_tasks(tmp_path):
    """A teammate's high/critical task, not claimed/created by the current user,
    surfaces under Team Critical when personalized."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-mine", "my task", severity="normal", created_by=ME)
    _task(storage, "t-theirs-critical", "teammate critical task", severity="critical", created_by=TEAMMATE)

    result = generate_prime(storage, current_email=ME["email"])
    assert "## Team Critical" in result
    assert "teammate critical task" in result


def test_your_recent_activity_matches_recorded_by_email(tmp_path):
    """'Your Recent Activity' shows your own episodes/decisions/discoveries
    (recorded_by.email match) and excludes a teammate's, even though both are
    present in a multi-user graph."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-mine", "my task", created_by=ME)
    _task(storage, "t-theirs", "teammate task", created_by=TEAMMATE)
    _add(storage, "e-mine", CognitionNodeType.EPISODE, "my episode", metadata={"recorded_by": ME})
    _add(storage, "e-theirs", CognitionNodeType.EPISODE, "teammate episode", metadata={"recorded_by": TEAMMATE})
    _add(storage, "dec-mine", CognitionNodeType.DECISION, "my decision", metadata={"recorded_by": ME})
    _add(storage, "disc-mine", CognitionNodeType.DISCOVERY, "my discovery", metadata={"recorded_by": ME})

    result = generate_prime(storage, current_email=ME["email"])
    assert "## Your Recent Activity" in result
    assert "my episode" in result
    assert "my decision" in result
    assert "my discovery" in result
    assert "teammate episode" not in result


def test_your_recent_activity_per_type_cap(tmp_path):
    """Each type in 'Your Recent Activity' is capped independently via its own
    config knob."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-mine", "my task", created_by=ME)
    _task(storage, "t-theirs", "teammate task", created_by=TEAMMATE)
    for i in range(4):
        _add(storage, f"e{i}", CognitionNodeType.EPISODE, f"my episode {i}", metadata={"recorded_by": ME})

    result = generate_prime(
        storage, PrimeConfig(prime_your_episode_limit=2), current_email=ME["email"],
    )
    assert result.count("[episode]") == 2


def test_generate_prime_your_activity_omitted_when_no_matching_activity(tmp_path):
    """Personalized mode with no activity of the current user's own -> no 'Your
    Recent Activity' header at all (consistent with the empty-drops-the-section
    rule used everywhere else)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-mine", "my task", created_by=ME)
    _task(storage, "t-theirs", "teammate task", created_by=TEAMMATE)
    _add(storage, "e-theirs", CognitionNodeType.EPISODE, "teammate episode", metadata={"recorded_by": TEAMMATE})

    result = generate_prime(storage, current_email=ME["email"])
    assert "## Your Recent Activity" not in result


def test_your_open_tasks_matches_case_insensitively(tmp_path):
    """Email matching is case-insensitive (casefold): a task stamped with a
    differently-cased email than the resolved current_email must still land
    under Your Open Tasks, not silently vanish. Peer-review-confirmed defect,
    fixed on top of 5364163 -- fails-before at that commit (exact match only)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    me_uppercased = {"name": "Alice", "email": "Alice@X.COM"}
    _task(storage, "t-mine", "my task differently cased", created_by=me_uppercased)
    _task(storage, "t-theirs", "teammate task", created_by=TEAMMATE)

    result = generate_prime(storage, current_email=ME["email"])  # "alice@x.com"
    assert "## Your Open Tasks" in result
    assert "my task differently cased" in result


def test_solo_graph_two_casings_of_one_email_stays_global_under_auto(tmp_path):
    """A solo user whose stamped emails vary only in casing (e.g. across two
    machines) must NOT false-trip the multi-user auto-detect into personalizing
    -- casefolded, both stamps count as ONE distinct email. Fails-before at
    5364163 (exact-match distinct-count sees two)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    me_uppercased = {"name": "Alice", "email": "ALICE@X.COM"}
    _task(storage, "t1", "task one", created_by=ME)
    _task(storage, "t2", "task two", created_by=me_uppercased)

    result = generate_prime(storage, current_email=ME["email"])
    assert "## Your Open Tasks" not in result
    assert "## Open Tasks" in result


def test_your_recent_activity_matches_cross_case(tmp_path):
    """'Your Recent Activity' matches recorded_by.email case-insensitively too.
    Fails-before at 5364163."""
    storage = CognitionStorage(tmp_path / ".cognition")
    me_uppercased = {"name": "Alice", "email": "ALICE@X.COM"}
    _task(storage, "t-mine", "my task", created_by=ME)
    _task(storage, "t-theirs", "teammate task", created_by=TEAMMATE)
    _add(storage, "e-mine", CognitionNodeType.EPISODE, "my cross-case episode", metadata={"recorded_by": me_uppercased})

    result = generate_prime(storage, current_email=ME["email"])
    assert "## Your Recent Activity" in result
    assert "my cross-case episode" in result


# ── WP-TC7: onboarding notice ────────────────────────────────────────────────


def _person(
    storage: CognitionStorage, node_id: str, email: str, *, name: str = "Someone",
    role: str = "engineer", seniority: str = "", reports_to_email: str = "",
) -> None:
    """Seed a minimal person node — the fields _has_person_node/_derive_role/
    _format_identity_header read.

    WP-TC16: gained `reports_to_email` (default "", matching a real person node's
    always-present-but-possibly-empty field) so the same helper serves both the
    onboarding tests (which never set it) and the role-derivation tests.

    WP-OnboardPayoff: gained `role` (defaulted to the pre-existing hardcoded
    "engineer", so every existing call site is byte-identical) and `seniority`
    (defaulted to "" — OMITTED from metadata entirely when blank, matching every
    pre-WP person node's shape exactly, not just an empty-string key) for the
    identity-header degradation-case tests."""
    person: dict = {
        "email": email.casefold(), "name": name, "role": role,
        "reports_to_email": reports_to_email.casefold(),
    }
    if seniority:
        person["seniority"] = seniority
    _add(
        storage, node_id, CognitionNodeType.PERSON, f"person: {name}",
        metadata={"person": person},
    )


def test_onboarding_notice_fires_for_unregistered_email_solo_graph(tmp_path):
    """Notice fires: resolvable current_email, no matching person node, solo graph
    (no other stamped emails at all -- must not be confused with the multi-user
    auto-detect gate, which is a wholly separate mechanism)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "d1", CognitionNodeType.DECISION, "some decision")

    result = generate_prime(storage, current_email=ME["email"])
    assert "## New Here?" in result
    assert ONBOARDING_NOTICE in result


def test_onboarding_notice_fires_for_unregistered_email_multiuser_graph(tmp_path):
    """Notice fires the same way in a multi-user graph -- personalization mode is
    orthogonal to the onboarding notice."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-mine", "my task", created_by=ME)
    _task(storage, "t-theirs", "teammate task", created_by=TEAMMATE)

    result = generate_prime(storage, current_email=ME["email"])
    assert "## New Here?" in result


def test_onboarding_notice_suppressed_when_person_node_registered(tmp_path):
    """A matching (casefolded) person node for current_email suppresses the notice."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-me", ME["email"])

    result = generate_prime(storage, current_email=ME["email"])
    assert "## New Here?" not in result


def test_onboarding_notice_suppressed_when_person_node_registered_cross_case(tmp_path):
    """Person-node match is case-insensitive, matching every other email-matching
    convention in this module (casefold, not exact string equality)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-me", "ALICE@X.COM")

    result = generate_prime(storage, current_email=ME["email"])  # "alice@x.com"
    assert "## New Here?" not in result


def test_onboarding_notice_suppressed_when_email_unresolvable(tmp_path):
    """No resolvable current_email -> nothing to onboard, notice never fires."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "d1", CognitionNodeType.DECISION, "some decision")

    result = generate_prime(storage, current_email=None)
    assert "## New Here?" not in result


def test_onboarding_notice_suppressed_when_declined(tmp_path):
    """An email present (casefolded) in .cognition/onboard-declined suppresses the
    notice -- the per-machine, git-ignored decline/snooze path."""
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    (cognition_dir / ONBOARD_DECLINE_FILENAME).write_text(
        ME["email"] + "\n", encoding="utf-8"
    )

    result = generate_prime(storage, current_email=ME["email"])
    assert "## New Here?" not in result


def test_onboarding_notice_decline_file_matches_case_insensitively(tmp_path):
    """Decline-file matching is casefolded, same as every other email match here."""
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    (cognition_dir / ONBOARD_DECLINE_FILENAME).write_text(
        "ALICE@X.COM\n", encoding="utf-8"
    )

    result = generate_prime(storage, current_email=ME["email"])  # "alice@x.com"
    assert "## New Here?" not in result


def test_onboarding_notice_decline_file_missing_is_empty_set_no_crash(tmp_path):
    """No decline file at all -> treated as no declines, no crash (the common case:
    most machines never write this file)."""
    storage = CognitionStorage(tmp_path / ".cognition")

    result = generate_prime(storage, current_email=ME["email"])
    assert "## New Here?" in result


def test_onboarding_notice_decline_file_blank_and_malformed_lines_ignored(tmp_path):
    """Blank lines and pure-whitespace lines in the decline file are ignored, not
    treated as a (blank) declined email that would vacuously "match" nothing but
    could otherwise corrupt the set."""
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    (cognition_dir / ONBOARD_DECLINE_FILENAME).write_text(
        "\n   \nbob@x.com\n\n", encoding="utf-8"
    )

    result = generate_prime(storage, current_email=ME["email"])
    assert "## New Here?" in result  # alice@x.com never declined
    result_bob = generate_prime(storage, current_email=TEAMMATE["email"])
    assert "## New Here?" not in result_bob  # bob@x.com did


def test_onboarding_notice_suppressed_when_prime_onboard_false_direct(tmp_path):
    """PrimeConfig(prime_onboard=False) suppresses the notice outright, even for an
    otherwise-qualifying unregistered email."""
    storage = CognitionStorage(tmp_path / ".cognition")

    result = generate_prime(storage, PrimeConfig(prime_onboard=False), current_email=ME["email"])
    assert "## New Here?" not in result


def test_onboarding_notice_suppressed_when_prime_onboard_false_via_settings(monkeypatch, tmp_path):
    """PRIME_ONBOARD=false flows Settings -> PrimeConfig.prime_onboard, matching
    every other prime_* knob's env-override wiring (field name uppercased, no
    prefix -- pydantic-settings' default, unlike vibe_cognition_no_git_hygiene
    which spells the prefix into the field name itself)."""
    monkeypatch.setenv("PRIME_ONBOARD", "false")
    settings = Settings()
    assert settings.prime_onboard is False

    storage = CognitionStorage(tmp_path / ".cognition")
    config = PrimeConfig(prime_onboard=settings.prime_onboard)
    result = generate_prime(storage, config, current_email=ME["email"])
    assert "## New Here?" not in result


def test_onboarding_notice_is_first_section_before_active_constraints(tmp_path):
    """Notice is pinned first -- ahead of '## Active Constraints' -- so it can't be
    buried under a long constraint list."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "c1", CognitionNodeType.CONSTRAINT, "some constraint", severity="high")

    result = generate_prime(storage, current_email=ME["email"])
    assert result.index("## New Here?") < result.index("## Active Constraints")


def test_onboarding_notice_contains_required_guidance_substrings(tmp_path):
    """The notice text itself names the exact tool/flag/path an agent needs -- this
    pins the literal guidance, not just presence, so a future edit can't silently
    drop one of the required facts."""
    storage = CognitionStorage(tmp_path / ".cognition")
    result = generate_prime(storage, current_email=ME["email"])
    assert "cognition_register_person" in result
    assert "email omitted" in result
    assert "from_agent=false" in result
    assert ONBOARD_DECLINE_FILENAME in result


def test_onboarding_notice_byte_identical_delta_when_registered(tmp_path):
    """A registered current_email produces output byte-identical to current_email=None
    -- proving the notice (and nothing else) is what current_email gates here, since
    a matching person node isn't itself a stamped email counted by personalization."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-me", ME["email"])
    _add(storage, "d1", CognitionNodeType.DECISION, "some decision")

    with_email = generate_prime(storage, current_email=ME["email"])
    without_email = generate_prime(storage, current_email=None)
    assert with_email == without_email


def test_onboarding_notice_unregistered_delta_is_exactly_the_notice(tmp_path):
    """An unregistered current_email's output is EXACTLY the current_email=None
    output with the notice section prepended -- not a rewritten or reordered
    digest. This is the acceptance criterion's "delta is exactly the notice" pin."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "d1", CognitionNodeType.DECISION, "some decision")

    with_notice = generate_prime(storage, current_email=ME["email"])
    without = generate_prime(storage, current_email=None)
    header = "# Vibe Cognition — Project Context\n\n"
    assert without.startswith(header)
    assert with_notice == without.replace(header, header + ONBOARDING_NOTICE + "\n\n", 1)


def test_onboarding_notice_absent_in_empty_graph_path(tmp_path, monkeypatch):
    """main()'s empty-graph branch injects ONBOARDING_BLOCK and never calls
    generate_prime at all -- the WP-TC7 notice must be ASSERTED ABSENT here, not
    merely untested, since the two onboarding mechanisms are mutually exclusive
    by construction (empty graph has no person nodes to register against yet)."""
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.delenv("VIBE_MIGRATION_NOTE", raising=False)
    monkeypatch.setattr("vibe_cognition.cognition.prime.resolve_git_identity", lambda repo: ME)

    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main(argv=[])

    ctx = json.loads(buf.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert ONBOARDING_BLOCK in ctx
    assert "## New Here?" not in ctx


# ── WP-OnboardPayoff: registered-team auto-detect + identity header ──────────
# Gate D S5 fix: a team's first-onboarded member (single writer so far, several
# people now registered) previously got ZERO personalized sections. This block
# covers the two changes: (1) 'auto' also personalizes on >1 registered person
# email, not just >1 stamped writer email; (2) a one-line identity header opens
# the personalized block for a registered current_email.


def test_auto_personalizes_on_two_registered_persons_single_writer_fails_before(tmp_path):
    """The exact Gate D S5 shape: every node so far written by ONE person, but
    TWO people are registered -- 'auto' must personalize. Fails-before: a
    stamped-email-only heuristic sees a single distinct writer and stays global."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t1", "solo-written task", created_by=ME)
    _person(storage, "p-me", ME["email"], name="Alice")
    _person(storage, "p-teammate", TEAMMATE["email"], name="Bob")

    result = generate_prime(storage, current_email=ME["email"])
    assert "## Your Open Tasks" in result
    assert "## Open Tasks\n" not in result


def test_auto_duplicate_person_nodes_one_email_stays_global_fails_before(tmp_path):
    """Replay-duplicate shape: TWO person nodes carrying the SAME email (the
    write path's already_registered guard prevents this at write time, but not
    on replay/hand-edited data). Must stay global -- a node-count implementation
    would wrongly see 2 'registered persons' and flip; the correct set-of-emails
    implementation sees exactly 1 distinct email."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t1", "solo task", created_by=ME)
    _person(storage, "p-me-1", ME["email"], name="Alice")
    _person(storage, "p-me-2", ME["email"], name="Alice (dup)")

    result = generate_prime(storage, current_email=ME["email"])
    assert "## Your Open Tasks" not in result
    assert "## Open Tasks" in result


def test_identity_header_all_fields(tmp_path):
    """Pinned literal (peer-review M2): name + role + seniority + manager, with
    the manager resolved by NAME (not the raw email) via the same person scan."""
    storage = CognitionStorage(tmp_path / ".cognition")
    manager = {"name": "Casey Lead", "email": "casey.lead@test.local"}
    _person(storage, "p-mgr", manager["email"], name="Casey Lead")
    _person(
        storage, "p-me", ME["email"], name="Jamie Junior",
        role="backend engineer", seniority="junior", reports_to_email=manager["email"],
    )

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert (
        "You are registered as Jamie Junior — backend engineer (junior), "
        "reporting to Casey Lead." in result
    )


def test_identity_header_role_empty_rest_present(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", "casey.lead@test.local", name="Casey Lead")
    _person(
        storage, "p-me", ME["email"], name="Jamie Junior",
        role="", seniority="junior", reports_to_email="casey.lead@test.local",
    )

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "You are registered as Jamie Junior (junior), reporting to Casey Lead." in result


def test_identity_header_only_name(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-me", ME["email"], name="Jamie Junior", role="")

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "You are registered as Jamie Junior." in result


def test_identity_header_manager_email_unresolvable_falls_back_to_raw_email(tmp_path):
    """No person node exists for the manager's email -- the header falls back to
    showing the raw email rather than dropping the "reporting to" clause or
    rendering the string "None"."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(
        storage, "p-me", ME["email"], name="Jamie Junior",
        role="backend engineer", seniority="junior", reports_to_email="casey.lead@test.local",
    )

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert (
        "You are registered as Jamie Junior — backend engineer (junior), "
        "reporting to casey.lead@test.local." in result
    )
    assert "None" not in result


def test_identity_header_and_new_here_banner_are_mutually_exclusive(tmp_path):
    """A registered current_email gets the header and never the banner; an
    unregistered current_email gets the banner and never the header -- asserted
    together since they're gated on the exact same (inverted) condition."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-me", ME["email"], name="Jamie Junior")
    _person(storage, "p-teammate", TEAMMATE["email"], name="Bob")  # >1 registered -> personalize

    registered = generate_prime(storage, current_email=ME["email"])
    assert "You are registered as" in registered
    assert "## New Here?" not in registered

    unregistered = generate_prime(storage, current_email="stranger@x.com")
    assert "You are registered as" not in unregistered
    assert "## New Here?" in unregistered


def test_identity_header_absent_when_personalize_off_even_if_registered(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-me", ME["email"], name="Jamie Junior")

    result = generate_prime(storage, PrimeConfig(prime_personalize="off"), current_email=ME["email"])
    assert "You are registered as" not in result


def test_gate_d_s5_replay_shape_junior_gets_header_after_registration(tmp_path):
    """Closes the Gate D S5 audit finding: one writer (the lead) has written
    everything so far; lead/senior/junior are all registered, junior reports to
    lead. Priming as the junior AFTER registration must personalize (auto),
    show the identity header, and NOT show the New Here banner -- the exact
    "first onboarded member gets zero payoff" gap this WP closes."""
    storage = CognitionStorage(tmp_path / ".cognition")
    lead = {"name": "Casey Lead", "email": "casey.lead@test.local"}
    senior = {"name": "Sam Senior", "email": "sam.senior@test.local"}
    junior = {"name": "Jamie Junior", "email": "jamie.junior@test.local"}
    _task(storage, "t1", "everything so far", created_by=lead)
    _person(storage, "p-lead", lead["email"], name=lead["name"], role="lead", seniority="senior")
    _person(
        storage, "p-senior", senior["email"], name=senior["name"],
        role="engineer", seniority="senior", reports_to_email=lead["email"],
    )
    _person(
        storage, "p-junior", junior["email"], name=junior["name"],
        role="engineer", seniority="junior", reports_to_email=lead["email"],
    )

    result = generate_prime(storage, current_email=junior["email"])
    assert "You are registered as Jamie Junior — engineer (junior), reporting to Casey Lead." in result
    assert "## New Here?" not in result


def test_derive_role_empty_email_short_circuit_constructs_without_crashing(tmp_path):
    """Peer-review H1: _RoleContext has TWO constructor call sites in
    _derive_role -- the early `not current_email` short-circuit is currently
    DEAD CODE from generate_prime's perspective (it only derives a role inside
    `if personalize:`, which already implies a non-empty email), so nothing in
    the generate_prime-level test suite exercises it. Call _derive_role
    directly with an empty email to lock in that this site still constructs a
    valid _RoleContext (my_manager_name included) instead of a latent
    TypeError if a future edit touches the dataclass fields."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-someone", "someone@x.com", name="Someone")

    role = _derive_role(storage, "")
    assert role.my_person is None
    assert role.direct_reports == []
    assert role.my_manager_email == ""
    assert role.my_manager_name == ""


# ── main() — hook payload ─────────────────────────────────────────────────────


def test_main_empty_graph_injects_onboarding(tmp_path, monkeypatch):
    """main(): empty .cognition → ONBOARDING_BLOCK injected in SessionStart JSON.

    Fails-before: if main() emitted generate_prime output on an empty graph
    (showing "No cognition history..." inside a SessionStart block instead of
    prompting the agent to set up vibe-cognition).
    """
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.delenv("VIBE_MIGRATION_NOTE", raising=False)

    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main(argv=[])  # WP-13: explicit argv, not pytest's own sys.argv

    data = json.loads(buf.getvalue())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert ONBOARDING_BLOCK in ctx


def test_main_populated_graph_emits_session_start_json(tmp_path, monkeypatch):
    """main(): populated graph → valid SessionStart JSON with prime sections.

    Fails-before: if main() crashed on a non-empty graph (e.g. bad import or
    missing section) or emitted the onboarding block when nodes existed.
    """
    # Seed a node so the graph is non-empty.
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "d1", CognitionNodeType.DECISION, "chose approach alpha")

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.delenv("VIBE_MIGRATION_NOTE", raising=False)

    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main(argv=[])  # WP-13: explicit argv, not pytest's own sys.argv

    data = json.loads(buf.getvalue())
    hook = data["hookSpecificOutput"]
    assert hook["hookEventName"] == "SessionStart"
    ctx = hook["additionalContext"]
    assert ONBOARDING_BLOCK not in ctx
    assert "chose approach alpha" in ctx


def test_main_migration_note_prepended(tmp_path, monkeypatch):
    """main(): VIBE_MIGRATION_NOTE → note appears before the prime body.

    Fails-before: if the note was appended after the prime (making it invisible
    when the context is truncated) or ignored entirely.
    """
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.setenv("VIBE_MIGRATION_NOTE", "Removed stale MCP entry: vibe-cognition")

    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main(argv=[])  # WP-13: explicit argv, not pytest's own sys.argv

    data = json.loads(buf.getvalue())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    note_pos = ctx.index("Removed stale MCP entry")
    onboard_pos = ctx.index(ONBOARDING_BLOCK[:30])
    assert note_pos < onboard_pos, "migration note must appear before the body"


# ── main() — WP-P13n-2 identity wiring ────────────────────────────────────────


def test_main_personalizes_when_identity_resolves_and_graph_is_multiuser(tmp_path, monkeypatch):
    """main() end-to-end: resolve_git_identity resolving the current user's
    email, in a multi-user graph, personalizes the injected prime -- exercising
    the real main()->generate_prime wiring, not just generate_prime directly."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-mine", "my task", created_by=ME)
    _task(storage, "t-theirs", "teammate task", created_by=TEAMMATE)

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.delenv("VIBE_MIGRATION_NOTE", raising=False)
    monkeypatch.setattr("vibe_cognition.cognition.prime.resolve_git_identity", lambda repo: ME)

    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main(argv=[])

    ctx = json.loads(buf.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert "## Your Open Tasks" in ctx
    assert "my task" in ctx


def test_main_unconfigured_identity_falls_back_to_global_no_crash(tmp_path, monkeypatch):
    """main() end-to-end: an unresolvable identity (empty email, e.g. a fresh
    machine with no git user.email configured) degrades to the global digest --
    no personal sections, no crash -- even though the graph IS multi-user."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-mine", "my task", created_by=ME)
    _task(storage, "t-theirs", "teammate task", created_by=TEAMMATE)

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.delenv("VIBE_MIGRATION_NOTE", raising=False)
    monkeypatch.setattr(
        "vibe_cognition.cognition.prime.resolve_git_identity",
        lambda repo: {"name": "unknown", "email": ""},
    )

    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main(argv=[])  # must not raise

    ctx = json.loads(buf.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert "## Your Open Tasks" not in ctx
    assert "## Open Tasks" in ctx


def test_main_end_to_end_emits_valid_json_with_onboarding_notice(tmp_path, monkeypatch):
    """main() end-to-end: an identity that resolves but has no person node yields
    valid SessionStart JSON with the onboarding notice present -- exercising the
    real main()->generate_prime wiring (not just generate_prime directly), and
    confirming the notice survives the full hook payload round-trip through
    json.dump/json.loads without corrupting the JSON."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "d1", CognitionNodeType.DECISION, "some decision")

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.delenv("VIBE_MIGRATION_NOTE", raising=False)
    monkeypatch.setattr("vibe_cognition.cognition.prime.resolve_git_identity", lambda repo: ME)

    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main(argv=[])

    data = json.loads(buf.getvalue())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "## New Here?" in ctx
    assert "cognition_register_person" in ctx


# ── argparse / --help correctness (WP-13, 4aaef22e25ea) ──────────────────────


def test_help_exits_zero_and_never_runs_the_hook_payload(tmp_path, monkeypatch, capsys):
    """Fails-before: no argparse at all, so --help was silently swallowed and
    the full session context dumped instead of printing usage.
    """
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(argv=["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage" in out.lower()
    assert "hookSpecificOutput" not in out, "--help must not run the hook payload"


def test_rejects_unknown_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(argv=["--bogus"])
    assert exc.value.code == 2
