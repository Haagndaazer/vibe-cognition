"""WP-Lifecycle-2 Stage 1 (docs/wp-lifecycle2-plan.md rev 4): unit + integration
coverage for the REPORT-ONLY identification release.

Stage 1 arms nothing new: `resolve_stdin_pipe_peer` and
`log_supervisor_identity` only observe and breadcrumb. These tests pin the
verdict semantics (via the same monkeypatch-the-seams convention as
test_lifecycle.py -- never mock the ctypes plumbing itself) and prove, with a
real process chain, that the pipe-peer resolution finds the true pipe CREATOR
through pass-through intermediaries (the production claude -> uv -> trampoline
shape). The integration harness spawns ONLY its own stand-ins and terminates
ONLY its own Popen handles (containment rules K1/K4).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from vibe_cognition import lifecycle

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Stage-1 identification is Windows-only (pipe-end APIs, OpenProcess)",
)

_FILE_TYPE_CHAR = 2  # console handle -- what a dev run's stdin looks like


# ── resolve_stdin_pipe_peer verdict matrix (seams monkeypatched) ─────────────


def test_peer_skipped_console(monkeypatch):
    """A console stdin (dev run) is never a finding -- same rule as
    arm_stdin_watch's FILE_TYPE_CHAR skip."""
    monkeypatch.setattr(lifecycle, "_raw_get_std_input_handle", lambda: 111)
    monkeypatch.setattr(lifecycle, "_raw_get_file_type", lambda h: _FILE_TYPE_CHAR)
    assert lifecycle.resolve_stdin_pipe_peer()["verdict"] == "skipped_console"


def test_peer_api_failed_when_both_ends_unreadable(monkeypatch):
    monkeypatch.setattr(lifecycle, "_raw_get_std_input_handle", lambda: 111)
    monkeypatch.setattr(lifecycle, "_raw_get_file_type", lambda h: lifecycle._FILE_TYPE_PIPE)
    monkeypatch.setattr(lifecycle, "_raw_get_pipe_end_pids", lambda h: (None, None))
    assert lifecycle.resolve_stdin_pipe_peer()["verdict"] == "api_failed"


def test_peer_is_self_when_both_ends_are_own_pid(monkeypatch):
    own = os.getpid()
    monkeypatch.setattr(lifecycle, "_raw_get_std_input_handle", lambda: 111)
    monkeypatch.setattr(lifecycle, "_raw_get_file_type", lambda h: lifecycle._FILE_TYPE_PIPE)
    monkeypatch.setattr(lifecycle, "_raw_get_pipe_end_pids", lambda h: (own, own))
    assert lifecycle.resolve_stdin_pipe_peer()["verdict"] == "peer_is_self"


def test_peer_ends_disagree_is_reported_not_resolved(monkeypatch):
    """Anonymous pipes report the creator on both ends; disagreement is an
    unseen topology -- Stage 1 must report it, never pick a side."""
    monkeypatch.setattr(lifecycle, "_raw_get_std_input_handle", lambda: 111)
    monkeypatch.setattr(lifecycle, "_raw_get_file_type", lambda h: lifecycle._FILE_TYPE_PIPE)
    monkeypatch.setattr(lifecycle, "_raw_get_pipe_end_pids", lambda h: (11111, 22222))
    result = lifecycle.resolve_stdin_pipe_peer()
    assert result["verdict"] == "ends_disagree"
    assert result["peer_pid"] is None


def _patch_pipe_and_ancestry(monkeypatch, peer_pid, parent_pid=40001, grandparent_pid=40002):
    """Common setup: stdin is a pipe whose creator is `peer_pid`; our resolved
    parent/grandparent are the given pids (never real ones)."""
    monkeypatch.setattr(lifecycle, "_raw_get_std_input_handle", lambda: 111)
    monkeypatch.setattr(lifecycle, "_raw_get_file_type", lambda h: lifecycle._FILE_TYPE_PIPE)
    monkeypatch.setattr(lifecycle, "_raw_get_pipe_end_pids", lambda h: (peer_pid, peer_pid))
    parents = iter([parent_pid, grandparent_pid])
    monkeypatch.setattr(lifecycle, "get_parent_pid_via_handle", lambda h: next(parents, None))


def test_peer_inside_ancestor_set(monkeypatch):
    """A peer that IS our parent/grandparent means an intermediary re-piped
    stdio -- the exact condition Stage 2's fallback walk exists for; Stage 1
    reports it (spike criterion pinned in rev 3)."""
    _patch_pipe_and_ancestry(monkeypatch, peer_pid=40002)  # == grandparent

    def fake_open(pid):
        return lifecycle._OpenResult(handle=1234)  # parent probe only

    monkeypatch.setattr(lifecycle, "_open_ancestor", fake_open)
    monkeypatch.setattr(lifecycle._kernel32, "CloseHandle", lambda h: 1)
    assert lifecycle.resolve_stdin_pipe_peer()["verdict"] == "peer_inside_ancestor_set"


@pytest.mark.parametrize(
    ("open_result", "younger", "expected"),
    [
        (lifecycle._OpenResult(pid_gone=True), None, "peer_gone"),
        (lifecycle._OpenResult(access_denied=True), None, "peer_access_denied"),
        (lifecycle._OpenResult(handle=1234), True, "peer_younger_than_self"),
        (lifecycle._OpenResult(handle=1234), False, "ok"),
    ],
)
def test_peer_open_outcomes(monkeypatch, open_result, younger, expected):
    peer = 55555
    _patch_pipe_and_ancestry(monkeypatch, peer_pid=peer)

    def fake_open(pid):
        if pid == 40001:  # parent probe
            return lifecycle._OpenResult(handle=999)
        assert pid == peer
        return open_result

    monkeypatch.setattr(lifecycle, "_open_ancestor", fake_open)
    monkeypatch.setattr(lifecycle, "is_younger_than_self", lambda h: younger)
    monkeypatch.setattr(lifecycle, "_query_image_name", lambda h: r"C:\fake\claude.exe")
    monkeypatch.setattr(lifecycle._kernel32, "CloseHandle", lambda h: 1)

    result = lifecycle.resolve_stdin_pipe_peer()
    assert result["verdict"] == expected
    if expected == "ok":
        assert result["peer_pid"] == peer
        assert result["peer_image"] == r"C:\fake\claude.exe"


def test_peer_resolution_never_raises(monkeypatch):
    """Log-only code must never break startup -- an exploding seam is
    reported as a verdict, not propagated."""
    monkeypatch.setattr(
        lifecycle, "_raw_get_std_input_handle", lambda: (_ for _ in ()).throw(OSError("boom"))
    )
    result = lifecycle.resolve_stdin_pipe_peer()
    assert result["verdict"] == "unresolved"
    assert "boom" in result["error"]


# ── log_supervisor_identity (sidecar side) ───────────────────────────────────


def test_supervisor_env_absent(monkeypatch):
    """No env var = spawned by a pre-Stage-1 server or run directly --
    expected, not a finding."""
    monkeypatch.delenv(lifecycle.SUPERVISOR_PID_ENV, raising=False)
    assert lifecycle.log_supervisor_identity()["verdict"] == "env_absent"


def test_supervisor_env_invalid(monkeypatch):
    monkeypatch.setenv(lifecycle.SUPERVISOR_PID_ENV, "not-a-pid")
    assert lifecycle.log_supervisor_identity()["verdict"] == "env_invalid"


@pytest.mark.parametrize(
    ("open_result", "younger", "expected"),
    [
        (lifecycle._OpenResult(pid_gone=True), None, "supervisor_gone"),
        (lifecycle._OpenResult(access_denied=True), None, "access_denied"),
        (lifecycle._OpenResult(handle=1234), True, "younger_than_self"),
        (lifecycle._OpenResult(handle=1234), False, "ok"),
    ],
)
def test_supervisor_open_outcomes(monkeypatch, open_result, younger, expected):
    monkeypatch.setenv(lifecycle.SUPERVISOR_PID_ENV, "77777")
    monkeypatch.setattr(lifecycle, "_open_ancestor", lambda pid: open_result)
    monkeypatch.setattr(lifecycle, "is_younger_than_self", lambda h: younger)
    monkeypatch.setattr(lifecycle, "_query_image_name", lambda h: r"C:\fake\python.exe")
    monkeypatch.setattr(lifecycle._kernel32, "CloseHandle", lambda h: 1)

    result = lifecycle.log_supervisor_identity()
    assert result["verdict"] == expected
    assert result["supervisor_pid"] == 77777


def test_supervisor_resolution_against_real_self(monkeypatch):
    """Non-mocked sanity check: our own live pid resolves verdict=ok (we are
    not younger than ourselves, and our own image is readable)."""
    monkeypatch.setenv(lifecycle.SUPERVISOR_PID_ENV, str(os.getpid()))
    result = lifecycle.log_supervisor_identity()
    assert result["verdict"] == "ok"
    assert result["supervisor_image"] and result["supervisor_image"].lower().endswith(
        ("python.exe", "pythonw.exe")
    )


# ── G4 fold: the HEALTHY arm path breadcrumbs its chain ──────────────────────


def test_healthy_arm_breadcrumbs_chain_with_images(capsys):
    """Fails on pre-Stage-1 main by construction: the healthy path used to
    stamp `parent_watch_armed` silently. One line of `grandparent_image=`
    would have exposed the vacuous depth-2 watch (G1) on day one -- Stage 1
    makes the healthy path state what it watches, every startup."""
    thread = lifecycle.arm_ancestor_watch(exit_fn=lambda reason, detail: None)
    err = capsys.readouterr().err
    assert thread is not None
    assert "parent_watch_armed" in err
    assert "self=" in err
    assert "parent=" in err
    # Image names present for at least the direct parent (grandparent may be
    # access-denied/poll in exotic environments; parent_image is opened with
    # the same rights the wait handle needs, so it must be there).
    assert "parent_image=" in err


# ── Integration: real chain, peer resolves through pass-through hops ─────────


def test_pipe_peer_resolves_creator_through_two_intermediaries(tmp_path: Path):
    """The production shape in miniature: stand-in (creates the stdio pipes,
    plays claude.exe) -> hop -> hop -> resolver (plays the server). The
    resolver must identify the STAND-IN as its pipe peer -- not itself, not
    either hop -- with verdict ok, because pass-through intermediaries do not
    re-pipe stdio (the rev-3-validated uv behavior).

    Containment: every process here is spawned by this chain itself; the only
    cleanup is communicate()/kill() on our OWN Popen handle (K1/K4)."""
    src_dir = Path(__file__).resolve().parents[1] / "src"

    hop = tmp_path / "hop.py"
    hop.write_text(
        textwrap.dedent(
            """
            import subprocess, sys
            # Pass-through: inherit stdio untouched, forward to the next command.
            sys.exit(subprocess.call(sys.argv[1:]))
            """
        ),
        encoding="utf-8",
    )

    resolver = tmp_path / "resolver.py"
    resolver.write_text(
        textwrap.dedent(
            """
            import json
            from vibe_cognition.lifecycle import resolve_stdin_pipe_peer
            print(json.dumps(resolve_stdin_pipe_peer()))
            """
        ),
        encoding="utf-8",
    )

    stand_in = tmp_path / "stand_in.py"
    stand_in.write_text(
        textwrap.dedent(
            """
            import json, os, subprocess, sys
            hop, resolver = sys.argv[1], sys.argv[2]
            # THIS process creates the stdio pipes (stdin=PIPE) -- it is the
            # pipe creator the resolver must find, two hops down.
            child = subprocess.Popen(
                [sys.executable, hop, sys.executable, hop, sys.executable, resolver],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            try:
                out, _ = child.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                child.kill()  # our OWN child, held by our own Popen handle
                raise
            print(json.dumps({"stand_in_pid": os.getpid(), "resolver": json.loads(out.strip())}))
            """
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [sys.executable, str(stand_in), str(hop), str(resolver)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    resolved = payload["resolver"]

    assert resolved["verdict"] == "ok", resolved
    assert resolved["peer_pid"] == payload["stand_in_pid"], resolved
    assert resolved["peer_image"].lower().endswith(("python.exe", "pythonw.exe"))
