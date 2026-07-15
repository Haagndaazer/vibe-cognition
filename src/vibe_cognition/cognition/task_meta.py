"""Task-transition-log helpers shared across cognition_tools, dashboard, and prime.

WP-TC16: relocated out of tools/cognition_tools.py so prime.py (stdlib + light
cognition-package imports only, never tools/cognition_tools -- that pulls in
chroma/embeddings) can compute claim age for the manager-rollup section without
violating prime's light-import constraint. cognition_tools.py re-exports the name
so its existing internal callers and tests/test_task.py's direct import both keep
working unchanged.
"""

from typing import Any


def _task_claimed_at(transitions: list[dict[str, Any]]) -> str | None:
    """The `at` of the LATEST ->in_progress transition (mirrors claimed_by's semantics
    in _update_task: a takeover re-stamps both together). Null when no such entry
    exists (legacy). Single implementation shared by _update_task, the dashboard,
    and prime's manager rollup (WP-TC4 design 5 / WP-TC16 relocation)."""
    claimed_at = None
    for tr in transitions:
        if tr.get("status") == "in_progress":
            claimed_at = tr.get("at")
    return claimed_at
