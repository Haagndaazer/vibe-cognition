"""T-1b: prime.py — generate_prime() sections and main() hook payload.

Pins: stdout IS the hook payload (SessionStart JSON), sections assembled correctly
(constraints severity-sorted, incidents 30-day windowed, empty→onboarding block),
migration note + hygiene line ordering. The onboarding branch is covered by
test_readme.py; this adds the populated-graph main() payload path.
"""

import io
import json
from datetime import UTC, datetime, timedelta

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
from vibe_cognition.cognition.prime import generate_prime, main
from vibe_cognition.cognition.readme import ONBOARDING_BLOCK

# ── helpers ───────────────────────────────────────────────────────────────────


def _add(storage: CognitionStorage, node_id: str, ntype: CognitionNodeType,
         summary: str, *, severity: str | None = None, timestamp: str | None = None,
         ) -> None:
    ts = timestamp or datetime.now(UTC).isoformat()
    storage.add_node(CognitionNode(
        id=node_id, type=ntype, summary=summary, detail="d",
        context=[], references=[], severity=severity, timestamp=ts, author="t",
    ))


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
    """
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "c1", CognitionNodeType.CONSTRAINT, "low-priority guard", severity="low")
    _add(storage, "c2", CognitionNodeType.CONSTRAINT, "critical hard rule", severity="critical")
    result = generate_prime(storage)
    # Critical must appear before low in the output.
    assert result.index("critical hard rule") < result.index("low-priority guard")


def test_generate_prime_incidents_windowed_30_days(tmp_path):
    """generate_prime: incidents older than 30 days are excluded from output.

    Fails-before: if all incidents were shown regardless of age (the window exists
    to keep the injected context from growing unboundedly on long-running projects).
    """
    storage = CognitionStorage(tmp_path / ".cognition")
    old_ts = (datetime.now(UTC) - timedelta(days=35)).isoformat()
    recent_ts = datetime.now(UTC).isoformat()
    _add(storage, "i1", CognitionNodeType.INCIDENT, "old outage", timestamp=old_ts)
    _add(storage, "i2", CognitionNodeType.INCIDENT, "recent outage", timestamp=recent_ts)

    result = generate_prime(storage)
    assert "recent outage" in result
    assert "old outage" not in result


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
    main()

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
    main()

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
    main()

    data = json.loads(buf.getvalue())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    note_pos = ctx.index("Removed stale MCP entry")
    onboard_pos = ctx.index(ONBOARDING_BLOCK[:30])
    assert note_pos < onboard_pos, "migration note must appear before the body"
