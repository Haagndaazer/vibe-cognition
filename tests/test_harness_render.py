"""WP-P0: rendered per-harness files must equal a fresh render of their sources,
every token must be consumed, every Codex harness block must actually change
the output, sources must not carry harness literals, and no output directory
may exist without a source."""

import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tools"))

import render_harness as rh  # noqa: E402

from vibe_cognition import harness  # noqa: E402

_FORBIDDEN = (
    "/vibe-", "$vibe-", "Agent tool", "spawn_agent", "/plugin update", "codex plugin",
    "CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT", "Claude Code", "Codex",
)
_FORBIDDEN_WORDS = re.compile(r"(haiku|sonnet)", re.IGNORECASE)
_BLOCK = re.compile(r"\{\{harness:([a-z-]+)\}\}.*?\{\{/harness\}\}", re.DOTALL)


def _sources() -> list[Path]:
    return sorted(rh.SKILLS_SRC.glob("*/SKILL.md")) + sorted(rh.AGENTS_SRC.glob("*.md"))


def test_rendered_outputs_match_sources():
    problems = rh.render_all(check=True)
    assert not problems, "\n".join(problems)


def test_every_target_renders_without_leftover_tokens():
    for src, _dst, hz in rh.targets():
        out = rh.render_text(src.read_text(encoding="utf-8"), hz)
        assert "{{" not in out and "}}" not in out, src


def test_codex_blocks_change_the_codex_output():
    """A renderer that ignored harness blocks would leave Claude and Codex
    outputs identical wherever a block exists — assert they differ there."""
    claude = harness.HARNESSES[harness.CLAUDE_CODE]
    codex = harness.HARNESSES[harness.CODEX]
    checked = 0
    for src in _sources():
        text = src.read_text(encoding="utf-8")
        if not _BLOCK.search(text):
            continue
        assert rh.render_text(text, claude) != rh.render_text(text, codex), src
        checked += 1
    assert checked > 0, "no source uses a harness block — the block mechanism is untested"


def test_token_substitution_differs_between_harnesses():
    """Sources carrying an invoke or spawn token must render differently per
    harness — proves substitution ran rather than copying bytes through."""
    claude = harness.HARNESSES[harness.CLAUDE_CODE]
    codex = harness.HARNESSES[harness.CODEX]
    src = rh.SKILLS_SRC / "vibe-cognition" / "SKILL.md"
    text = src.read_text(encoding="utf-8")
    assert "{{invoke:" in text
    a, b = rh.render_text(text, claude), rh.render_text(text, codex)
    assert a != b and "/vibe-curate" in a and "$vibe-document" in b
    assert "$vibe-curate" not in b


def test_sources_have_no_harness_literals_outside_blocks():
    offenders = []
    for src in _sources():
        text = _BLOCK.sub("", src.read_text(encoding="utf-8"))
        for lit in _FORBIDDEN:
            if lit in text:
                offenders.append(f"{src.relative_to(_REPO)}: {lit!r}")
        for m in _FORBIDDEN_WORDS.finditer(text):
            offenders.append(f"{src.relative_to(_REPO)}: {m.group(0)!r}")
    assert not offenders, "\n".join(offenders)


def test_no_orphan_skill_or_agent_outputs():
    src_skills = {p.parent.name for p in rh.SKILLS_SRC.glob("*/SKILL.md")}
    for out_dir in (_REPO / "skills", _REPO / "adapters" / "codex" / "skills"):
        for d in out_dir.iterdir():
            if d.is_dir():
                assert d.name in src_skills, f"orphan output {d}"
    src_agents = {p.name for p in rh.AGENTS_SRC.glob("*.md")}
    for f in (_REPO / "agents").glob("*.md"):
        assert f.name in src_agents, f"orphan output {f}"


def test_codex_skill_descriptions_fit_the_skill_list_budget():
    total = 0
    for name in rh.CODEX_SKILLS:
        text = (_REPO / "adapters" / "codex" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        m = re.search(r"^description:\s*(.*)$", text, re.M)
        assert m, name
        desc = m.group(1)
        assert len(desc) <= 1024, (name, len(desc))
        total += len(desc)
    assert total <= 8000


def test_codex_renders_use_codex_invocation():
    for name in rh.CODEX_SKILLS:
        text = (_REPO / "adapters" / "codex" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert "/vibe-" not in text, name
        assert "Claude Code" not in text or name == "vibe-cognition", name


@pytest.mark.parametrize("tier", ["small", "mid"])
def test_model_token_requires_a_default(tier):
    codex = harness.HARNESSES[harness.CODEX]
    if codex.default_models[tier] is None:
        with pytest.raises(rh.RenderError):
            rh.render_text(f"model: {{{{model:{tier}}}}}", codex)


def test_unknown_token_and_nesting_are_errors():
    claude = harness.HARNESSES[harness.CLAUDE_CODE]
    with pytest.raises(rh.RenderError):
        rh.render_text("{{bogus}}", claude)
    with pytest.raises(rh.RenderError):
        rh.render_text("{{harness:codex}}a{{harness:claude-code}}b{{/harness}}{{/harness}}", claude)
    with pytest.raises(rh.RenderError):
        rh.render_text("text {{/harness}}", claude)


def test_title_case_model_token():
    claude = harness.HARNESSES[harness.CLAUDE_CODE]
    assert rh.render_text("{{Model:small}} vs {{model:small}}", claude) == "Haiku vs haiku"


def test_codex_cognition_skill_does_not_instruct_running_curate():
    """The Codex render must not tell the agent to run a skill that is not
    shipped on Codex; it must say curation runs from Claude Code for now."""
    text = (_REPO / "adapters" / "codex" / "skills" / "vibe-cognition" / "SKILL.md").read_text(encoding="utf-8")
    assert "run `$vibe-curate`" not in text and "run the `$vibe-curate`" not in text
    assert "not available on Codex yet" in text
