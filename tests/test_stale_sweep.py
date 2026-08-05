"""WP-Lifecycle-2 Stage 1 (F4): the machine-wide stale-sibling sweep is
LOG-ONLY telemetry. These tests pin the classification semantics (pure
function, no processes) and smoke the real enumeration path read-only."""

from __future__ import annotations

import sys

import pytest

from vibe_cognition import stale_sweep

_UV_PYTHON = r"C:\Users\x\AppData\Roaming\uv\python\cpython-3.12.11-windows\python.exe"
_TRAMPOLINE = r"C:\Users\x\.claude\plugins\data\vibe-cognition-coltondyck\.venv\Scripts\python.exe"
_NOW = 1_800_000_000.0


def _filetime(unix_seconds: float) -> int:
    return int((unix_seconds + stale_sweep._FILETIME_EPOCH_DIFF_SECONDS) * 1e7)


def test_classify_counts_only_real_interpreters_with_server_cmdline():
    """The dedup pin: a tree is counted ONCE, at its real interpreter — the
    trampoline (same cmdline, venv Scripts path) must not count, or every
    tree triple-counts via uv + trampoline + real."""
    rows = [
        {"pid": 10, "image_path": _UV_PYTHON, "cmdline": "python -m vibe_cognition.server", "created_filetime": _filetime(_NOW - 100)},
        {"pid": 11, "image_path": _TRAMPOLINE, "cmdline": "python -m vibe_cognition.server", "created_filetime": _filetime(_NOW - 100)},
        {"pid": 12, "image_path": _UV_PYTHON, "cmdline": "python -m vibe_cognition.embeddings.sidecar", "created_filetime": _filetime(_NOW - 100)},
        {"pid": 13, "image_path": r"C:\Python312\python.exe", "cmdline": "python -m vibe_cognition.server", "created_filetime": _filetime(_NOW - 100)},
    ]
    result = stale_sweep.classify_rows(rows, now=_NOW)
    assert result["server_count"] == 1
    assert result["server_pids"] == [10]
    assert result["unverified_interpreter_count"] == 0
    assert result["method"] == "cmdline"


def test_classify_unreadable_cmdline_degrades_labeled_never_folded():
    """A uv-managed python whose cmdline can't be read is reported as
    UNVERIFIED (possible over-count, labeled) — never silently added to the
    server count and never dropped."""
    rows = [
        {"pid": 20, "image_path": _UV_PYTHON, "cmdline": None, "created_filetime": _filetime(_NOW - 50)},
        {"pid": 21, "image_path": _UV_PYTHON, "cmdline": "python -m vibe_cognition.server", "created_filetime": _filetime(_NOW - 500)},
    ]
    result = stale_sweep.classify_rows(rows, now=_NOW)
    assert result["server_count"] == 1
    assert result["unverified_interpreter_count"] == 1
    assert result["method"] == "cmdline+image-only-degraded"


def test_classify_oldest_age_from_verified_servers():
    rows = [
        {"pid": 30, "image_path": _UV_PYTHON, "cmdline": "x vibe_cognition.server", "created_filetime": _filetime(_NOW - 3600)},
        {"pid": 31, "image_path": _UV_PYTHON, "cmdline": "x vibe_cognition.server", "created_filetime": _filetime(_NOW - 60)},
    ]
    result = stale_sweep.classify_rows(rows, now=_NOW)
    assert result["oldest_server_age_seconds"] == pytest.approx(3600, abs=1)


def test_classify_empty_rows():
    result = stale_sweep.classify_rows([], now=_NOW)
    assert result["server_count"] == 0
    assert result["oldest_server_age_seconds"] is None


@pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp enumeration is Windows-only")
def test_run_stale_sweep_smoke_read_only():
    """Real enumeration, read-only: returns a labeled dict, never errors.
    Running under pytest there is at least our own python — but no count is
    asserted (the machine's session state is not this test's business)."""
    result = stale_sweep.run_stale_sweep()
    assert "error" not in result
    assert "machine-wide" in result["scope"]
    assert result["server_count"] >= 0
    assert isinstance(result["server_pids"], list)
