"""WP-Readme: tests for cognition_readme tool + prime.py empty-graph onboarding."""

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType, generate_node_id
from vibe_cognition.cognition.prime import main
from vibe_cognition.cognition.readme import (
    COGNITION_GETTING_STARTED,
    COGNITION_GUIDE,
    ONBOARDING_BLOCK,
)
from vibe_cognition.cognition.storage import CognitionStorage
from vibe_cognition.tools.readme_tool import cognition_readme_core

# ---------------------------------------------------------------------------
# prime.py onboarding tests
# ---------------------------------------------------------------------------


def _run_prime(tmp_path: Path, env: dict) -> dict:
    """Run prime.main() with a patched env and capture its stdout JSON."""
    buf = StringIO()
    with patch.dict("os.environ", env, clear=False), patch("sys.stdout", buf):
        main()
    return json.loads(buf.getvalue())


def _context(output: dict) -> str:
    return output["hookSpecificOutput"]["additionalContext"]


def test_prime_onboarding_when_cognition_absent(tmp_path):
    """prime emits onboarding when .cognition/ does not exist."""
    result = _run_prime(tmp_path, {"REPO_PATH": str(tmp_path), "VIBE_MIGRATION_NOTE": ""})
    ctx = _context(result)
    assert ONBOARDING_BLOCK.strip() in ctx
    assert "No cognition history recorded yet." not in ctx


def test_prime_onboarding_and_note_together(tmp_path):
    """Both migration note AND onboarding emit when .cognition/ absent and note is set."""
    note = "Removed stale vibe-cognition from .mcp.json (preserved: all other servers)."
    result = _run_prime(tmp_path, {"REPO_PATH": str(tmp_path), "VIBE_MIGRATION_NOTE": note})
    ctx = _context(result)
    # Note comes first
    assert ctx.index(note) < ctx.index(ONBOARDING_BLOCK.strip())


def test_prime_onboarding_when_nodes_zero_empty_journal(tmp_path):
    """prime emits onboarding (not fallback) when .cognition/ exists with empty journal."""
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    # journal.jsonl exists but is empty (zero nodes)
    (cognition_dir / "journal.jsonl").write_text("", encoding="utf-8")

    result = _run_prime(tmp_path, {"REPO_PATH": str(tmp_path), "VIBE_MIGRATION_NOTE": ""})
    ctx = _context(result)
    assert ONBOARDING_BLOCK.strip() in ctx
    assert "No cognition history recorded yet." not in ctx


def test_prime_onboarding_when_nodes_zero_missing_journal(tmp_path):
    """prime emits onboarding when .cognition/ exists but journal.jsonl is absent."""
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    # No journal.jsonl -- realistic first-run-after-dir-creation shape.

    result = _run_prime(tmp_path, {"REPO_PATH": str(tmp_path), "VIBE_MIGRATION_NOTE": ""})
    ctx = _context(result)
    assert ONBOARDING_BLOCK.strip() in ctx
    assert "No cognition history recorded yet." not in ctx


def test_prime_no_onboarding_when_nodes_present(tmp_path):
    """prime does NOT emit onboarding when graph has content."""
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    ts = "2026-06-21T00:00:00+00:00"
    node = CognitionNode(
        id=generate_node_id("decision", "Use JSONL as the journal format", ts),
        type=CognitionNodeType.DECISION,
        summary="Use JSONL as the journal format",
        detail="Append-only, human-readable, concurrent-safe.",
        context=[],
        references=[],
        timestamp=ts,
        author="test",
    )
    storage.add_node(node)

    result = _run_prime(tmp_path, {"REPO_PATH": str(tmp_path), "VIBE_MIGRATION_NOTE": ""})
    ctx = _context(result)
    assert ONBOARDING_BLOCK.strip() not in ctx
    # generate_prime path fired -- header present
    assert "Vibe Cognition" in ctx


# ---------------------------------------------------------------------------
# cognition_readme tool tests
# ---------------------------------------------------------------------------


def test_cognition_readme_tool_returns_expected_shape():
    """cognition_readme_core returns a dict with non-empty guide and getting_started."""
    result = cognition_readme_core()
    assert isinstance(result.get("guide"), str) and result["guide"], \
        "guide must be a non-empty string"
    assert isinstance(result.get("getting_started"), str) and result["getting_started"], \
        "getting_started must be a non-empty string"


def test_readme_constants_are_ascii_clean():
    """All readme.py constants must be ASCII-only (hook path constraint)."""
    assert COGNITION_GUIDE.isascii(), "COGNITION_GUIDE contains non-ASCII characters"
    assert COGNITION_GETTING_STARTED.isascii(), "COGNITION_GETTING_STARTED contains non-ASCII"
    assert ONBOARDING_BLOCK.isascii(), "ONBOARDING_BLOCK contains non-ASCII characters"
