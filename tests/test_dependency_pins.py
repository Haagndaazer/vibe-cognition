"""WP-Lifecycle-2 Stage 1 AC-PIN: the mcp<2 / fastmcp<4 pins are load-bearing.

mcp 2.0.0's stdio_server() serves from a private DUPLICATE of stdin and
repoints fd 0 to the null device — that silently blinds BOTH lifecycle.py
watches that read the process stdin handle (arm_stdin_watch's PeekNamedPipe
poll and resolve_stdin_pipe_peer's pipe-end resolution), because they arm
inside the lifespan, which runs after stdio_server() is entered. Re-validate
both watches before ANY mcp 2.x upgrade, then update these assertions.

Both the pyproject CONSTRAINT and the LOCKED version are asserted: a
lockfile-only check is regenerable away by `uv lock --upgrade`."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _project_dependencies() -> str:
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL)
    assert match, "pyproject.toml [project] dependencies block not found"
    return match.group(1)


def _locked_version(package: str) -> str:
    text = (_ROOT / "uv.lock").read_text(encoding="utf-8")
    match = re.search(
        rf'^name = "{re.escape(package)}"\nversion = "([^"]+)"', text, re.MULTILINE
    )
    assert match, f"{package} not found in uv.lock"
    return match.group(1)


def test_mcp_is_a_direct_dependency_pinned_below_2():
    deps = _project_dependencies()
    assert re.search(r'"mcp<2"', deps), (
        "mcp<2 must be a DIRECT dependency — see this module's docstring for "
        "the fd-repointing hazard it guards against"
    )


def test_fastmcp_upper_bounded_below_4():
    deps = _project_dependencies()
    assert re.search(r'"fastmcp>=[^"]*,<4"', deps), (
        "fastmcp must carry a <4 upper bound — a future fastmcp vendoring its "
        "own stdio transport would bypass the mcp<2 pin entirely"
    )


def test_locked_mcp_is_1x():
    major = int(_locked_version("mcp").split(".")[0])
    assert major == 1


def test_locked_fastmcp_below_4():
    major = int(_locked_version("fastmcp").split(".")[0])
    assert major < 4
