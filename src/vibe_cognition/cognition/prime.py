"""Prime command — outputs compact project context for Claude Code session injection."""

import argparse
import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..config import Settings, resolve_repo_path_env
from .git_hygiene import _acquire_lock, _release_lock, check_hygiene_state, format_hygiene_announce
from .git_identity import resolve_git_identity
from .models import CognitionEdgeType, CognitionNodeType
from .readme import ONBOARDING_BLOCK
from .storage import REHYDRATE_FLAG_FILENAME, CognitionStorage
from .task_meta import _task_claimed_at

SEVERITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}

_TASK_CLOSED_STATUSES = frozenset({"done", "cancelled"})

# WP-TC7: new-user onboarding notice. Per-machine, local file (never synced via the
# graph/journal) -- one casefolded email per line. Written by the AGENT via an
# ordinary file append when the human declines/snoozes (no new MCP tool, no graph
# write); read here only. Filename referenced by git_hygiene.py's versioned writer
# (kept as a plain string there too, not imported, matching that module's existing
# stdlib-only/standalone convention for REHYDRATE_FLAG_FILENAME).
ONBOARD_DECLINE_FILENAME = "onboard-declined"

# WP-TC14: "Since You Were Gone" digest. Per-machine, git-ignored, UNCOMMITTED
# marker file -- casefolded email -> aware-UTC ISO timestamp of that email's
# last session-start (JSON object, read-modify-write preserves other emails'
# entries -- the per-email ruling: a manager and subordinate sharing a machine
# must not stomp each other's marker). Written ONLY by prime.py's own main()
# (see _stamp_last_seen), never by generate_prime itself (read-only invariant).
LAST_SEEN_FILENAME = "last-seen.json"
_LAST_SEEN_LOCK_ATTEMPTS = 3
_LAST_SEEN_LOCK_RETRY_DELAY_S = 0.02

ONBOARDING_NOTICE = (
    "## New Here?\n"
    "This graph has no person node for your email yet.\n"
    "- Ask the human: name, role, seniority (owner|senior|mid|junior), and who "
    "they directly report to (optional).\n"
    "- Then call cognition_register_person (email omitted -- the server resolves "
    "it) with from_agent=false (the human dictated this, not you).\n"
    "- If they'd rather skip it: append their casefolded email to "
    f".cognition/{ONBOARD_DECLINE_FILENAME} (one per line) -- never create a "
    "placeholder person node."
)


@dataclass(frozen=True)
class PrimeConfig:
    """Trim knobs for the session-start prime digest.

    These defaults ARE the trimmed target output — main() builds a config from
    Settings in one try/except and falls back to these on any failure, so a
    broken env degrades to the same trimmed shape rather than the old fat one.
    """

    prime_constraint_limit: int = 5
    prime_task_cap: int = 5
    prime_pattern_limit: int = 3
    prime_decision_limit: int = 3
    prime_incident_days: int = 14
    prime_summary_maxlen: int = 110
    prime_incident_min_severity: str = "high"
    prime_workflow_limit: int = 5

    # WP-P13n-2: personalization knobs (see Settings for the full description of
    # each field — these defaults must match Settings' the same way the trim
    # knobs above do).
    prime_personalize: str = "auto"
    prime_your_tasks_cap: int = 5
    prime_team_critical_cap: int = 5
    prime_your_episode_limit: int = 5
    prime_your_decision_limit: int = 5
    prime_your_discovery_limit: int = 5

    # WP-TC7: new-user onboarding notice.
    prime_onboard: bool = True

    # WP-TC16: role-aware prime (manager rollup / subordinate view) knobs.
    prime_stale_claim_days: int = 7
    prime_rollup_cap: int = 5
    prime_manager_decision_limit: int = 3

    # WP-TC14: "Since You Were Gone" digest knobs. No separate on/off knob --
    # the section self-gates on personalize (TC16 no-dead-knob philosophy).
    prime_digest_cap: int = 5
    prime_digest_fallback_days: int = 7


def _truncate(text: str, maxlen: int) -> str:
    """Truncate text to maxlen, cutting at the last whitespace before it.

    maxlen<=0 or a short-enough string is a no-op. If no whitespace is found
    before maxlen (e.g. a long URL/hash), hard-cut at maxlen rather than
    silently dropping only the last char.
    """
    if maxlen <= 0 or len(text) <= maxlen:
        return text
    cut = text.rfind(" ", 0, maxlen)
    if cut <= 0:
        cut = maxlen
    return text[:cut].rstrip() + "…"


def _format_node(node: dict, maxlen: int = 0) -> str:
    """Format a single node as a compact bullet point."""
    severity = node.get("severity")
    suffix = f" (severity: {severity})" if severity else ""
    summary = _truncate(node.get("summary", "No summary"), maxlen)
    return f"- [{node.get('type', '?')}] {summary}{suffix}"


def _format_task(node: dict, maxlen: int = 0) -> str:
    """Format a single open task with its status/owner/priority."""
    meta = node.get("metadata", {})
    bits = [f"status: {meta.get('status', 'open')}"]
    owner = meta.get("owner")
    if owner:
        bits.append(f"owner: {owner}")
    priority = node.get("severity")
    if priority:
        bits.append(f"priority: {priority}")
    summary = _truncate(node.get("summary", "No summary"), maxlen)
    return f"- [task] {summary} ({', '.join(bits)})"


def _format_tasks(storage: CognitionStorage, cap: int, maxlen: int = 0) -> str:
    """Format open tasks (status not in done/cancelled), priority- then recency-sorted,
    capped at the top-N with an overflow line. Mirrors _format_constraints but bounded so
    the session-start payload can't balloon on a long backlog."""
    nodes = storage.get_nodes_by_type(CognitionNodeType.TASK)
    open_tasks = [
        n for n in nodes
        if n.get("metadata", {}).get("status", "open") not in _TASK_CLOSED_STATUSES
    ]
    if not open_tasks:
        return ""

    # Two-pass stable sort: recency desc, then severity asc (severity primary).
    open_tasks.sort(key=lambda n: n.get("timestamp", ""), reverse=True)
    open_tasks.sort(key=lambda n: SEVERITY_ORDER.get(n.get("severity", "normal"), 2))

    shown = open_tasks[:cap]
    lines = [_format_task(n, maxlen) for n in shown]
    overflow = len(open_tasks) - len(shown)
    if overflow > 0:
        lines.append(f"- +{overflow} more open tasks — use cognition_list_tasks")
    return "## Open Tasks\n" + "\n".join(lines)


# ── WP-P13n-2: personalization ──────────────────────────────────────────────
#
# Email is the ONLY match key (decision, doc:aa047b0b0e0a) — owner/author are
# free text and are never matched, to avoid both false-positive name collisions
# and silently dropping agent-owned tasks that have no human name to match.


def _node_email(node: dict) -> str:
    """The stamped identity email for personalization matching, casefolded (never
    `.lower()` — casefold is the correct Unicode-aware match normalization):
    `recorded_by.email` (every non-task node, via _record_node) if present, else
    `created_by.email` (task nodes, which carry created_by instead of
    recorded_by). Never falls back to `author`/`owner` — those are
    caller-provided free text, not server-resolved. The stamp itself (WP-P13n-1
    provenance record) is untouched; this is a match-time normalization only."""
    meta = node.get("metadata", {})
    recorded = meta.get("recorded_by")
    if isinstance(recorded, dict) and recorded.get("email"):
        return recorded["email"].casefold()
    created = meta.get("created_by")
    if isinstance(created, dict) and created.get("email"):
        return created["email"].casefold()
    return ""


def _distinct_stamped_emails(storage: CognitionStorage) -> set[str]:
    """Every non-empty stamped email in the graph, for the multi-user auto-detect.

    An empty email (unconfigured git identity) is excluded from the count itself
    -- a solo user who sets `user.email` partway through their history must not
    flip the graph to "multi-user" just because some nodes predate the config."""
    return {email for n in storage.get_all_nodes() if (email := _node_email(n))}


def _registered_person_emails(storage: CognitionStorage) -> set[str]:
    """Every non-empty registered person email in the graph (WP-OnboardPayoff),
    for the multi-user auto-detect's second signal.

    A SET OF EMAILS, not a node count -- duplicate person nodes carrying the SAME
    email (a journal-replay shape; the write path's `already_registered` guard
    prevents this at write time but not on replay/hand-edited data, same lesson
    as WP-TC9's 98dcca4) must not flip a solo graph to "multi-user". Trusts stored
    casefolding (module convention, matches `_has_person_node`)."""
    return {
        email
        for n in storage.get_nodes_by_type(CognitionNodeType.PERSON)
        if (email := n.get("metadata", {}).get("person", {}).get("email"))
    }


def _should_personalize(storage: CognitionStorage, config: PrimeConfig, current_email: str) -> bool:
    """Whether this prime run personalizes, per `config.prime_personalize`.

    No resolvable current-user email means there's nothing to match against --
    global digest regardless of mode. 'off' is always global; 'on' is always
    personalized (once an email exists); 'auto' personalizes when EITHER the
    graph has more than one distinct stamped email OR more than one registered
    person email (WP-OnboardPayoff) -- multiple REGISTERED people is a stronger
    team signal than multiple writers (registration is an explicit team act), and
    catches a team's first-onboarded-member case where every node so far was
    written by one person but several people are now registered. The
    stamped-email check runs first (short-circuits the person scan in the common
    single-writer/unregistered case; PERSON nodes are few and this runs once per
    prime call, not hot-path, but the ordering still avoids the extra scan when
    unneeded). A solo graph's output stays byte-identical to the pre-WP-P13n-2
    global digest either way -- a solo graph has at most one registered person
    email (itself)."""
    if not current_email or config.prime_personalize == "off":
        return False
    if config.prime_personalize == "on":
        return True
    return (
        len(_distinct_stamped_emails(storage)) > 1
        or len(_registered_person_emails(storage)) > 1
    )


def _task_matches_email(node: dict, email: str) -> bool:
    """A task is "yours" if you created it, currently hold the claim, OR are the
    assignee -- created_by/claimed_by are server-resolved stamp dicts (WP-P13n-1),
    while `assigned_to` (WP-TC8) is a bare casefolded email STRING (client-declared,
    trust-based -- see cognition_update_task), never the free-text `owner`. Matched
    case-insensitively (casefold, not lower) on all sides -- `email` is already
    casefolded by the single normalization point in `generate_prime`, but this
    function casefolds the stamp (and re-casefolds `email`) so it's correct
    called in isolation too."""
    meta = node.get("metadata", {})
    email = email.casefold()
    for key in ("created_by", "claimed_by"):
        stamp = meta.get(key)
        if isinstance(stamp, dict) and stamp.get("email") and stamp["email"].casefold() == email:
            return True
    assigned_to = meta.get("assigned_to")
    return bool(isinstance(assigned_to, str) and assigned_to and assigned_to.casefold() == email)


def _open_tasks(storage: CognitionStorage) -> list[dict]:
    nodes = storage.get_nodes_by_type(CognitionNodeType.TASK)
    return [
        n for n in nodes
        if n.get("metadata", {}).get("status", "open") not in _TASK_CLOSED_STATUSES
    ]


def _sort_tasks(nodes: list[dict]) -> list[dict]:
    """Same two-pass stable sort as _format_tasks: recency desc, then priority asc."""
    nodes = sorted(nodes, key=lambda n: n.get("timestamp", ""), reverse=True)
    nodes.sort(key=lambda n: SEVERITY_ORDER.get(n.get("severity", "normal"), 2))
    return nodes


def _format_your_tasks(
    storage: CognitionStorage, cap: int, current_email: str, maxlen: int = 0
) -> tuple[str, set[str]]:
    """'Your Open Tasks': open tasks you created or currently claim, capped.

    Returns (section_text, all_your_task_ids) -- the FULL id set (not just the
    shown/capped subset) so _format_team_critical can exclude every task that's
    already yours, including ones past the cap (already accounted for by the
    overflow line here, not worth re-surfacing under Team Critical too)."""
    mine = [n for n in _open_tasks(storage) if _task_matches_email(n, current_email)]
    if not mine:
        return "", set()

    mine_sorted = _sort_tasks(mine)
    shown = mine_sorted[:cap]
    lines = [_format_task(n, maxlen) for n in shown]
    overflow = len(mine_sorted) - len(shown)
    if overflow > 0:
        lines.append(f"- +{overflow} more of your open tasks — use cognition_list_tasks")
    return "## Your Open Tasks\n" + "\n".join(lines), {n["id"] for n in mine}


def _format_team_critical(
    storage: CognitionStorage, cap: int, exclude_ids: set[str], maxlen: int = 0
) -> str:
    """'Team Critical': open critical/high tasks not already shown under Your
    Open Tasks -- other people's (or unclaimed) urgent work."""
    critical = [
        n for n in _open_tasks(storage)
        if n["id"] not in exclude_ids and n.get("severity") in ("critical", "high")
    ]
    if not critical:
        return ""

    shown = _sort_tasks(critical)[:cap]
    lines = [_format_task(n, maxlen) for n in shown]
    overflow = len(critical) - len(shown)
    if overflow > 0:
        lines.append(f"- +{overflow} more critical/high tasks — use cognition_list_tasks")
    return "## Team Critical\n" + "\n".join(lines)


def _format_your_activity(
    storage: CognitionStorage, config: PrimeConfig, current_email: str, maxlen: int = 0
) -> str:
    """'Your Recent Activity': your own most recent episodes, decisions, and
    discoveries (recorded_by.email match), each type capped independently and
    grouped in that order under one header."""
    groups = (
        (CognitionNodeType.EPISODE, config.prime_your_episode_limit),
        (CognitionNodeType.DECISION, config.prime_your_decision_limit),
        (CognitionNodeType.DISCOVERY, config.prime_your_discovery_limit),
    )
    current_email = current_email.casefold()
    lines: list[str] = []
    for node_type, limit in groups:
        mine = [n for n in storage.get_nodes_by_type(node_type) if _node_email(n) == current_email]
        mine.sort(key=lambda n: n.get("timestamp", ""), reverse=True)
        lines.extend(_format_node(n, maxlen) for n in mine[:limit])
    if not lines:
        return ""
    return "## Your Recent Activity\n" + "\n".join(lines)


def _format_constraints(storage: CognitionStorage, limit: int, maxlen: int = 0) -> str:
    """Format active constraints, sorted by severity, dropping only `low` (C2).

    HEAD-filtered like _format_workflows (task 0d7e84d52537, folded into
    WP-P13n-2 per the personalized-prime scope decision): a constraint with an
    incoming SUPERSEDES edge is an old version and is excluded -- only the
    superseding HEAD is shown, so a revised constraint doesn't duplicate."""
    nodes = storage.get_nodes_by_type(CognitionNodeType.CONSTRAINT)
    nodes = [
        n for n in nodes
        if n.get("severity") != "low"
        and not storage.get_predecessors(n["id"], CognitionEdgeType.SUPERSEDES)
    ]
    if not nodes:
        return ""

    nodes.sort(key=lambda n: SEVERITY_ORDER.get(n.get("severity", "normal"), 2))
    shown = nodes[:limit]
    lines = [_format_node(n, maxlen) for n in shown]
    return "## Active Constraints\n" + "\n".join(lines)


def _format_workflows(storage: CognitionStorage, limit: int, maxlen: int = 0) -> str:
    """Format stored workflow HEAD titles (supersession-resolved), capped with the
    existing overflow idiom. A workflow with an incoming SUPERSEDES edge is an old
    version (supersedes points newer -> older), so it is excluded here -- only
    HEADs are shown. Cheap in-memory filter; no embeddings involved."""
    nodes = storage.get_nodes_by_type(CognitionNodeType.WORKFLOW)
    heads = [
        n for n in nodes
        if not storage.get_predecessors(n["id"], CognitionEdgeType.SUPERSEDES)
    ]
    if not heads:
        return ""

    heads.sort(key=lambda n: n.get("timestamp", ""), reverse=True)
    shown = heads[:limit]
    lines = [_format_node(n, maxlen) for n in shown]
    overflow = len(heads) - len(shown)
    if overflow > 0:
        lines.append(f"- +{overflow} more workflows — use cognition_get_workflow")
    return "## Workflows\n" + "\n".join(lines)


def _format_document_count(storage: CognitionStorage) -> str:
    """One-line stored-document count so agents know the document tools exist,
    even before they have a reason to call cognition_get_document. Omitted when
    zero (consistent with every other section's empty-drops-the-section rule).

    HEAD-filtered same as _format_workflows: documents version via SUPERSEDES too
    (_validate_supersedes_shape legalizes document->document), so a naive raw count
    would over-report a revised document once per revision instead of once."""
    nodes = storage.get_nodes_by_type(CognitionNodeType.DOCUMENT)
    heads = [
        n for n in nodes
        if not storage.get_predecessors(n["id"], CognitionEdgeType.SUPERSEDES)
    ]
    count = len(heads)
    if count == 0:
        return ""
    noun = "document" if count == 1 else "documents"
    return (
        f"{count} stored {noun} — use cognition_search or cognition_get_document "
        "to retrieve, cognition_store_document to add more."
    )


def _format_patterns(storage: CognitionStorage, limit: int, maxlen: int = 0) -> str:
    """Format recent patterns."""
    nodes = storage.get_recent_nodes(limit=limit, node_type=CognitionNodeType.PATTERN)
    if not nodes:
        return ""

    lines = [_format_node(n, maxlen) for n in nodes]
    return "## Recent Patterns\n" + "\n".join(lines)


def _format_decisions(storage: CognitionStorage, limit: int, maxlen: int = 0) -> str:
    """Format recent decisions."""
    nodes = storage.get_recent_nodes(limit=limit, node_type=CognitionNodeType.DECISION)
    if not nodes:
        return ""

    lines = [_format_node(n, maxlen) for n in nodes]
    return "## Recent Decisions\n" + "\n".join(lines)


def _format_incidents(
    storage: CognitionStorage, days: int, min_severity: str, maxlen: int = 0
) -> str:
    """Format recent incidents from the last N days at or above min_severity."""
    nodes = storage.get_nodes_by_type(CognitionNodeType.INCIDENT)
    if not nodes:
        return ""

    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    min_rank = SEVERITY_ORDER.get(min_severity, 1)
    recent = [
        n for n in nodes
        if n.get("timestamp", "") >= cutoff
        and SEVERITY_ORDER.get(n.get("severity", "normal"), 2) <= min_rank
    ]
    if not recent:
        return ""

    recent.sort(key=lambda n: SEVERITY_ORDER.get(n.get("severity", "normal"), 2))
    lines = [_format_node(n, maxlen) for n in recent]
    return "## Recent Incidents\n" + "\n".join(lines)


def _onboard_declined_emails(cognition_dir: Path) -> set[str]:
    """Casefolded emails that declined/snoozed onboarding on THIS machine. Missing
    file == empty set; blank/malformed lines are ignored. Never raises."""
    path = cognition_dir / ONBOARD_DECLINE_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip().casefold() for line in raw.splitlines() if line.strip()}


def _has_person_node(storage: CognitionStorage, email: str) -> bool:
    """Whether a person node's (casefolded, stored-casefolded) email matches."""
    for n in storage.get_nodes_by_type(CognitionNodeType.PERSON):
        person = n.get("metadata", {}).get("person", {})
        if person.get("email") == email:
            return True
    return False


# ── WP-TC16: role-aware prime (manager rollup / subordinate view) ──────────────
#
# Reporting relationship (reports_to_email) is DISTINCT from person.role (a
# free-text job title) — never conflate the two. Graph owns HUMAN roles only;
# agent roles stay in teammate-comms (ruling 6be2e867f91e).


@dataclass(frozen=True)
class _RoleContext:
    """Result of ONE person-node scan (WP-TC16) -- built once per prime run and
    threaded into the manager-rollup, subordinate-decisions, and (WP-OnboardPayoff)
    identity-header sections, so a middle manager's prime never re-scans person
    nodes for any of them."""

    my_person: dict | None
    direct_reports: list[dict]
    my_manager_email: str  # casefolded; "" when absent
    # WP-OnboardPayoff: resolved NAME of my_manager_email (via the same scan's
    # email->name map), "" when unresolved -- defaulted so the early short-circuit
    # in _derive_role (which legitimately has no manager to resolve) stays a valid
    # construction even if that call site is ever missed in a future edit.
    my_manager_name: str = ""


def _derive_role(storage: CognitionStorage, current_email: str) -> _RoleContext:
    """Resolve `current_email` (already casefolded by generate_prime's single
    normalization point) into a `_RoleContext` via ONE `get_nodes_by_type(PERSON)`
    scan: `my_person` (a person node whose stored, already-casefolded email
    matches), `direct_reports` (person nodes whose `reports_to_email` matches
    `current_email`), `my_manager_email` (my_person's own `reports_to_email`,
    already casefolded at write time -- see `_register_person`/`_update_person`),
    and `my_manager_name` (WP-OnboardPayoff: resolved from an email->name map
    built during the SAME scan -- one scan preserved, no second pass for the
    identity header). MANAGER role iff `direct_reports` non-empty; SUBORDINATE
    role iff `my_manager_email` non-empty; both may hold (middle manager). An
    empty `current_email` short-circuits to an all-empty context without
    scanning."""
    my_person: dict | None = None
    direct_reports: list[dict] = []
    if not current_email:
        return _RoleContext(my_person, direct_reports, "", "")
    by_email: dict[str, str] = {}
    for n in storage.get_nodes_by_type(CognitionNodeType.PERSON):
        person = n.get("metadata", {}).get("person", {})
        email = person.get("email")
        if email:
            by_email[email] = person.get("name") or ""
        if email == current_email:
            my_person = n
        if person.get("reports_to_email") == current_email:
            direct_reports.append(n)
    my_manager_email = ""
    if my_person is not None:
        my_manager_email = my_person.get("metadata", {}).get("person", {}).get("reports_to_email") or ""
    my_manager_name = by_email.get(my_manager_email, "") if my_manager_email else ""
    return _RoleContext(my_person, direct_reports, my_manager_email, my_manager_name)


def _parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO timestamp, tolerating a NAIVE (no-tzinfo) string -- treated
    as UTC -- from a replayed or hand-edited journal. Write paths always stamp
    aware timestamps, but replay validates nothing; a naive-but-valid string
    parses fine via `fromisoformat` and would otherwise raise `TypeError` (not
    `ValueError`) on the aware-naive subtraction downstream, crashing
    `generate_prime` for every user of that graph (same class as WP-TC9's
    98dcca4 lesson: write-side validation is not protection against replay).
    Returns `None` only for a genuinely unparseable string."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _humanize_claim_age(claimed_at: str | None) -> str:
    """`claimed_at` (an ISO timestamp, or None for a legacy/unattributed claim) as
    a compact human age string, day granularity -- "0d" for same-day. None input
    (never dereferenced as a stale check here; staleness is the caller's job)
    renders as "unknown age" so a legacy row is still readable, never crashes."""
    if not claimed_at:
        return "unknown age"
    claimed = _parse_iso_datetime(claimed_at)
    if claimed is None:
        return "unknown age"
    days = max(0, (datetime.now(UTC) - claimed).days)
    return f"{days}d"


def _format_your_team(
    storage: CognitionStorage, config: PrimeConfig, role: _RoleContext, maxlen: int = 0
) -> str:
    """'Your Team' (MANAGER role, personalized-only): one TASK scan bucketed by
    claimant email among direct reports. In-progress rows show claimant + claim
    age; a row is STALE iff claim age is strictly greater than
    `config.prime_stale_claim_days` (exactly-N-days-old is NOT stale; a null/
    legacy claimed_at is NEVER stale -- unverifiable is not the same as stale,
    the standing doctrine). Blocked rows always show. Unclaimed/unstamped tasks
    never appear (attribution doctrine). Capped at `config.prime_rollup_cap`
    TOTAL rows: stale first (most actionable), then blocked, then in-progress,
    recency-desc within each group; an overflow line names the remainder."""
    report_names: dict[str, str] = {}
    for p in role.direct_reports:
        info = p.get("metadata", {}).get("person", {})
        email = info.get("email") or ""
        if email:
            report_names[email] = info.get("name") or email
    if not report_names:
        return ""

    stale_cutoff = timedelta(days=config.prime_stale_claim_days)
    now = datetime.now(UTC)

    stale_rows: list[tuple[dict, str]] = []
    blocked_rows: list[tuple[dict, str]] = []
    fresh_rows: list[tuple[dict, str]] = []
    for n in storage.get_nodes_by_type(CognitionNodeType.TASK):
        meta = n.get("metadata", {})
        claimed_by = meta.get("claimed_by")
        raw_claimant_email = claimed_by.get("email") if isinstance(claimed_by, dict) else None
        # claimed_by.email is a verbatim git-config provenance stamp, never
        # casefolded at write time (unlike person emails) -- casefold here at
        # read time, matching the _task_matches_email/_node_email precedent,
        # so a mixed-case git config still matches the casefolded report_names keys.
        claimant_email = raw_claimant_email.casefold() if raw_claimant_email else None
        if not claimant_email or claimant_email not in report_names:
            continue
        status = meta.get("status", "open")
        if status == "blocked":
            blocked_rows.append((n, claimant_email))
        elif status == "in_progress":
            claimed_at = _task_claimed_at(meta.get("transitions", []))
            is_stale = False
            if claimed_at:
                parsed_claimed_at = _parse_iso_datetime(claimed_at)
                if parsed_claimed_at is not None:
                    is_stale = (now - parsed_claimed_at) > stale_cutoff
            (stale_rows if is_stale else fresh_rows).append((n, claimant_email))

    if not stale_rows and not blocked_rows and not fresh_rows:
        return ""

    def _recency_desc(rows: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
        return sorted(rows, key=lambda pair: pair[0].get("timestamp", ""), reverse=True)

    ordered = _recency_desc(stale_rows) + _recency_desc(blocked_rows) + _recency_desc(fresh_rows)
    total = len(ordered)
    shown = ordered[: config.prime_rollup_cap]

    lines: list[str] = []
    for n, claimant_email in shown:
        summary = _truncate(n.get("summary", "No summary"), maxlen)
        name = report_names.get(claimant_email, claimant_email)
        meta = n.get("metadata", {})
        if meta.get("status") == "blocked":
            lines.append(f"- {summary} ({name}, blocked)")
        else:
            claimed_at = _task_claimed_at(meta.get("transitions", []))
            age = _humanize_claim_age(claimed_at)
            lines.append(f"- {summary} ({name}, claimed {age})")

    overflow = total - len(shown)
    if overflow > 0:
        lines.append(f"- +{overflow} more of your team's tasks — use cognition_list_tasks")
    return "## Your Team\n" + "\n".join(lines)


def _format_manager_decisions(
    storage: CognitionStorage, config: PrimeConfig, role: _RoleContext, maxlen: int = 0
) -> str:
    """'Your Manager's Recent Decisions' (SUBORDINATE role, personalized-only):
    decision nodes whose `_node_email` matches `role.my_manager_email`, newest
    first, capped at `config.prime_manager_decision_limit`. Deliberately NO
    HEAD-filter -- mirrors `_format_decisions` exactly (the global Recent
    Decisions model has no supersedes check either), so a superseded decision
    can legitimately appear here too, same as in the global section. This is a
    KNOWN, documented overlap with global Recent Decisions -- deduping would
    silently change the global section's own semantics, so it is left alone.
    A dangling manager email (no person node registered for it) still works:
    decisions filter by the stamped email string, and the manager's name comes
    from the decision's own `recorded_by`, not from a person-node lookup.
    "Own claims" -- the other half of the manager/subordinate ruling -- is
    ALREADY covered by 'Your Open Tasks' claimed_by branch (WP-P13n-2); this
    section does not duplicate that."""
    if not role.my_manager_email:
        return ""
    mine = [
        n for n in storage.get_nodes_by_type(CognitionNodeType.DECISION)
        if _node_email(n) == role.my_manager_email
    ]
    if not mine:
        return ""
    mine.sort(key=lambda n: n.get("timestamp", ""), reverse=True)
    shown = mine[: config.prime_manager_decision_limit]
    lines = [_format_node(n, maxlen) for n in shown]
    return "## Your Manager's Recent Decisions\n" + "\n".join(lines)


# ── WP-TC14: "Since You Were Gone" digest ───────────────────────────────────


def _last_seen_for(cognition_dir: Path, email: str) -> str | None:
    """My last-seen marker timestamp (aware-UTC ISO string), or None when the
    file is missing/unreadable/malformed JSON/non-dict/the value for this
    email is missing-or-non-string (e.g. a null entry) -- never raises (the
    onboard-declined OSError->empty-set model, extended to JSONDecodeError).
    None means "first run or corrupted", NEVER "no digest" -- the caller
    falls back to a capped lookback window instead."""
    email = (email or "").casefold()
    if not email:
        return None
    path = cognition_dir / LAST_SEEN_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(email)
    return value if isinstance(value, str) and value else None


def _stamp_last_seen(cognition_dir: Path, email: str) -> None:
    """Read-modify-write my (casefolded) entry in .cognition/last-seen.json to
    now (aware UTC ISO), UNCONDITIONALLY overwriting whatever was there --
    last-writer-wins is also the clock-skew self-heal (a future-dated marker
    gets replaced with real now on the next session). No-op on an empty email.

    Called ONLY from prime.py's own main() (the CLI/hook path), AFTER prime
    output is produced -- generate_prime itself stays pure read-only (mirrors
    where _consume_rehydrate_flag's mutation already lives). Guarded by
    git_hygiene's established lock (60s stale detection) since two teammates
    starting sessions on a shared machine concurrently is exactly the
    per-email ruling's race -- an unlocked RMW can silently lose the other
    email's fresh entry. A short bounded retry (3 attempts, 20ms apart, worst
    case ~40ms) gives realistically-concurrent same-machine session-starts a
    fair chance to both land rather than the second one being dropped by a
    single failed attempt; retries exhausted still degrades to "skip the
    stamp entirely" (a missed stamp just falls back to the lookback window
    next session -- never blocks or fails the SessionStart hook). The write
    itself is atomic (temp file in the same dir + os.replace) so a crash
    never leaves torn JSON, and the whole body is wrapped in
    suppress(OSError) -- a read-only filesystem must never fail the hook.
    """
    email = (email or "").casefold()
    if not email:
        return
    lock_path = cognition_dir / f"{LAST_SEEN_FILENAME}.lock"
    acquired = False
    for attempt in range(_LAST_SEEN_LOCK_ATTEMPTS):
        if _acquire_lock(lock_path):
            acquired = True
            break
        if attempt < _LAST_SEEN_LOCK_ATTEMPTS - 1:
            time.sleep(_LAST_SEEN_LOCK_RETRY_DELAY_S)
    if not acquired:
        return
    try:
        with contextlib.suppress(OSError):
            path = cognition_dir / LAST_SEEN_FILENAME
            tmp_path = cognition_dir / f"{LAST_SEEN_FILENAME}.tmp"
            # Clean up a stray .tmp left by a process killed between write_text
            # and os.replace on a prior stamp -- gate F1: unlink is a no-op if
            # nothing is there, so this never affects the happy path. Must run
            # BEFORE the write attempt below, so a stray survives even if THIS
            # stamp also fails to write (a second disk hiccup, say).
            tmp_path.unlink(missing_ok=True)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, json.JSONDecodeError, ValueError):
                data = {}
            data[email] = datetime.now(UTC).isoformat()
            tmp_path.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp_path, path)
    finally:
        _release_lock(lock_path)


def _format_since_you_were_gone(
    storage: CognitionStorage, config: PrimeConfig, current_email: str, maxlen: int = 0
) -> str:
    """'Since You Were Gone' (personalized-only): decision/constraint/incident
    nodes with `timestamp` STRICTLY greater than a per-email high-water-mark
    cutoff (my last-seen marker when present, else `now -
    prime_digest_fallback_days` -- a capped lookback, never a full-history
    dump). Comparison is LEXICOGRAPHIC ISO string compare only -- no datetime
    parsing anywhere in this function, so a naive/malformed node timestamp
    simply compares low and drops out or shows (degrade, never crash -- the
    TC16-F2 naive-timestamp crash class is structurally absent here).

    Excludes nodes whose `_node_email` equals my casefolded email ("your own
    writes are not news to you"). UNSTAMPED nodes (`_node_email` == "") are
    INCLUDED -- an awareness view reports content, not people (deliberate
    divergence from the rollup's attribution doctrine, which gates on
    positive attribution because it names people).

    Constraints are HEAD-filtered (mirrors Active Constraints -- the inline
    `not storage.get_predecessors(..., SUPERSEDES)` expression, a 4th copy of
    the same pattern already inlined 3x in this file). Decisions and
    incidents are NOT HEAD-filtered -- mirrors their own global sections
    exactly (same "mirror each type's existing semantics" precedent TC16
    established for Your Manager's Recent Decisions).

    Newest-first interleave across all three types, capped at
    `config.prime_digest_cap` total with the standard overflow line. A new
    decision can legitimately ALSO appear in Your Manager's Recent Decisions
    and the global Recent Decisions section -- deliberate, same overlap
    ruling as TC16 (deduping would couple sections' semantics)."""
    marker = _last_seen_for(storage.cognition_dir, current_email)
    if marker is not None:
        cutoff = marker
    else:
        cutoff = (datetime.now(UTC) - timedelta(days=config.prime_digest_fallback_days)).isoformat()

    candidates: list[tuple[dict, str]] = []
    for node_type, label in (
        (CognitionNodeType.DECISION, "decision"),
        (CognitionNodeType.CONSTRAINT, "constraint"),
        (CognitionNodeType.INCIDENT, "incident"),
    ):
        for n in storage.get_nodes_by_type(node_type):
            if n.get("timestamp", "") <= cutoff:
                continue
            if _node_email(n) == current_email:
                continue
            if node_type == CognitionNodeType.CONSTRAINT and storage.get_predecessors(
                n["id"], CognitionEdgeType.SUPERSEDES
            ):
                continue
            candidates.append((n, label))

    if not candidates:
        return ""

    candidates.sort(key=lambda pair: pair[0].get("timestamp", ""), reverse=True)
    total = len(candidates)
    shown = candidates[: config.prime_digest_cap]

    lines: list[str] = []
    for n, label in shown:
        summary = _truncate(n.get("summary", "No summary"), maxlen)
        recorded = n.get("metadata", {}).get("recorded_by")
        name = recorded.get("name") if isinstance(recorded, dict) else None
        name = name or "unattributed"
        date = n.get("timestamp", "")[:10]
        lines.append(f"- [{label}] {summary} ({name}, {date})")

    overflow = total - len(shown)
    if overflow > 0:
        lines.append(f"- +{overflow} more since you were gone — use cognition_search")
    return "## Since You Were Gone\n" + "\n".join(lines)


def _onboarding_notice(storage: CognitionStorage, config: PrimeConfig, current_email: str) -> str:
    """The new-user onboarding notice (WP-TC7), or "" when it should not fire.

    Fires when ALL: current_email resolved, prime_onboard on, no matching person
    node, and the email hasn't declined. `current_email` here is ALREADY casefolded
    by generate_prime's single normalization point -- this function does not
    re-casefold, matching _task_matches_email's documented convention of trusting
    an already-normalized caller within this module."""
    if not current_email or not config.prime_onboard:
        return ""
    if _has_person_node(storage, current_email):
        return ""
    if current_email in _onboard_declined_emails(storage.cognition_dir):
        return ""
    return ONBOARDING_NOTICE


def _format_identity_header(role: _RoleContext) -> str:
    """The one-line preamble opening the personalized block once `current_email`
    resolves to a registered person node (WP-OnboardPayoff, Gate D S5 fix) -- ""
    when `role.my_person` is None. Gating is structural, not a new knob: this
    requires a matching person node, `_onboarding_notice`'s New Here banner
    requires NO matching person node -- mutually exclusive by construction. A
    plain preamble line (no `##` heading -- it's not a section, just an anchor).

    Format algebra (pinned, peer-review M2 -- exact punctuation, do not drift):
    start with "You are registered as {name}"; append " — {role}" iff the
    person's `role` is non-empty; append " ({seniority})" iff `seniority` is
    non-empty; append ", reporting to {manager}" iff `reports_to_email` is
    non-empty, where `{manager}` is `role.my_manager_name` when resolved, else
    the raw `reports_to_email` (an unresolvable manager email falls back to
    showing the email rather than disappearing silently); always end with '.'.
    Never crashes, never renders the string "None" for a missing field."""
    if role.my_person is None:
        return ""
    person = role.my_person.get("metadata", {}).get("person", {})
    line = f"You are registered as {person.get('name') or ''}"
    if person.get("role"):
        line += f" — {person['role']}"
    if person.get("seniority"):
        line += f" ({person['seniority']})"
    reports_to_email = person.get("reports_to_email") or ""
    if reports_to_email:
        manager = role.my_manager_name or reports_to_email
        line += f", reporting to {manager}"
    return line + "."


def generate_prime(
    storage: CognitionStorage,
    config: PrimeConfig | None = None,
    current_email: str | None = None,
) -> str:
    """Generate the prime markdown output.

    Args:
        storage: Hydrated CognitionStorage instance
        config: Trim knobs; defaults to PrimeConfig() (the trimmed target shape)
        current_email: The resolved git identity's email (WP-P13n-2), or None/""
            when unresolvable. Gates personalization -- see `_should_personalize`.
            Matched case-insensitively (casefolded once here, the single
            normalization point) against stamped emails, which are themselves
            casefolded by `_node_email`/`_task_matches_email` -- stamps stay
            stored verbatim (WP-P13n-1 provenance untouched); this is a
            prime.py match-time-only normalization. Global-only sections
            (constraints, workflows, documents, patterns, decisions,
            incidents) are unaffected either way.

    When personalized AND `current_email` resolves to a registered person node,
    the block opens with a one-line identity header ("You are registered as
    ...", WP-OnboardPayoff) naming the user, role/seniority, and manager --
    the registration payoff the New Here banner otherwise only implies. The
    header and the banner are mutually exclusive by construction (the header
    needs a matching person node, the banner needs the absence of one).

    Returns:
        Markdown string with project context
    """
    if config is None:
        config = PrimeConfig()

    maxlen = config.prime_summary_maxlen
    current_email = (current_email or "").casefold()
    personalize = _should_personalize(storage, config, current_email)

    # WP-TC7: pinned FIRST section, before Active Constraints.
    sections = [_onboarding_notice(storage, config, current_email)]
    sections.append(_format_constraints(storage, config.prime_constraint_limit, maxlen))

    if personalize:
        # WP-OnboardPayoff/TC16/TC14: pinned order Identity header -> Your Tasks
        # -> Team Critical -> Your Team -> Your Manager's Recent Decisions ->
        # Since You Were Gone -> Your Recent Activity. Role derivation is ONE
        # person-node scan shared by the identity header and both TC16 sections
        # (a middle manager gets both, and the header's manager-name resolution
        # comes from the same scan) -- moved up so it runs once, only when
        # personalizing (never scanned for the global digest).
        role = _derive_role(storage, current_email)
        sections.append(_format_identity_header(role))
        your_tasks, mine_ids = _format_your_tasks(
            storage, config.prime_your_tasks_cap, current_email, maxlen
        )
        sections.append(your_tasks)
        sections.append(_format_team_critical(storage, config.prime_team_critical_cap, mine_ids, maxlen))
        sections.append(_format_your_team(storage, config, role, maxlen))
        sections.append(_format_manager_decisions(storage, config, role, maxlen))
        sections.append(_format_since_you_were_gone(storage, config, current_email, maxlen))
        sections.append(_format_your_activity(storage, config, current_email, maxlen))
    else:
        sections.append(_format_tasks(storage, config.prime_task_cap, maxlen))

    sections.extend([
        _format_workflows(storage, config.prime_workflow_limit, maxlen),
        _format_document_count(storage),
        _format_patterns(storage, config.prime_pattern_limit, maxlen),
        _format_decisions(storage, config.prime_decision_limit, maxlen),
        _format_incidents(storage, config.prime_incident_days, config.prime_incident_min_severity, maxlen),
    ])

    body = "\n\n".join(s for s in sections if s)
    if not body:
        body = "No cognition history recorded yet."

    return (
        "# Vibe Cognition — Project Context\n\n"
        + body
        + "\n\nUse cognition_search and cognition_get_history for full details."
    )


def _consume_rehydrate_flag(cognition_dir: Path) -> str:
    """One-shot journal-loss alert for the next session start (WP-1 item 1.4).

    A server process that detected a LOSSY rehydrate-reset (journal shrunk or
    replaced under its replay offset, dropping in-memory nodes) persists a small
    flag file (storage.REHYDRATE_FLAG_FILENAME). Prime runs in a separate
    process, so this file IS the cross-process plumbing: read it, format a
    warning section, and delete it (shown once — the server's own get_status
    keeps reporting the event for its process lifetime). Never raises; an
    unreadable flag is consumed silently so it cannot wedge every future prime.
    """
    flag = cognition_dir / REHYDRATE_FLAG_FILENAME
    try:
        raw = flag.read_text(encoding="utf-8")
    except OSError:
        return ""
    with contextlib.suppress(OSError):
        flag.unlink()
    try:
        info = json.loads(raw)
        lost = int(info["nodes_lost"])
        at = str(info.get("at", "unknown time"))
    except (ValueError, TypeError, KeyError):
        return ""
    sample = info.get("sample_missing_ids") or []
    sample_note = f" (e.g. {', '.join(sample)})" if sample else ""
    return (
        "WARNING (vibe-cognition): in a previous session the journal was replaced or "
        f"truncated under a live server ({at}); {lost} node(s) recorded in that "
        f"session are no longer on disk{sample_note} — "
        "check `git log -- .cognition/journal.jsonl` or a teammate's clone to recover, "
        "and alert the user."
    )


def main(argv: list[str] | None = None):
    """Entry point for vibe-cognition-prime CLI command.

    Outputs JSON for Claude Code SessionStart hooks.
    Reads REPO_PATH env var or uses cwd. Optionally prepends, in this pinned
    order, a migration note (VIBE_MIGRATION_NOTE, set by the hook's
    migrate_mcp step), an update nudge (VIBE_UPDATE_NOTE, set by the hook's
    update_check step), and a what's-new notice (VIBE_WHATSNEW_NOTE, set by
    the hook's whats_new step) -- any subset may be present, and all are
    surfaced in the same hook output instead of suppressing project-context
    injection.

    When the graph is empty (.cognition/ absent OR nodes == 0), injects an
    onboarding block instructing the LLM to alert the user and call
    cognition_readme. Migration note and onboarding are independent: both emit
    if both conditions hold (note first, then onboarding).

    Also consumes the one-shot journal-loss flag (see _consume_rehydrate_flag)
    and injects its warning ahead of the project context when present.

    ``argv``: explicit args list (tests pass ``[]``); ``None`` (the real CLI
    invocation) reads ``sys.argv[1:]`` via argparse's own default, matching
    migrate_mcp.py's ``main(argv=None)`` convention.
    """
    parser = argparse.ArgumentParser(
        prog="vibe-cognition-prime",
        description=(
            "Print the session-start context digest (SessionStart hook JSON) for "
            "the project at $REPO_PATH (or the current directory). This is the "
            "same output the plugin's SessionStart hook injects automatically -- "
            "run this by hand to preview it. Takes no arguments; reads REPO_PATH "
            "and VIBE_MIGRATION_NOTE from the environment."
        ),
    )
    parser.parse_args(argv)  # WP-13 (4aaef22e25ea): --help correctness only, no new flags

    note = os.environ.get("VIBE_MIGRATION_NOTE", "").strip()
    update_note = os.environ.get("VIBE_UPDATE_NOTE", "").strip()
    whatsnew_note = os.environ.get("VIBE_WHATSNEW_NOTE", "").strip()
    repo_path = resolve_repo_path_env()
    cognition_dir = repo_path / ".cognition"

    sections: list[str] = []
    if note:
        sections.append(note)
    if update_note:
        sections.append(update_note)
    if whatsnew_note:
        sections.append(whatsnew_note)

    try:
        hygiene_state = check_hygiene_state(repo_path, cognition_dir)
        hygiene_line = format_hygiene_announce(hygiene_state)
        if hygiene_line:
            sections.append(hygiene_line)
    except Exception:  # noqa: BLE001
        pass

    # Journal-loss alert (WP-1): surfaced BEFORE the project context so it can't
    # be buried; consumed so it shows exactly once.
    rehydrate_note = _consume_rehydrate_flag(cognition_dir)
    if rehydrate_note:
        sections.append(rehydrate_note)

    storage: CognitionStorage | None = None
    if cognition_dir.exists():
        storage = CognitionStorage(cognition_dir)

    empty = storage is None or storage.get_statistics()["nodes"] == 0

    if empty:
        sections.append(ONBOARDING_BLOCK)
    else:
        try:
            settings = Settings()
            config = PrimeConfig(
                prime_constraint_limit=settings.prime_constraint_limit,
                prime_task_cap=settings.prime_task_cap,
                prime_pattern_limit=settings.prime_pattern_limit,
                prime_decision_limit=settings.prime_decision_limit,
                prime_incident_days=settings.prime_incident_days,
                prime_summary_maxlen=settings.prime_summary_maxlen,
                prime_incident_min_severity=settings.prime_incident_min_severity,
                prime_workflow_limit=settings.prime_workflow_limit,
                prime_personalize=settings.prime_personalize,
                prime_your_tasks_cap=settings.prime_your_tasks_cap,
                prime_team_critical_cap=settings.prime_team_critical_cap,
                prime_your_episode_limit=settings.prime_your_episode_limit,
                prime_your_decision_limit=settings.prime_your_decision_limit,
                prime_your_discovery_limit=settings.prime_your_discovery_limit,
                prime_onboard=settings.prime_onboard,
                prime_stale_claim_days=settings.prime_stale_claim_days,
                prime_rollup_cap=settings.prime_rollup_cap,
                prime_manager_decision_limit=settings.prime_manager_decision_limit,
                prime_digest_cap=settings.prime_digest_cap,
                prime_digest_fallback_days=settings.prime_digest_fallback_days,
            )
        except Exception:  # noqa: BLE001
            config = PrimeConfig()
        # resolve_git_identity is file-read-only and never raises (v0.12.1 P0
        # contract) -- no try/except needed around it, unlike Settings() above.
        current_email = resolve_git_identity(repo_path).get("email") or None
        sections.append(generate_prime(storage, config, current_email))  # type: ignore[arg-type]
        # WP-TC14: stamp AFTER output is produced, only on this (main()'s own)
        # CLI/hook path -- generate_prime itself stays pure read-only, and
        # instructions.py's compact-reinject main() never reaches this branch.
        _stamp_last_seen(cognition_dir, current_email or "")

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n\n".join(sections),
        }
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
