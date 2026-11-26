"""Owner watches (WP-Memory-Lifecycle Stage B): the server waits on its real
client, the sidecar waits on its server. Seams are monkeypatched as in
test_lifecycle.py; the one real-process test waits on a child that exits by
itself, so nothing here ends a process."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from vibe_cognition import lifecycle

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="owner watches are Windows-only")


def _collect():
    exits: list[tuple[str, str]] = []
    return exits, lambda reason, detail="": exits.append((reason, detail))


def _open_ok(monkeypatch, younger=False):
    monkeypatch.setattr(lifecycle, "_open_ancestor", lambda pid: lifecycle._OpenResult(handle=4321))
    monkeypatch.setattr(lifecycle, "is_younger_than_self", lambda h: younger)
    monkeypatch.setattr(lifecycle._kernel32, "CloseHandle", lambda h: 1)


# ── client_watch_entry ───────────────────────────────────────────────────────


@pytest.mark.parametrize("image", [r"C:\x\claude.exe", r"C:\x\claude.exe.old.1794521472997.42852", None])
def test_client_verdict_ok_arms_whatever_the_image_is_called(monkeypatch, image):
    _open_ok(monkeypatch)
    entry = lifecycle.client_watch_entry({"verdict": "ok", "peer_pid": 555, "peer_image": image})
    assert entry == ["wait", 4321, 555, lifecycle.CLIENT_WATCH_REASON]


@pytest.mark.parametrize(
    "verdict",
    [
        "peer_gone",
        "peer_younger_than_self",
        "peer_inside_ancestor_set",
        "ends_disagree",
        "api_failed",
        "peer_access_denied",
        "peer_is_self",
        "skipped_console",
        "unresolved",
    ],
)
def test_client_any_other_verdict_arms_nothing(monkeypatch, verdict):
    opened: list[int] = []
    monkeypatch.setattr(lifecycle, "_open_ancestor", lambda pid: opened.append(pid))
    assert lifecycle.client_watch_entry({"verdict": verdict, "peer_pid": 555}) is None
    assert opened == []


@pytest.mark.parametrize("younger", [None, True])
def test_client_that_looks_dead_at_arm_time_arms_nothing_and_never_exits(monkeypatch, younger):
    """The client is inferred from the pipe, so a wrong inference must not
    stop the server from starting."""
    if younger is None:
        monkeypatch.setattr(lifecycle, "_open_ancestor", lambda pid: lifecycle._OpenResult(pid_gone=True))
    else:
        _open_ok(monkeypatch, younger=True)
    assert lifecycle.client_watch_entry({"verdict": "ok", "peer_pid": 555}) is None


def test_client_reopen_denied_arms_nothing(monkeypatch):
    """No polling fallback for the client: only a real wait handle arms."""
    monkeypatch.setattr(lifecycle, "_open_ancestor", lambda pid: lifecycle._OpenResult(access_denied=True))
    assert lifecycle.client_watch_entry({"verdict": "ok", "peer_pid": 555}) is None


@pytest.mark.parametrize(("last_error", "alive"), [(lifecycle._ERROR_ACCESS_DENIED, True), (87, False), (0, False)])
def test_a_denied_open_is_not_taken_as_death_by_the_poll(monkeypatch, last_error, alive):
    monkeypatch.setattr(lifecycle, "_raw_open_process", lambda rights, pid: 0)
    monkeypatch.setattr(lifecycle, "_raw_last_error", lambda: last_error)
    assert lifecycle._pid_is_alive(999999) is alive


# ── supervisor_watch_entry ───────────────────────────────────────────────────


def test_supervisor_verdict_ok_arms(monkeypatch):
    _open_ok(monkeypatch)
    exits, exit_fn = _collect()
    entry = lifecycle.supervisor_watch_entry({"verdict": "ok", "supervisor_pid": 777}, exit_fn=exit_fn)
    assert entry == ["wait", 4321, 777, lifecycle.SUPERVISOR_WATCH_REASON]
    assert exits == []


@pytest.mark.parametrize("verdict", ["supervisor_gone", "younger_than_self"])
def test_supervisor_dead_at_arm_time_exits_now(verdict):
    exits, exit_fn = _collect()
    assert lifecycle.supervisor_watch_entry({"verdict": verdict, "supervisor_pid": 777}, exit_fn=exit_fn) is None
    assert [reason for reason, _ in exits] == [lifecycle.SUPERVISOR_WATCH_REASON]


def test_supervisor_that_dies_between_log_and_arm_exits_now(monkeypatch):
    monkeypatch.setattr(lifecycle, "_open_ancestor", lambda pid: lifecycle._OpenResult(pid_gone=True))
    exits, exit_fn = _collect()
    assert lifecycle.supervisor_watch_entry({"verdict": "ok", "supervisor_pid": 777}, exit_fn=exit_fn) is None
    assert [reason for reason, _ in exits] == [lifecycle.SUPERVISOR_WATCH_REASON]


def test_an_owner_younger_than_us_is_refused_against_a_real_process():
    exits, exit_fn = _collect()
    younger = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(1.0)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        entry = lifecycle._owner_watch_entry(younger.pid, lifecycle.SUPERVISOR_WATCH_REASON, "supervisor", exit_fn)
        assert entry is None
        assert len(exits) == 1 and "younger" in exits[0][1]
    finally:
        younger.wait(timeout=30)


@pytest.mark.parametrize("verdict", ["env_absent", "env_invalid", "access_denied", "unresolved"])
def test_supervisor_unknown_arms_nothing_and_does_not_exit(verdict):
    exits, exit_fn = _collect()
    assert lifecycle.supervisor_watch_entry({"verdict": verdict, "supervisor_pid": None}, exit_fn=exit_fn) is None
    assert exits == []


# ── the wait loop names the entry that fired ─────────────────────────────────


def test_polled_owner_death_fires_with_the_owner_reason(monkeypatch):
    exits, exit_fn = _collect()
    monkeypatch.setattr(lifecycle, "_pid_is_alive", lambda pid: pid != 555)
    monkeypatch.setattr(lifecycle, "_POLL_INTERVAL_SECONDS", 0.02)
    thread = lifecycle.arm_ancestor_watch(
        depth=1, exit_fn=exit_fn, extra_entries=[["poll", None, 555, lifecycle.CLIENT_WATCH_REASON]]
    )
    assert thread is not None
    thread.join(timeout=5.0)
    assert [reason for reason, _ in exits] == [lifecycle.CLIENT_WATCH_REASON]
    assert "555" in exits[0][1]


def test_signaled_owner_handle_fires_with_the_owner_reason_against_a_real_process(capsys):
    """Real OpenProcess + WaitForMultipleObjects: the owner is a child that
    exits by itself; our real parent stays alive, so only the owner entry
    can be what fired."""
    exits, exit_fn = _collect()
    owner = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(1.5)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Built by hand: the entry helper rightly refuses an owner younger than us.
        handle = lifecycle._raw_open_process(lifecycle._PROCESS_ACCESS, owner.pid)
        assert handle
        entry = ["wait", handle, owner.pid, lifecycle.SUPERVISOR_WATCH_REASON]
        thread = lifecycle.arm_ancestor_watch(depth=1, exit_fn=exit_fn, extra_entries=[entry])
        assert thread is not None
        assert f"{lifecycle.SUPERVISOR_WATCH_REASON}={owner.pid}(wait)" in capsys.readouterr().err
        time.sleep(0.3)
        assert exits == []
        thread.join(timeout=20.0)
        assert [reason for reason, _ in exits] == [lifecycle.SUPERVISOR_WATCH_REASON]
    finally:
        owner.wait(timeout=30)


def test_parent_entries_keep_the_parent_reason(monkeypatch):
    exits, exit_fn = _collect()
    monkeypatch.setattr(lifecycle, "get_parent_pid_via_handle", lambda h: 4242)
    monkeypatch.setattr(lifecycle, "_open_ancestor", lambda pid: lifecycle._OpenResult(access_denied=True))
    monkeypatch.setattr(lifecycle, "_pid_is_alive", lambda pid: pid != 4242)
    monkeypatch.setattr(lifecycle, "_POLL_INTERVAL_SECONDS", 0.02)
    thread = lifecycle.arm_ancestor_watch(
        depth=1, exit_fn=exit_fn, extra_entries=[["poll", None, os.getpid(), lifecycle.CLIENT_WATCH_REASON]]
    )
    assert thread is not None
    thread.join(timeout=5.0)
    assert [reason for reason, _ in exits] == [lifecycle.PARENT_WATCH_REASON]
