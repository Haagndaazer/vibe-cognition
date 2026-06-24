"""WP-D3: doc-drift GUARD — the vibe-cognition SKILL tool table and the edge-type
lists must stay in sync with the code (turns audit S-3 into a regression guard, the
same structural-binding discipline as documents_with_sha applied to docs).

These are PRESENCE checks (name appears in the doc), not semantics checks — they
can't catch a wrong "How Created" label; that needs manual prose review.
"""

import asyncio
from pathlib import Path

from fastmcp import FastMCP

from vibe_cognition.cognition.models import CognitionEdgeType, CognitionNodeType
from vibe_cognition.tools import register_all_tools

_REPO = Path(__file__).resolve().parent.parent
_SKILL = _REPO / "skills" / "vibe-cognition" / "SKILL.md"
_README = _REPO / "README.md"


def _registered_tool_names() -> set[str]:
    mcp = FastMCP("drift-guard")
    register_all_tools(mcp)
    tools = asyncio.run(mcp.list_tools())  # FastMCP 3.x async API (.name per tool)
    return {t.name for t in tools}


def test_skill_tool_table_documents_every_registered_tool():
    """Every registered MCP tool name must appear in the vibe-cognition SKILL.md
    (anywhere in the file — main table or the service/dashboard row). Fails if a
    future tool is added without documenting it (exactly the S-3 miss)."""
    skill_text = _SKILL.read_text(encoding="utf-8")
    registered = _registered_tool_names()
    assert registered, "no tools enumerated — register_all_tools/list_tools changed?"
    missing = sorted(name for name in registered if name not in skill_text)
    assert not missing, f"SKILL.md does not document registered tools: {missing}"


def test_node_types_documented_in_skill_and_readme():
    """Every CognitionNodeType value must appear in BOTH SKILL.md and README.md.

    Mirrors the edge-type ==N guard: forces any future node type (e.g. 'task') to
    self-document before shipping, or this guard fails explicitly rather than silently."""
    expected = {e.value for e in CognitionNodeType}
    skill_text = _SKILL.read_text(encoding="utf-8")
    readme_text = _README.read_text(encoding="utf-8")
    skill_missing = sorted(v for v in expected if v not in skill_text)
    readme_missing = sorted(v for v in expected if v not in readme_text)
    assert not skill_missing, f"SKILL.md missing node types: {skill_missing}"
    assert not readme_missing, f"README.md missing node types: {readme_missing}"


def test_edge_types_documented_in_skill_and_readme():
    """Every user-facing edge type (all CognitionEdgeType except the reserved
    duplicate_of) must be named in BOTH the SKILL and the README, so the historical
    '3 vs 4 vs 5 types' drift can't reappear. Pinned to the enum, not a literal list."""
    expected = {
        e.value for e in CognitionEdgeType if e is not CognitionEdgeType.DUPLICATE_OF
    }
    assert len(expected) == 6, f"edge-type count changed ({len(expected)}); update the docs + this guard"
    skill_text = _SKILL.read_text(encoding="utf-8")
    readme_text = _README.read_text(encoding="utf-8")
    skill_missing = sorted(v for v in expected if v not in skill_text)
    readme_missing = sorted(v for v in expected if v not in readme_text)
    assert not skill_missing, f"SKILL.md missing edge types: {skill_missing}"
    assert not readme_missing, f"README.md missing edge types: {readme_missing}"
