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
         ) -> None:
    ts = timestamp or datetime.now(UTC).isoformat()
    storage.add_node(CognitionNode(
        id=node_id, type=ntype, summary=summary, detail="d",
        context=[], references=[], severity=severity, timestamp=ts, author="t",
    ))


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


def test_generate_prime_task_cap_honored_via_config(tmp_path):
    """Task cap is wired to PrimeConfig — an explicit override changes the count
    without any env monkeypatching."""
    storage = CognitionStorage(tmp_path / ".cognition")
    for i in range(4):
        _add(storage, f"t{i}", CognitionNodeType.TASK, f"task {i}")

    result = generate_prime(storage, PrimeConfig(prime_task_cap=2))
    assert result.count("- [task]") == 2
    assert "+2 more open tasks" in result


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
