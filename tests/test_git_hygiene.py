"""WP-Git-Hygiene-Auto: tests for git_hygiene.py."""

import contextlib
import os
import time
from pathlib import Path
from unittest.mock import patch

from vibe_cognition.cognition.git_hygiene import (
    _FLAG_FILENAME,
    _GITATTRIBUTES_MARKER,
    _GITATTRIBUTES_RULE,
    GIT_HYGIENE_VERSION,
    check_hygiene_state,
    ensure_git_hygiene,
    format_hygiene_announce,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_git_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal fake git repo and .cognition/ dir. Returns (repo, cognition)."""
    (tmp_path / ".git").mkdir()
    cognition = tmp_path / ".cognition"
    cognition.mkdir()
    return tmp_path, cognition


def _run(repo: Path, cognition: Path, env: dict | None = None) -> None:
    env = env or {}
    with patch.dict(os.environ, env, clear=False):
        ensure_git_hygiene(repo, cognition)


# ---------------------------------------------------------------------------
# .gitattributes tests
# ---------------------------------------------------------------------------


def test_gitattributes_created_when_absent(tmp_path):
    """Creates .gitattributes with marker+rule when absent in a git repo; drops flag."""
    repo, cognition = _make_git_repo(tmp_path)
    _run(repo, cognition)

    ga = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert _GITATTRIBUTES_MARKER in ga
    assert _GITATTRIBUTES_RULE in ga
    assert "-text" not in ga
    assert (cognition / _FLAG_FILENAME).exists()


def test_gitattributes_appended_without_clobbering(tmp_path):
    """Pre-seeded unrelated rules are preserved; our block is appended."""
    repo, cognition = _make_git_repo(tmp_path)
    existing = "*.py text=auto\n*.sh eol=lf\n"
    (repo / ".gitattributes").write_text(existing, encoding="utf-8")

    _run(repo, cognition)

    ga = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert "*.py text=auto" in ga
    assert "*.sh eol=lf" in ga
    assert _GITATTRIBUTES_MARKER in ga
    assert _GITATTRIBUTES_RULE in ga


def test_gitattributes_trailing_newline_normalized(tmp_path):
    """File without trailing newline gets one added before our block."""
    repo, cognition = _make_git_repo(tmp_path)
    (repo / ".gitattributes").write_text("*.py text=auto", encoding="utf-8")

    _run(repo, cognition)

    ga = (repo / ".gitattributes").read_text(encoding="utf-8")
    lines = ga.splitlines()
    assert lines[0] == "*.py text=auto"
    assert _GITATTRIBUTES_RULE in lines


def test_gitattributes_skip_when_merge_token_already_present(tmp_path):
    """No duplicate appended when an existing journal-path line already has merge=."""
    repo, cognition = _make_git_repo(tmp_path)
    existing = ".cognition/journal.jsonl merge=union\n"
    (repo / ".gitattributes").write_text(existing, encoding="utf-8")

    _run(repo, cognition)

    ga = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert ga.count("merge=union") == 1


def test_gitattributes_append_when_journal_line_has_no_merge(tmp_path):
    """Appends our merge=union even when a non-merge journal line exists (B3)."""
    repo, cognition = _make_git_repo(tmp_path)
    (repo / ".gitattributes").write_text(".cognition/journal.jsonl text\n", encoding="utf-8")

    _run(repo, cognition)

    ga = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert _GITATTRIBUTES_RULE in ga


def test_gitattributes_no_dash_text(tmp_path):
    """Written .gitattributes must never contain -text."""
    repo, cognition = _make_git_repo(tmp_path)
    _run(repo, cognition)

    ga = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert "-text" not in ga


def test_gitattributes_not_created_when_not_git_repo(tmp_path):
    """No .gitattributes created when repo_path has no .git (not a git repo)."""
    cognition = tmp_path / ".cognition"
    cognition.mkdir()

    _run(tmp_path, cognition)

    assert not (tmp_path / ".gitattributes").exists()


def test_gitattributes_not_created_when_git_missing_subdir(tmp_path):
    """No upward walk: if .git is not at repo_path, skip silently."""
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / ".git").mkdir()
    sub = parent / "sub"
    sub.mkdir()
    cognition = sub / ".cognition"
    cognition.mkdir()

    _run(sub, cognition)

    assert not (sub / ".gitattributes").exists()


# ---------------------------------------------------------------------------
# .cognition/.gitignore tests
# ---------------------------------------------------------------------------


def test_gitignore_created_when_absent(tmp_path):
    """Creates .cognition/.gitignore with chromadb/ when absent."""
    repo, cognition = _make_git_repo(tmp_path)
    _run(repo, cognition)

    gi = (cognition / ".gitignore").read_text(encoding="utf-8")
    assert "chromadb/" in gi


def test_gitignore_appended_without_clobbering(tmp_path):
    """Pre-seeded .cognition/.gitignore preserved; chromadb/ appended."""
    repo, cognition = _make_git_repo(tmp_path)
    (cognition / ".gitignore").write_text("*.tmp\n", encoding="utf-8")

    _run(repo, cognition)

    gi = (cognition / ".gitignore").read_text(encoding="utf-8")
    assert "*.tmp" in gi
    assert "chromadb/" in gi


def test_gitignore_no_dup_when_chromadb_present(tmp_path):
    """chromadb/ not duplicated if already in .cognition/.gitignore."""
    repo, cognition = _make_git_repo(tmp_path)
    (cognition / ".gitignore").write_text("chromadb/\n", encoding="utf-8")

    _run(repo, cognition)

    gi = (cognition / ".gitignore").read_text(encoding="utf-8")
    assert gi.count("chromadb/") == 1


def test_gitignore_no_dup_bare_chromadb(tmp_path):
    """chromadb (without slash) in .cognition/.gitignore also counts as present."""
    repo, cognition = _make_git_repo(tmp_path)
    (cognition / ".gitignore").write_text("chromadb\n", encoding="utf-8")

    _run(repo, cognition)

    gi = (cognition / ".gitignore").read_text(encoding="utf-8")
    assert gi.count("chromadb") == 1


def test_gitignore_flag_is_listed(tmp_path):
    """Flag file .git-hygiene-managed is listed in .cognition/.gitignore."""
    repo, cognition = _make_git_repo(tmp_path)
    _run(repo, cognition)

    gi = (cognition / ".gitignore").read_text(encoding="utf-8")
    assert _FLAG_FILENAME in gi


def test_gitignore_onboard_declined_is_listed(tmp_path):
    """WP-TC7: onboard-declined (the per-machine onboarding decline file,
    prime.ONBOARD_DECLINE_FILENAME) is listed in .cognition/.gitignore after a
    fresh run — it must never sync via git any more than the rehydrate flag does."""
    repo, cognition = _make_git_repo(tmp_path)
    _run(repo, cognition)

    gi = (cognition / ".gitignore").read_text(encoding="utf-8")
    assert "onboard-declined" in gi


def test_gitignore_onboard_declined_added_via_version_refire(tmp_path):
    """WP-TC7 (GIT_HYGIENE_VERSION 2 -> 3): a flag stamped at the OLD version on an
    existing install (pre-TC7 .gitignore already has chromadb/, the old flag, *.lock,
    and the rehydrate entry but NOT onboard-declined) triggers exactly one re-run
    that appends only the new entry — mirrors test_versioned_rerun_on_stale_flag but
    pins the SPECIFIC new rule this bump exists to add."""
    repo, cognition = _make_git_repo(tmp_path)
    (cognition / ".gitignore").write_text(
        "# vibe-cognition managed - do not remove\n"
        "chromadb/\n"
        f"{_FLAG_FILENAME}\n"
        "*.lock\n"
        ".last-rehydrate.json\n",
        encoding="utf-8",
    )
    (cognition / _FLAG_FILENAME).write_text(str(GIT_HYGIENE_VERSION - 1), encoding="utf-8")

    _run(repo, cognition)

    gi = (cognition / ".gitignore").read_text(encoding="utf-8")
    assert gi.count("onboard-declined") == 1
    assert int((cognition / _FLAG_FILENAME).read_text(encoding="utf-8").strip()) == GIT_HYGIENE_VERSION


# ---------------------------------------------------------------------------
# Flag / opt-out / idempotency / announce tests
# ---------------------------------------------------------------------------


def test_no_dup_on_second_run(tmp_path):
    """Running twice produces no duplicates in either file (flag short-circuits 2nd)."""
    repo, cognition = _make_git_repo(tmp_path)
    _run(repo, cognition)
    _run(repo, cognition)

    ga = (repo / ".gitattributes").read_text(encoding="utf-8")
    gi = (cognition / ".gitignore").read_text(encoding="utf-8")
    assert ga.count("merge=union") == 1
    assert ga.count(_GITATTRIBUTES_MARKER) == 1
    assert gi.count("chromadb/") == 1


def test_existing_project_no_flag_runs_pass(tmp_path):
    """B1: .cognition/ exists but flag absent → pass still runs and writes rules."""
    repo, cognition = _make_git_repo(tmp_path)
    # cognition dir already exists (simulates existing install), no flag

    _run(repo, cognition)

    assert (repo / ".gitattributes").exists()
    assert (cognition / ".gitignore").exists()
    assert (cognition / _FLAG_FILENAME).exists()


def test_revocation_respected(tmp_path):
    """B1: flag present, both rules deleted → pass does NOT re-add either."""
    repo, cognition = _make_git_repo(tmp_path)
    # Write flag at current version
    (cognition / _FLAG_FILENAME).write_text(str(GIT_HYGIENE_VERSION), encoding="utf-8")

    _run(repo, cognition)

    # Neither file should be created
    assert not (repo / ".gitattributes").exists()
    assert not (cognition / ".gitignore").exists()


def test_opt_out_skips_all(tmp_path):
    """VIBE_COGNITION_NO_GIT_HYGIENE=1 → no write, flag not dropped."""
    repo, cognition = _make_git_repo(tmp_path)
    _run(repo, cognition, env={"VIBE_COGNITION_NO_GIT_HYGIENE": "1"})

    assert not (repo / ".gitattributes").exists()
    assert not (cognition / ".gitignore").exists()
    assert not (cognition / _FLAG_FILENAME).exists()


def test_opt_out_true_suppresses(tmp_path):
    """VIBE_COGNITION_NO_GIT_HYGIENE=true → suppressed."""
    repo, cognition = _make_git_repo(tmp_path)
    _run(repo, cognition, env={"VIBE_COGNITION_NO_GIT_HYGIENE": "true"})
    assert not (repo / ".gitattributes").exists()


def test_opt_out_zero_does_not_suppress(tmp_path):
    """VIBE_COGNITION_NO_GIT_HYGIENE=0 → hygiene RUNS (0 is not truthy)."""
    repo, cognition = _make_git_repo(tmp_path)
    _run(repo, cognition, env={"VIBE_COGNITION_NO_GIT_HYGIENE": "0"})
    assert (repo / ".gitattributes").exists()


def test_opt_out_false_does_not_suppress(tmp_path):
    """VIBE_COGNITION_NO_GIT_HYGIENE=false → hygiene RUNS."""
    repo, cognition = _make_git_repo(tmp_path)
    _run(repo, cognition, env={"VIBE_COGNITION_NO_GIT_HYGIENE": "false"})
    assert (repo / ".gitattributes").exists()


def test_opt_out_empty_does_not_suppress(tmp_path):
    """VIBE_COGNITION_NO_GIT_HYGIENE='' → hygiene RUNS."""
    repo, cognition = _make_git_repo(tmp_path)
    _run(repo, cognition, env={"VIBE_COGNITION_NO_GIT_HYGIENE": ""})
    assert (repo / ".gitattributes").exists()


def test_no_dup_on_concurrent_double_call(tmp_path):
    """Simulates two startups both passing outer _needs_gitattributes check before either
    acquires the lock — the re-check inside the lock must prevent a duplicate block."""

    repo, cognition = _make_git_repo(tmp_path)

    # Run once to establish the file; then manually clear the flag so a second
    # call would re-enter the write path, but seed a merge= line directly so
    # the inner re-check finds it already done.
    _run(repo, cognition)
    # Verify only one block was written
    ga = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert ga.count(_GITATTRIBUTES_MARKER) == 1
    assert ga.count("merge=union") == 1

    # Now remove flag + re-run: re-check inside lock must detect existing merge= line
    (cognition / _FLAG_FILENAME).unlink()
    _run(repo, cognition)

    ga2 = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert ga2.count(_GITATTRIBUTES_MARKER) == 1
    assert ga2.count("merge=union") == 1


def test_stale_lock_is_broken(tmp_path):
    """A lock file older than _LOCK_STALE_SECONDS must be removed and reacquired."""

    repo, cognition = _make_git_repo(tmp_path)

    # Plant a stale .gitattributes.lock under .cognition/ (our new lock location)
    stale_lock = cognition / ".gitattributes.lock"
    stale_lock.write_text("stale", encoding="utf-8")
    # Backdate the mtime by 120 seconds
    old_time = time.time() - 120
    os.utime(stale_lock, (old_time, old_time))

    _run(repo, cognition)

    # Hygiene should have run despite the stale lock
    assert (repo / ".gitattributes").exists()
    assert _GITATTRIBUTES_RULE in (repo / ".gitattributes").read_text(encoding="utf-8")


def test_gitignore_contains_lock_glob(tmp_path):
    """*.lock must be listed in .cognition/.gitignore (lock-file litter fix)."""
    repo, cognition = _make_git_repo(tmp_path)
    _run(repo, cognition)

    gi = (cognition / ".gitignore").read_text(encoding="utf-8")
    assert "*.lock" in gi


def test_lock_files_placed_under_cognition(tmp_path):
    """Lock files must not appear at the repo root — they live under .cognition/."""
    repo, cognition = _make_git_repo(tmp_path)
    _run(repo, cognition)

    assert not (repo / ".gitattributes.lock").exists()
    assert not (repo / ".gitignore.lock").exists()


def test_partial_failure_no_flag(tmp_path, monkeypatch):
    """Q1: gitattributes write succeeds, gitignore writer raises → flag NOT written; retry succeeds."""
    repo, cognition = _make_git_repo(tmp_path)

    import vibe_cognition.cognition.git_hygiene as gh_mod

    call_count = [0]
    original_write_gitignore = gh_mod._write_gitignore

    def failing_write_gitignore(cd):
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("simulated failure")
        return original_write_gitignore(cd)

    monkeypatch.setattr(gh_mod, "_write_gitignore", failing_write_gitignore)

    # First run: gitignore raises → flag not written
    with contextlib.suppress(Exception):
        ensure_git_hygiene(repo, cognition)

    assert not (cognition / _FLAG_FILENAME).exists()

    # Restore and retry
    monkeypatch.setattr(gh_mod, "_write_gitignore", original_write_gitignore)
    ensure_git_hygiene(repo, cognition)

    assert (cognition / ".gitignore").exists()
    assert (cognition / _FLAG_FILENAME).exists()


def test_versioned_rerun_on_stale_flag(tmp_path):
    """Q3: flag present but content < current version → pass re-runs, stamps current version."""
    repo, cognition = _make_git_repo(tmp_path)
    (cognition / _FLAG_FILENAME).write_text(str(GIT_HYGIENE_VERSION - 1), encoding="utf-8")

    _run(repo, cognition)

    assert (repo / ".gitattributes").exists()
    assert int((cognition / _FLAG_FILENAME).read_text(encoding="utf-8").strip()) == GIT_HYGIENE_VERSION


def test_versioned_no_rerun_on_current_flag(tmp_path):
    """Q3: flag content >= current version → pass does nothing."""
    repo, cognition = _make_git_repo(tmp_path)
    (cognition / _FLAG_FILENAME).write_text(str(GIT_HYGIENE_VERSION), encoding="utf-8")

    _run(repo, cognition)

    assert not (repo / ".gitattributes").exists()


# ---------------------------------------------------------------------------
# check_hygiene_state + format_hygiene_announce tests
# ---------------------------------------------------------------------------


def test_announce_configured(tmp_path):
    """After a successful run, check_hygiene_state reflects both configured."""
    repo, cognition = _make_git_repo(tmp_path)
    _run(repo, cognition)

    state = check_hygiene_state(repo, cognition)
    assert state["gitattr_configured"]
    assert state["gitignore_configured"]

    line = format_hygiene_announce(state)
    assert "union-merge" in line
    assert "chromadb" in line


def test_announce_nothing_configured(tmp_path):
    """No configured rules → empty announce string."""
    repo, cognition = _make_git_repo(tmp_path)
    state = check_hygiene_state(repo, cognition)
    assert format_hygiene_announce(state) == ""


def test_announce_performs_no_writes(tmp_path):
    """Q7: announce (check_hygiene_state) must not write any files."""
    repo, cognition = _make_git_repo(tmp_path)
    before = list(repo.rglob("*"))
    check_hygiene_state(repo, cognition)
    after = list(repo.rglob("*"))
    assert before == after
