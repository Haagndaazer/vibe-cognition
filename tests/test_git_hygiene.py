"""WP-Git-Hygiene-Auto: tests for git_hygiene.py."""

import contextlib
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from vibe_cognition.cognition.git_hygiene import (
    _FLAG_FILENAME,
    _GITATTRIBUTES_MARKER,
    _GITATTRIBUTES_PEOPLE_RULE,
    _GITATTRIBUTES_RULE,
    GIT_HYGIENE_VERSION,
    check_hygiene_state,
    ensure_git_hygiene,
    format_hygiene_announce,
)
from vibe_cognition.cognition.local_paths import read_path as local_read_path
from vibe_cognition.cognition.local_paths import write_path as local_write_path

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


def _make_real_git_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a REAL git repo (via `git init`) + .cognition/ dir. Needed only for
    the gate-F1 regression test below, which must exercise actual git
    ignore-matching semantics (glob vs. bare filename) -- the fake .git/ marker
    from _make_git_repo is sufficient everywhere else since ensure_git_hygiene
    only checks (repo_path / ".git").exists()."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    cognition = tmp_path / ".cognition"
    cognition.mkdir()
    return tmp_path, cognition


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
    assert local_read_path(cognition, _FLAG_FILENAME).exists()


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
    """No duplicate journal rule when an existing journal-path line already has
    merge= — v6: the people rule is still appended (it wasn't covered)."""
    repo, cognition = _make_git_repo(tmp_path)
    existing = ".cognition/journal.jsonl merge=union\n"
    (repo / ".gitattributes").write_text(existing, encoding="utf-8")

    _run(repo, cognition)

    ga = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert ga.count(".cognition/journal.jsonl merge=union") == 1  # not duplicated
    assert ga.count(_GITATTRIBUTES_PEOPLE_RULE) == 1  # v6 rule added


def test_gitattributes_skip_when_both_rules_present(tmp_path):
    """Nothing appended (not even the marker) when BOTH managed rules are covered."""
    repo, cognition = _make_git_repo(tmp_path)
    existing = (
        ".cognition/journal.jsonl merge=union\n"
        ".cognition/people/*.jsonl merge=union\n"
    )
    (repo / ".gitattributes").write_text(existing, encoding="utf-8")

    _run(repo, cognition)

    ga = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert ga == existing  # byte-identical: fully covered, no append at all


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
# v7: non-git working copies still get .cognition/.gitignore
# Field defect (Survival2, doc:920a7237029f): the pass returned early without
# .git, so SVN working copies got no ignore file and machine-local
# last-seen.json was committed to SVN.
# ---------------------------------------------------------------------------


def test_gitignore_written_when_not_git_repo(tmp_path):
    """An SVN (or any non-git) working copy still gets .cognition/.gitignore."""
    cognition = tmp_path / ".cognition"
    cognition.mkdir()

    _run(tmp_path, cognition)

    gitignore = cognition / ".gitignore"
    assert gitignore.exists()
    body = gitignore.read_text(encoding="utf-8")
    for entry in ("local/", "*.lock", "chromadb/"):
        assert entry in body
    assert not (tmp_path / ".gitattributes").exists()


def test_flag_written_when_not_git_repo(tmp_path):
    """The pass completes (flag stamped) without .git, so it runs exactly once."""
    cognition = tmp_path / ".cognition"
    cognition.mkdir()

    _run(tmp_path, cognition)

    assert int(local_read_path(cognition, _FLAG_FILENAME).read_text(encoding="utf-8").strip()) == GIT_HYGIENE_VERSION


def test_gitignore_backfilled_on_version_bump_when_not_git_repo(tmp_path):
    """A non-git working copy stamped at an OLD version re-runs once and self-heals.

    This is the upgrade path for working copies already damaged by the v6 defect.
    """
    cognition = tmp_path / ".cognition"
    cognition.mkdir()
    local_write_path(cognition, _FLAG_FILENAME).write_text(str(GIT_HYGIENE_VERSION - 1), encoding="utf-8")

    _run(tmp_path, cognition)

    assert "local/" in (cognition / ".gitignore").read_text(encoding="utf-8")


def test_no_write_when_cognition_dir_absent(tmp_path):
    """No .cognition/ dir → nothing is created anywhere."""
    _run(tmp_path, tmp_path / ".cognition")

    assert not (tmp_path / ".cognition").exists()
    assert not (tmp_path / ".gitattributes").exists()


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
    assert ga.count(_GITATTRIBUTES_RULE) == 1
    assert ga.count(_GITATTRIBUTES_PEOPLE_RULE) == 1
    assert ga.count(_GITATTRIBUTES_MARKER) == 1
    assert gi.count("chromadb/") == 1


def test_existing_project_no_flag_runs_pass(tmp_path):
    """B1: .cognition/ exists but flag absent → pass still runs and writes rules."""
    repo, cognition = _make_git_repo(tmp_path)
    # cognition dir already exists (simulates existing install), no flag

    _run(repo, cognition)

    assert (repo / ".gitattributes").exists()
    assert (cognition / ".gitignore").exists()
    assert local_read_path(cognition, _FLAG_FILENAME).exists()


def test_revocation_respected(tmp_path):
    """B1: flag present, both rules deleted → pass does NOT re-add either."""
    repo, cognition = _make_git_repo(tmp_path)
    # Write flag at current version
    local_write_path(cognition, _FLAG_FILENAME).write_text(str(GIT_HYGIENE_VERSION), encoding="utf-8")

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
    assert not local_read_path(cognition, _FLAG_FILENAME).exists()


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
    assert ga.count(_GITATTRIBUTES_RULE) == 1
    assert ga.count(_GITATTRIBUTES_PEOPLE_RULE) == 1

    # Now remove flag + re-run: re-check inside lock must detect existing merge= line
    local_read_path(cognition, _FLAG_FILENAME).unlink()
    _run(repo, cognition)

    ga2 = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert ga2.count(_GITATTRIBUTES_MARKER) == 1
    assert ga2.count(_GITATTRIBUTES_RULE) == 1
    assert ga2.count(_GITATTRIBUTES_PEOPLE_RULE) == 1


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

    assert not local_read_path(cognition, _FLAG_FILENAME).exists()

    # Restore and retry
    monkeypatch.setattr(gh_mod, "_write_gitignore", original_write_gitignore)
    ensure_git_hygiene(repo, cognition)

    assert (cognition / ".gitignore").exists()
    assert local_read_path(cognition, _FLAG_FILENAME).exists()


def test_gitattributes_people_rule_added_via_version_refire(tmp_path):
    """WP-EnvFacts-A (GIT_HYGIENE_VERSION 5 -> 6): a v5-stamped install whose
    .gitattributes already carries the journal rule gains ONLY the people glob
    on the next init — the first .gitattributes-touching bump. Fails-before
    both sides: a pre-bump build never adds the people rule on re-init, and a
    naive rewrite would duplicate the journal rule."""
    repo, cognition = _make_git_repo(tmp_path)
    (repo / ".gitattributes").write_text(
        f"{_GITATTRIBUTES_MARKER}\n{_GITATTRIBUTES_RULE}\n", encoding="utf-8"
    )
    local_write_path(cognition, _FLAG_FILENAME).write_text(str(GIT_HYGIENE_VERSION - 1), encoding="utf-8")

    _run(repo, cognition)

    ga = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert ga.count(_GITATTRIBUTES_RULE) == 1  # journal rule NOT duplicated
    assert ga.count(_GITATTRIBUTES_PEOPLE_RULE) == 1  # new v6 rule added once
    assert int(local_read_path(cognition, _FLAG_FILENAME).read_text(encoding="utf-8").strip()) == GIT_HYGIENE_VERSION


def test_versioned_rerun_on_stale_flag(tmp_path):
    """Q3: flag present but content < current version → pass re-runs, stamps current version."""
    repo, cognition = _make_git_repo(tmp_path)
    local_write_path(cognition, _FLAG_FILENAME).write_text(str(GIT_HYGIENE_VERSION - 1), encoding="utf-8")

    _run(repo, cognition)

    assert (repo / ".gitattributes").exists()
    assert int(local_read_path(cognition, _FLAG_FILENAME).read_text(encoding="utf-8").strip()) == GIT_HYGIENE_VERSION


def test_versioned_no_rerun_on_current_flag(tmp_path):
    """Q3: flag content >= current version → pass does nothing."""
    repo, cognition = _make_git_repo(tmp_path)
    local_write_path(cognition, _FLAG_FILENAME).write_text(str(GIT_HYGIENE_VERSION), encoding="utf-8")

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
    assert ".cognition/.gitignore" in line
    assert state["is_git"]
    assert not state["is_svn"]
    # A git repo with union-merge in place must NOT carry the no-union warning.
    assert "CONFLICT" not in line


def test_a_relocation_deferred_by_a_locked_file_is_retried_next_start(tmp_path, monkeypatch):
    """Review finding, reproduced: the pass wrote its done-flag even when a legacy
    identity.json could not be moved (Windows file in use), so it never retried --
    and the new ignore list names only `local/`, leaving the file unignored."""
    repo, cognition = _make_git_repo(tmp_path)
    (cognition / "identity.json").write_text('{"name": "A", "email": "a@x.com"}', encoding="utf-8")
    real_replace = Path.replace

    def locked(self, target):
        if self.name == "identity.json":
            raise PermissionError("in use")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", locked)
    _run(repo, cognition)
    assert (cognition / "identity.json").exists()
    flag = local_read_path(cognition, _FLAG_FILENAME)
    assert not flag.exists() or flag.read_text(encoding="utf-8").strip() != str(GIT_HYGIENE_VERSION), (
        "pass marked done with a machine-local file still at the legacy path"
    )

    monkeypatch.setattr(Path, "replace", real_replace)
    _run(repo, cognition)
    assert not (cognition / "identity.json").exists()
    assert (cognition / "local" / "identity.json").exists()
    assert local_read_path(cognition, _FLAG_FILENAME).read_text(encoding="utf-8").strip() == str(GIT_HYGIENE_VERSION)


def test_announce_warns_no_union_merge_on_svn(tmp_path):
    """v7: an SVN working copy is told plainly that appends conflict and how to resolve."""
    cognition = tmp_path / ".cognition"
    cognition.mkdir()
    (tmp_path / ".svn").mkdir()

    _run(tmp_path, cognition)

    state = check_hygiene_state(tmp_path, cognition)
    assert state["is_svn"]
    assert not state["is_git"]
    assert state["gitignore_configured"]
    assert not state["gitattr_configured"]

    line = format_hygiene_announce(state)
    assert "SVN has no union-merge equivalent" in line
    assert "CONFLICT" in line
    assert "BOTH" in line
    # "local-only files ignored" is false on SVN until the property is set by hand.
    assert "local-only files ignored" not in line
    assert "svn:global-ignores" in line
    assert "svn add --force .cognition" in line


def test_a_project_inside_a_larger_svn_checkout_is_recognised_as_svn(tmp_path):
    """Since SVN 1.7 `.svn` exists only at the checkout root, so a project checked
    out as `trunk/proj` has none of its own and was reported as an unknown VCS."""
    (tmp_path / ".svn").mkdir()
    project = tmp_path / "proj"
    cognition = project / ".cognition"
    cognition.mkdir(parents=True)
    assert check_hygiene_state(project, cognition)["is_svn"]


def test_a_git_repo_inside_an_svn_checkout_is_not_svn(tmp_path):
    (tmp_path / ".svn").mkdir()
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    cognition = project / ".cognition"
    cognition.mkdir()
    assert not check_hygiene_state(project, cognition)["is_svn"]


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


# ---------------------------------------------------------------------------
# v9: ONE entry covers every machine-local file, including ones nobody listed.
# Replaces the eight per-name enumeration tests this supersedes.
# ---------------------------------------------------------------------------


def test_local_dir_ignores_every_machine_local_file_including_unlisted(tmp_path):
    """The whole point of v9: a file nobody enumerated is still ignored.

    Uses a REAL git repo, because the claim is about git's own ignore matching,
    not about our string being present in a file. Before v9 each name needed its
    own entry, so anything unlisted was committable.
    """
    repo, cognition = _make_real_git_repo(tmp_path)

    _run(repo, cognition)

    local = cognition / "local"
    local.mkdir(exist_ok=True)
    for name in ("identity.json", "identity.json.tmp", "last-seen.json",
                 "onboard-declined", ".last-rehydrate.json",
                 "a-machine-local-file-nobody-enumerated"):
        (local / name).write_text("x", encoding="utf-8")

    out = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "local/" not in out, f"machine-local files are not ignored:\n{out}"


def test_relocation_moves_legacy_files_and_preserves_content(tmp_path):
    """An existing install's machine-local state survives the move to local/."""
    repo, cognition = _make_git_repo(tmp_path)
    (cognition / "identity.json").write_text('{"name":"Ada"}', encoding="utf-8")
    (cognition / "last-seen.json").write_text('{"a@b.com":"t"}', encoding="utf-8")
    local_write_path(cognition, _FLAG_FILENAME).write_text(str(GIT_HYGIENE_VERSION - 1), encoding="utf-8")

    _run(repo, cognition)

    assert not (cognition / "identity.json").exists(), "legacy file left behind"
    assert (cognition / "local" / "identity.json").read_text(encoding="utf-8") == '{"name":"Ada"}'
    assert (cognition / "local" / "last-seen.json").exists()


def test_relocation_never_clobbers_a_newer_destination(tmp_path):
    """A faster process's newer copy wins; the stale legacy source is dropped."""
    repo, cognition = _make_git_repo(tmp_path)
    (cognition / "identity.json").write_text("STALE", encoding="utf-8")
    (cognition / "local").mkdir()
    (cognition / "local" / "identity.json").write_text("NEWER", encoding="utf-8")
    local_write_path(cognition, _FLAG_FILENAME).write_text(str(GIT_HYGIENE_VERSION - 1), encoding="utf-8")

    _run(repo, cognition)

    assert (cognition / "local" / "identity.json").read_text(encoding="utf-8") == "NEWER"
    assert not (cognition / "identity.json").exists()
