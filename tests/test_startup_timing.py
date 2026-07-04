"""WP-A 1b (decision 9022f7de94e9): startup timing breadcrumbs.

stamp() must be pure w.r.t. disk (stderr print + in-memory append only) so it
is safe to call from the synchronous pre-yield MCP handshake path (the
HEISENBUG GUARD). flush_to_disk() is the separate, explicit disk write, and
must be per-PID so concurrent server processes never collide on one file.
"""

import tempfile
import time
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


# ── prune_old_logs (Vince's gate note: unbounded temp-dir growth) ────────────


def _make_log(log_dir: Path, pid: int, age_days: float = 0.0) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / f"pid-{pid}.log"
    p.write_text("x", encoding="utf-8")
    if age_days:
        import os
        stamp = time.time() - age_days * 86400
        os.utime(p, (stamp, stamp))
    return p


def test_prune_removes_files_older_than_max_age_days(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    log_dir = tmp_path / "vibe-cognition-startup"
    old = _make_log(log_dir, 111, age_days=10)
    fresh = _make_log(log_dir, 222, age_days=0)

    _startup_timing.prune_old_logs(max_age_days=7, keep_recent=50)

    assert not old.exists(), "file older than max_age_days must be pruned"
    assert fresh.exists(), "a fresh file must survive the age rule"


def test_prune_caps_total_count_via_keep_recent(monkeypatch, tmp_path):
    """keep-N rule: with more files than keep_recent (all within max_age_days),
    only the keep_recent MOST RECENT (by mtime) survive."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    log_dir = tmp_path / "vibe-cognition-startup"
    # 10 files, staggered mtimes (pid 0 oldest .. pid 9 newest), all well
    # within max_age_days so only the keep_recent rule is in play.
    paths = [_make_log(log_dir, i, age_days=(10 - i) * 0.01) for i in range(10)]

    _startup_timing.prune_old_logs(max_age_days=7, keep_recent=4)

    survivors = {p for p in paths if p.exists()}
    # The 4 NEWEST (highest pid / smallest age_days here) must survive.
    assert survivors == set(paths[6:]), f"wrong survivors: {sorted(p.name for p in survivors)}"


def test_prune_never_removes_a_just_created_file(monkeypatch, tmp_path):
    """A concurrent server's just-written file (mtime ~= now) must survive
    even when many other files exceed keep_recent -- proven here by giving
    it the newest mtime among a batch that otherwise all get pruned."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    log_dir = tmp_path / "vibe-cognition-startup"
    # Many old files plus one genuinely fresh one -- fresh must survive both
    # the age rule (not older than max_age_days) and the keep-N rule (ranks
    # first by recency).
    for i in range(60):
        _make_log(log_dir, i, age_days=8)
    fresh = _make_log(log_dir, 9999, age_days=0)

    _startup_timing.prune_old_logs(max_age_days=7, keep_recent=1)

    assert fresh.exists()


def test_prune_swallows_missing_file_race(monkeypatch, tmp_path):
    """CONCURRENCY-SAFE: two servers pruning the same directory may both
    target the same stale file -- a FileNotFoundError on unlink (the other
    process already deleted it) must be swallowed, not raised."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    log_dir = tmp_path / "vibe-cognition-startup"
    _make_log(log_dir, 1, age_days=10)

    def _boom(self):
        raise FileNotFoundError("simulated: another process already pruned this")

    monkeypatch.setattr(Path, "unlink", _boom)
    _startup_timing.prune_old_logs(max_age_days=7)  # must not raise


def test_prune_never_raises_when_log_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "does-not-exist"))
    _startup_timing.prune_old_logs()  # must not raise -- no dir yet is normal
