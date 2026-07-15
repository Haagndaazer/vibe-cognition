"""WP-TC16: role-aware prime sections (manager rollup / subordinate view).

Covers: role derivation (one person-node scan), '## Your Team' (manager rollup:
in-progress claimant+age, stale boundary at exactly prime_stale_claim_days, null
claimed_at never stale, blocked, cap+overflow, non-report/unstamped exclusion),
'## Your Manager's Recent Decisions' (subordinate view: no HEAD-filter, cap,
dangling manager email), middle-manager coexistence, pinned section order, the
role-less byte-identical compat invariant, and the _task_claimed_at relocation.
"""

from datetime import UTC, datetime, timedelta

from vibe_cognition.cognition import CognitionEdge, CognitionEdgeType, CognitionStorage
from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
from vibe_cognition.cognition.prime import PrimeConfig, generate_prime
from vibe_cognition.cognition.task_meta import _task_claimed_at as _shared_task_claimed_at
from vibe_cognition.tools.cognition_tools import _task_claimed_at as _tools_task_claimed_at

# ── helpers ───────────────────────────────────────────────────────────────────

MGR = {"name": "Manny", "email": "mgr@x.com"}
BOB = {"name": "Bob", "email": "bob@x.com"}
CAROL = {"name": "Carol", "email": "carol@x.com"}
OUTSIDER = {"name": "Otto", "email": "outsider@x.com"}


def _add(storage: CognitionStorage, node_id: str, ntype: CognitionNodeType,
         summary: str, *, severity: str | None = None, timestamp: str | None = None,
         metadata: dict | None = None) -> None:
    ts = timestamp or datetime.now(UTC).isoformat()
    storage.add_node(CognitionNode(
        id=node_id, type=ntype, summary=summary, detail="d",
        context=[], references=[], severity=severity, timestamp=ts, author="t",
        metadata=metadata or {},
    ))


def _person(storage: CognitionStorage, node_id: str, person: dict, *, reports_to_email: str = "") -> None:
    _add(storage, node_id, CognitionNodeType.PERSON, f"{person['name']} — person", metadata={
        "person": {
            "email": person["email"], "name": person["name"], "role": "eng",
            "seniority": "mid", "reports_to_email": reports_to_email,
        },
        "profile_history": [], "recorded_by": person, "from_agent": False,
    })


def _claimed_task(
    storage: CognitionStorage, node_id: str, summary: str, *, status: str,
    claimant: dict, claimed_at: str | None = None, timestamp: str | None = None,
) -> None:
    """A task claimed by `claimant`. `claimed_at=None` means NO in_progress
    transition entry exists (legacy/unattributed claim) -- _task_claimed_at
    returns None for it, exercising the null-claimed_at-never-stale path."""
    transitions = [{"status": "open", "at": timestamp or datetime.now(UTC).isoformat(), "by": claimant}]
    if claimed_at is not None:
        transitions.append({"status": "in_progress", "at": claimed_at, "by": claimant})
    _add(storage, node_id, CognitionNodeType.TASK, summary, timestamp=timestamp, metadata={
        "status": status, "claimed_by": claimant, "created_by": claimant,
        "transitions": transitions,
    })


def _decision_by(storage: CognitionStorage, node_id: str, summary: str, author: dict, *, timestamp: str | None = None) -> None:
    _add(storage, node_id, CognitionNodeType.DECISION, summary, timestamp=timestamp,
         metadata={"recorded_by": author, "from_agent": False})


def _section(result: str, header: str) -> str:
    """Isolate one '## Header\\n...' section's body from the full prime output,
    up to the next '## ' header or end of string. Needed because the GLOBAL
    'Recent Decisions' section (unscoped, all authors) can otherwise coincide
    with 'Your Manager's Recent Decisions' content and produce false-positive
    substring matches against the whole `result` string."""
    start = result.index(header)
    rest = result[start + len(header):]
    next_header = rest.find("\n## ")
    body = rest if next_header == -1 else rest[:next_header]
    return header + body


class _FrozenClock:
    """Freezes datetime.now(); .fromisoformat proxies to the real class so
    claimed_at parsing (an unrelated code path) is unaffected."""

    _FROZEN = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls._FROZEN

    @staticmethod
    def fromisoformat(s):
        return datetime.fromisoformat(s)


# ── role derivation ──────────────────────────────────────────────────────────


def test_manager_role_from_direct_reports(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    _claimed_task(storage, "t1", "bob's task", status="in_progress", claimant=BOB,
                  claimed_at=(datetime.now(UTC) - timedelta(days=1)).isoformat())

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=MGR["email"])
    assert "## Your Team" in result
    assert "bob's task" in result


def test_subordinate_role_from_own_reports_to_email(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    _decision_by(storage, "d1", "mgr made a call", MGR)

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=BOB["email"])
    assert "## Your Manager's Recent Decisions" in result
    assert "mgr made a call" in result


def test_neither_role_no_new_sections(tmp_path):
    """No direct reports, no reports_to_email -> neither section, even though the
    user HAS a registered person node."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-solo", OUTSIDER)
    _decision_by(storage, "d1", "someone's decision", MGR)

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=OUTSIDER["email"])
    assert "## Your Team" not in result
    assert "## Your Manager's Recent Decisions" not in result


def test_middle_manager_both_sections_coexist_in_pinned_order(tmp_path):
    """MGR reports to BIGBOSS (subordinate) and manages BOB (manager) -> both
    sections appear, in the pinned order: Team Critical -> Your Team -> Your
    Manager's Recent Decisions -> Your Recent Activity."""
    storage = CognitionStorage(tmp_path / ".cognition")
    bigboss = {"name": "Big", "email": "bigboss@x.com"}
    _person(storage, "p-mgr", MGR, reports_to_email=bigboss["email"])
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    _person(storage, "p-boss", bigboss)
    _claimed_task(storage, "t1", "bob's task", status="in_progress", claimant=BOB,
                  claimed_at=(datetime.now(UTC) - timedelta(days=1)).isoformat())
    _decision_by(storage, "d1", "bigboss made a call", bigboss)
    _add(storage, "crit1", CognitionNodeType.TASK, "urgent unclaimed work", severity="critical",
         metadata={"status": "open"})
    _decision_by(storage, "d2", "mgr's own recent decision", MGR)

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=MGR["email"])
    assert "## Your Team" in result
    assert "## Your Manager's Recent Decisions" in result
    i_crit = result.index("## Team Critical")
    i_team = result.index("## Your Team")
    i_mgr_dec = result.index("## Your Manager's Recent Decisions")
    i_activity = result.index("## Your Recent Activity")
    assert i_crit < i_team < i_mgr_dec < i_activity


# ── '## Your Team' manager rollup ───────────────────────────────────────────


def test_your_team_in_progress_shows_claimant_and_age(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    _claimed_task(storage, "t1", "bob's in-progress task", status="in_progress", claimant=BOB,
                  claimed_at=(datetime.now(UTC) - timedelta(days=2)).isoformat())

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=MGR["email"])
    assert "- bob's in-progress task (Bob, claimed 2d)" in result


def test_your_team_blocked_row_format(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    _claimed_task(storage, "t1", "bob's blocked task", status="blocked", claimant=BOB,
                  claimed_at=(datetime.now(UTC) - timedelta(days=1)).isoformat())

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=MGR["email"])
    assert "- bob's blocked task (Bob, blocked)" in result


def test_your_team_stale_boundary_both_sides_and_stale_first_ordering(monkeypatch, tmp_path):
    """Exactly prime_stale_claim_days (7) old is NOT stale; 7 days + 1 second IS
    stale. Carol's stale task must sort BEFORE Bob's fresh task even though Bob's
    task has a strictly more recent node timestamp -- proving stale-first
    ordering beats plain recency, at the exact boundary."""
    import vibe_cognition.cognition.prime as prime_module
    monkeypatch.setattr(prime_module, "datetime", _FrozenClock)
    now = _FrozenClock._FROZEN

    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    _person(storage, "p-carol", CAROL, reports_to_email=MGR["email"])
    # Carol: stale (claimed just over 7 days ago), task created recently.
    _claimed_task(
        storage, "t-carol", "carol's stale task", status="in_progress", claimant=CAROL,
        claimed_at=(now - timedelta(days=7, seconds=1)).isoformat(),
        timestamp=(now - timedelta(days=1)).isoformat(),
    )
    # Bob: NOT stale (claimed exactly 7 days ago), task created more recently than Carol's.
    _claimed_task(
        storage, "t-bob", "bob's boundary task", status="in_progress", claimant=BOB,
        claimed_at=(now - timedelta(days=7)).isoformat(),
        timestamp=now.isoformat(),
    )

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=MGR["email"])
    assert "carol's stale task" in result
    assert "bob's boundary task" in result
    assert result.index("carol's stale task") < result.index("bob's boundary task"), (
        "stale row must sort before a non-stale row even when the non-stale row is more recent"
    )


def test_your_team_null_claimed_at_never_stale(tmp_path):
    """A legacy in-progress claim with NO in_progress transition entry (claimed_at
    is None) must NEVER be treated as stale -- it must sort AFTER a genuinely
    stale claim regardless of node recency. Bob's legacy row is given the MORE
    recent node timestamp deliberately: if it were wrongly bucketed as stale, its
    recency would put it FIRST within that bucket (a same-bucket recency-desc
    tie would otherwise mask a wrong bucket assignment and make this a
    false-negative probe -- see WP-TC4's redundant-guard lesson)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    _person(storage, "p-carol", CAROL, reports_to_email=MGR["email"])
    _claimed_task(storage, "t-bob-legacy", "bob's legacy claim", status="in_progress",
                  claimant=BOB, claimed_at=None, timestamp=datetime.now(UTC).isoformat())
    _claimed_task(storage, "t-carol-stale", "carol's genuinely stale claim", status="in_progress",
                  claimant=CAROL, claimed_at=(datetime.now(UTC) - timedelta(days=10)).isoformat(),
                  timestamp=(datetime.now(UTC) - timedelta(days=60)).isoformat())

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=MGR["email"])
    assert "- bob's legacy claim (Bob, claimed unknown age)" in result
    assert result.index("carol's genuinely stale claim") < result.index("bob's legacy claim"), (
        "a null-claimed_at row must never be treated as stale (sorted before a real stale row)"
    )


def test_your_team_excludes_non_report_and_unstamped_claims(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    # Claimed by someone who is NOT a direct report of MGR.
    _claimed_task(storage, "t-outsider", "outsider's task", status="in_progress",
                  claimant=OUTSIDER, claimed_at=datetime.now(UTC).isoformat())
    # in_progress but claimed_by missing entirely (unstamped) -- must never appear.
    _add(storage, "t-unstamped", CognitionNodeType.TASK, "unstamped in-progress task",
         metadata={"status": "in_progress", "transitions": []})
    # A report's task that's still just "open" (not in_progress/blocked) -- excluded.
    _add(storage, "t-open", CognitionNodeType.TASK, "bob's open task",
         metadata={"status": "open", "claimed_by": BOB, "transitions": []})

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=MGR["email"])
    assert "## Your Team" not in result
    assert "outsider's task" not in result
    assert "unstamped in-progress task" not in result
    assert "bob's open task" not in result


def test_your_team_cap_and_overflow(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    for i in range(4):
        _claimed_task(
            storage, f"t{i}", f"bob's task {i}", status="in_progress", claimant=BOB,
            claimed_at=(datetime.now(UTC) - timedelta(hours=i)).isoformat(),
            timestamp=(datetime.now(UTC) - timedelta(hours=i)).isoformat(),
        )

    result = generate_prime(
        storage, PrimeConfig(prime_personalize="on", prime_rollup_cap=2), current_email=MGR["email"]
    )
    shown = sum(result.count(f"bob's task {i}") for i in range(4))
    assert shown == 2
    assert "+2 more of your team's tasks — use cognition_list_tasks" in result


# ── gate fixup: mixed-case email + naive timestamp replay tolerance ─────────


def test_your_team_matches_mixed_case_claimant_email(tmp_path):
    """claimed_by.email is a verbatim git-config provenance stamp, never
    casefolded at write time (unlike person emails, which ARE casefolded at
    write) -- a report whose git config stamps a mixed-case email must still
    match the casefolded report_names keys and appear in the rollup."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])  # registered lowercase
    bob_mixed_case_stamp = {"name": "Bob", "email": "Bob@X.COM"}
    _claimed_task(
        storage, "t1", "bob's mixed-case-stamped task", status="in_progress",
        claimant=bob_mixed_case_stamp, claimed_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
    )

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=MGR["email"])
    assert "## Your Team" in result
    assert "bob's mixed-case-stamped task" in result
    assert "(Bob, claimed 1d)" in result


def test_your_team_naive_claimed_at_does_not_crash(tmp_path):
    """A naive (no-tzinfo) claimed_at -- as could appear in a replayed or
    hand-edited journal -- must not crash generate_prime (the SessionStart
    hook) via an uncaught TypeError on aware-minus-naive subtraction. Same
    failure class as WP-TC9's 98dcca4 fixup: write-side validation is not
    protection against replay. Exercises both the stale check and
    _humanize_claim_age, since both parse this same claimed_at string."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    naive_claimed_at = "2026-07-14T12:00:00"  # no tzinfo
    _claimed_task(
        storage, "t1", "bob's naively-stamped task", status="in_progress",
        claimant=BOB, claimed_at=naive_claimed_at,
    )

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=MGR["email"])
    assert "## Your Team" in result
    assert "bob's naively-stamped task" in result


# ── '## Your Manager's Recent Decisions' subordinate view ───────────────────


def test_manager_decisions_capped_newest_first(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    for i in range(5):
        _decision_by(
            storage, f"d{i}", f"decision number {i}", MGR,
            timestamp=(datetime.now(UTC) - timedelta(hours=i)).isoformat(),
        )

    result = generate_prime(
        storage, PrimeConfig(prime_personalize="on", prime_manager_decision_limit=2),
        current_email=BOB["email"],
    )
    section = _section(result, "## Your Manager's Recent Decisions")
    shown = sum(section.count(f"decision number {i}") for i in range(5))
    assert shown == 2
    assert "decision number 0" in section  # newest (hours=0) survives the cap
    assert "decision number 1" in section


def test_manager_decisions_no_head_filter_mirrors_format_decisions(tmp_path):
    """A superseded (non-HEAD) decision by the manager STILL appears here --
    deliberately, since the global Recent Decisions model has no HEAD-filter
    either (unlike constraints/workflows)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    _decision_by(storage, "d-old", "old superseded decision", MGR)
    _decision_by(storage, "d-new", "new superseding decision", MGR)
    storage.add_edge(CognitionEdge(
        from_id="d-new", to_id="d-old", edge_type=CognitionEdgeType.SUPERSEDES,
        timestamp=datetime.now(UTC).isoformat(),
    ))

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=BOB["email"])
    assert "old superseded decision" in result
    assert "new superseding decision" in result


def test_manager_decisions_by_others_absent(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    _decision_by(storage, "d1", "manager's decision", MGR)
    _decision_by(storage, "d2", "unrelated decision", OUTSIDER)

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=BOB["email"])
    section = _section(result, "## Your Manager's Recent Decisions")
    assert "manager's decision" in section
    assert "unrelated decision" not in section


def test_manager_decisions_dangling_manager_email_still_renders(tmp_path):
    """BOB's reports_to_email points at an email with NO registered person node --
    the section still populates (matched by email string, not a person lookup)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    dangling_mgr_email = "ghost-mgr@x.com"
    _person(storage, "p-bob", BOB, reports_to_email=dangling_mgr_email)
    _decision_by(storage, "d1", "ghost manager's decision", {"name": "Ghost", "email": dangling_mgr_email})

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=BOB["email"])
    assert "## Your Manager's Recent Decisions" in result
    assert "ghost manager's decision" in result


# ── role-less compat: the strongest invariant ───────────────────────────────


def test_role_less_user_byte_identical_regardless_of_unrelated_team_hierarchy(tmp_path):
    """A user with no person node, no reports either direction: prime output must
    be byte-identical whether or not an UNRELATED team hierarchy exists elsewhere
    in the graph -- role data for other people must never leak into a role-less
    user's own prime."""
    alice = {"name": "Alice", "email": "alice@x.com"}
    # Pinned timestamps: identical node content across both storages must sort
    # identically -- real-clock timestamps captured at slightly different wall
    # moments between the two storage builds could otherwise tie-break
    # differently and produce a false-negative diff unrelated to role leakage.
    t_d1 = "2026-07-15T10:00:00+00:00"
    t_d2 = "2026-07-15T09:00:00+00:00"
    t_claim = "2026-07-15T08:00:00+00:00"

    storage_no_roles = CognitionStorage(tmp_path / "no_roles" / ".cognition")
    _decision_by(storage_no_roles, "d1", "alice's decision", alice, timestamp=t_d1)
    _decision_by(storage_no_roles, "d2", "teammate decision", BOB, timestamp=t_d2)
    _claimed_task(storage_no_roles, "t1", "alice's task", status="in_progress", claimant=alice,
                  claimed_at=t_claim, timestamp=t_claim)

    storage_with_roles = CognitionStorage(tmp_path / "with_roles" / ".cognition")
    _decision_by(storage_with_roles, "d1", "alice's decision", alice, timestamp=t_d1)
    _decision_by(storage_with_roles, "d2", "teammate decision", BOB, timestamp=t_d2)
    _claimed_task(storage_with_roles, "t1", "alice's task", status="in_progress", claimant=alice,
                  claimed_at=t_claim, timestamp=t_claim)
    # Unrelated hierarchy: MGR manages BOB and CAROL. Alice appears nowhere in it.
    _person(storage_with_roles, "p-mgr", MGR)
    _person(storage_with_roles, "p-bob", BOB, reports_to_email=MGR["email"])
    _person(storage_with_roles, "p-carol", CAROL, reports_to_email=MGR["email"])
    _claimed_task(storage_with_roles, "t2", "bob's task", status="in_progress", claimant=BOB,
                  claimed_at=t_claim, timestamp=t_claim)

    config = PrimeConfig(prime_personalize="on")
    result_no_roles = generate_prime(storage_no_roles, config, current_email=alice["email"])
    result_with_roles = generate_prime(storage_with_roles, config, current_email=alice["email"])
    assert result_no_roles == result_with_roles
    assert "## Your Team" not in result_with_roles
    assert "## Your Manager's Recent Decisions" not in result_with_roles


def test_role_sections_absent_when_personalization_off(tmp_path):
    """prime_personalize='off' kills the role sections along with everything else
    personalized -- even for a user who genuinely has direct reports."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    _claimed_task(storage, "t1", "bob's task", status="in_progress", claimant=BOB,
                  claimed_at=datetime.now(UTC).isoformat())

    result = generate_prime(storage, PrimeConfig(prime_personalize="off"), current_email=MGR["email"])
    assert "## Your Team" not in result


def test_role_less_no_session_email_no_new_sections(tmp_path):
    """No resolvable current_email at all -> no role sections (and no crash),
    even in a graph that otherwise has a full team hierarchy."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p-mgr", MGR)
    _person(storage, "p-bob", BOB, reports_to_email=MGR["email"])
    _decision_by(storage, "d1", "some decision", MGR)

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=None)
    assert "## Your Team" not in result
    assert "## Your Manager's Recent Decisions" not in result


# ── helper relocation ────────────────────────────────────────────────────────


def test_task_claimed_at_single_implementation_shared_by_all_three_consumers():
    """cognition_tools' and the direct task_meta import resolve to the SAME
    function object -- proving the re-export, not a second hand-rolled copy."""
    assert _tools_task_claimed_at is _shared_task_claimed_at


def test_task_claimed_at_computation_matches_across_import_paths():
    transitions = [
        {"status": "open", "at": "2026-01-01T00:00:00+00:00"},
        {"status": "in_progress", "at": "2026-01-02T00:00:00+00:00"},
        {"status": "in_progress", "at": "2026-01-03T00:00:00+00:00"},  # last-wins
    ]
    assert _shared_task_claimed_at(transitions) == "2026-01-03T00:00:00+00:00"
    assert _tools_task_claimed_at(transitions) == "2026-01-03T00:00:00+00:00"
    assert _shared_task_claimed_at([]) is None


def test_prime_module_does_not_import_cognition_tools():
    """Prime's light-import constraint: it must never pull in tools/cognition_tools
    (which drags in chroma/embeddings) -- confirmed by inspecting the compiled
    module's own import statements, not just a runtime sys.modules snapshot
    (which could be polluted by import order in the same test process)."""
    import ast
    import inspect

    import vibe_cognition.cognition.prime as prime_module

    source = inspect.getsource(prime_module)
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
    assert not any("cognition_tools" in m for m in imported_modules), imported_modules
