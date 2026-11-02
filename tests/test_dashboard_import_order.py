"""The standalone dashboard CLI must import whichever module loads first (task bcce7f606c08)."""

import subprocess
import sys

import pytest

FIRST_IMPORTS = [
    "vibe_cognition.dashboard.cli",
    "vibe_cognition.dashboard.server",
    "vibe_cognition.dashboard.api",
    "vibe_cognition.dashboard",
    "vibe_cognition.tools",
    "vibe_cognition.tools.dashboard_tool",
]


@pytest.mark.parametrize("module", FIRST_IMPORTS)
def test_module_imports_first_in_a_fresh_interpreter(module):
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_dashboard_cli_help_runs():
    proc = subprocess.run(
        [sys.executable, "-m", "vibe_cognition.dashboard.cli", "--help"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "vibe-cognition-dashboard" in proc.stdout
