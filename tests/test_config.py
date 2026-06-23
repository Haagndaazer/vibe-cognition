"""T-1b: config.py — Settings and _default_repo_path coverage.

Pins: CLAUDE_PROJECT_DIR preference over cwd, repo_path validator rejects
missing/non-dir, derived properties, VIBE_COGNITION_NO_GIT_HYGIENE binding.
All zero coverage before this WP.
"""

from pathlib import Path

import pytest

from vibe_cognition.config import Settings, _default_repo_path

# ── _default_repo_path ────────────────────────────────────────────────────────


def test_default_repo_path_prefers_claude_project_dir(tmp_path, monkeypatch):
    """_default_repo_path: CLAUDE_PROJECT_DIR beats cwd.

    Fails-before: if the function returned cwd regardless of env (the env fallback
    was added to support Claude Code plugin injection; cwd would be wrong there).
    """
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    result = _default_repo_path()
    assert result == tmp_path


def test_default_repo_path_falls_back_to_cwd(monkeypatch):
    """_default_repo_path: when CLAUDE_PROJECT_DIR absent, returns cwd."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    result = _default_repo_path()
    assert result == Path.cwd()


# ── validate_repo_path ────────────────────────────────────────────────────────


def test_validate_repo_path_rejects_missing(tmp_path):
    """Settings.repo_path: non-existent path → ValidationError, not AttributeError.

    Fails-before: if the validator silently accepted a bad path and only failed
    later when properties like cognition_dir tried to resolve against it.
    """
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="does not exist"):
        Settings(repo_path=tmp_path / "does_not_exist")


def test_validate_repo_path_rejects_file(tmp_path):
    """Settings.repo_path: a file path (not dir) → ValidationError."""
    from pydantic import ValidationError
    f = tmp_path / "not_a_dir.txt"
    f.write_text("x")
    with pytest.raises(ValidationError, match="not a directory"):
        Settings(repo_path=f)


def test_validate_repo_path_accepts_dir(tmp_path):
    """Settings.repo_path: valid directory is accepted and resolved."""
    s = Settings(repo_path=tmp_path)
    assert s.repo_path == tmp_path.resolve()


# ── derived properties ────────────────────────────────────────────────────────


def test_effective_repo_name_defaults_to_dir_name(tmp_path):
    """effective_repo_name: no repo_name → directory name of repo_path."""
    s = Settings(repo_path=tmp_path)
    assert s.effective_repo_name == tmp_path.name


def test_effective_repo_name_uses_explicit_name(tmp_path):
    """effective_repo_name: explicit repo_name overrides directory name."""
    s = Settings(repo_path=tmp_path, repo_name="my-project")
    assert s.effective_repo_name == "my-project"


def test_cognition_dir_is_dot_cognition_under_repo(tmp_path):
    """cognition_dir: always .cognition/ under repo_path."""
    s = Settings(repo_path=tmp_path)
    assert s.cognition_dir == tmp_path.resolve() / ".cognition"


def test_cognition_chromadb_path_is_chromadb_under_cognition(tmp_path):
    """cognition_chromadb_path: always .cognition/chromadb/ under repo_path."""
    s = Settings(repo_path=tmp_path)
    assert s.cognition_chromadb_path == tmp_path.resolve() / ".cognition" / "chromadb"


# ── VIBE_COGNITION_NO_GIT_HYGIENE binding ────────────────────────────────────


def test_no_git_hygiene_defaults_false(tmp_path):
    """VIBE_COGNITION_NO_GIT_HYGIENE: unset → False (git hygiene runs by default)."""
    s = Settings(repo_path=tmp_path)
    assert s.vibe_cognition_no_git_hygiene is False


def test_no_git_hygiene_set_true_from_env(tmp_path, monkeypatch):
    """VIBE_COGNITION_NO_GIT_HYGIENE=1 → vibe_cognition_no_git_hygiene=True.

    Fails-before: if the field wasn't bound to the env var and always returned False
    regardless of the env (suppresses the auto-hygiene pass for shared-worktree repos).
    """
    monkeypatch.setenv("VIBE_COGNITION_NO_GIT_HYGIENE", "1")
    s = Settings(repo_path=tmp_path)
    assert s.vibe_cognition_no_git_hygiene is True
