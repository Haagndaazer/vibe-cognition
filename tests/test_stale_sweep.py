"""The machine-wide stale-sibling sweep is LOG-ONLY telemetry. These tests
pin the classification semantics (pure function, no processes) and smoke the
real enumeration path read-only."""

from __future__ import annotations

import sys

import pytest

from vibe_cognition import stale_sweep

_UV_PYTHON = r"C:\Users\x\AppData\Roaming\uv\python\cpython-3.12.11-windows\python.exe"
_STORE_PYTHON = r"C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.13_x\python3.13.exe"
_LAUNCHER = r"C:\Users\x\.claude\plugins\data\vibe-cognition-coltondyck\.venv\Scripts\python.exe"
_SERVER = "python -m vibe_cognition.server"
_SIDECAR = "python -m vibe_cognition.embeddings.sidecar"
_NOW = 1_800_000_000.0


def _filetime(unix_seconds: float) -> int:
    return int((unix_seconds + stale_sweep._FILETIME_EPOCH_DIFF_SECONDS) * 1e7)


def _row(pid, parent_pid, image, cmdline, age=100.0):
    return {
        "pid": pid,
        "parent_pid": parent_pid,
        "image_path": image,
        "cmdline": cmdline,
        "created_filetime": _filetime(_NOW - age),
    }


@pytest.mark.parametrize("interpreter", [_UV_PYTHON, _STORE_PYTHON, r"C:\Python312\python.exe"])
def test_a_tree_counts_once_at_its_real_interpreter_wherever_python_lives(interpreter):
    """launcher -> interpreter -> sidecar launcher -> sidecar: one server,
    reported at the interpreter's pid. uv.exe is never a row."""
    rows = [
        _row(11, 5, _LAUNCHER, _SERVER, age=100.0),
        _row(10, 11, interpreter, _SERVER, age=99.9),
        _row(13, 10, _LAUNCHER, _SIDECAR, age=95.0),
        _row(12, 13, interpreter, _SIDECAR, age=94.9),
    ]
    result = stale_sweep.classify_rows(rows, now=_NOW)
    assert result["server_count"] == 1
    assert result["server_pids"] == [10]
    assert result["unverified_interpreter_count"] == 0
    assert result["method"] == "cmdline"


def test_thirteen_store_python_trees_count_thirteen():
    rows = []
    for n in range(13):
        launcher, real = 1000 + 2 * n, 1001 + 2 * n
        rows.append(_row(launcher, 50 + n, _LAUNCHER, _SERVER))
        rows.append(_row(real, launcher, _STORE_PYTHON, _SERVER))
    result = stale_sweep.classify_rows(rows, now=_NOW)
    assert result["server_count"] == 13
    assert result["server_pids"] == [1001 + 2 * n for n in range(13)]


def test_a_server_with_no_launcher_counts_once():
    rows = [_row(10, 5, _UV_PYTHON, _SERVER)]
    assert stale_sweep.classify_rows(rows, now=_NOW)["server_pids"] == [10]


def test_a_reused_parent_pid_does_not_hide_a_server():
    """Row 20's parent pid equals row 10's pid, but 20 was created first, so
    10 cannot be its parent: both are leaves."""
    rows = [
        _row(10, 5, _UV_PYTHON, _SERVER, age=50.0),
        _row(20, 10, _UV_PYTHON, _SERVER, age=500.0),
    ]
    assert stale_sweep.classify_rows(rows, now=_NOW)["server_pids"] == [10, 20]


def test_unreadable_cmdline_is_reported_separately_never_folded():
    rows = [
        _row(20, 5, _UV_PYTHON, None, age=50.0),
        _row(21, 6, _UV_PYTHON, _SERVER, age=500.0),
    ]
    result = stale_sweep.classify_rows(rows, now=_NOW)
    assert result["server_count"] == 1
    assert result["unverified_interpreter_count"] == 1
    assert result["method"] == "cmdline+image-only-degraded"


def test_oldest_age_comes_from_counted_servers():
    rows = [
        _row(30, 5, _UV_PYTHON, _SERVER, age=3600.0),
        _row(31, 6, _UV_PYTHON, _SERVER, age=60.0),
    ]
    result = stale_sweep.classify_rows(rows, now=_NOW)
    assert result["oldest_server_age_seconds"] == pytest.approx(3600, abs=1)


def test_empty_rows():
    result = stale_sweep.classify_rows([], now=_NOW)
    assert result["server_count"] == 0
    assert result["oldest_server_age_seconds"] is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [("python.exe", True), ("pythonw.exe", True), ("Python3.13.exe", True), ("uv.exe", False), ("py.exe", False)],
)
def test_python_image_names(name, expected):
    assert stale_sweep.is_python_image(name) is expected


@pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp enumeration is Windows-only")
def test_run_stale_sweep_smoke_read_only():
    """Real enumeration, read-only: returns a labeled dict, never errors. No
    count is asserted (the machine's session state is not this test's
    business)."""
    result = stale_sweep.run_stale_sweep()
    assert "error" not in result
    assert "machine-wide" in result["scope"]
    assert result["server_count"] >= 0
    assert isinstance(result["server_pids"], list)


@pytest.mark.parametrize(
    "cmdline",
    [
        'python -c "print(1)  # vibe_cognition.server"',
        "python -m vibe_cognition.server_tools",
        "python -m vibe_cognition.embeddings.sidecar",
        "python tests/about_vibe_cognition.server.py",
    ],
)
def test_only_the_module_launch_form_is_a_server(cmdline):
    assert stale_sweep.classify_rows([_row(10, 5, _UV_PYTHON, cmdline)], now=_NOW)["server_count"] == 0


def test_a_quoted_launcher_path_still_counts():
    cmdline = f'"{_LAUNCHER}" -m vibe_cognition.server'
    assert stale_sweep.classify_rows([_row(10, 5, _LAUNCHER, cmdline)], now=_NOW)["server_count"] == 1
