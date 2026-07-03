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
_CURATE_SKILL = _REPO / "skills" / "vibe-curate" / "SKILL.md"
_EDGE_ANALYZER = _REPO / "agents" / "curate-edge-analyzer.md"


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
    """Every user-facing edge type (all CognitionEdgeType — duplicate_of was
    RETIRED in WP-14, no longer a member at all) must be named in BOTH the
    SKILL and the README, so the historical '3 vs 4 vs 5 types' drift can't
    reappear. Pinned to the enum, not a literal list."""
    expected = {e.value for e in CognitionEdgeType}
    assert len(expected) == 6, f"edge-type count changed ({len(expected)}); update the docs + this guard"
    skill_text = _SKILL.read_text(encoding="utf-8")
    readme_text = _README.read_text(encoding="utf-8")
    skill_missing = sorted(v for v in expected if v not in skill_text)
    readme_missing = sorted(v for v in expected if v not in readme_text)
    assert not skill_missing, f"SKILL.md missing edge types: {skill_missing}"
    assert not readme_missing, f"README.md missing edge types: {readme_missing}"


def test_edge_analyzer_output_schema_includes_source_field():
    """WP-10 (9d5a19b30055): edge-analyzer.md's example JSON output must include a
    "source" key, since cognition_add_edges_batch reads source PER-EDGE from each
    array element (default "batch") -- omitting it from the schema means curate's
    provenance ("curate-skill") silently never lands, even though the skill's prose
    says to set it.

    Fails-before: the old schema block had no "source" key at all.
    """
    text = _EDGE_ANALYZER.read_text(encoding="utf-8")
    assert '"source"' in text, "edge-analyzer.md's output schema is missing a source key"


def test_curate_skill_files_do_not_claim_part_of_is_forbidden():
    """WP-10 (9d5a19b30055): neither vibe-curate/SKILL.md nor edge-analyzer.md may
    claim agent part_of is "forbidden" -- nothing in the tool layer rejects it (only
    duplicate_of was ever tool-rejected, and WP-14 retired it entirely rather than
    leaving the rejection in place); the honest position (post-WP-8) is "not
    blocked, just usually redundant with the deterministic matcher; NEVER for
    tasks specifically, which IS a hard skill-level rule but not a tool-level one".

    Fails-before: SKILL.md said "agent part_of is already forbidden" -- literally false.
    """
    for path in (_CURATE_SKILL, _EDGE_ANALYZER):
        text = path.read_text(encoding="utf-8")
        assert "already forbidden" not in text, f"{path.name} still claims part_of is forbidden"
        assert "is forbidden" not in text, f"{path.name} still claims part_of is forbidden"
