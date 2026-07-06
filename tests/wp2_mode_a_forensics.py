"""§W2-a forensics repro (docs/wp-wedge2-plan.md rev 4). WP2-AC2(ii): regression
coverage for the plain-import-lock CLASS -- NOT a reproduction of Incident A.

The §W2-a forensics run (2026-07-06, ratified by Vince, rev 4 outcome note)
was NEGATIVE: this exact repro could not hang any of the four known
function-body-import sites, and the real Incident-A mechanism is very likely
the Windows OS loader-lock class, which cannot be reproduced from pure Python
(same limitation as mode (b)). What THIS script guards going forward is
narrower and still real: that no *future* function-body import added to the
tool surface collides with CPython's own per-module import lock while a
background thread is mid-import of something else. If someone reintroduces a
colliding import, this script starts failing again.

NOT collected directly by pytest (no test_ prefix) -- it is a subprocess-
isolated script, run via `uv run python tests/wp2_mode_a_forensics.py <tmp_dir>`
(driven by test_wp_wedge2.py's wrapper test) because it needs a fresh process
with scipy.interpolate._fitpack not-yet-imported. Blocks that REAL module at
exec_module level via a meta_path finder wrapping the real loader -- not a
synthetic name -- from a background thread that calls the REAL
EmbeddingGenerator.from_config(sentence-transformers backend), mirroring
_load_embeddings_and_sync. Meanwhile drives REAL FastMCP dispatch (fastmcp.Client
in-memory transport, not mock_mcp) against a representative tool set during the
load window and reports, per tool, whether it returned within the bound or hung --
and if it hung, every thread's live stack (the in-process equivalent of a py-spy
capture) so the blocking site is directly readable, not inferred. Exits non-zero
if any tool call failed to return within BOUND_SECONDS.
"""

import asyncio
import importlib.abc
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

BLOCK_TARGET = "scipy.interpolate._fitpack"
BOUND_SECONDS = 10.0
BG_HIT_TIMEOUT = 60.0

if len(sys.argv) < 2:
    print("usage: wp2_mode_a_forensics.py <repo_tmp_dir>", file=sys.stderr)
    sys.exit(2)

repo_path = Path(sys.argv[1])
repo_path.mkdir(parents=True, exist_ok=True)
os.environ["REPO_PATH"] = str(repo_path)
os.environ["EMBEDDING_BACKEND"] = "sentence-transformers"

gate = threading.Event()
hit = threading.Event()


class _BlockingLoader(importlib.abc.Loader):
    """Wraps the REAL loader; blocks inside exec_module (create_module already ran
    by the time exec_module is invoked) exactly where Incident B's dump showed the
    bg thread stuck -- not a synthetic hook, the genuine loader for a genuine module."""

    def __init__(self, real_loader):
        self._real = real_loader

    def create_module(self, spec):
        return self._real.create_module(spec)

    def exec_module(self, module):
        hit.set()
        gate.wait(timeout=BG_HIT_TIMEOUT)
        self._real.exec_module(module)


class _BlockingFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != BLOCK_TARGET:
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            find = getattr(finder, "find_spec", None)
            if find is None:
                continue
            spec = find(fullname, path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = _BlockingLoader(spec.loader)
                return spec
        return None


# Install BEFORE importing vibe_cognition -- BLOCK_TARGET must still be
# not-yet-imported for the hook to have anything to intercept.
assert BLOCK_TARGET not in sys.modules, f"{BLOCK_TARGET} already imported -- repro invalid"
sys.meta_path.insert(0, _BlockingFinder())

from fastmcp import Client  # noqa: E402

from vibe_cognition.server import mcp  # noqa: E402

# A representative tool set covering the four candidate function-body-import
# sites named in docs/wp-wedge2-plan.md §2.3, plus a couple of generic reads:
#   get_status               -> tools/service_tools.py:87  (project_registry import)
#   cognition_search         -> generic gated read
#   cognition_add_task       -> generic gated write
#   cognition_get_history    -> tools/cognition_tools.py:438 (import json as _json)
TOOL_ARGS = {
    "get_status": {},
    "cognition_search": {"query": "test"},
    "cognition_add_task": {"summary": "s", "detail": "d", "context": "c"},
    "cognition_get_history": {},
}


async def _call_one(client, name, kwargs):
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(client.call_tool(name, kwargs), timeout=BOUND_SECONDS)
        return {"tool": name, "ok": True, "elapsed": round(time.monotonic() - t0, 3)}
    except TimeoutError:
        stacks = {
            tid: "".join(traceback.format_stack(frame))
            for tid, frame in sys._current_frames().items()
        }
        thread_names = {t.ident: t.name for t in threading.enumerate()}
        return {
            "tool": name,
            "ok": False,
            "elapsed": round(time.monotonic() - t0, 3),
            "stacks": {thread_names.get(tid, str(tid)): s for tid, s in stacks.items()},
        }


async def main():
    async with Client(mcp) as client:
        assert hit.wait(timeout=30), "bg thread never reached the blocked import -- repro invalid"
        ctx = mcp._lifespan_result
        assert not ctx["embedding_ready"].is_set(), "embeddings already ready -- block didn't hold"

        results = await asyncio.gather(
            *(_call_one(client, name, kwargs) for name, kwargs in TOOL_ARGS.items())
        )
    gate.set()  # release the bg thread's blocked exec_module so lifespan can clean up

    print(json.dumps(list(results), indent=2, default=str))
    if not all(r["ok"] for r in results):
        sys.exit(1)


asyncio.run(main())
