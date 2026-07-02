"""Standing server instructions + a SessionStart re-injection entry point.

The MCP server surfaces ``SERVER_INSTRUCTIONS`` to the agent every session via the
FastMCP ``instructions`` field (the MCP ``initialize`` handshake). It is undocumented
whether those survive a context compaction, so the plugin ALSO re-injects them after a
compact via a ``SessionStart`` hook (matcher ``compact``) that runs this module's
``main()`` and emits the text as ``additionalContext``.

WP-7 (530adc9e6f3f): ``main()`` ALSO regenerates the prime data digest (open
tasks, constraints, patterns, decisions, incidents) via the SAME
``generate_prime()`` the startup hook uses — before this, a compact restored
only the static standing practices, silently dropping the graph's actual
backlog in exactly the long sessions that compact, defeating "the graph IS
the backlog." No new summarization logic; this reuses the existing (already
v0.13.0-trimmed) prime digest unchanged.

That pulls in ``CognitionStorage``/``networkx`` -- imported LAZILY inside
``main()`` (not at module level) so merely importing ``SERVER_INSTRUCTIONS``
elsewhere (server.py, at every session start) never pays it; only an actual
compact invocation does. SERVER_INSTRUCTIONS itself stays plain ASCII, and
``json.dump``'s default ``ensure_ascii=True`` \\u-escapes anything from the
prime digest that isn't, so Windows stdout safety is unaffected either way.
Still excludes the genuinely heavy embeddings stack (torch/chromadb), which
this entry point never needs -- SentenceTransformers backend model loading
lives entirely behind the embedding_generator, not the graph/storage layer.
"""

import json
import sys

from .config import Settings, resolve_repo_path_env

# Surfaced to the agent every session as "MCP Server Instructions" (server.py passes
# this to FastMCP) AND re-injected after a compact (see main()). ASCII-only on purpose.
# THE owning channel for the record->curate loop (WP-7, 9aca47c5803d) — paid most
# consistently (every session AND every compact) of the three places that used to
# restate it, so the other two (readme.py's COGNITION_GUIDE, cognition_readme) now
# point back here instead of duplicating the mechanics. Measured cost: ~370 tokens
# (char-count/4 estimate; re-measure and update this comment if the text changes
# materially — the v0.13.0 prime trim accounting did NOT include this constant, see
# 9aca47c5803d).
SERVER_INSTRUCTIONS = (
    "Vibe Cognition maintains this project's knowledge graph: the durable, "
    "cross-session record of decisions, failures, discoveries, constraints, "
    "patterns, and reasoning. Three standing practices keep it valuable for "
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
    "3. VALIDATE SUGGESTIONS AGAINST HISTORY. Before proposing an improvement "
    "or fix, query the graph (cognition_search, cognition_get_chain) for how "
    "the current state came to be, and distinguish deliberate decisions (whose "
    "reasons may still hold) from genuine oversights.\n"
    "\n"
    "After recording, run the /vibe-curate skill to add semantic edges; only "
    "deterministic part_of edges (from shared references) are automatic. For "
    "full guidance, use the /vibe-cognition skill.\n"
    "\n"
    "Also: check cognition_list_tasks before picking up work (the backlog), and "
    "cognition_get_workflow before starting any multi-step task (existing "
    "procedures) -- both tools mandate this themselves; it is repeated here so "
    "the pushed contract doesn't omit gates the tools declare non-optional."
)

# Header so the re-injected (post-compact) block is self-explaining when it sits next to
# any MCP instructions that may have survived the compaction.
_REINJECT_HEADER = "# Vibe Cognition - Standing Practices (re-injected after compaction)"


def main() -> None:
    """Emit the standing instructions + prime data digest as SessionStart
    ``additionalContext`` JSON.

    Invoked by the ``compact``-matched SessionStart hook. Always emits the
    standing-practices block (the matcher already gates this to post-
    compaction, so the rules are re-armed even on a project with no
    ``.cognition/`` data yet). ALSO appends the prime digest (WP-7,
    530adc9e6f3f) when a non-empty graph is found -- reuses
    ``generate_prime()`` unchanged (the same, already-trimmed digest the
    startup hook produces), no new summarization invented. Best-effort: any
    failure building the digest (missing config, unreadable journal, etc.)
    is swallowed so it can never suppress the standing-practices reinject,
    which must always get through.
    """
    sections = [f"{_REINJECT_HEADER}\n\n{SERVER_INSTRUCTIONS}"]

    try:
        from .cognition.prime import PrimeConfig, generate_prime
        from .cognition.storage import CognitionStorage

        repo_path = resolve_repo_path_env()
        cognition_dir = repo_path / ".cognition"
        if cognition_dir.exists():
            storage = CognitionStorage(cognition_dir)
            if storage.get_statistics()["nodes"] > 0:
                try:
                    settings = Settings()
                    config = PrimeConfig(
                        prime_constraint_limit=settings.prime_constraint_limit,
                        prime_task_cap=settings.prime_task_cap,
                        prime_pattern_limit=settings.prime_pattern_limit,
                        prime_decision_limit=settings.prime_decision_limit,
                        prime_incident_days=settings.prime_incident_days,
                        prime_summary_maxlen=settings.prime_summary_maxlen,
                        prime_incident_min_severity=settings.prime_incident_min_severity,
                    )
                except Exception:  # noqa: BLE001
                    config = PrimeConfig()
                sections.append(generate_prime(storage, config))
    except Exception:  # noqa: BLE001
        pass

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n\n".join(sections),
        }
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    main()
