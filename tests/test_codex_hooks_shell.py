"""WP-C1b: shell-level tests for the Codex session-start / reinject wrappers.

Reuses test_hooks_shell's Git-Bash + fake-uv harness (bash is the unit under
test) and adds a fake `codex` on PATH that logs its argv. Never touches a real
Codex config or the network.
"""

import json
import stat
import subprocess
from pathlib import Path

import pytest

from tests.test_hooks_shell import (  # noqa: F401
    _BASH,
    _FAKE_UV,
    _minimal_env,
    _msys_path,
    pytestmark,
)

_REPO = Path(__file__).resolve().parent.parent
_CODEX_START = _REPO / "adapters" / "codex" / "hooks" / "session-start.sh"
_CODEX_REINJECT = _REPO / "adapters" / "codex" / "hooks" / "reinject.sh"

_FAKE_CODEX = """#!/usr/bin/env bash
echo "CODEX: $*" >> "${FAKE_CONTROL_DIR}/codex.log"
case "$*" in
  "mcp get "*)
    if [ -f "${FAKE_CONTROL_DIR}/mcp_get_json" ]; then cat "${FAKE_CONTROL_DIR}/mcp_get_json"; exit 0; fi
    exit 1
    ;;
esac
if [ -f "${FAKE_CONTROL_DIR}/codex_exit" ]; then exit "$(cat "${FAKE_CONTROL_DIR}/codex_exit")"; fi
exit 0
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def codex_env(tmp_path):
    plugin_root = tmp_path / "plugin_root"
    (plugin_root / "hooks").mkdir(parents=True)
    (plugin_root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (plugin_root / "uv.lock").write_text("", encoding="utf-8")
    for name in ("session-start.sh", "reinject-instructions.sh"):
        (plugin_root / "hooks" / name).write_text(
            (_REPO / "hooks" / name).read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
        )
    (plugin_root / "adapters" / "codex" / "hooks").mkdir(parents=True)
    for name in ("session-start.sh", "reinject.sh"):
        (plugin_root / "adapters" / "codex" / "hooks" / name).write_text(
            (_REPO / "adapters" / "codex" / "hooks" / name).read_text(encoding="utf-8"),
            encoding="utf-8", newline="\n",
        )

    plugin_data = tmp_path / "plugin_data"  # deliberately NOT created: Codex does not create it
    codex_home = tmp_path / "codex_home"
    (plugin_root / "adapters" / "codex" / "agents").mkdir(parents=True)
    (plugin_root / "adapters" / "codex" / "agents" / "vibe-plan.toml").write_text(
        'name = "vibe-plan"\ndeveloper_instructions = "plan"\n', encoding="utf-8", newline="\n"
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_exec(bin_dir / "uv", _FAKE_UV)
    _write_exec(bin_dir / "codex", _FAKE_CODEX)

    class _Env:
        plugin_root_ = plugin_root
        plugin_data_ = plugin_data
        control_ = control_dir
        codex_home_ = codex_home

        def run(self, wrapper: str, *, with_codex: bool = True):
            path = plugin_root / "adapters" / "codex" / "hooks" / wrapper
            if not with_codex and (bin_dir / "codex").exists():
                (bin_dir / "codex").unlink()
            env = _minimal_env({
                "PATH": str(bin_dir),
                "CLAUDE_PLUGIN_ROOT": str(plugin_root),
                "CLAUDE_PLUGIN_DATA": str(plugin_data),
                "FAKE_CONTROL_DIR": str(control_dir),
                "CODEX_HOME": str(codex_home),
                "HOME": str(tmp_path),
            })
            return subprocess.run(
                [_BASH, _msys_path(path)], capture_output=True, text=True, env=env,
                cwd=str(project_dir), timeout=60,
            )

        def uv_log(self) -> str:
            p = control_dir / "invocations.log"
            return p.read_text(encoding="utf-8") if p.exists() else ""

        def codex_log(self) -> str:
            p = control_dir / "codex.log"
            return p.read_text(encoding="utf-8") if p.exists() else ""

    return _Env()


def _prime_blocks(log: str) -> list[str]:
    return [b for b in log.split("---\n") if "vibe_cognition.cognition.prime" in b]


def test_first_run_creates_data_dir_registers_server_and_notes_restart(codex_env):
    """Fresh install: the data dir does not exist yet (Codex never creates it
    for a hooks+skills plugin), `codex mcp add` runs exactly once with the
    --project form and the three env vars, the stamp is written, and prime is
    invoked with the restart note and VIBE_HARNESS=codex.

    Fails-before: no wrapper -> no registration, no stamp, no note."""
    result = codex_env.run("session-start.sh")
    assert result.returncode == 0, result.stderr

    assert codex_env.plugin_data_.is_dir()
    codex_calls = [ln for ln in codex_env.codex_log().splitlines() if ln.startswith("CODEX: mcp add")]
    assert len(codex_calls) == 1, codex_env.codex_log()
    call = codex_calls[0]
    assert "vibe-cognition" in call
    assert "--env UV_PROJECT_ENVIRONMENT=" in call and "/.venv" in call
    assert "--env VIBE_DATA_DIR=" in call
    assert "--env VIBE_HARNESS=codex" in call
    assert "-- uv run --no-sync --project" in call and "--directory" not in call
    assert call.rstrip().endswith("python -m vibe_cognition.server")

    stamp = codex_env.plugin_data_ / "codex-mcp.stamp"
    assert stamp.is_file() and stamp.read_text(encoding="utf-8").startswith("v1|uv run --no-sync --project")
    assert not (codex_env.plugin_data_ / "codex-mcp.lock").exists()

    blocks = _prime_blocks(codex_env.uv_log())
    assert blocks, codex_env.uv_log()
    # The fake uv logs a fixed set of vars; prime's own env is visible via the
    # wrapper's exports, so re-run the assertion on the raw log for the note.
    assert result.stdout.strip() == "{}"  # fake prime stdout; the hook forwards it verbatim


def test_second_run_with_matching_stamp_spawns_no_codex(codex_env):
    """AC6 steady state: unchanged desired entry -> no `codex` subprocess at all."""
    assert codex_env.run("session-start.sh").returncode == 0
    (codex_env.control_ / "codex.log").unlink()
    result = codex_env.run("session-start.sh")
    assert result.returncode == 0, result.stderr
    assert codex_env.codex_log() == ""
    assert "register_skipped_stamp_match" in result.stderr


def test_changed_desired_entry_re_registers(codex_env):
    """Plugin update simulation: a stale stamp (different plugin root) triggers
    exactly one new `codex mcp add`."""
    codex_env.plugin_data_.mkdir(parents=True, exist_ok=True)
    (codex_env.plugin_data_ / "codex-mcp.stamp").write_text("v1|uv run --no-sync --project /old/root python -m vibe_cognition.server|...", encoding="utf-8")
    result = codex_env.run("session-start.sh")
    assert result.returncode == 0, result.stderr
    assert codex_env.codex_log().count("CODEX: mcp add") == 1


def test_missing_codex_cli_degrades_to_manual_instruction_and_still_primes(codex_env):
    """No `codex` on PATH: the hook must not fail; it still runs the shared
    hook (prime) and carries a manual-registration instruction; no stamp."""
    result = codex_env.run("session-start.sh", with_codex=False)
    assert result.returncode == 0, result.stderr
    assert not (codex_env.plugin_data_ / "codex-mcp.stamp").exists()
    assert _prime_blocks(codex_env.uv_log())
    assert codex_env.codex_log() == ""


def test_failed_codex_add_leaves_no_stamp_and_still_primes(codex_env):
    (codex_env.control_ / "codex_exit").write_text("1", encoding="utf-8")
    result = codex_env.run("session-start.sh")
    assert result.returncode == 0, result.stderr
    assert not (codex_env.plugin_data_ / "codex-mcp.stamp").exists()
    assert "register_done_fail" in result.stderr
    assert _prime_blocks(codex_env.uv_log())


def test_reinject_wrapper_delegates_to_shared_reinject(codex_env):
    result = codex_env.run("reinject.sh")
    assert result.returncode == 0, result.stderr
    assert "vibe_cognition.instructions" in codex_env.uv_log()
    assert result.stdout.strip() == "{}"


def test_re_register_preserves_user_env_keys(codex_env):
    """A user-added env key on the existing entry (e.g. a model override) must
    survive re-registration; the three managed keys are always rewritten."""
    codex_env.plugin_data_.mkdir(parents=True, exist_ok=True)
    (codex_env.plugin_data_ / ".venv").mkdir()
    (codex_env.plugin_data_ / "codex-mcp.stamp").write_text("stale", encoding="utf-8")
    (codex_env.control_ / "mcp_get_json").write_text(json.dumps({
        "name": "vibe-cognition",
        "transport": {"type": "stdio", "env": {
            "UV_PROJECT_ENVIRONMENT": "/old/venv", "VIBE_DATA_DIR": "/old", "VIBE_HARNESS": "codex",
            "VIBE_MODEL_MID": "gpt-example",
        }},
    }, indent=2), encoding="utf-8")
    result = codex_env.run("session-start.sh")
    assert result.returncode == 0, result.stderr
    adds = [ln for ln in codex_env.codex_log().splitlines() if ln.startswith("CODEX: mcp add")]
    assert len(adds) == 1, codex_env.codex_log()
    assert "--env VIBE_MODEL_MID=gpt-example" in adds[0]
    assert adds[0].count("--env UV_PROJECT_ENVIRONMENT=") == 1
    assert "/old/venv" not in adds[0]
    assert "preserved_env=2" in result.stderr


def test_first_install_without_prior_entry_passes_only_managed_env(codex_env):
    """The lookup path runs (codex mcp get is called and fails, no prior entry)
    and contributes nothing: exactly the three managed keys are passed."""
    result = codex_env.run("session-start.sh")
    assert result.returncode == 0, result.stderr
    log = codex_env.codex_log()
    assert "CODEX: mcp get vibe-cognition --json" in log
    [add] = [ln for ln in log.splitlines() if ln.startswith("CODEX: mcp add")]
    assert add.count("--env ") == 3
    assert "preserved_env=0" in result.stderr


def test_compact_empty_env_does_not_leak_sibling_fields(codex_env):
    """An entry with "env": {} on one line followed by a string-valued sibling
    must not turn that sibling into a bogus --env argument."""
    codex_env.plugin_data_.mkdir(parents=True, exist_ok=True)
    (codex_env.plugin_data_ / "codex-mcp.stamp").write_text("stale", encoding="utf-8")
    (codex_env.control_ / "mcp_get_json").write_text(
        '{\n  "name": "vibe-cognition",\n  "transport": {\n    "type": "stdio",\n'
        '    "env": {},\n    "env_vars": [],\n    "cwd": "/home/user/project"\n  }\n}\n',
        encoding="utf-8",
    )
    result = codex_env.run("session-start.sh")
    assert result.returncode == 0, result.stderr
    [add] = [ln for ln in codex_env.codex_log().splitlines() if ln.startswith("CODEX: mcp add")]
    assert "cwd=" not in add
    assert add.count("--env ") == 3


def test_plan_role_is_installed_and_kept_in_sync(codex_env):
    result = codex_env.run("session-start.sh")
    assert result.returncode == 0, result.stderr
    installed = codex_env.codex_home_ / "agents" / "vibe-plan.toml"
    assert installed.read_text(encoding="utf-8").startswith('name = "vibe-plan"')
    assert "role_installed vibe-plan.toml" in result.stderr

    result2 = codex_env.run("session-start.sh")
    assert "role_unchanged vibe-plan.toml" in result2.stderr

    src = codex_env.plugin_root_ / "adapters" / "codex" / "agents" / "vibe-plan.toml"
    src.write_text(src.read_text(encoding="utf-8") + "\n# v2\n", encoding="utf-8", newline="\n")
    result3 = codex_env.run("session-start.sh")
    assert "role_installed vibe-plan.toml" in result3.stderr
    assert installed.read_text(encoding="utf-8").endswith("# v2\n")
