"""Tests for .github/scripts/check_version_match.py (WP-6, b48927a30e66).

Path-loads the script (it lives outside the installed package, like the CI
pyright ratchet) so its main() can be exercised directly rather than only
via a subprocess -- consistent with the project's file-path-load pattern for
non-package scripts (see test_journal_concurrency.py's _APPENDER).
"""

import importlib.util
import json
import pathlib

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / ".github" / "scripts" / "check_version_match.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_version_match", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fixtures(tmp_path, pyproject_version, plugin_version):
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "{pyproject_version}"\n', encoding="utf-8"
    )
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"version": plugin_version}), encoding="utf-8"
    )


def test_versions_match_returns_zero(tmp_path, monkeypatch):
    module = _load_module()
    _write_fixtures(tmp_path, "1.2.3", "1.2.3")
    monkeypatch.setattr(module, "PYPROJECT_PATH", tmp_path / "pyproject.toml")
    monkeypatch.setattr(module, "PLUGIN_JSON_PATH", tmp_path / ".claude-plugin" / "plugin.json")

    assert module.main() == 0


def test_versions_mismatch_returns_one(tmp_path, monkeypatch, capsys):
    """Fails-before: no check existed at all, so a forgotten bump was silent."""
    module = _load_module()
    _write_fixtures(tmp_path, "1.2.3", "1.2.4")
    monkeypatch.setattr(module, "PYPROJECT_PATH", tmp_path / "pyproject.toml")
    monkeypatch.setattr(module, "PLUGIN_JSON_PATH", tmp_path / ".claude-plugin" / "plugin.json")

    assert module.main() == 1
    assert "mismatch" in capsys.readouterr().err


def test_missing_pyproject_version_key_returns_one(tmp_path, monkeypatch):
    module = _load_module()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
    monkeypatch.setattr(module, "PYPROJECT_PATH", tmp_path / "pyproject.toml")
    monkeypatch.setattr(module, "PLUGIN_JSON_PATH", tmp_path / ".claude-plugin" / "plugin.json")

    assert module.main() == 1


def test_missing_plugin_version_key_returns_one(tmp_path, monkeypatch):
    module = _load_module()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "1.0.0"\n', encoding="utf-8"
    )
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
    monkeypatch.setattr(module, "PYPROJECT_PATH", tmp_path / "pyproject.toml")
    monkeypatch.setattr(module, "PLUGIN_JSON_PATH", tmp_path / ".claude-plugin" / "plugin.json")

    assert module.main() == 1


def test_real_repo_files_currently_match():
    """Regression guard against the actual repo state -- the local equivalent
    of the CI step, so a forgotten bump is caught by `pytest`, not just CI."""
    module = _load_module()
    assert module.main() == 0
