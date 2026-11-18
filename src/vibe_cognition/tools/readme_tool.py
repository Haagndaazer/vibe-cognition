"""MCP tool: serve the vibe-cognition orientation guide."""

from __future__ import annotations

import os
from typing import Any

from fastmcp import Context

from .. import harness, running_version, whats_new
from ..cognition.readme import COGNITION_GETTING_STARTED, COGNITION_GUIDE
from .dispatch import dispatch_tool


def cognition_readme_core() -> dict[str, str]:
    """Pure core for cognition_readme (no MCP context required)."""
    return {"guide": COGNITION_GUIDE, "getting_started": COGNITION_GETTING_STARTED}


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def confirm_changes_seen_core() -> dict[str, Any]:
    """Pure core for cognition_readme(changes_seen=true)."""
    if harness.current().name != harness.CLAUDE_CODE:
        return {"recorded": False, "message": whats_new.NOTHING_TO_CONFIRM}
    recorded, message = whats_new.confirm(
        _first_env("VIBE_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"),
        _first_env("VIBE_DATA_DIR", "CLAUDE_PLUGIN_DATA"),
        running_version.code_version(),
    )
    return {"recorded": recorded, "message": message}


def register_readme_tool(mcp) -> None:
    """Register the cognition_readme tool with the MCP server."""

    @dispatch_tool(mcp)
    def cognition_readme(ctx: Context, changes_seen: bool = False) -> dict[str, Any]:
        """Return the vibe-cognition orientation guide and getting-started procedure.

        Pull surface complementing the always-pushed instructions.py standing
        practices. Call this when dropped into an unfamiliar project, when the
        graph is empty and you need to explain vibe-cognition to the user, or
        any time you want a structured reference for the tool groups, node/edge
        types, and when-to-record triggers.

        Second use: pass changes_seen=true, ONLY after you have told the user
        the "what's new" note shown at session start after a plugin update.
        That note repeats at every session start (at most 5 times) until this
        call records that the user was told. It then returns the confirmation
        shape below, not the guide.

        No `project` arg: this tool serves orientation for THIS project's server.
        For a loaded foreign project, guidance comes from that project's own
        vibe-cognition server.

        Args:
            ctx: MCP context (injected automatically).
            changes_seen: Default false (return the guide). true = the user has
                been told the session-start what's-new note; stop showing it.
                Writes only a per-machine marker in the plugin's data folder,
                never the graph.

        Returns:
            Default (changes_seen false):
            {
              guide: Full markdown orientation -- what vibe-cognition is, the
                     record->curate core loop, all tool groups, node and edge
                     types, when-to-record triggers, and cross-project usage.
              getting_started: Short act-now procedure for a project with no
                               cognition history yet (safe to call on any project).
            }
            With changes_seen=true:
            {
              recorded: bool -- true when the note for the installed version is
                        now marked as told (also true if it already was).
              message: One line to act on. When recorded is false it says why:
                       "Nothing to confirm" (no plugin install to track, or not
                       Claude Code -- nothing to do); "Not recorded: this server
                       is running vX but the installed plugin is vY" (this
                       session's server predates the update -- reconnect or
                       restart, then confirm again); "Not recorded: vX is
                       already confirmed" (a newer version was confirmed
                       elsewhere -- restart); or "Could not record" (the marker
                       file could not be written -- the note may show again).
            }
            Never raises and never returns an "error" key.
        """
        if changes_seen:
            return confirm_changes_seen_core()
        return cognition_readme_core()
