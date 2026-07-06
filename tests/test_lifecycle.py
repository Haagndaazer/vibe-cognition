"""WP-Lifecycle (P1, docs/wp-lifecycle-plan.md rev 3): unit coverage for
src/vibe_cognition/lifecycle.py's primitives -- the pieces that don't need a
real second process. WPL-AC1/AC2/AC3 (real uv-run-intermediary topology,
subprocess-isolated, Windows-real) live in test_wp_lifecycle_integration.py.

All Windows-specific tests here drive the real ctypes bindings against the
REAL current process (GetCurrentProcess() is always a valid, waitable pseudo
handle) so a test double is never a raw Python object masquerading as a
Win32 HANDLE -- only the pid-resolution and OpenProcess-outcome SEAMS
(`_open_ancestor`, `get_parent_pid_via_handle`, `is_younger_than_self`) are
monkeypatched, never the underlying ctypes plumbing itself.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time

import pytest

from vibe_cognition import _startup_timing, lifecycle

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="lifecycle.py's primary primitives (NtQueryInformationProcess, "
    "OpenProcess, WaitForMultipleObjects, PeekNamedPipe) are Windows-only",
)


# ── PID-reuse guard (creation-time comparison, WPL-AC4) ──────────────────────


def test_is_younger_than_self_true_when_other_created_after_self(monkeypatch):
    """A candidate ancestor whose creation time is AFTER our own start time
    cannot be our real ancestor (we couldn't have been spawned by a process
    that doesn't exist yet) -- it's a reused pid.

    Fails-before: without this guard, a reused ancestor pid would be silently
    trusted, permanently disabling the watch (WaitForMultipleObjects would
    wait on a handle belonging to an unrelated, possibly long-lived process,
    and the real orphan condition would never be detected)."""
    times = iter([200, 100])  # other=200 (checked first), self=100

    monkeypatch.setattr(lifecycle, "_get_process_creation_time", lambda h: next(times))
    assert lifecycle.is_younger_than_self(object()) is True


def test_is_younger_than_self_false_when_other_created_before_self():
    def fake_creation_time(handle):
        return next(fake_creation_time.values)

    fake_creation_time.values = iter([50, 100])  # other=50, self=100
    import vibe_cognition.lifecycle as mod

    orig = mod._get_process_creation_time
    mod._get_process_creation_time = fake_creation_time
    try:
        assert lifecycle.is_younger_than_self(object()) is False
    finally:
        mod._get_process_creation_time = orig


def test_is_younger_than_self_none_when_other_creation_time_unreadable(monkeypatch):
    """Fail-open: an unreadable creation time returns None (not True/False) --
    the caller treats this as 'can't validate', not as license to crash the
    watch or to wrongly assume reuse."""
    monkeypatch.setattr(lifecycle, "_get_process_creation_time", lambda h: None)
    assert lifecycle.is_younger_than_self(object()) is None


def test_is_younger_than_self_true_against_a_real_handle():
    """Non-mocked sanity check: our own process is never younger than
    itself, so comparing GetCurrentProcess() against itself must be False."""
    handle = lifecycle._kernel32.GetCurrentProcess()
    assert lifecycle.is_younger_than_self(handle) is False


# ── OpenProcess semantics (spec, not suggestions) ────────────────────────────


def test_open_ancestor_success_returns_handle(monkeypatch):
    sentinel_handle = 12345

    monkeypatch.setattr(lifecycle, "_raw_open_process", lambda rights, pid: sentinel_handle)
    result = lifecycle._open_ancestor(999)
    assert result.handle == sentinel_handle
    assert not result.pid_gone
    assert not result.access_denied


def test_open_ancestor_null_with_access_denied_falls_back_to_poll(monkeypatch):
    """ACCESS_DENIED on a live process must fall back to slow polling -- it
    must NOT be treated as 'pid is gone' (that would wrongly exit on a
    same-machine permissions quirk instead of a genuine orphan)."""
    monkeypatch.setattr(lifecycle, "_raw_open_process", lambda rights, pid: 0)
    monkeypatch.setattr(lifecycle, "_raw_last_error", lambda: lifecycle._ERROR_ACCESS_DENIED)
    result = lifecycle._open_ancestor(999)
    assert result.access_denied
    assert not result.pid_gone
    assert result.handle is None


def test_open_ancestor_null_with_invalid_parameter_means_pid_gone(monkeypatch):
    monkeypatch.setattr(lifecycle, "_raw_open_process", lambda rights, pid: 0)
    monkeypatch.setattr(lifecycle, "_raw_last_error", lambda: lifecycle._ERROR_INVALID_PARAMETER)
    result = lifecycle._open_ancestor(999)
    assert result.pid_gone
    assert not result.access_denied


def test_open_ancestor_null_with_unexpected_error_defaults_to_pid_gone(monkeypatch):
    """Any OpenProcess failure other than ACCESS_DENIED is treated as 'pid is
    gone' -- OpenProcess has no other legitimate failure mode for this
    specific rights request against a plain pid."""
    monkeypatch.setattr(lifecycle, "_raw_open_process", lambda rights, pid: 0)
    monkeypatch.setattr(lifecycle, "_raw_last_error", lambda: 1234)
    result = lifecycle._open_ancestor(999)
    assert result.pid_gone


# ── _pid_is_alive (ACCESS_DENIED polling fallback) ───────────────────────────


def test_pid_is_alive_true_for_the_real_current_process():
    assert lifecycle._pid_is_alive(os.getpid()) is True


def test_pid_is_alive_false_when_open_fails(monkeypatch):
    monkeypatch.setattr(lifecycle, "_raw_open_process", lambda rights, pid: 0)
    assert lifecycle._pid_is_alive(999999) is False


def test_pid_is_alive_false_when_exit_code_is_not_still_active(monkeypatch):
    real_handle = lifecycle._kernel32.GetCurrentProcess()
    monkeypatch.setattr(lifecycle, "_raw_open_process", lambda rights, pid: real_handle)

    def fake_get_exit_code(handle, out_ptr):
        out_ptr._obj.value = 0  # anything other than STILL_ACTIVE (259)
        return True

    monkeypatch.setattr(lifecycle._kernel32, "GetExitCodeProcess", fake_get_exit_code)
    # Don't let the fake CloseHandle actually close our real pseudo-handle's
    # underlying resource semantics in a way that breaks other tests --
    # GetCurrentProcess()'s pseudo handle is documented as safe to "close"
    # (a no-op), so the real CloseHandle call this exercises is harmless.
    assert lifecycle._pid_is_alive(999999) is False


# ── arm_ancestor_watch: OpenProcess-outcome branching (WPL-AC1/AC2 logic) ────


def test_arm_ancestor_watch_direct_parent_already_gone_exits_immediately(monkeypatch):
    """Fails-before: without this special-case, a server that starts up
    already-orphaned (direct parent uv exited before the watch could even
    arm) would run forever undetected until SOME other path noticed."""
    exits = []
    monkeypatch.setattr(lifecycle, "get_parent_pid_via_handle", lambda h: 4242)
    monkeypatch.setattr(
        lifecycle, "_open_ancestor", lambda pid: lifecycle._OpenResult(pid_gone=True)
    )

    thread = lifecycle.arm_ancestor_watch(depth=2, exit_fn=lambda r, d="": exits.append((r, d)))
    assert thread is None
    assert len(exits) == 1
    assert exits[0][0] == "parent_death_exit"


def test_arm_ancestor_watch_direct_parent_reused_exits_immediately(monkeypatch):
    """A reused direct-parent pid is treated the same as 'genuinely gone' --
    trusting it would wait on an unrelated process forever."""
    exits = []
    real_handle = lifecycle._kernel32.GetCurrentProcess()
    monkeypatch.setattr(lifecycle, "get_parent_pid_via_handle", lambda h: 4242)
    monkeypatch.setattr(
        lifecycle, "_open_ancestor", lambda pid: lifecycle._OpenResult(handle=real_handle)
    )
    monkeypatch.setattr(lifecycle, "is_younger_than_self", lambda h: True)

    thread = lifecycle.arm_ancestor_watch(depth=2, exit_fn=lambda r, d="": exits.append((r, d)))
    assert thread is None
    assert len(exits) == 1
    assert "reused" in exits[0][1]


def test_arm_ancestor_watch_grandparent_gone_at_arm_time_is_degraded_not_fatal(monkeypatch):
    """The rev-1 -> rev-2 fix: a grandparent that's already gone when we try
    to resolve/open it must NOT cause an exit -- a launch shim that
    legitimately exits right after spawning uv is a normal topology, not an
    orphan. Must degrade to watching just the direct parent (+ pipe watch),
    breadcrumbed, and keep running.

    Fails-before (rev-1 BLOCKER class): treating this NULL the same as the
    direct-parent NULL would insta-exit every server whose launch topology
    has any disposable process above uv -- a churn loop, not a fix."""
    exits = []
    real_handle = lifecycle._kernel32.GetCurrentProcess()
    parent_pid = 4242
    grandparent_pid = 9999

    def fake_get_parent_pid(handle):
        return grandparent_pid if handle is real_handle else parent_pid

    call_count = {"n": 0}

    def fake_open_ancestor(pid):
        call_count["n"] += 1
        if pid == parent_pid:
            return lifecycle._OpenResult(handle=real_handle)
        assert pid == grandparent_pid
        return lifecycle._OpenResult(pid_gone=True)

    monkeypatch.setattr(lifecycle, "get_parent_pid_via_handle", fake_get_parent_pid)
    monkeypatch.setattr(lifecycle, "_open_ancestor", fake_open_ancestor)
    monkeypatch.setattr(lifecycle, "is_younger_than_self", lambda h: False)

    thread = lifecycle.arm_ancestor_watch(depth=2, exit_fn=lambda r, d="": exits.append((r, d)))
    try:
        time.sleep(0.3)
        assert exits == [], "degraded arm must never exit"
        assert thread is not None and thread.is_alive()
        assert _startup_timing.breadcrumbs[-1][0] == "parent_watch_armed_degraded"
    finally:
        # Daemon thread; it's parked in a WaitForMultipleObjects on our own
        # real (never-dying-during-the-test) process handle. No teardown
        # needed -- it dies with the test process.
        pass


def test_arm_ancestor_watch_full_chain_resolves_cleanly_no_exit(monkeypatch):
    """The happy path: both parent and grandparent resolve and open cleanly
    -- armed, not degraded, no exit."""
    exits = []
    real_handle = lifecycle._kernel32.GetCurrentProcess()
    parent_pid = 111
    grandparent_pid = 222

    def fake_get_parent_pid(handle):
        return grandparent_pid if handle is real_handle else parent_pid

    monkeypatch.setattr(lifecycle, "get_parent_pid_via_handle", fake_get_parent_pid)
    monkeypatch.setattr(
        lifecycle, "_open_ancestor", lambda pid: lifecycle._OpenResult(handle=real_handle)
    )
    monkeypatch.setattr(lifecycle, "is_younger_than_self", lambda h: False)
    monkeypatch.setattr(lifecycle, "_query_image_name", lambda h: "fake.exe")

    thread = lifecycle.arm_ancestor_watch(depth=2, exit_fn=lambda r, d="": exits.append((r, d)))
    time.sleep(0.3)
    assert exits == []
    assert thread is not None and thread.is_alive()
    assert _startup_timing.breadcrumbs[-1][0] == "parent_watch_armed"


def test_arm_ancestor_watch_access_denied_falls_back_to_polling_and_still_exits(monkeypatch):
    """ACCESS_DENIED must not silently disable the watch -- it degrades to
    polling, which must still eventually detect death and exit."""
    exits = []
    monkeypatch.setattr(lifecycle, "get_parent_pid_via_handle", lambda h: 4242)
    monkeypatch.setattr(
        lifecycle, "_open_ancestor", lambda pid: lifecycle._OpenResult(access_denied=True)
    )
    monkeypatch.setattr(lifecycle, "_pid_is_alive", lambda pid: False)
    monkeypatch.setattr(lifecycle, "_POLL_INTERVAL_SECONDS", 0.02)

    thread = lifecycle.arm_ancestor_watch(depth=1, exit_fn=lambda r, d="": exits.append((r, d)))
    assert thread is not None
    thread.join(timeout=2.0)
    assert len(exits) == 1
    assert "polled" in exits[0][1]


# ── POSIX fallback (pure Python -- exercised regardless of skip marker) ──────


def test_arm_ancestor_watch_posix_detects_reparenting(monkeypatch):
    """os.getppid() changes the instant the real parent dies (reparented to
    the reaper) -- a slow poll is sufficient; this is deliberately NOT
    over-engineered relative to the Windows path."""
    exits = []
    ppids = iter([100, 100, 999])  # unchanged, unchanged, then changed

    monkeypatch.setattr(os, "getppid", lambda: next(ppids))
    monkeypatch.setattr(lifecycle, "_POLL_INTERVAL_SECONDS", 0.02)

    thread = lifecycle.arm_ancestor_watch_posix(exit_fn=lambda r, d="": exits.append((r, d)))
    thread.join(timeout=2.0)
    assert len(exits) == 1
    assert exits[0][0] == "parent_death_exit"


# ── arm_stdin_watch: console-skip gate + broken-pipe detection ──────────────


def test_arm_stdin_watch_skips_console_stdin(monkeypatch):
    """A console/dev run has FILE_TYPE_CHAR stdin -- PeekNamedPipe would
    error immediately there. Must skip the watch entirely (never exit on
    it), not treat it as an immediately-broken pipe."""
    monkeypatch.setattr(lifecycle._kernel32, "GetFileType", lambda h: 2)  # FILE_TYPE_CHAR

    thread = lifecycle.arm_stdin_watch()
    assert thread is None
    assert _startup_timing.breadcrumbs[-1][0] == "stdin_watch_skipped_console"


def test_arm_stdin_watch_exits_after_grace_on_broken_pipe(monkeypatch):
    monkeypatch.setattr(lifecycle._kernel32, "GetFileType", lambda h: 3)  # FILE_TYPE_PIPE

    def fake_peek(handle, buf, buf_size, bytes_read_ptr, bytes_avail_ptr, msg_bytes_ptr):
        ctypes.set_last_error(lifecycle._ERROR_BROKEN_PIPE)
        return False

    monkeypatch.setattr(lifecycle._kernel32, "PeekNamedPipe", fake_peek)

    exits = []
    started = time.monotonic()
    thread = lifecycle.arm_stdin_watch(
        grace_seconds=0.1, exit_fn=lambda r, d="": exits.append((r, d, time.monotonic() - started))
    )
    assert thread is not None
    thread.join(timeout=2.0)
    assert len(exits) == 1
    assert exits[0][0] == "stdin_pipe_closed_exit"
    # time.sleep() has coarse timer granularity on Windows -- allow a small
    # tolerance rather than require an exact >= grace_seconds.
    assert exits[0][2] >= 0.08  # grace period was actually honored


def test_arm_stdin_watch_ignores_transient_non_broken_pipe_errors(monkeypatch):
    """A PeekNamedPipe failure that ISN'T ERROR_BROKEN_PIPE must not be
    treated as a confirmed closed pipe -- avoid a false-positive exit."""
    monkeypatch.setattr(lifecycle._kernel32, "GetFileType", lambda h: 3)  # FILE_TYPE_PIPE
    monkeypatch.setattr(lifecycle, "_STDIN_POLL_INTERVAL_SECONDS", 0.02)

    def fake_peek(handle, buf, buf_size, bytes_read_ptr, bytes_avail_ptr, msg_bytes_ptr):
        ctypes.set_last_error(9999)  # not ERROR_BROKEN_PIPE
        return False

    monkeypatch.setattr(lifecycle._kernel32, "PeekNamedPipe", fake_peek)

    exits = []
    thread = lifecycle.arm_stdin_watch(
        grace_seconds=0.05, exit_fn=lambda r, d="": exits.append((r, d))
    )
    assert thread is not None
    time.sleep(0.3)
    assert exits == [], "a non-broken-pipe error must never trigger exit"
