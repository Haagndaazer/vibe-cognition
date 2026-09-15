"""One-time git hygiene pass for .cognition/ — stdlib-only, no deps.

Runs on every CognitionStorage startup; gated by a content-versioned, git-ignored
sidecar flag so the pass executes exactly ONCE per working copy (idempotent),
except when the schema version is bumped for future writers (triggers one re-run).

Two writes (both idempotent, locked, crash-proof):
  1. repo-root .gitattributes  — .cognition/journal.jsonl merge=union AND
                                 .cognition/people/*.jsonl merge=union (v6,
                                 per-person env-fact delta files)
  2. .cognition/.gitignore     — local/ (every machine-local file), *.lock, and
                                 chromadb/ (pre-0.32.0 teammates)

Machine-local state lives in .cognition/local/ so ONE ignore entry covers it
forever; see local_paths.py. The v9 pass relocates the legacy files there.

The .gitattributes write is git-gated; the .cognition/.gitignore write runs under
ANY VCS (v7) -- a Subversion working copy needs the local-only files kept out of
version control just as much, and it has no .git to gate on.

`merge=union` is a MERGE-DRIVER attribute — it only changes 3-way merge resolution
and never participates in checkout/checkin filtering, so adding it does NOT
re-smudge or byte-rewrite the committed journal blob.  This is the exact reason it
is safe where -text (an EOL/filter attribute) is NOT: -text reactivates the C-3
byte-rewrite + duplication scar (nodes 90ee3c1b968c, 54304ecf567c).  The writer
emits ONLY merge=union, never -text.

Opt-out: set VIBE_COGNITION_NO_VCS_HYGIENE=1 (or the older
VIBE_COGNITION_NO_GIT_HYGIENE; true/yes/on also work) to skip this pass and the SVN
one (the flag is not written; the pass retries on next start when the env is
cleared).  "0", "false", and empty string do NOT suppress the pass.

Re-arm: delete .cognition/local/.git-hygiene-managed to make the pass re-run
(re-adds any rule that was removed).
"""

import contextlib
import logging
import os
import time
from pathlib import Path

from .local_paths import RELOCATED_FILENAMES, local_dir, read_path, write_path

logger = logging.getLogger(__name__)

# Bump this integer when a NEW writer is added to ensure every working copy
# re-runs the pass exactly once more to pick up the new rule.
# v2: .last-rehydrate.json added to .cognition/.gitignore (WP-1 loss visibility).
# v3: onboard-declined added to .cognition/.gitignore (WP-TC7 onboarding decline file
#     -- per-machine, must never sync via git any more than the rehydrate flag does).
# v4: last-seen.json added to .cognition/.gitignore (WP-TC14 "Since You Were Gone"
#     digest marker -- machine-local, per-email, must never sync via git). Amended
#     in place (not v5, since v4 had not shipped anywhere yet) to a glob,
#     last-seen.json*, covering the .tmp sibling of the atomic write (gate F1).
# v5: backfill-identity-map.skeleton.json added to .cognition/.gitignore (task
#     962ab7b442d5, Train C review finding a) -- the legacy-identity-backfill
#     CLI's dry-run scratch artifact (the user edits it, then re-supplies it
#     via --map-file). A working file, not graph history -- must never ride
#     into a journal-flush `git add .cognition/` commit.
# v6: .cognition/people/*.jsonl merge=union added to .gitattributes
#     (WP-EnvFacts-A) -- per-person env-fact delta files are append-only
#     single-writer JSONL exactly like the journal, so they share its
#     union-merge posture. FIRST bump to touch the .gitattributes writer
#     (v2-v5 were gitignore-only): the single-rule check was generalized to
#     the _GITATTRIBUTES_RULES list for it.
# v7: the pass no longer returns early when there is no .git. The .gitattributes
#     writer stays git-gated; the .cognition/.gitignore writer now runs for any
#     VCS. Field defect (Survival2, doc:920a7237029f): SVN working copies got no
#     ignore file at all, so machine-local last-seen.json was committed to SVN and
#     conflicted for every teammate.
# v9: machine-local files RELOCATED into .cognition/local/, and the ignore list
#     collapsed from eight enumerated names to three entries (local/, *.lock,
#     chromadb/). Each file used to need its own line, so the list only covered
#     names someone remembered: an SVN lab run showed `svn add --force` sweeping
#     identity.json and an unlisted file in while the enumerated ones were
#     skipped. A future machine-local file now needs no ignore change at all.
#     Paths resolve through local_paths (read local, fall back to legacy, write
#     local), so an existing working copy keeps its state across the upgrade.
# v8: identity.json* added to .cognition/.gitignore -- the machine-local
#     confirmed graph identity (identity.IDENTITY_FILENAME). Says who is
#     driving THIS checkout, never shared; the .tmp sibling of its atomic
#     write is covered by the same glob.
# v10: .cognition/journal/*.jsonl merge=union -- per-person journal shards. One person
#     in two clones or worktrees still appends to the same shard, so it needs the same
#     union merge the legacy journal has.
GIT_HYGIENE_VERSION = 10

_GITATTRIBUTES_MARKER = "# vibe-cognition: append-only journal union-merge (safe to remove)"
_GITATTRIBUTES_RULE = ".cognition/journal.jsonl merge=union"
# v6 (WP-EnvFacts-A): per-person env-fact files — same append-only union-merge
# posture as the journal. Committed files (NOT gitignored); the glob covers
# every identity's file, present and future.
_GITATTRIBUTES_PEOPLE_RULE = ".cognition/people/*.jsonl merge=union"
_GITATTRIBUTES_SHARD_RULE = ".cognition/journal/*.jsonl merge=union"
# Every (path-token, full-rule) pair the writer manages. A rule is "covered"
# when a non-comment line for its exact path token already carries ANY merge=
# attribute (user overrides are respected, same as the original journal rule).
_GITATTRIBUTES_RULES: tuple[tuple[str, str], ...] = (
    (".cognition/journal.jsonl", _GITATTRIBUTES_RULE),
    (".cognition/people/*.jsonl", _GITATTRIBUTES_PEOPLE_RULE),
    (".cognition/journal/*.jsonl", _GITATTRIBUTES_SHARD_RULE),
)
# v9: ONE entry covers every machine-local file, present and future. Each used to
# need its own line, so the list only ever covered names someone remembered -- an
# SVN lab run showed `svn add --force` sweeping identity.json and an unlisted
# file straight in while the enumerated ones were skipped.
# `*.lock` stays separate: locks live beside what they lock, and moving them
# mid-upgrade would leave two plugin versions locking different paths, so the
# lock would silently stop being mutually exclusive.
# `chromadb/` stays for teammates on pre-0.32.0 versions that still write it
# into the repo.
_GITIGNORE_ENTRIES: tuple[str, ...] = ("local/", "*.lock", "chromadb/")
_FLAG_FILENAME = ".git-hygiene-managed"

# A lock older than this is assumed stale (leftover from a hard-killed process).
_LOCK_STALE_SECONDS = 60


def _read_flag(cognition_dir: Path) -> int | None:
    """Return the numeric version in the flag file, or None if absent/unreadable.

    Goes through the local-path accessor FIRST: this flag decides whether the
    versioned pass has run AND is one of the files that pass relocates, so a
    non-local-aware read would re-decide "not migrated" on every single start.
    """
    flag_path = read_path(cognition_dir, _FLAG_FILENAME)
    try:
        return int(flag_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _write_flag(cognition_dir: Path) -> None:
    flag_path = write_path(cognition_dir, _FLAG_FILENAME)
    flag_path.write_text(str(GIT_HYGIENE_VERSION), encoding="utf-8")


def _acquire_lock(lock_path: Path) -> bool:
    """Try to create the lock file exclusively.  Returns True if acquired.

    If the file exists but is older than _LOCK_STALE_SECONDS (a hard-kill
    left it behind), remove it and retry once — otherwise a crashed startup
    would make the write permanently silent until manual cleanup.
    """
    try:
        lock_path.open("x").close()
        return True
    except FileExistsError:
        try:
            age = time.time() - lock_path.stat().st_mtime
            if age > _LOCK_STALE_SECONDS:
                lock_path.unlink()
                lock_path.open("x").close()
                return True
        except OSError:
            pass
        return False
    except OSError:
        return False


def _release_lock(lock_path: Path) -> None:
    with contextlib.suppress(OSError):
        lock_path.unlink()


def _missing_gitattributes_rules(gitattributes_path: Path) -> list[str]:
    """The managed rules NOT yet covered in .gitattributes (v6: list-generalized).

    A rule is covered only when an existing non-comment line for its exact
    path token ALREADY carries a merge= token.  A path line WITHOUT merge=
    does not cover it (appending a second matching line is legal; git
    accumulates attributes).  Unreadable file -> all rules missing (same
    conservative posture as before).
    """
    if not gitattributes_path.exists():
        return [rule for _, rule in _GITATTRIBUTES_RULES]
    covered: set[str] = set()
    try:
        for line in gitattributes_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            tokens = stripped.split()
            if tokens and any(t.startswith("merge=") for t in tokens[1:]):
                covered.add(tokens[0])
    except OSError:
        return [rule for _, rule in _GITATTRIBUTES_RULES]
    return [rule for token, rule in _GITATTRIBUTES_RULES if token not in covered]


def _needs_gitattributes(gitattributes_path: Path) -> bool:
    """Return True if any managed rule still needs appending."""
    return bool(_missing_gitattributes_rules(gitattributes_path))


def _write_gitattributes(gitattributes_path: Path, cognition_dir: Path) -> bool:
    """Append the marker + rule block to .gitattributes.  Returns True on success.

    Lock file lives under .cognition/ (a dir we own) rather than next to
    .gitattributes at the repo root, so it stays out of git status and
    is already covered by the *.lock entry in .cognition/.gitignore.
    """
    lock = cognition_dir / ".gitattributes.lock"
    if not _acquire_lock(lock):
        return False
    try:
        # Re-check inside the lock: a concurrent startup may have written it
        # between our outer _needs_gitattributes check and lock acquisition.
        missing = _missing_gitattributes_rules(gitattributes_path)
        if not missing:
            return True
        existing = ""
        if gitattributes_path.exists():
            try:
                existing = gitattributes_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.debug("git-hygiene: cannot read .gitattributes: %s", exc)
                return False
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        block = prefix + _GITATTRIBUTES_MARKER + "\n" + "\n".join(missing) + "\n"
        try:
            with gitattributes_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(block)
        except OSError as exc:
            logger.debug("git-hygiene: cannot write .gitattributes: %s", exc)
            return False
        return True
    finally:
        _release_lock(lock)


def _needs_gitignore_entry(gitignore_path: Path, entry: str, bare: str) -> bool:
    """Return True if gitignore_path does not already contain a non-comment line
    matching entry or bare (e.g. 'chromadb/' or 'chromadb')."""
    if not gitignore_path.exists():
        return True
    try:
        for line in gitignore_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            if stripped in (entry, bare):
                return False
    except OSError:
        return True
    return True


def _write_gitignore(cognition_dir: Path) -> bool:
    """Ensure .cognition/.gitignore contains every entry in _GITIGNORE_ENTRIES.

    Appends only what is MISSING, never rewriting the file: a repo upgraded from a
    pre-0.38 version keeps its old per-file entries alongside the new `local/`,
    which is a harmless superset. A fresh repo gets the three current entries.
    Returns True if the file is correct after the call (success or already-present).
    """
    gitignore_path = cognition_dir / ".gitignore"
    lock = cognition_dir / ".gitignore.lock"
    if not _acquire_lock(lock):
        return False
    try:
        missing = [
            e for e in _GITIGNORE_ENTRIES
            if _needs_gitignore_entry(gitignore_path, e, e.rstrip("/"))
        ]
        if not missing:
            return True

        if not gitignore_path.exists():
            try:
                gitignore_path.write_text(
                    "\n".join(["# vibe-cognition managed - do not remove", *missing]) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.debug("git-hygiene: cannot write .cognition/.gitignore: %s", exc)
                return False
            return True

        try:
            existing = gitignore_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("git-hygiene: cannot read .cognition/.gitignore: %s", exc)
            return False

        lines_to_add = missing

        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        addition = prefix + "\n".join(lines_to_add) + "\n"
        try:
            with gitignore_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(addition)
        except OSError as exc:
            logger.debug("git-hygiene: cannot append to .cognition/.gitignore: %s", exc)
            return False
        return True
    finally:
        _release_lock(lock)


OPT_OUT_ENV = "VIBE_COGNITION_NO_VCS_HYGIENE"
LEGACY_OPT_OUT_ENV = "VIBE_COGNITION_NO_GIT_HYGIENE"


def vcs_hygiene_opted_out() -> bool:
    """True if either opt-out variable is truthy; it suppresses the git AND SVN passes.

    Only "1", "true", "yes", "on" (case-insensitive) suppress.
    "0", "false", "no", "off", and the empty string do NOT.
    """
    return any(
        os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")
        for name in (OPT_OUT_ENV, LEGACY_OPT_OUT_ENV)
    )


def _opt_out() -> bool:
    return vcs_hygiene_opted_out()


def _legacy_files_remain(cognition_dir: Path) -> bool:
    return any(
        (cognition_dir / candidate).is_file()
        for name in RELOCATED_FILENAMES for candidate in (name, f"{name}.tmp")
    )


def _relocate_local_files(cognition_dir: Path) -> bool:
    """Move machine-local files into .cognition/local/ (v9). Never raises.

    Under one lock so two servers starting together cannot both move; atomic
    rename so an interruption cannot leave a half-written destination; skip when
    the destination already holds content so a faster process's newer copy is
    never clobbered; and defer on PermissionError (Windows file-in-use) so a
    write in flight is never truncated -- the next pass retries.

    Returns True only when no machine-local file is left at a legacy path. The
    caller must not mark the pass done otherwise: the new ignore list names only
    `local/`, so a stranded identity.json would be unignored and never retried.
    """
    lock = cognition_dir / ".relocate.lock"
    if not _acquire_lock(lock):
        return not _legacy_files_remain(cognition_dir)
    try:
        target_dir = local_dir(cognition_dir)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.debug("git-hygiene: cannot create local/: %s", exc)
            return False
        for name in RELOCATED_FILENAMES:
            for candidate in (name, f"{name}.tmp"):
                src = cognition_dir / candidate
                dst = target_dir / candidate
                try:
                    if not src.is_file():
                        continue
                    if dst.exists() and dst.stat().st_size > 0:
                        src.unlink()  # a newer copy already landed; drop the stale source
                        continue
                    src.replace(dst)
                except PermissionError:
                    logger.debug("git-hygiene: %s in use, deferring relocation", candidate)
                except OSError as exc:
                    logger.debug("git-hygiene: relocating %s failed: %s", candidate, exc)
        return not _legacy_files_remain(cognition_dir)
    finally:
        _release_lock(lock)


def ensure_git_hygiene(repo_path: Path, cognition_dir: Path) -> None:
    """Run the one-time git hygiene pass.  Never raises — all failures are logged + swallowed.

    Args:
        repo_path: Repository root; .gitattributes is written only when it holds .git.
        cognition_dir: .cognition/ directory (flag lives here).
    """
    if _opt_out():
        return

    if not cognition_dir.is_dir():
        return

    flag_version = _read_flag(cognition_dir)
    if flag_version is not None and flag_version >= GIT_HYGIENE_VERSION:
        return

    # v9: relocate BEFORE writing the ignore list, so the trimmed list never
    # exists while the files it no longer names are still at the legacy path.
    relocated = _relocate_local_files(cognition_dir)

    is_git = (repo_path / ".git").exists()

    gitattributes_path = repo_path / ".gitattributes"
    ga_ok = True

    if is_git and _needs_gitattributes(gitattributes_path):
        ga_ok = _write_gitattributes(gitattributes_path, cognition_dir)
    # else already present, or not a git repo — counts as resolved

    gi_ok = _write_gitignore(cognition_dir)

    if ga_ok and gi_ok and relocated:
        try:
            _write_flag(cognition_dir)
        except OSError as exc:
            logger.debug("git-hygiene: cannot write flag: %s", exc)


def check_hygiene_state(repo_path: Path, cognition_dir: Path) -> dict:
    """Read-only check of what git-hygiene rules are in place.  For prime.py announce.

    Returns a dict with keys:
      - gitattr_configured: our marker is present in .gitattributes
      - gitignore_configured: chromadb/ is present in .cognition/.gitignore
      - is_git / is_svn: which VCS the working copy is under, if any
    Never raises.
    """
    result = {
        "gitattr_configured": False,
        "gitignore_configured": False,
        "is_git": False,
        "is_svn": False,
    }
    with contextlib.suppress(OSError):
        result["is_git"] = (repo_path / ".git").exists()
    with contextlib.suppress(OSError):
        # .svn exists only at the working-copy root, which may be above the project.
        for candidate in (repo_path, *repo_path.parents):
            if (candidate / ".svn").exists():
                result["is_svn"] = True
                break
            if (candidate / ".git").exists():
                break
    try:
        gitattributes_path = repo_path / ".gitattributes"
        if gitattributes_path.exists():
            content = gitattributes_path.read_text(encoding="utf-8")
            result["gitattr_configured"] = _GITATTRIBUTES_MARKER in content
    except OSError:
        pass
    try:
        gitignore_path = cognition_dir / ".gitignore"
        if gitignore_path.exists():
            for line in gitignore_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped in _GITIGNORE_ENTRIES:
                    result["gitignore_configured"] = True
                    break
    except OSError:
        pass
    return result


def format_hygiene_announce(state: dict) -> str:
    """Format a one-line announce string from check_hygiene_state output, or empty string.

    On a non-git working copy the line also warns that the journal has no
    union-merge equivalent, since a reader who knows the git behaviour would
    otherwise assume parity.
    """
    parts = []
    if state.get("gitattr_configured"):
        parts.append("journal union-merge (.gitattributes)")
    if state.get("gitignore_configured") and state.get("is_svn") and not state.get("is_git"):
        parts.append(
            ".cognition/.gitignore written, but SVN does NOT read it -- mirror it once as "
            "svn:global-ignores and run `svn add --force .cognition` before every commit, "
            "or new profiles never reach teammates"
        )
    elif state.get("gitignore_configured"):
        parts.append("local-only files ignored (.cognition/.gitignore)")
    if not parts:
        return ""
    line = "vibe-cognition configured: " + ", ".join(parts) + "."
    if not state.get("gitattr_configured"):
        vcs = "SVN" if state.get("is_svn") else "This VCS"
        line += (
            f" {vcs} has no union-merge equivalent, so concurrent journal appends"
            " CONFLICT -- resolve by keeping BOTH sides (the journal is append-only"
            " and order does not matter). See 'Team setup (svn)' via cognition_readme."
        )
    return line
