"""WP-Readme: tests for cognition_readme tool + prime.py empty-graph onboarding."""

import inspect
import json
import re
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from vibe_cognition.cognition.git_hygiene import _GITATTRIBUTES_RULE
from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType, generate_node_id
from vibe_cognition.cognition.prime import main
from vibe_cognition.cognition.readme import (
    COGNITION_GETTING_STARTED,
    COGNITION_GUIDE,
    ONBOARDING_BLOCK,
)
from vibe_cognition.cognition.storage import CognitionStorage
from vibe_cognition.tools.cognition_tools import register_cognition_tools
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


def test_getting_started_record_example_matches_real_signature(mock_mcp):
    """WP-2 item 3: the cognition_record example in COGNITION_GETTING_STARTED must
    use real, current kwargs of the actual tool -- not a stale/typo'd shape (it
    used to say type= instead of node_type= and omit required context/author).

    Extracts the kwarg names used in the example call and asserts they are a
    SUBSET of cognition_record's real parameter names, so a future rename of
    cognition_record can't silently leave the example drifted again.

    Fails-before: "type" is not a real parameter (real one is "node_type") --
    this would have failed against the old example text.
    """
    register_cognition_tools(mock_mcp)
    real_params = set(inspect.signature(mock_mcp.tools["cognition_record"]).parameters) - {"ctx"}

    match = re.search(r"cognition_record\((.*?)\)", COGNITION_GETTING_STARTED, re.DOTALL)
    assert match, "no cognition_record(...) example found in COGNITION_GETTING_STARTED"
    example_kwargs = set(re.findall(r"(\w+)=", match.group(1)))

    assert example_kwargs, "example call has no kwargs -- regex likely broken"
    unknown = example_kwargs - real_params
    assert not unknown, (
        f"example uses kwarg(s) not in cognition_record's real signature: {unknown} "
        f"(real params: {real_params})"
    )
    required = {"node_type", "summary", "detail", "context", "author"}
    missing = required - example_kwargs
    assert not missing, f"example is missing required kwarg(s): {missing}"


def test_guide_core_loop_references_server_instructions_not_restates():
    """WP-7 (9aca47c5803d): the record->curate loop's mechanics live in ONE
    owning channel (SERVER_INSTRUCTIONS) now; COGNITION_GUIDE's "core loop"
    section points back at it instead of independently restating the full
    loop, so the two can't quietly drift apart the way Stage-1's skill split
    did.

    Fails-before: the old text fully restated "1. Record as you work... 2.
    Curate after recording..." with no reference to the standing practices.
    """
    normalized = " ".join(COGNITION_GUIDE.split())  # collapse wrapping whitespace
    assert "MCP server instructions" in normalized
    assert "Three standing practices" in normalized


def test_readme_constants_are_ascii_clean():
    """All readme.py constants must be ASCII-only (hook path constraint)."""
    assert COGNITION_GUIDE.isascii(), "COGNITION_GUIDE contains non-ASCII characters"
    assert COGNITION_GETTING_STARTED.isascii(), "COGNITION_GETTING_STARTED contains non-ASCII"
    assert ONBOARDING_BLOCK.isascii(), "ONBOARDING_BLOCK contains non-ASCII characters"


def test_gitattr_union_merge_present_in_guide():
    """COGNITION_GUIDE must contain the union-merge gitattributes instruction."""
    assert "merge=union" in COGNITION_GUIDE


def test_gitattr_union_merge_present_in_getting_started():
    """COGNITION_GETTING_STARTED must contain the union-merge gitattributes pointer."""
    assert "merge=union" in COGNITION_GETTING_STARTED


def test_gitattr_auto_write_never_includes_text_flag():
    """git_hygiene's AUTO-WRITTEN rule must always be exactly merge=union, never
    -text (decision 9f13a8099e03, known-intentional in the fable-audit burndown
    plan). WP-1 item 3 narrowed the old blanket "COGNITION_GUIDE must never even
    mention -text" guard to this: -text may be DISCLOSED in docs as a manual,
    cut-over-gated team decision (scar: 90ee3c1b968c -- applying -text without a
    cut-over commit IS the byte-rewrite it defends against), but the server must
    never write it automatically."""
    assert _GITATTRIBUTES_RULE == ".cognition/journal.jsonl merge=union"
    assert "-text" not in _GITATTRIBUTES_RULE


def test_gitattr_text_flag_disclosure_is_cut_over_gated():
    """If COGNITION_GUIDE discloses -text, it must also warn about the cut-over
    requirement (scar: 90ee3c1b968c) -- disclosure without that caveat would
    recreate the exact hazard decision 9f13a8099e03 originally banned."""
    if "-text" in COGNITION_GUIDE:
        assert "cut-over" in COGNITION_GUIDE.lower()
