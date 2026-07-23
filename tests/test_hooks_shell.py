"""WP-11 (1a796b2be9b5): shell-level regression harness for the SessionStart hooks.

bash IS the unit under test here, so a subprocess invocation is unavoidable --
the sanctioned exception to the project's no-subprocess-in-tests rule. NEVER
invokes real `uv`/network: a fake `uv` on PATH logs every invocation (argv +
the env vars the hook sets) and returns pre-seeded, controllable output, so
these tests run in milliseconds and never touch the internet.

Covers exactly the WP-11 scope: the B-3 backslash-path class (discovery
41e24b74219d), the migrate-note surfacing branch, and health-probe branch
selection -- not a full line-by-line script audit.
"""

import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SESSION_START = _REPO / "hooks" / "session-start.sh"
_REINJECT = _REPO / "hooks" / "reinject-instructions.sh"


def _find_git_bash() -> str:
    """Resolve Git Bash specifically, not whatever `bash` PATH lookup finds --
    on this machine plain "bash" resolves to WSL's launcher (C:\\Windows\\
    System32\\bash.exe), which mounts drives at /mnt/c/... instead of /c/...
    and produced spurious "No such file" failures with MSYS-style paths.
    Git Bash is what the plugin's own hooks are written for/tested against."""
    for candidate in (
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return shutil.which("bash") or "bash"  # last resort; tests will fail loudly if wrong


_BASH = _find_git_bash() if sys.platform == "win32" else "bash"


def _git_bash_coreutils_dirs() -> list[str]:
    """Directories that make coreutils (cat, mkdir, dirname, sha256sum,
    cygpath, ...) resolve -- derived from the SAME pinned bash.exe path, not
    from whatever ambient PATH the pytest-invoking process happens to carry.

    Vince's redirect: 7/8 tests failed on his machine with "cat: command not
    found" (exit 127) even though they ran 3x stable on mine -- because the
    harness built its subprocess env from `dict(os.environ)`, inheriting
    PATH from whoever launched pytest instead of constructing it
    deterministically. Fix: never touch os.environ for PATH; derive
    coreutils location structurally from _BASH's own resolved path.
    """
    bash_path = Path(_BASH)
    usr_bin = bash_path.parent  # .../Git/usr/bin
    git_root = usr_bin.parent.parent  # .../Git/usr/bin -> usr -> Git
    mingw_bin = git_root / "mingw64" / "bin"
    return [str(usr_bin), str(mingw_bin)]


def _minimal_env(extra: dict) -> dict:
    """Build a subprocess env EXPLICITLY -- no os.environ inheritance beyond
    the couple of vars Windows/bash genuinely need to function. `extra`'s
    PATH entries (if any) are prepended before the coreutils dirs."""
    extra_path = extra.pop("PATH", "")
    path_parts = ([extra_path] if extra_path else []) + _git_bash_coreutils_dirs()
    env = {
        "PATH": os.pathsep.join(path_parts),
        # Windows itself (not just Git Bash) needs these to spawn processes /
        # resolve DLLs correctly; SystemRoot is the commonly-required one.
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
    }
    env.update(extra)
    return env


def _msys_path(p) -> str:
    """C:\\foo\\bar -> /c/foo/bar -- MSYS bash (Git Bash on Windows) doesn't
    resolve a drive-letter path as its own script argument; needs POSIX form."""
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = f"/{s[0].lower()}{s[2:]}"
    return s

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="hooks are Windows/Git-Bash-first; only verified there")


def test_harness_precondition_coreutils_resolve_in_minimal_env():
    """Precondition guard for every other test in this file: cat/mkdir/dirname
    (all used by session-start.sh/reinject-instructions.sh) must resolve
    inside _minimal_env's constructed PATH. If this fails, every other test's
    127-exit-code failures are a symptom of THIS, not a real hook bug -- fail
    here first with a clear message instead of downstream confusion.
    """
    env = _minimal_env({})
    for tool in ("cat", "mkdir", "dirname", "sha256sum", "cygpath"):
        result = subprocess.run(
            [_BASH, "-c", f"command -v {tool}"],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.returncode == 0 and result.stdout.strip(), (
            f"{tool!r} does not resolve inside _minimal_env's PATH "
            f"({env['PATH']!r}) -- the coreutils dirs derived from _BASH "
            f"({_BASH!r}) are wrong for this machine's Git install layout."
        )


_FAKE_UV = """#!/usr/bin/env bash
# Fake uv for hook shell tests -- logs every invocation + relevant env vars,
# dispatches canned stdout/exit-code by inspecting argv. Never touches the
# network or a real venv.
LOG="${FAKE_CONTROL_DIR}/invocations.log"
{
  echo "ARGS: $*"
  echo "UV_PROJECT_ENVIRONMENT=${UV_PROJECT_ENVIRONMENT:-}"
  echo "VIBE_MIGRATION_NOTE=${VIBE_MIGRATION_NOTE:-}"
  echo "VIBE_UPDATE_NOTE=${VIBE_UPDATE_NOTE:-}"
  echo "REPO_PATH=${REPO_PATH:-}"
  echo "CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-}"
  echo "CLAUDE_PLUGIN_DATA=${CLAUDE_PLUGIN_DATA:-}"
  echo "---"
} >> "$LOG"

ARGS="$*"
_read_ctrl() {
  # _read_ctrl <file> <default>
  if [ -f "${FAKE_CONTROL_DIR}/$1" ]; then cat "${FAKE_CONTROL_DIR}/$1"; else printf '%s' "$2"; fi
}

case "$ARGS" in
  "sync --project"*)
    # Anchored at the START of ARGS -- unanchored *"sync --project"* also
    # matched "run --no-sync --project ..." (contains "sync --project" as a
    # substring of "no-sync --project"), silently hijacking every other case.
    exit "$(_read_ctrl sync_exit 0)"
    ;;
  *"import torch, chromadb"*)
    exit "$(_read_ctrl health_probe_exit 0)"
    ;;
  *"vibe_cognition.migrate_mcp"*)
    _read_ctrl migrate_stdout ""
    exit "$(_read_ctrl migrate_exit 0)"
    ;;
  *"vibe_cognition.update_check"*)
    _read_ctrl update_stdout ""
    exit "$(_read_ctrl update_exit 0)"
    ;;
  *"vibe_cognition.cognition.prime"*)
    _read_ctrl prime_stdout '{}'
    exit "$(_read_ctrl prime_exit 0)"
    ;;
  *"vibe_cognition.instructions"*)
    _read_ctrl instructions_stdout '{}'
    exit "$(_read_ctrl instructions_exit 0)"
    ;;
  *)
    exit 0
    ;;
esac
"""


@pytest.fixture
def hook_env(tmp_path):
    """Build an isolated fake-uv-on-PATH environment for one hook invocation.

    Returns a namespace with paths + a `run(hook_path)` helper. Callers seed
    `control_dir` files (e.g. `migrate_stdout`) before calling `run`.
    """
    plugin_root = tmp_path / "plugin_root"
    plugin_root.mkdir()
    (plugin_root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (plugin_root / "uv.lock").write_text("", encoding="utf-8")

    plugin_data = tmp_path / "plugin_data"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()

    uv_path = bin_dir / "uv"
    uv_path.write_text(_FAKE_UV, encoding="utf-8", newline="\n")
    uv_path.chmod(uv_path.stat().st_mode | stat.S_IEXEC)

    class _Env:
        def __init__(self):
            self.plugin_root = plugin_root
            self.plugin_data = plugin_data
            self.project_dir = project_dir
            self.control_dir = control_dir
            self.bin_dir = bin_dir

        def seed(self, name: str, content: str) -> None:
            (self.control_dir / name).write_text(content, encoding="utf-8", newline="\n")

        def log_text(self) -> str:
            log = self.control_dir / "invocations.log"
            return log.read_text(encoding="utf-8") if log.exists() else ""

        def run(
            self, hook_path: Path, *,
            plugin_root_override: str | None = None,
            omit_plugin_data: bool = False,
            extra_env: dict | None = None,
        ):
            extra = {
                "PATH": str(self.bin_dir),
                "CLAUDE_PLUGIN_ROOT": plugin_root_override or str(self.plugin_root),
                "CLAUDE_PROJECT_DIR": str(self.project_dir),
                "FAKE_CONTROL_DIR": str(self.control_dir),
            }
            if not omit_plugin_data:
                extra["CLAUDE_PLUGIN_DATA"] = str(self.plugin_data)
            if extra_env:
                extra.update(extra_env)
            env = _minimal_env(extra)
            return subprocess.run(
                [_BASH, _msys_path(hook_path)],
                capture_output=True, text=True, env=env, timeout=30,
            )

    return _Env()


# ── B-3 backslash-path class (discovery 41e24b74219d) ────────────────────────


def test_backslash_plugin_root_does_not_nest_venv_in_pinned_cache(hook_env, tmp_path):
    """CLAUDE_PLUGIN_DATA unset + a Windows backslash CLAUDE_PLUGIN_ROOT: the
    computed UV_PROJECT_ENVIRONMENT (VENV_DIR) must NOT equal a path nested
    inside plugin_root itself -- that was the B-3 bug (the buggy `%/*` glob
    left a backslash PLUGIN_ROOT completely unstripped).

    Fails-before (pre-fix `%/*`): PLUGIN_DATA == PLUGIN_ROOT unchanged, so
    VENV_DIR would land inside the version-pinned plugin_root -- exactly the
    bug this regression guard exists to catch if it's ever reintroduced.
    """
    backslash_root = str(hook_env.plugin_root).replace("/", "\\")

    hook_env.seed("health_probe_exit", "0")
    # omit_plugin_data forces the fallback path under test: PLUGIN_DATA
    # computed from PLUGIN_ROOT via the %[/\\]* expansion, not from an
    # explicitly-provided CLAUDE_PLUGIN_DATA.
    result = hook_env.run(
        _SESSION_START, plugin_root_override=backslash_root, omit_plugin_data=True
    )

    assert result.returncode == 0, result.stderr
    log = hook_env.log_text()
    venv_lines = [
        line for line in log.splitlines() if line.startswith("UV_PROJECT_ENVIRONMENT=")
    ]
    assert venv_lines, f"uv was never invoked with UV_PROJECT_ENVIRONMENT set: {log}"
    venv_dir = venv_lines[0].split("=", 1)[1]
    assert venv_dir, "VENV_DIR resolved to an empty string"
    # The bug: PLUGIN_DATA == PLUGIN_ROOT (unstripped) -> venv nests INSIDE the
    # version-pinned root. Assert the venv dir is NOT a subpath of plugin_root.
    plugin_root_norm = str(hook_env.plugin_root).replace("\\", "/").rstrip("/")
    assert not venv_dir.replace("\\", "/").startswith(plugin_root_norm + "/"), (
        f"venv dir {venv_dir!r} nested inside plugin_root {plugin_root_norm!r} -- B-3 regression"
    )


# ── migrate-note surfacing branch ─────────────────────────────────────────────


def test_migrate_note_is_passed_through_to_prime_invocation(hook_env):
    """A note printed by the (faked) migrate_mcp step must reach prime's
    invocation via VIBE_MIGRATION_NOTE -- this is the wiring session-start.sh
    Step 3 -> Step 4 depends on for the migration note to ever reach the user.
    """
    note = "Vibe Cognition removed a stale entry (preserved: some-other-server)."
    hook_env.seed("migrate_stdout", note)
    hook_env.seed("health_probe_exit", "0")

    result = hook_env.run(_SESSION_START)

    assert result.returncode == 0, result.stderr
    log = hook_env.log_text()
    # Find the invocation block that called prime, and check ITS logged
    # VIBE_MIGRATION_NOTE line carries the note migrate_mcp "printed".
    blocks = log.split("---\n")
    prime_blocks = [b for b in blocks if "vibe_cognition.cognition.prime" in b]
    assert prime_blocks, f"prime was never invoked: {log}"
    assert any(f"VIBE_MIGRATION_NOTE={note}" in b for b in prime_blocks), (
        f"migrate note not forwarded to prime's invocation: {prime_blocks}"
    )


def test_no_migrate_note_forwards_empty_string(hook_env):
    """When migrate_mcp prints nothing (the common case -- no stale entry),
    prime must see an EMPTY VIBE_MIGRATION_NOTE, not a stale/leftover value."""
    hook_env.seed("migrate_stdout", "")
    hook_env.seed("health_probe_exit", "0")

    result = hook_env.run(_SESSION_START)

    assert result.returncode == 0, result.stderr
    log = hook_env.log_text()
    blocks = log.split("---\n")
    prime_blocks = [b for b in blocks if "vibe_cognition.cognition.prime" in b]
    assert prime_blocks
    assert any("VIBE_MIGRATION_NOTE=\n" in b or b.rstrip().endswith("VIBE_MIGRATION_NOTE=") for b in prime_blocks)


# ── update_check (WP-Nudge-1): kill switch, throttle gate, note forwarding ───


def test_update_check_invoked_on_first_run_no_stamp_note_forwarded(hook_env):
    """No existing stamp (first run): update_check.py IS invoked, and its
    printed note reaches prime via VIBE_UPDATE_NOTE -- same wiring shape as
    the migrate-note forwarding tests above."""
    note = "vibe-cognition v0.29.0 is available (you have v0.28.0)."
    hook_env.seed("health_probe_exit", "0")
    hook_env.seed("update_stdout", note)

    result = hook_env.run(_SESSION_START)

    assert result.returncode == 0, result.stderr
    log = hook_env.log_text()
    blocks = log.split("---\n")
    assert any("vibe_cognition.update_check" in b for b in blocks), (
        f"update_check was never invoked: {log}"
    )
    prime_blocks = [b for b in blocks if "vibe_cognition.cognition.prime" in b]
    assert prime_blocks, f"prime was never invoked: {log}"
    assert any(f"VIBE_UPDATE_NOTE={note}" in b for b in prime_blocks), (
        f"update note not forwarded to prime's invocation: {prime_blocks}"
    )


def test_no_update_note_forwards_empty_string(hook_env):
    """update_check prints nothing (the common case) -> prime must see an
    EMPTY VIBE_UPDATE_NOTE, not a stale/leftover value."""
    hook_env.seed("health_probe_exit", "0")
    hook_env.seed("update_stdout", "")

    result = hook_env.run(_SESSION_START)

    assert result.returncode == 0, result.stderr
    log = hook_env.log_text()
    blocks = log.split("---\n")
    prime_blocks = [b for b in blocks if "vibe_cognition.cognition.prime" in b]
    assert prime_blocks
    assert any(
        "VIBE_UPDATE_NOTE=\n" in b or b.rstrip().endswith("VIBE_UPDATE_NOTE=")
        for b in prime_blocks
    )


def test_update_check_skipped_when_stamp_fresh(hook_env):
    """A stamp file written just now (< 24h old): update_check.py is NEVER
    invoked -- no uv process, no network, per the throttle gate."""
    hook_env.plugin_data.mkdir(parents=True, exist_ok=True)
    stamp = hook_env.plugin_data / "update-check.json"
    stamp.write_text('{"checked_at": "", "remote_version": ""}', encoding="utf-8")

    hook_env.seed("health_probe_exit", "0")
    hook_env.seed("update_stdout", "should never be seen")

    result = hook_env.run(_SESSION_START)

    assert result.returncode == 0, result.stderr
    log = hook_env.log_text()
    blocks = log.split("---\n")
    assert not any("vibe_cognition.update_check" in b for b in blocks), (
        f"update_check was invoked despite a fresh stamp: {log}"
    )


def test_update_check_proceeds_when_stamp_older_than_24h(hook_env):
    """The mirror of the fresh-stamp case (Vince's redirect): a stamp OLDER
    than 24h must still trigger an update_check invocation -- pins that the
    gate reads `find`'s OUTPUT (empty when nothing matches -mtime -1), not
    its exit status (0 regardless of a match), which would otherwise make a
    stale stamp silently read as "fresh" forever after day one.

    Fails-before (the exact bug Vince flagged): gating on `find`'s exit code
    instead of its output -- `find` exits 0 whether or not anything matched,
    so a naive `if find "$STAMP" -mtime -1 >/dev/null; then SKIP` would never
    proceed again once a stamp existed.
    """
    hook_env.plugin_data.mkdir(parents=True, exist_ok=True)
    stamp = hook_env.plugin_data / "update-check.json"
    stamp.write_text('{"checked_at": "", "remote_version": ""}', encoding="utf-8")
    old_time = time.time() - (25 * 3600)
    os.utime(stamp, (old_time, old_time))

    hook_env.seed("health_probe_exit", "0")
    hook_env.seed("update_stdout", "vibe-cognition v0.29.0 is available.")

    result = hook_env.run(_SESSION_START)

    assert result.returncode == 0, result.stderr
    log = hook_env.log_text()
    blocks = log.split("---\n")
    assert any("vibe_cognition.update_check" in b for b in blocks), (
        f"update_check was not invoked despite a stale (25h) stamp: {log}"
    )


def test_update_check_skipped_when_nudge_off(hook_env):
    """VIBE_UPDATE_NUDGE=off skips the check entirely, even with no stamp at
    all (would otherwise be a guaranteed-invoke case) -- the kill switch is
    checked bash-side BEFORE the throttle gate, so it also saves the process
    spawn, not just the nudge text."""
    hook_env.seed("health_probe_exit", "0")
    hook_env.seed("update_stdout", "should never be seen")

    result = hook_env.run(_SESSION_START, extra_env={"VIBE_UPDATE_NUDGE": "off"})

    assert result.returncode == 0, result.stderr
    log = hook_env.log_text()
    blocks = log.split("---\n")
    assert not any("vibe_cognition.update_check" in b for b in blocks), (
        f"update_check was invoked despite VIBE_UPDATE_NUDGE=off: {log}"
    )


def test_update_check_breadcrumbs_present(hook_env):
    """update_check_start/done breadcrumbs appear on stderr, matching the
    other two `uv run` call sites' instrumentation."""
    hook_env.seed("health_probe_exit", "0")

    result = hook_env.run(_SESSION_START)

    assert result.returncode == 0, result.stderr
    assert "update_check_start" in result.stderr
    assert "update_check_done" in result.stderr
    assert "update_check_start" not in result.stdout
    assert "update_check_done" not in result.stdout


# ── health-probe branch selection ─────────────────────────────────────────────


def test_health_probe_success_writes_stamp_and_proceeds(hook_env):
    """A successful health probe (import torch, chromadb) writes the stamp
    file and the hook proceeds to migrate+prime -- normal happy path."""
    hook_env.seed("health_probe_exit", "0")
    hook_env.seed("prime_stdout", '{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "hi"}}')

    result = hook_env.run(_SESSION_START)

    assert result.returncode == 0, result.stderr
    stamp = hook_env.plugin_data / ".venv" / ".uv-sync-stamp"
    assert stamp.exists(), "stamp was not written on a successful health probe"
    assert "hi" in result.stdout


def test_health_probe_failure_emits_multicause_message_and_no_stamp(hook_env):
    """A failing health probe (half-installed venv) must NOT write the stamp
    (so it re-warns every start until repaired) and must emit the multi-cause
    message (WP-11, c3074f43cd49) -- not just the old DLL-lock-only wording.

    Fails-before: the message named only the DLL-lock cause; this pins that
    the interrupted-download and disk-full alternates are also present.
    """
    hook_env.seed("health_probe_exit", "1")

    result = hook_env.run(_SESSION_START)

    assert result.returncode == 0, result.stderr  # hook itself always exits 0
    stamp = hook_env.plugin_data / ".venv" / ".uv-sync-stamp"
    assert not stamp.exists(), "stamp must not be written when the health probe fails"
    assert "close ALL Claude Code sessions" in result.stdout
    assert "interrupted" in result.stdout.lower()
    assert "disk" in result.stdout.lower()
    assert "delete" in result.stdout.lower()


def test_no_uv_on_path_emits_warning_and_exits_zero(hook_env):
    """No uv on PATH: a clean warning JSON, not a hard failure that could
    block the rest of the session start sequence.

    _minimal_env's PATH is built from ONLY the coreutils dirs (no fake bin_dir
    prepended here, no ambient-PATH inheritance) -- real `uv` is structurally
    absent, not filtered out of an inherited PATH after the fact.
    """
    env = _minimal_env({
        "CLAUDE_PLUGIN_ROOT": str(hook_env.plugin_root),
        "CLAUDE_PLUGIN_DATA": str(hook_env.plugin_data),
        "CLAUDE_PROJECT_DIR": str(hook_env.project_dir),
        "FAKE_CONTROL_DIR": str(hook_env.control_dir),
    })

    result = subprocess.run(
        [_BASH, _msys_path(_SESSION_START)], capture_output=True, text=True, env=env, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "requires 'uv'" in result.stdout


# ── WP-A 1b (decision 9022f7de94e9): uv-run timing breadcrumbs ───────────────


def test_four_uv_run_breadcrumb_pairs_appear_on_stderr(hook_env):
    """Each of the four `uv run` call sites (health probe, migrate_mcp,
    update_check (WP-Nudge-1), prime) must emit a start+done breadcrumb pair
    to STDERR (never stdout -- that must stay reserved for the hook's JSON
    output), tagged with the hook's own PID.

    Fails-before: no instrumentation existed, so hook-vs-server venv overlap
    (distinguishing H1 venv-lock contention from H5 baseline cold-start tax)
    was undiagnosable from a log the user could read.
    """
    hook_env.seed("health_probe_exit", "0")

    result = hook_env.run(_SESSION_START)

    assert result.returncode == 0, result.stderr
    for label in (
        "probe_start", "probe_done_ok",
        "migrate_mcp_start", "migrate_mcp_done",
        "update_check_start", "update_check_done",
        "prime_start", "prime_done",
    ):
        assert label in result.stderr, f"missing breadcrumb {label!r} in stderr: {result.stderr}"
        assert label not in result.stdout, f"breadcrumb {label!r} leaked into stdout JSON: {result.stdout}"
    assert "pid=" in result.stderr


def test_probe_failure_breadcrumb_reflects_failure_branch(hook_env):
    """A failing health probe emits probe_done_fail, not probe_done_ok --
    the breadcrumb must reflect which branch actually ran."""
    hook_env.seed("health_probe_exit", "1")

    result = hook_env.run(_SESSION_START)

    assert result.returncode == 0, result.stderr
    assert "probe_done_fail" in result.stderr
    assert "probe_done_ok" not in result.stderr


# ── reinject-instructions.sh ──────────────────────────────────────────────────


def test_reinject_instructions_passes_through_stdout(hook_env):
    """reinject-instructions.sh: successful python output is echoed verbatim."""
    hook_env.seed(
        "instructions_stdout",
        '{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "standing practices"}}',
    )
    result = hook_env.run(_REINJECT)

    assert result.returncode == 0, result.stderr
    assert "standing practices" in result.stdout


def test_reinject_instructions_falls_back_to_empty_object_on_failure(hook_env):
    """reinject-instructions.sh: if the underlying python call fails/produces
    nothing, the hook must emit valid '{}' JSON, not empty stdout (which
    could confuse the Claude Code hook parser)."""
    hook_env.seed("instructions_exit", "1")
    hook_env.seed("instructions_stdout", "")

    result = hook_env.run(_REINJECT)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "{}"
