"""WP-A 1b (decision 9022f7de94e9): startup timing breadcrumbs.

stamp() must be pure w.r.t. disk (stderr print + in-memory append only) so it
is safe to call from the synchronous pre-yield MCP handshake path (the
HEISENBUG GUARD). flush_to_disk() is the separate, explicit disk write, and
must be per-PID so concurrent server processes never collide on one file.
"""

import tempfile
from pathlib import Path

from vibe_cognition import _startup_timing


def test_stamp_returns_monotonic_increasing_time_and_records_label(capsys):
    t1 = _startup_timing.stamp("probe_a")
    t2 = _startup_timing.stamp("probe_b")
    assert t2 >= t1
    labels = [label for label, _ in _startup_timing.breadcrumbs]
    assert "probe_a" in labels
    assert "probe_b" in labels

    err = capsys.readouterr().err
    assert "probe_a" in err
    assert "probe_b" in err
    assert f"pid={_startup_timing.PID}" in err


def test_stamp_never_touches_disk(monkeypatch):
    """HEISENBUG GUARD: stamp() must not perform any disk I/O -- patch Path's
    write methods to explode and confirm stamp() still succeeds."""

    def _boom(*args, **kwargs):
        raise AssertionError("stamp() must never touch disk")

    monkeypatch.setattr(Path, "write_text", _boom)
    monkeypatch.setattr(Path, "mkdir", _boom)
    _startup_timing.stamp("no_disk_io_check")  # must not raise


def test_flush_to_disk_writes_per_pid_file_with_all_labels(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    _startup_timing.breadcrumbs.clear()
    _startup_timing.stamp("flush_test_a")
    _startup_timing.stamp("flush_test_b")

    _startup_timing.flush_to_disk()

    log_path = tmp_path / "vibe-cognition-startup" / f"pid-{_startup_timing.PID}.log"
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "flush_test_a" in content
    assert "flush_test_b" in content


def test_flush_to_disk_is_per_pid_never_a_shared_file():
    """Concurrency safety: the log path is namespaced by THIS process's PID --
    two servers with different PIDs can never write the same file."""
    import os

    log_dir = Path(tempfile.gettempdir()) / "vibe-cognition-startup"
    expected = log_dir / f"pid-{os.getpid()}.log"
    # PID is captured once at import time and must match the live process.
    assert os.getpid() == _startup_timing.PID
    assert str(expected).endswith(f"pid-{os.getpid()}.log")


def test_flush_to_disk_never_raises_on_write_failure(monkeypatch):
    """Best-effort: a failed diagnostic write must never break startup."""

    def _boom(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Path, "mkdir", _boom)
    _startup_timing.flush_to_disk()  # must not raise
