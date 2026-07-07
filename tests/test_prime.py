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
from vibe_cognition.cognition.prime import PrimeConfig, _truncate, generate_prime, main
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
          claimed_by: dict | None = None, status: str = "open") -> None:
    """Seed a task node with WP-P13n-1 provenance stamps (created_by/claimed_by),
    for WP-P13n-2 email-matching tests."""
    meta: dict = {"status": status}
    if created_by is not None:
        meta["created_by"] = created_by
    if claimed_by is not None:
        meta["claimed_by"] = claimed_by
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
    criterion's "solo output byte-identical" pin."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t1", "solo task", severity="high", created_by=ME)

    with_email = generate_prime(storage, current_email=ME["email"])
    without_email = generate_prime(storage, current_email=None)
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
    global. That's tested separately; this test isolates claimed_by-matching."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task(storage, "t-claimed", "claimed by me", created_by=TEAMMATE, claimed_by=ME)
    _task(storage, "t-theirs", "not mine", created_by=TEAMMATE)

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "claimed by me" in result
    assert "not mine" not in result


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
