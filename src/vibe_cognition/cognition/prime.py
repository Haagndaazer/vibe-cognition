"""Prime command — outputs compact project context for Claude Code session injection."""

import argparse
import contextlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..config import Settings, resolve_repo_path_env
from .git_hygiene import check_hygiene_state, format_hygiene_announce
from .git_identity import resolve_git_identity
from .models import CognitionEdgeType, CognitionNodeType
from .readme import ONBOARDING_BLOCK
from .storage import REHYDRATE_FLAG_FILENAME, CognitionStorage

SEVERITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}

_TASK_CLOSED_STATUSES = frozenset({"done", "cancelled"})

# WP-TC7: new-user onboarding notice. Per-machine, local file (never synced via the
# graph/journal) -- one casefolded email per line. Written by the AGENT via an
# ordinary file append when the human declines/snoozes (no new MCP tool, no graph
# write); read here only. Filename referenced by git_hygiene.py's versioned writer
# (kept as a plain string there too, not imported, matching that module's existing
# stdlib-only/standalone convention for REHYDRATE_FLAG_FILENAME).
ONBOARD_DECLINE_FILENAME = "onboard-declined"

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


def _should_personalize(storage: CognitionStorage, config: PrimeConfig, current_email: str) -> bool:
    """Whether this prime run personalizes, per `config.prime_personalize`.

    No resolvable current-user email means there's nothing to match against --
    global digest regardless of mode. 'off' is always global; 'on' is always
    personalized (once an email exists); 'auto' personalizes only when the graph
    has more than one distinct stamped email (a solo graph's output must stay
    byte-identical to the pre-WP-P13n-2 global digest)."""
    if not current_email or config.prime_personalize == "off":
        return False
    if config.prime_personalize == "on":
        return True
    return len(_distinct_stamped_emails(storage)) > 1


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
        your_tasks, mine_ids = _format_your_tasks(
            storage, config.prime_your_tasks_cap, current_email, maxlen
        )
        sections.append(your_tasks)
        sections.append(_format_team_critical(storage, config.prime_team_critical_cap, mine_ids, maxlen))
    else:
        sections.append(_format_tasks(storage, config.prime_task_cap, maxlen))

    if personalize:
        sections.append(_format_your_activity(storage, config, current_email, maxlen))

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
    Reads REPO_PATH env var or uses cwd. Optionally prepends a one-line
    migration note from VIBE_MIGRATION_NOTE (set by the SessionStart hook when
    it removes a stale per-project MCP entry), so that note is surfaced in the
    same hook output instead of suppressing project-context injection.

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
    repo_path = resolve_repo_path_env()
    cognition_dir = repo_path / ".cognition"

    sections: list[str] = []
    if note:
        sections.append(note)

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
            )
        except Exception:  # noqa: BLE001
            config = PrimeConfig()
        # resolve_git_identity is file-read-only and never raises (v0.12.1 P0
        # contract) -- no try/except needed around it, unlike Settings() above.
        current_email = resolve_git_identity(repo_path).get("email") or None
        sections.append(generate_prime(storage, config, current_email))  # type: ignore[arg-type]

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n\n".join(sections),
        }
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
