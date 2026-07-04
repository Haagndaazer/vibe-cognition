"""WP-B (decision 9022f7de94e9): .claude-plugin/plugin.json server launch args.

Loads the REAL repo file (not a synthetic fixture) -- this pins the actual
shipped config, mirroring the cross-process journal test's _REPO pattern.
"""

import json
import pathlib

_REPO = pathlib.Path(__file__).resolve().parents[1]
_PLUGIN_JSON = _REPO / ".claude-plugin" / "plugin.json"


def _server_args() -> list[str]:
    data = json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))
    return data["mcpServers"]["vibe-cognition"]["args"]


def test_server_launch_args_include_no_sync():
    """--no-sync stops the server spawn from re-syncing/locking the shared
    venv (H1 contention + H5 cold-start tax) on every launch -- paired,
    per WP-B's mandatory precondition, with the _venv_guard fail-fast so a
    genuinely broken venv still fails clearly instead of launching bare.

    Fails-before: args had no --no-sync, so N concurrent server launches all
    raced `uv run`'s own venv sync/lock step.
    """
    args = _server_args()
    assert "--no-sync" in args


def test_no_sync_comes_after_run_subcommand():
    """--no-sync is a flag on `uv run`, not a bare top-level uv flag -- it
    must appear after "run" in the args list for uv to parse it correctly."""
    args = _server_args()
    assert args[0] == "run"
    assert args.index("--no-sync") > args.index("run")


def test_server_still_launched_via_python_dash_m_module():
    """Zero regression: the actual module invocation shape is unchanged --
    only a flag was added, not the launch mechanism."""
    args = _server_args()
    assert args[-3:] == ["python", "-m", "vibe_cognition.server"]
