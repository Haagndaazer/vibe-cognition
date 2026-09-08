"""WP-C1b: the Codex plugin surface — manifest parity, hook file shape,
wrapper files, dev marketplace. Loads the REAL repo files (test_plugin_json
pattern) so the shipped config is what gets pinned.

Every assertion here fails against a tree without WP-C1b (the files do not
exist) and against the specific drift it guards (version skew, a skill path
that vanished, a Codex `source` tag Codex would silently drop).
"""

import json
import pathlib

_REPO = pathlib.Path(__file__).resolve().parents[1]
_CLAUDE_MANIFEST = _REPO / ".claude-plugin" / "plugin.json"
_CODEX_MANIFEST = _REPO / ".codex-plugin" / "plugin.json"
_CODEX_HOOKS = _REPO / "adapters" / "codex" / "hooks.json"
_MARKETPLACE = _REPO / ".agents" / "plugins" / "marketplace.json"


def _load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _pyproject_version() -> str:
    for line in (_REPO / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise AssertionError("pyproject.toml has no top-level version line")


# ── manifest parity ──────────────────────────────────────────────────────────


def test_codex_and_claude_manifests_agree_on_identity():
    claude = _load(_CLAUDE_MANIFEST)
    codex = _load(_CODEX_MANIFEST)
    assert codex["name"] == claude["name"] == "vibe-cognition"
    assert codex["version"] == claude["version"] == _pyproject_version()
    assert codex["description"] == claude["description"]


def test_codex_manifest_declares_no_mcp_server():
    """B-hook design: the server is registered by the SessionStart hook via
    `codex mcp add`, never by the manifest (a manifest-declared server would
    run with cwd forced to the plugin root and lose the project)."""
    codex = _load(_CODEX_MANIFEST)
    assert "mcpServers" not in codex
    assert not (_REPO / ".mcp.json").exists()
    assert not (_REPO / "mcp.json").exists()


def test_codex_manifest_paths_start_with_dot_slash_and_exist():
    """Codex silently drops manifest paths that do not start with `./`."""
    codex = _load(_CODEX_MANIFEST)
    paths = list(codex["skills"]) + list(codex["hooks"])
    assert paths
    for rel in paths:
        assert rel.startswith("./"), rel
        assert (_REPO / rel[2:]).exists(), rel


def test_codex_skills_exclude_agent_tool_driven_skills():
    """curate/backfill drive the Claude Agent tool, which Codex lacks."""
    codex = _load(_CODEX_MANIFEST)
    names = {pathlib.PurePosixPath(p).name for p in codex["skills"]}
    assert {"vibe-cognition", "vibe-document", "vibe-workflow", "vibe-dashboard"} <= names
    assert "vibe-curate" not in names
    assert "vibe-backfill" not in names


def test_codex_manifest_points_at_codex_hooks_not_claude_hooks():
    codex = _load(_CODEX_MANIFEST)
    assert codex["hooks"] == ["./adapters/codex/hooks.json"]


# ── hooks.json shape (Codex schema is deny_unknown_fields) ───────────────────


def _codex_handlers() -> list[tuple[str, dict]]:
    hooks = _load(_CODEX_HOOKS)
    out = []
    for group in hooks["hooks"]["SessionStart"]:
        for handler in group["hooks"]:
            out.append((group["matcher"], handler))
    return out


def test_codex_hooks_cover_start_sources_and_compact():
    matchers = {m for m, _ in _codex_handlers()}
    assert "startup|resume|clear" in matchers
    assert "compact" in matchers


def test_codex_hook_handlers_use_only_known_fields():
    allowed = {"type", "command", "commandWindows", "timeout", "async", "statusMessage", "additionalContextLimit"}
    for _, handler in _codex_handlers():
        assert handler["type"] == "command"
        assert set(handler) <= allowed, set(handler) - allowed


def test_codex_hook_commands_reference_plugin_root_and_existing_wrappers():
    for _, handler in _codex_handlers():
        for key in ("command", "commandWindows"):
            assert "${CLAUDE_PLUGIN_ROOT}" in handler[key], (key, handler[key])
        posix = handler["command"].split('"')[1]
        rel = posix.replace("${CLAUDE_PLUGIN_ROOT}/", "")
        assert (_REPO / rel).exists(), rel
        win = handler["commandWindows"].replace("${CLAUDE_PLUGIN_ROOT}\\", "").replace("\\", "/")
        assert (_REPO / win).exists(), win


def test_codex_windows_command_is_a_single_unquoted_path():
    """Codex wraps the whole Windows command line in quotes itself
    (`cmd.exe /C "<command_line>"`); quotes of our own inside it are what broke
    the manual rig (round 1). One bare path, no spaces added by us."""
    for _, handler in _codex_handlers():
        win = handler["commandWindows"]
        assert '"' not in win
        assert " " not in win.replace("${CLAUDE_PLUGIN_ROOT}", "")


def test_codex_hooks_raise_additional_context_limit_above_default():
    """Codex spills additionalContext past 2,500 tokens by default; the prime
    digest is routinely larger."""
    for _, handler in _codex_handlers():
        assert handler["additionalContextLimit"] >= 20000


# ── wrapper files ────────────────────────────────────────────────────────────


def test_codex_windows_wrappers_find_git_bash_not_wsl():
    finder = (_REPO / "adapters" / "codex" / "hooks" / "find-git-bash.cmd").read_text(encoding="utf-8")
    assert "where git.exe" in finder
    assert "bin\\bash.exe" in finder
    for name in ("session-start.cmd", "reinject.cmd"):
        text = (_REPO / "adapters" / "codex" / "hooks" / name).read_text(encoding="utf-8")
        assert "find-git-bash.cmd" in text
        assert "hookSpecificOutput" in text  # readable degrade when Git Bash is absent


def test_codex_bash_wrappers_set_harness_and_delegate_to_shared_hooks():
    start = (_REPO / "adapters" / "codex" / "hooks" / "session-start.sh").read_text(encoding="utf-8")
    reinject = (_REPO / "adapters" / "codex" / "hooks" / "reinject.sh").read_text(encoding="utf-8")
    assert "export VIBE_HARNESS=codex" in start and "export VIBE_HARNESS=codex" in reinject
    assert 'mkdir -p "$PLUGIN_DATA"' in start
    assert "codex mcp add" in start and "codex mcp remove" not in start
    assert "--project" in start and "--directory" not in start
    assert 'exec bash "${PLUGIN_ROOT}/hooks/session-start.sh"' in start
    assert 'exec bash "${PLUGIN_ROOT}/hooks/reinject-instructions.sh"' in reinject


# ── dev marketplace ──────────────────────────────────────────────────────────


def test_dev_marketplace_uses_a_source_tag_codex_accepts():
    """Codex accepts `local`, `url`, `git-subdir`, `npm`; anything else is
    dropped with only a debug-log warning and the plugin never appears."""
    market = _load(_MARKETPLACE)
    [entry] = [p for p in market["plugins"] if p["name"] == "vibe-cognition"]
    assert entry["source"]["source"] in {"local", "url", "git-subdir", "npm"}
    assert entry["source"]["url"].startswith("https://github.com/Haagndaazer/vibe-cognition")
    assert entry["policy"]["installation"] in {"AVAILABLE", "INSTALLED_BY_DEFAULT"}
    assert entry["policy"]["authentication"] in {"ON_INSTALL", "ON_USE"}
    assert market["name"] == "vibe-cognition-dev"
