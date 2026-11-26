"""The package caps OpenBLAS threads before numpy can load (WP-Memory-Lifecycle Stage A)."""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
VAR = "OPENBLAS_NUM_THREADS"

_PRIVATE_BYTES = """
import ctypes
from ctypes import wintypes

class _PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
        (name, ctypes.c_size_t)
        for name in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
            "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
            "PrivateUsage",
        )
    ]

kernel32 = ctypes.WinDLL("kernel32")
psapi = ctypes.WinDLL("psapi")
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
counters = _PMC()
counters.cb = ctypes.sizeof(_PMC)
assert psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
print(counters.PrivateUsage)
"""


def _run(code: str, preset: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k != VAR}
    env["PYTHONPATH"] = str(SRC)
    if preset is not None:
        env[VAR] = preset
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_package_import_caps_openblas_threads():
    assert _run(f"import os, vibe_cognition; print(os.environ.get({VAR!r}))") == "1"


def test_a_preset_value_is_left_alone():
    assert _run(f"import os, vibe_cognition; print(os.environ.get({VAR!r}))", preset="3") == "3"


def test_package_init_imports_only_os_and_does_not_load_numpy():
    tree = ast.parse((SRC / "vibe_cognition" / "__init__.py").read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or ".")
    assert imported == ["os"]
    assert _run("import sys, vibe_cognition; print('numpy' in sys.modules)") == "False"


@pytest.mark.skipif(sys.platform != "win32", reason="private-bytes probe is Win32-only")
@pytest.mark.skipif((os.cpu_count() or 1) < 8, reason="OpenBLAS buffer cost is too small to measure under 8 cores")
def test_numpy_commits_far_less_memory_after_the_package_import():
    uncapped = int(_run("import numpy\n" + _PRIVATE_BYTES))
    capped = int(_run("import vibe_cognition, numpy\n" + _PRIVATE_BYTES))
    assert capped < uncapped / 2, f"capped={capped >> 20} MB uncapped={uncapped >> 20} MB"
