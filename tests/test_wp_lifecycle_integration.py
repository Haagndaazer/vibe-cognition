"""WP-Lifecycle (P1, docs/wp-lifecycle-plan.md rev 3): WPL-AC1/AC2/AC3
subprocess-real integration tests. Windows-only, skipped elsewhere.

Real 3-tier topology for every test here: a disposable "client" stand-in
process (spawned directly by this test) spawns `uv run ... python
wp_lifecycle_launcher.py` as ITS OWN child -- exactly plugin.json's launch
shape (disposable -> uv -> python; on Windows there is no exec, so uv's pid
is never the leaf python process's pid). Spawning python directly, skipping
the uv intermediary, would make these tests vacuous-by-topology -- the
rev-1 BLOCKER the brief calls out by name. No mocks of the Win32 wait: real
OpenProcess/WaitForMultipleObjects/PeekNamedPipe run for real against real
processes.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="WP-Lifecycle is Windows-only")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LAUNCHER = _REPO_ROOT / "tests" / "wp_lifecycle_launcher.py"
_UV_EXE = shutil.which("uv") or "uv"

_EXIT_BOUND_SECONDS = 5.0
_TEST_SLACK_SECONDS = 3.0  # process-spawn/OS-scheduling noise allowance on top of the spec bound
_POLL_INTERVAL_SECONDS = 0.1
_ARM_TIMEOUT_SECONDS = 60.0  # `uv run` cold-resolve + Python startup can be slow the first time

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _is_alive(pid: int) -> bool:
    """ctypes-direct liveness check -- avoids PowerShell/tasklist subprocess
    overhead in what's otherwise a tight poll loop timing the exit bound."""
    handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _wait_for_file_containing(path: Path, needle: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                if needle in path.read_text(encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                pass
        time.sleep(_POLL_INTERVAL_SECONDS)
    return False


# The disposable "client" stand-in: spawns `uv run ... python
# wp_lifecycle_launcher.py` as ITS OWN child (matching plugin.json's launch
# line), then either waits to be killed directly (WPL-AC1/AC2) or polls for a
# sentinel file before closing ONLY the child's stdin while staying alive
# itself (WPL-AC3 -- proves the exit is stdin-driven, not ancestor-driven).
_DISPOSABLE_ANCESTOR_SCRIPT = r"""
import json, os, subprocess, sys, time

uv_exe, repo_dir, launcher_path, env_extra_json, stderr_log_path, close_stdin_sentinel = sys.argv[1:7]
env = dict(os.environ)
env.update(json.loads(env_extra_json))

stderr_log = open(stderr_log_path, "wb")
child = subprocess.Popen(
    [uv_exe, "run", "--no-sync", "--directory", repo_dir, "python", launcher_path],
    stdin=subprocess.PIPE,
    stdout=subprocess.DEVNULL,
    stderr=stderr_log,
    env=env,
)
print("SPAWNED", flush=True)

while not os.path.exists(close_stdin_sentinel):
    if child.poll() is not None:
        break
    time.sleep(0.1)
else:
    child.stdin.close()
    print("CLOSED_CHILD_STDIN", flush=True)
    # Deliberately outlive the child by a wide margin -- proves this
    # process's own lifetime is NOT tied to the child's (WPL-AC3 must show
    # the leaf exits on its OWN stdin-closure detection, independent of
    # whether/when this ancestor happens to notice or reap it).
    time.sleep(30)
    sys.exit(0)

child.wait()
"""


def _spawn_disposable_ancestor(
    tmp_path: Path,
    env_extra: dict,
    close_stdin_sentinel: Path,
) -> tuple[subprocess.Popen, Path]:
    stderr_log = tmp_path / "server_stderr.log"
    ancestor = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _DISPOSABLE_ANCESTOR_SCRIPT,
            _UV_EXE,
            str(_REPO_ROOT),
            str(_LAUNCHER),
            json.dumps(env_extra),
            str(stderr_log),
            str(close_stdin_sentinel),
        ],
        # DEVNULL-only subprocess rule (v0.12.1 pipe-drain class): this
        # process's own stdout/stderr are never read, so PIPE-and-never-
        # drain would risk the same OS-pipe-buffer-fills deadlock class.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return ancestor, stderr_log


def _base_env(tmp_path: Path, pidfile: Path) -> dict:
    return {
        "REPO_PATH": str(tmp_path / "repo"),
        "EMBEDDING_BACKEND": "ollama",  # from_config still runs (server.py:549) -- ollama just
        # skips the §3a subprocess import probe, keeping this test fast and focused.
        "VIBE_LIFECYCLE_TEST_PIDFILE": str(pidfile),
    }


@pytest.fixture
def lifecycle_env(tmp_path):
    pidfile = tmp_path / "leaf.pid"
    close_stdin_sentinel = tmp_path / "close_stdin.sentinel"
    (tmp_path / "repo").mkdir()
    return {
        "tmp_path": tmp_path,
        "pidfile": pidfile,
        "close_stdin_sentinel": close_stdin_sentinel,
    }


def _read_leaf_pid(pidfile: Path, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pidfile.exists():
            text = pidfile.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"leaf process pid file never appeared within {timeout}s -- "
        "`uv run` never got far enough to reach wp_lifecycle_launcher.py"
    )


_PROCESS_TERMINATE = 0x0001


def _cleanup(ancestor: subprocess.Popen, leaf_pid: int | None) -> None:
    with contextlib.suppress(Exception):
        if ancestor.poll() is None:
            ancestor.kill()
            ancestor.wait(timeout=10)
    if leaf_pid is not None and _is_alive(leaf_pid):
        handle = ctypes.windll.kernel32.OpenProcess(_PROCESS_TERMINATE, False, leaf_pid)
        if handle:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.CloseHandle(handle)


# ── WPL-AC1: ancestor death, healthy bg thread ───────────────────────────────


def test_wpl_ac1_server_exits_within_bound_when_disposable_ancestor_dies(lifecycle_env):
    """Real uv-run-intermediary topology (disposable -> uv -> python, exactly
    plugin.json's shape): kill the disposable ancestor; the leaf server
    process must exit within the bound.

    Fails-before: a server with no ancestor-death watch at all would never
    exit on its own here -- it would only die if/when the (untrusted, per
    the brief's evidence) client-side reap happened to catch the orphaned
    tree, which is exactly the gap this WP closes."""
    tmp_path = lifecycle_env["tmp_path"]
    pidfile = lifecycle_env["pidfile"]
    env_extra = _base_env(tmp_path, pidfile)

    ancestor, stderr_log = _spawn_disposable_ancestor(
        tmp_path, env_extra, lifecycle_env["close_stdin_sentinel"]
    )
    leaf_pid = None
    try:
        leaf_pid = _read_leaf_pid(pidfile, timeout=_ARM_TIMEOUT_SECONDS)
        assert _wait_for_file_containing(
            stderr_log, "parent_watch_armed", timeout=_ARM_TIMEOUT_SECONDS
        ), "ancestor watch never armed -- test would be vacuous if we killed before arming"

        t0 = time.monotonic()
        ancestor.kill()

        died_at = None
        deadline = t0 + _EXIT_BOUND_SECONDS + _TEST_SLACK_SECONDS
        while time.monotonic() < deadline:
            if not _is_alive(leaf_pid):
                died_at = time.monotonic() - t0
                break
            time.sleep(_POLL_INTERVAL_SECONDS)

        assert died_at is not None, (
            f"leaf process {leaf_pid} still alive {_EXIT_BOUND_SECONDS + _TEST_SLACK_SECONDS}s "
            "after disposable ancestor was killed"
        )
        assert died_at <= _EXIT_BOUND_SECONDS + _TEST_SLACK_SECONDS
    finally:
        _cleanup(ancestor, leaf_pid)


# ── WPL-AC2: ancestor death, bg thread PERMANENTLY WEDGED ────────────────────


def test_wpl_ac2_server_still_exits_within_bound_when_bg_thread_is_wedged(lifecycle_env):
    """Same topology as AC1, but with the bg thread wedged mid-import
    (EmbeddingGenerator.from_config blocked forever) via
    wp_lifecycle_launcher.py's VIBE_LIFECYCLE_TEST_WEDGE_BG hook -- this is
    the entire point of the ancestor-death watch: a graceful-only shutdown
    path (joins, atexit, lifespan cleanup) would hang forever here, since
    all of those need the wedged bg thread to unwind. Only a watch that
    forces os._exit independent of any of that can pass this test.

    Fails-before: a naive "wait for bg thread to join, then exit" shutdown
    handler passes AC1 (bg thread finishes fast) but hangs forever here --
    exactly the gap AC2 exists to close."""
    tmp_path = lifecycle_env["tmp_path"]
    pidfile = lifecycle_env["pidfile"]
    env_extra = _base_env(tmp_path, pidfile)
    env_extra["VIBE_LIFECYCLE_TEST_WEDGE_BG"] = "1"

    ancestor, stderr_log = _spawn_disposable_ancestor(
        tmp_path, env_extra, lifecycle_env["close_stdin_sentinel"]
    )
    leaf_pid = None
    try:
        leaf_pid = _read_leaf_pid(pidfile, timeout=_ARM_TIMEOUT_SECONDS)
        assert _wait_for_file_containing(
            stderr_log, "parent_watch_armed", timeout=_ARM_TIMEOUT_SECONDS
        ), "ancestor watch never armed -- test would be vacuous if we killed before arming"
        # Confirm the wedge actually engaged (bg thread reached from_config
        # and is stuck there, never reaching bg_model_loaded) -- otherwise a
        # fast bg thread would make this indistinguishable from AC1.
        assert _wait_for_file_containing(
            stderr_log, "bg_model_load_start", timeout=_ARM_TIMEOUT_SECONDS
        ), "bg thread never reached from_config -- wedge harness didn't engage"
        assert "bg_model_loaded" not in stderr_log.read_text(encoding="utf-8", errors="replace"), (
            "bg thread finished loading -- the wedge (a never-set threading.Event) didn't hold"
        )

        t0 = time.monotonic()
        ancestor.kill()

        died_at = None
        deadline = t0 + _EXIT_BOUND_SECONDS + _TEST_SLACK_SECONDS
        while time.monotonic() < deadline:
            if not _is_alive(leaf_pid):
                died_at = time.monotonic() - t0
                break
            time.sleep(_POLL_INTERVAL_SECONDS)

        assert died_at is not None, (
            f"leaf process {leaf_pid} (bg thread wedged) still alive "
            f"{_EXIT_BOUND_SECONDS + _TEST_SLACK_SECONDS}s after disposable ancestor was killed -- "
            "a graceful-only shutdown cannot pass this; os._exit must fire"
        )
        assert died_at <= _EXIT_BOUND_SECONDS + _TEST_SLACK_SECONDS
    finally:
        _cleanup(ancestor, leaf_pid)


# ── WPL-AC3: stdin closed, ancestor stays ALIVE, event loop PERMANENTLY FROZEN


def test_wpl_ac3_server_exits_within_grace_when_stdin_closes_and_loop_is_frozen(lifecycle_env):
    """Close the leaf server's stdin WITHOUT killing its ancestor (the
    disposable process stays alive throughout -- proves the exit is
    stdin-driven, not ancestor-driven), with the event loop deliberately and
    permanently frozen (a genuine non-yielding sync loop, not a cooperative
    sleep(0) spin) via wp_lifecycle_launcher.py's VIBE_LIFECYCLE_TEST_BUSY_LOOP
    hook. The MCP-conventional stdin-EOF graceful shutdown path rides the
    event loop and CANNOT fire here by construction -- only the
    loop-independent PeekNamedPipe watch thread can pass this test.

    Fails-before: the pre-WP-Lifecycle stdin-EOF path (a `to_thread`
    readline whose "" result must be processed ON the loop) never fires
    against a frozen loop -- this is exactly the mode the leaking servers
    were observed in (docs/wp-lifecycle-plan.md §1)."""
    tmp_path = lifecycle_env["tmp_path"]
    pidfile = lifecycle_env["pidfile"]
    close_stdin_sentinel = lifecycle_env["close_stdin_sentinel"]
    env_extra = _base_env(tmp_path, pidfile)
    env_extra["VIBE_LIFECYCLE_TEST_BUSY_LOOP"] = "1"

    ancestor, stderr_log = _spawn_disposable_ancestor(tmp_path, env_extra, close_stdin_sentinel)
    leaf_pid = None
    try:
        leaf_pid = _read_leaf_pid(pidfile, timeout=_ARM_TIMEOUT_SECONDS)
        assert _wait_for_file_containing(
            stderr_log, "stdin_watch_armed", timeout=_ARM_TIMEOUT_SECONDS
        ), "stdin watch never armed -- test would be vacuous"
        assert _wait_for_file_containing(
            stderr_log, "LAUNCHER_BUSY_LOOP_ENGAGED", timeout=_ARM_TIMEOUT_SECONDS
        ), "busy-loop task never engaged -- test would not prove loop-independence"

        t0 = time.monotonic()
        close_stdin_sentinel.write_text("go", encoding="utf-8")

        died_at = None
        deadline = t0 + _EXIT_BOUND_SECONDS + _TEST_SLACK_SECONDS
        while time.monotonic() < deadline:
            if not _is_alive(leaf_pid):
                died_at = time.monotonic() - t0
                break
            time.sleep(_POLL_INTERVAL_SECONDS)

        assert ancestor.poll() is None, (
            "the disposable ancestor itself died -- this would prove nothing about "
            "stdin-closure handling specifically"
        )
        assert died_at is not None, (
            f"leaf process {leaf_pid} (loop frozen) still alive "
            f"{_EXIT_BOUND_SECONDS + _TEST_SLACK_SECONDS}s after its stdin was closed -- "
            "the loop-riding graceful path can't fire here by construction; "
            "the PeekNamedPipe watch's os._exit fallback must"
        )
        assert died_at <= _EXIT_BOUND_SECONDS + _TEST_SLACK_SECONDS
    finally:
        _cleanup(ancestor, leaf_pid)
