"""Standing server instructions + a SessionStart re-injection entry point.

The MCP server surfaces ``SERVER_INSTRUCTIONS`` to the agent every session via the
FastMCP ``instructions`` field (the MCP ``initialize`` handshake). It is undocumented
whether those survive a context compaction, so the plugin ALSO re-injects them after a
compact via a ``SessionStart`` hook (matcher ``compact``) that runs this module's
``main()`` and emits the text as ``additionalContext``.

Kept deliberately STDLIB-ONLY (``json``/``os``/``sys``) and ASCII-ONLY so
``python -m vibe_cognition.instructions`` is fast (no torch/chromadb import) and safe on
Windows stdout. Mirrors the import profile of ``migrate_mcp`` / ``prime``.
"""

import json
import sys

# Surfaced to the agent every session as "MCP Server Instructions" (server.py passes
# this to FastMCP) AND re-injected after a compact (see main()). ASCII-only on purpose.
SERVER_INSTRUCTIONS = (
    "Vibe Cognition maintains this project's knowledge graph: the durable, "
    "cross-session record of decisions, failures, discoveries, constraints, "
    "patterns, and reasoning. Two standing practices keep it valuable for "
    "non-trivial work:\n"
    "\n"
    "1. CHECK HISTORY FIRST. Before starting a new task or writing a plan, "
    "search the graph (cognition_search, cognition_get_history) so past "
    "decisions and known failures are respected and not re-litigated.\n"
    "\n"
    "2. RECORD AS YOU WORK. Capture cognitive history with cognition_record as "
    "it happens: decisions (with rejected alternatives), failures, non-obvious "
    "discoveries, constraints, reusable patterns, and an episode when a unit of "
    "work completes. Include references (issue/PR/commit) so nodes link to "
    "their episode.\n"
    "\n"
    "After recording, run the /vibe-curate skill to add semantic edges; only "
    "deterministic part_of edges (from shared references) are automatic. For "
    "full guidance, use the /vibe-cognition skill."
)

# Header so the re-injected (post-compact) block is self-explaining when it sits next to
# any MCP instructions that may have survived the compaction.
_REINJECT_HEADER = "# Vibe Cognition - Standing Practices (re-injected after compaction)"


def main() -> None:
    """Emit the standing instructions as SessionStart ``additionalContext`` JSON.

    Invoked by the ``compact``-matched SessionStart hook. Always emits (the matcher
    already gates this to post-compaction), so the rules are re-armed even on a
    project with no ``.cognition/`` data yet.
    """
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": f"{_REINJECT_HEADER}\n\n{SERVER_INSTRUCTIONS}",
        }
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
