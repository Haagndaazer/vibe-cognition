"""Read-only venv health guard for --no-sync launches (WP-B, decision 9022f7de94e9).

Imported early in server.py -- BEFORE the heavy ``from .embeddings import ...``
line, which eagerly pulls in chromadb at MODULE TOP (``embeddings/__init__.py``
-> ``storage.py``). On a broken/incomplete venv that import would otherwise
crash with a raw, unactionable ImportError before ``server.py``'s ``main()``
is ever reached -- `-m` execution runs the whole module top-to-bottom
(defining ``mcp``, registering tools) before it gets to the
``if __name__ == "__main__"`` guard at the bottom, so a check placed INSIDE
``main()`` (as originally scoped) would be dead code on exactly the failure
path it exists to catch. This module's own import performs the check and
exits early instead, with a clear message.

Never runs `uv sync` or otherwise mutates the venv -- the SessionStart hook
owns healing (600s budget for a first-install sync); N concurrent servers
each trying to self-heal the same shared venv would collide. READ-ONLY: on a
healthy venv (steady state, every existing install) this costs nothing beyond
an import Python was already about to do.

WP-C RECONCILIATION (decision 9022f7de94e9): torch is checked by PRESENCE
ONLY (``importlib.util.find_spec``), never actually imported here. WP-C made
torch/sentence_transformers a LAZY import (moved into
``SentenceTransformersBackend.__init__``, loaded in the background thread
AFTER the handshake) specifically to get its ~9.6s import cost off the
pre-handshake path -- if this guard still did a REAL ``import torch`` at
module load, that win would be completely neutralized (module import would
be just as slow as before WP-C). chromadb, in contrast, stays a REAL import:
``server.py``'s lifespan opens ChromaDB pre-yield regardless (WP-A), so
paying that cost here is free -- there is no path where the guard runs but
the real ChromaDB open doesn't. A torch DLL that's actually broken is instead
caught later, in the background thread, where it hits the EXISTING
``embedding_error`` graceful-degradation path (``embedding_ready`` still
gets set so tools don't hang; ``get_status`` surfaces the error) -- strictly
better than today, not a regression, since torch was never checked at all
before WP-B existed.
"""

import importlib
import importlib.util
import sys

# Actually imported (needed pre-yield in lifespan regardless -- free to check here).
REAL_IMPORT_MODULES = ("chromadb",)
# Presence-checked only -- see WP-C RECONCILIATION above for why torch must
# NOT be actually imported by this guard.
PRESENCE_ONLY_MODULES = ("torch",)


def check(
    real_import_modules: tuple[str, ...] = REAL_IMPORT_MODULES,
    presence_only_modules: tuple[str, ...] = PRESENCE_ONLY_MODULES,
) -> tuple[bool, str]:
    """Pure check: do the required native deps actually import (or, for
    presence-only modules, exist at all)?

    Never mutates anything, and never actually imports a presence-only
    module. Returns ``(True, "")`` on success, or
    ``(False, "<module>: <error>")`` naming the first module that failed.
    """
    for mod in real_import_modules:
        try:
            importlib.import_module(mod)
        except Exception as e:  # noqa: BLE001 - report ANY import failure verbatim
            return False, f"{mod}: {e}"
    for mod in presence_only_modules:
        if importlib.util.find_spec(mod) is None:
            return False, f"{mod}: module not found (presence check only -- never imported here)"
    return True, ""


def check_or_exit() -> None:
    """Act on ``check()``: a no-op on a healthy venv; a clear stderr message
    + ``sys.exit(1)`` on a broken/incomplete one."""
    ready, err = check()
    if ready:
        return
    print(
        "vibe-cognition: the Python environment is broken or incomplete "
        f"(import failed -- {err}). This server was launched with --no-sync, "
        "so it will not attempt to repair the environment itself -- that is "
        "the SessionStart hook's job (600s budget on install/update). Close "
        "ALL Claude Code sessions and reopen one to let the hook heal it, or "
        "delete the plugin's .venv to force a clean rebuild.",
        file=sys.stderr,
    )
    sys.exit(1)


check_or_exit()
