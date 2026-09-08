"""Harness policy table: every fact that differs between the agent harnesses
this plugin runs under, resolved once from VIBE_HARNESS."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

CLAUDE_CODE = "claude-code"
CODEX = "codex"
PLUGIN_NAME = "vibe-cognition"

_MODEL_ENV = {"small": "VIBE_MODEL_SMALL", "mid": "VIBE_MODEL_MID"}
INHERIT = "inherit"


@dataclass(frozen=True)
class Harness:
    name: str
    display_name: str
    skill_prefix: str
    spawn_tool: str
    spawn_background_hint: str
    spawn_depth_note: str
    plugin_root_var: str
    plugin_data_var: str
    default_models: dict[str, str | None]
    containment: str
    update_source: str
    update_cta_template: str
    update_cta_no_market_template: str
    curation_available: bool

    def skill_invoke(self, skill: str) -> str:
        return f"{self.skill_prefix}{skill}"

    def model(self, tier: str) -> str | None:
        env_name = _MODEL_ENV[tier]
        override = os.environ.get(env_name, "").strip()
        if override.lower() == INHERIT:
            return None
        if override:
            return override
        return self.default_models[tier]

    def model_source(self, tier: str) -> str:
        override = os.environ.get(_MODEL_ENV[tier], "").strip()
        if override.lower() == INHERIT:
            return "env:inherit"
        if override:
            return "env"
        return "default" if self.default_models[tier] else "default_pending"

    def update_cta(self, marketplace: str | None) -> str:
        market = (marketplace or "").strip()
        if not market:
            return self.update_cta_no_market_template.format(plugin=PLUGIN_NAME)
        return self.update_cta_template.format(plugin=PLUGIN_NAME, market=market, suffix=f"@{market}")

    def status_block(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "skill_prefix": self.skill_prefix,
            "spawn_tool": self.spawn_tool,
            "curation_available": self.curation_available,
            "containment": self.containment,
            "models": {
                tier: {"model": self.model(tier), "source": self.model_source(tier)}
                for tier in ("small", "mid")
            },
        }


HARNESSES: dict[str, Harness] = {
    CLAUDE_CODE: Harness(
        name=CLAUDE_CODE,
        display_name="Claude Code",
        skill_prefix="/",
        spawn_tool="Agent tool",
        spawn_background_hint="pass `run_in_background: true` so the launcher gets the completion notification",
        spawn_depth_note="subagents may spawn their own subagents",
        plugin_root_var="CLAUDE_PLUGIN_ROOT",
        plugin_data_var="CLAUDE_PLUGIN_DATA",
        default_models={"small": "haiku", "mid": "sonnet"},
        containment="subagent tool whitelist plus the curation-session token",
        update_source="claude-marketplace",
        update_cta_template="/plugin update {plugin}{suffix}",
        update_cta_no_market_template="/plugin update {plugin}",
        curation_available=True,
    ),
    CODEX: Harness(
        name=CODEX,
        display_name="Codex",
        skill_prefix="$",
        spawn_tool="`spawn_agent` tool",
        spawn_background_hint="`spawn_agent` returns immediately; do not `wait_agent` in the launcher — `get_status.uncurated` is the ground truth",
        spawn_depth_note="a stock install caps subagent depth at 1; a subagent's own spawn is refused unless the user raises `agents.max_depth`",
        plugin_root_var="PLUGIN_ROOT",
        plugin_data_var="PLUGIN_DATA",
        default_models={"small": None, "mid": None},
        containment="curation-session token only (Codex roles cannot restrict tools)",
        update_source="codex-marketplace",
        update_cta_template="codex plugin marketplace upgrade {market} && codex plugin add {plugin}{suffix} (then restart Codex)",
        update_cta_no_market_template="re-add the plugin from your Codex marketplace: codex plugin marketplace upgrade <marketplace> && codex plugin add {plugin}@<marketplace> (then restart Codex)",
        curation_available=False,
    ),
}


def current() -> Harness:
    name = os.environ.get("VIBE_HARNESS", "").strip().lower() or CLAUDE_CODE
    return HARNESSES.get(name, HARNESSES[CLAUDE_CODE])


def is_codex() -> bool:
    return current().name == CODEX
