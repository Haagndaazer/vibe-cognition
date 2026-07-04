"""Read-only venv health guard for --no-sync launches (WP-B, decision 9022f7de94e9).

Imported early in server.py -- BEFORE the heavy ``from .embeddings import ...``
line, which eagerly pulls in chromadb and sentence-transformers/torch at
MODULE TOP (``embeddings/__init__.py`` -> ``generator.py`` / ``storage.py``).
On a broken/incomplete venv that import would otherwise crash with a raw,
unactionable ImportError before ``server.py``'s ``main()`` is ever reached --
`-m` execution runs the whole module top-to-bottom (defining ``mcp``,
registering tools) before it gets to the ``if __name__ == "__main__"`` guard
at the bottom, so a check placed INSIDE ``main()`` (as originally scoped)
would be dead code on exactly the failure path it exists to catch. This
module's own import performs the check and exits early instead, with a clear
message.

Never runs `uv sync` or otherwise mutates the venv -- the SessionStart hook
owns healing (600s budget for a first-install sync); N concurrent servers
each trying to self-heal the same shared venv would collide. READ-ONLY: on a
healthy venv (steady state, every existing install) this costs nothing beyond
an import Python was already about to do.
"""

import importlib
import sys

REQUIRED_MODULES = ("chromadb", "torch")


def check(modules: tuple[str, ...] = REQUIRED_MODULES) -> tuple[bool, str]:
    """Pure check: do the heavy native deps actually import?

    Never mutates anything. Returns ``(True, "")`` on success, or
    ``(False, "<module>: <error>")`` naming the first module that failed.
    """
    for mod in modules:
        try:
            importlib.import_module(mod)
        except Exception as e:  # noqa: BLE001 - report ANY import failure verbatim
            return False, f"{mod}: {e}"
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
