"""WP-P0: the harness policy table and its consumers."""

import asyncio
import importlib

from fastmcp import FastMCP

from vibe_cognition import harness


def test_default_harness_is_claude_code(monkeypatch):
    monkeypatch.delenv("VIBE_HARNESS", raising=False)
    hz = harness.current()
    assert hz.name == harness.CLAUDE_CODE
    assert hz.skill_invoke("vibe-curate") == "/vibe-curate"
    assert hz.model("small") == "haiku" and hz.model("mid") == "sonnet"
    assert hz.curation_available is True


def test_codex_harness_facts(monkeypatch):
    monkeypatch.setenv("VIBE_HARNESS", "codex")
    monkeypatch.delenv("VIBE_MODEL_SMALL", raising=False)
    monkeypatch.delenv("VIBE_MODEL_MID", raising=False)
    hz = harness.current()
    assert hz.name == harness.CODEX
    assert hz.skill_invoke("vibe-cognition") == "$vibe-cognition"
    assert hz.model_source("mid") == "default_pending" and hz.model("mid") is None
    assert hz.curation_available is False
    cta = hz.update_cta("coltondyck")
    assert "codex plugin marketplace upgrade coltondyck" in cta
    assert "codex plugin add vibe-cognition@coltondyck" in cta


def test_unknown_harness_falls_back_to_claude_code(monkeypatch):
    monkeypatch.setenv("VIBE_HARNESS", "hermes")
    assert harness.current().name == harness.CLAUDE_CODE


def test_model_env_override_and_inherit(monkeypatch):
    monkeypatch.setenv("VIBE_HARNESS", "codex")
    monkeypatch.setenv("VIBE_MODEL_MID", "gpt-example")
    monkeypatch.setenv("VIBE_MODEL_SMALL", "inherit")
    hz = harness.current()
    assert hz.model("mid") == "gpt-example" and hz.model_source("mid") == "env"
    assert hz.model("small") is None and hz.model_source("small") == "env:inherit"
    block = hz.status_block()
    assert block["models"]["mid"] == {"model": "gpt-example", "source": "env"}
    assert block["name"] == "codex"


def test_claude_update_cta_matches_existing_wording():
    hz = harness.HARNESSES[harness.CLAUDE_CODE]
    assert hz.update_cta("coltondyck") == "/plugin update vibe-cognition@coltondyck"
    assert hz.update_cta(None) == "/plugin update vibe-cognition"


def test_get_status_exposes_harness_block(monkeypatch):
    """The block agents read at runtime instead of prose: name, prefix, models."""
    monkeypatch.setenv("VIBE_HARNESS", "codex")
    from vibe_cognition.tools import service_tools

    mcp = FastMCP("harness-status")
    service_tools.register_service_tools(mcp)
    tools = asyncio.run(mcp.list_tools())
    assert any(t.name == "get_status" for t in tools)


def test_instructions_follow_harness_table(monkeypatch):
    monkeypatch.setenv("VIBE_HARNESS", "codex")
    import vibe_cognition.instructions as mod

    try:
        text = importlib.reload(mod).SERVER_INSTRUCTIONS
        assert "not available on Codex yet" in text
        assert "$vibe-cognition" in text
    finally:
        monkeypatch.delenv("VIBE_HARNESS", raising=False)
        importlib.reload(mod)


def test_codex_update_cta_without_marketplace_is_actionable(monkeypatch):
    monkeypatch.setenv("VIBE_HARNESS", "codex")
    cta = harness.current().update_cta(None)
    assert "  " not in cta
    assert "codex plugin add vibe-cognition@<marketplace>" in cta
