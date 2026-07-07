"""WP-Sidecar (P0 endgame) §S-c: runtime sys.modules assertion.

The AST guard (tests/test_wp_wedge.py) catches the heavy chain
(torch/scipy/sentence_transformers/transformers/sklearn) being imported
ANYWHERE in the server process's source at authoring time. This is the
runtime analog: even if some future dependency transitively pulls one of
these names in behind our backs (a third-party library upgrade, not a line
of our own code), this catches it actually having happened, in the live
process, at the two moments that matter most -- the MCP handshake and the
first tool call.

Never raises: a violation here means the wedge-source invariant broke, but
crashing the server over an import that already happened accomplishes
nothing (the cost is already paid) -- log loudly instead, so it's visible in
fleet logs/CI, and let the server keep serving.
"""

from __future__ import annotations

import re
import sys

_HEAVY_MODULE_RE = re.compile(r"^(torch|scipy|sentence_transformers|transformers|sklearn)(\.|$)")


def find_heavy_modules_in_sys_modules() -> list[str]:
    """Return the sorted list of sys.modules keys matching the heavy chain,
    or an empty list if none are present."""
    return sorted(name for name in sys.modules if _HEAVY_MODULE_RE.match(name))


def check_and_log(moment: str) -> None:
    """Check sys.modules for the heavy chain and log loudly (never raise) if
    found. `moment` is a short label (e.g. "handshake", "first_tool_call")
    identifying WHEN this fired, for the log line."""
    found = find_heavy_modules_in_sys_modules()
    if found:
        print(
            f"[vibe-cognition] INVARIANT VIOLATION at {moment}: heavy embedding-chain "
            f"module(s) present in the SERVER process's sys.modules: {found} -- "
            "this should be structurally impossible after WP-Sidecar (the chain "
            "should only ever load in the sidecar subprocess). Not fatal, but "
            "the wedge-source invariant this WP exists to guarantee has broken.",
            file=sys.stderr,
            flush=True,
        )
