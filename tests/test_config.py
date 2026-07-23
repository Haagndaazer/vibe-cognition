"""T-1b: config.py — Settings and _default_repo_path coverage.

Pins: CLAUDE_PROJECT_DIR preference over cwd, repo_path validator rejects
missing/non-dir, derived properties, VIBE_COGNITION_NO_GIT_HYGIENE binding.
All zero coverage before this WP.
"""

from pathlib import Path

import pytest

from vibe_cognition.config import Settings, _default_repo_path, resolve_repo_path_env

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


# ── WP-6 (b603f667130f): empty REPO_PATH must not silently misdirect ─────────


def test_settings_empty_repo_path_env_ignored_falls_back_to_default(tmp_path, monkeypatch):
    """An explicitly-empty REPO_PATH env var must be treated as ABSENT
    (env_ignore_empty), not as an override -- pydantic-settings only invokes
    the default_factory when the env var is fully unset, so without this,
    REPO_PATH="" flows straight to the validator as "" and (via the
    Path("") == Path(".") pathlib alias) silently resolves to the process's
    cwd instead of the intended project.

    Fails-before: without env_ignore_empty, Settings() would accept
    repo_path="" and resolve to cwd, ignoring CLAUDE_PROJECT_DIR entirely.
    """
    monkeypatch.setenv("REPO_PATH", "")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    s = Settings()
    assert s.repo_path == tmp_path.resolve()


def test_settings_env_file_not_configured():
    """WP-6 (d4a153f23a4c): env_file is dropped (not pinned) -- a project-
    level .env would be inert (resolved against the shared plugin cwd) and a
    plugin-root .env would silently become global config for every project.
    Config is env-var-only."""
    assert Settings.model_config.get("env_file") is None


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


def test_validate_repo_path_rejects_explicit_empty_string():
    """WP-6 defense in depth: an explicit empty-string repo_path is rejected
    with a clear error, not silently aliased to cwd via the
    Path("") == Path(".") pathlib quirk -- exists()/is_dir() alone would NOT
    catch this, since "." trivially exists and is a directory."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="empty string"):
        Settings(repo_path="")  # type: ignore[arg-type]


# ── resolve_repo_path_env ─────────────────────────────────────────────────────


def test_resolve_repo_path_env_reads_set_value(monkeypatch, tmp_path):
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    assert resolve_repo_path_env() == Path(str(tmp_path))


def test_resolve_repo_path_env_empty_falls_back_to_default(monkeypatch, tmp_path):
    """Fails-before (the exact bug WP-6 fixes across prime.py/backfill.py): a
    naive os.environ.get("REPO_PATH", default) only falls back when the key
    is ABSENT, not when it's present-but-empty."""
    monkeypatch.setenv("REPO_PATH", "")
    assert resolve_repo_path_env(default=tmp_path) == tmp_path


def test_resolve_repo_path_env_absent_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.delenv("REPO_PATH", raising=False)
    assert resolve_repo_path_env(default=tmp_path) == tmp_path


def test_resolve_repo_path_env_absent_no_default_uses_cwd(monkeypatch):
    monkeypatch.delenv("REPO_PATH", raising=False)
    assert resolve_repo_path_env() == Path.cwd()


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


# ── VIBE_UPDATE_NUDGE binding (WP-Nudge-1) ───────────────────────────────────
# update_check.py itself is stdlib-only and reads the env var directly (never
# constructs a Settings instance) -- this field is the honest config-surface
# for that SAME env var, not a separate channel. Confirms pydantic-settings
# derives VIBE_UPDATE_NUDGE from the vibe_ prefix, same trick as
# vibe_cognition_no_git_hygiene above.


def test_update_nudge_defaults_true(tmp_path):
    """vibe_update_nudge: unset -> True (nudge on by default -- Colton's ruling)."""
    s = Settings(repo_path=tmp_path)
    assert s.vibe_update_nudge is True


def test_update_nudge_set_false_from_env(tmp_path, monkeypatch):
    """VIBE_UPDATE_NUDGE=off is the bash/module-side kill switch value; the
    pydantic field itself is a plain bool, so pydantic-settings' own bool
    coercion is what's under test here (0/false/no -- accepted boolean
    spellings), not the "off" string (a bash/module convenience, not a
    pydantic bool literal)."""
    monkeypatch.setenv("VIBE_UPDATE_NUDGE", "false")
    s = Settings(repo_path=tmp_path)
    assert s.vibe_update_nudge is False


# ── VIBE_WHATS_NEW binding (WP-WhatsNew-1) ───────────────────────────────────
# whats_new.py itself is stdlib-only and reads the env var directly (never
# constructs a Settings instance) -- this field is the honest config-surface
# for that SAME env var, mirroring vibe_update_nudge above.


def test_whats_new_defaults_true(tmp_path):
    """vibe_whats_new: unset -> True (shown by default -- Colton's ruling)."""
    s = Settings(repo_path=tmp_path)
    assert s.vibe_whats_new is True


def test_whats_new_set_false_from_env(tmp_path, monkeypatch):
    """VIBE_WHATS_NEW=off is the bash/module-side kill switch value; the
    pydantic field itself is a plain bool, so pydantic-settings' own bool
    coercion is what's under test here (0/false/no), not the "off" string."""
    monkeypatch.setenv("VIBE_WHATS_NEW", "false")
    s = Settings(repo_path=tmp_path)
    assert s.vibe_whats_new is False


# ── DISPATCH_STALL_THRESHOLD binding (WP-Wedge-2 §W2-f) ──────────────────────


def test_dispatch_stall_threshold_defaults_to_30(tmp_path):
    """§W2-f: default matches _DispatchStallForensics' documented threshold."""
    s = Settings(repo_path=tmp_path)
    assert s.dispatch_stall_threshold == 30.0


def test_dispatch_stall_threshold_overridable_from_env(tmp_path, monkeypatch):
    """§W2-f: DISPATCH_STALL_THRESHOLD overrides the default. All five existing
    stall-forensics tests (test_wp_wedge2.py) drive the threshold through a bare
    SimpleNamespace stand-in for config, which never exercises the real
    Settings()/env path -- this is that missing sibling coverage (gate finding, MINOR).

    Fails-before: without the Settings field, no env var could reach the
    _DispatchStallForensics middleware's threshold read at all.
    """
    monkeypatch.setenv("DISPATCH_STALL_THRESHOLD", "45")
    s = Settings(repo_path=tmp_path)
    assert s.dispatch_stall_threshold == 45.0


# ── WP-Sidecar config bindings ────────────────────────────────────────────────


def test_sidecar_load_timeout_defaults_and_overridable(tmp_path, monkeypatch):
    s = Settings(repo_path=tmp_path)
    assert s.sidecar_load_timeout == 180.0
    monkeypatch.setenv("SIDECAR_LOAD_TIMEOUT", "90")
    s = Settings(repo_path=tmp_path)
    assert s.sidecar_load_timeout == 90.0


def test_sidecar_request_timeout_defaults_and_overridable(tmp_path, monkeypatch):
    s = Settings(repo_path=tmp_path)
    assert s.sidecar_request_timeout == 30.0
    monkeypatch.setenv("SIDECAR_REQUEST_TIMEOUT", "15")
    s = Settings(repo_path=tmp_path)
    assert s.sidecar_request_timeout == 15.0


def test_sidecar_mutex_wait_timeout_defaults_and_overridable(tmp_path, monkeypatch):
    s = Settings(repo_path=tmp_path)
    assert s.sidecar_mutex_wait_timeout == 300.0
    monkeypatch.setenv("SIDECAR_MUTEX_WAIT_TIMEOUT", "60")
    s = Settings(repo_path=tmp_path)
    assert s.sidecar_mutex_wait_timeout == 60.0


def test_sidecar_max_retry_attempts_defaults_and_overridable(tmp_path, monkeypatch):
    s = Settings(repo_path=tmp_path)
    assert s.sidecar_max_retry_attempts == 3
    monkeypatch.setenv("SIDECAR_MAX_RETRY_ATTEMPTS", "5")
    s = Settings(repo_path=tmp_path)
    assert s.sidecar_max_retry_attempts == 5


def test_sidecar_retry_backoff_seconds_defaults_and_overridable(tmp_path, monkeypatch):
    s = Settings(repo_path=tmp_path)
    assert s.sidecar_retry_backoff_seconds == 10.0
    monkeypatch.setenv("SIDECAR_RETRY_BACKOFF_SECONDS", "2")
    s = Settings(repo_path=tmp_path)
    assert s.sidecar_retry_backoff_seconds == 2.0


def test_sidecar_periodic_retry_interval_defaults_and_overridable(tmp_path, monkeypatch):
    s = Settings(repo_path=tmp_path)
    assert s.sidecar_periodic_retry_interval == 300.0
    monkeypatch.setenv("SIDECAR_PERIODIC_RETRY_INTERVAL", "120")
    s = Settings(repo_path=tmp_path)
    assert s.sidecar_periodic_retry_interval == 120.0
