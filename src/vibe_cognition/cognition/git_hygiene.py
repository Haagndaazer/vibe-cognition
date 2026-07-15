"""One-time git hygiene pass for .cognition/ — stdlib-only, no deps.

Runs on every CognitionStorage startup; gated by a content-versioned, git-ignored
sidecar flag so the pass executes exactly ONCE per working copy (idempotent),
except when the schema version is bumped for future writers (triggers one re-run).

Two writes (both idempotent, locked, crash-proof):
  1. repo-root .gitattributes  — .cognition/journal.jsonl merge=union
  2. .cognition/.gitignore     — chromadb/, .git-hygiene-managed, *.lock,
                                 .last-rehydrate.json (local loss-alert flag), and
                                 onboard-declined (local onboarding decline file)

`merge=union` is a MERGE-DRIVER attribute — it only changes 3-way merge resolution
and never participates in checkout/checkin filtering, so adding it does NOT
re-smudge or byte-rewrite the committed journal blob.  This is the exact reason it
is safe where -text (an EOL/filter attribute) is NOT: -text reactivates the C-3
byte-rewrite + duplication scar (nodes 90ee3c1b968c, 54304ecf567c).  The writer
emits ONLY merge=union, never -text.

Opt-out: set VIBE_COGNITION_NO_GIT_HYGIENE=1 (or true/yes/on) to skip the whole
pass (the flag is not written; the pass retries on next start when the env is
cleared).  "0", "false", and empty string do NOT suppress the pass.

Re-arm: delete .cognition/.git-hygiene-managed to make the pass re-run (re-adds
any rule that was removed).
"""

import contextlib
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Bump this integer when a NEW writer is added to ensure every working copy
# re-runs the pass exactly once more to pick up the new rule.
# v2: .last-rehydrate.json added to .cognition/.gitignore (WP-1 loss visibility).
# v3: onboard-declined added to .cognition/.gitignore (WP-TC7 onboarding decline file
#     -- per-machine, must never sync via git any more than the rehydrate flag does).
GIT_HYGIENE_VERSION = 3

_GITATTRIBUTES_MARKER = "# vibe-cognition: append-only journal union-merge (safe to remove)"
_GITATTRIBUTES_RULE = ".cognition/journal.jsonl merge=union"
_GITIGNORE_CHROMADB = "chromadb/"
_GITIGNORE_FLAG = ".git-hygiene-managed"
_GITIGNORE_LOCKS = "*.lock"
# Local-only rehydrate loss-alert flag (storage.REHYDRATE_FLAG_FILENAME) — string
# duplicated here rather than imported to keep this module stdlib-only/standalone.
_GITIGNORE_REHYDRATE = ".last-rehydrate.json"
# Local-only onboarding decline file (prime.ONBOARD_DECLINE_FILENAME) — string
# duplicated here for the same reason as _GITIGNORE_REHYDRATE above.
_GITIGNORE_ONBOARD_DECLINED = "onboard-declined"
_FLAG_FILENAME = ".git-hygiene-managed"

# A lock older than this is assumed stale (leftover from a hard-killed process).
_LOCK_STALE_SECONDS = 60


def _read_flag(cognition_dir: Path) -> int | None:
    """Return the numeric version in the flag file, or None if absent/unreadable."""
    flag_path = cognition_dir / _FLAG_FILENAME
    try:
        return int(flag_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _write_flag(cognition_dir: Path) -> None:
    flag_path = cognition_dir / _FLAG_FILENAME
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


def _needs_gitattributes(gitattributes_path: Path) -> bool:
    """Return True if we need to append our merge=union block.

    Skip only when an existing non-comment journal-path line ALREADY carries a
    merge= token.  If a journal-path line exists WITHOUT merge=, still return True
    (appending a second matching line is legal; git accumulates attributes).
    """
    if not gitattributes_path.exists():
        return True
    try:
        for line in gitattributes_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            tokens = stripped.split()
            if tokens and tokens[0] == ".cognition/journal.jsonl" and any(t.startswith("merge=") for t in tokens[1:]):
                return False
    except OSError:
        return True
    return True


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
        if not _needs_gitattributes(gitattributes_path):
            return True
        existing = ""
        if gitattributes_path.exists():
            try:
                existing = gitattributes_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.debug("git-hygiene: cannot read .gitattributes: %s", exc)
                return False
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        block = f"{prefix}{_GITATTRIBUTES_MARKER}\n{_GITATTRIBUTES_RULE}\n"
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
    """Ensure .cognition/.gitignore contains chromadb/, .git-hygiene-managed, *.lock,
    .last-rehydrate.json, and onboard-declined.
    Returns True if the file is correct after the call (success or already-present)."""
    gitignore_path = cognition_dir / ".gitignore"
    lock = cognition_dir / ".gitignore.lock"
    if not _acquire_lock(lock):
        return False
    try:
        need_chromadb = _needs_gitignore_entry(gitignore_path, _GITIGNORE_CHROMADB, "chromadb")
        need_flag = _needs_gitignore_entry(gitignore_path, _GITIGNORE_FLAG, _GITIGNORE_FLAG)
        need_locks = _needs_gitignore_entry(gitignore_path, _GITIGNORE_LOCKS, _GITIGNORE_LOCKS)
        need_rehydrate = _needs_gitignore_entry(
            gitignore_path, _GITIGNORE_REHYDRATE, _GITIGNORE_REHYDRATE
        )
        need_onboard_declined = _needs_gitignore_entry(
            gitignore_path, _GITIGNORE_ONBOARD_DECLINED, _GITIGNORE_ONBOARD_DECLINED
        )
        if not any((need_chromadb, need_flag, need_locks, need_rehydrate, need_onboard_declined)):
            return True

        if not gitignore_path.exists():
            lines_to_add = ["# vibe-cognition managed - do not remove"]
            if need_chromadb:
                lines_to_add.append(_GITIGNORE_CHROMADB)
            if need_flag:
                lines_to_add.append(_GITIGNORE_FLAG)
            if need_locks:
                lines_to_add.append(_GITIGNORE_LOCKS)
            if need_rehydrate:
                lines_to_add.append(_GITIGNORE_REHYDRATE)
            if need_onboard_declined:
                lines_to_add.append(_GITIGNORE_ONBOARD_DECLINED)
            try:
                gitignore_path.write_text("\n".join(lines_to_add) + "\n", encoding="utf-8")
            except OSError as exc:
                logger.debug("git-hygiene: cannot write .cognition/.gitignore: %s", exc)
                return False
            return True

        try:
            existing = gitignore_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("git-hygiene: cannot read .cognition/.gitignore: %s", exc)
            return False

        lines_to_add = []
        if need_chromadb:
            lines_to_add.append(_GITIGNORE_CHROMADB)
        if need_flag:
            lines_to_add.append(_GITIGNORE_FLAG)
        if need_locks:
            lines_to_add.append(_GITIGNORE_LOCKS)
        if need_rehydrate:
            lines_to_add.append(_GITIGNORE_REHYDRATE)
        if need_onboard_declined:
            lines_to_add.append(_GITIGNORE_ONBOARD_DECLINED)

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


def _opt_out() -> bool:
    """Return True if VIBE_COGNITION_NO_GIT_HYGIENE is set to a truthy value.

    Only "1", "true", "yes", "on" (case-insensitive) suppress the pass.
    "0", "false", "no", "off", and the empty string do NOT suppress it.
    """
    val = os.environ.get("VIBE_COGNITION_NO_GIT_HYGIENE", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def ensure_git_hygiene(repo_path: Path, cognition_dir: Path) -> None:
    """Run the one-time git hygiene pass.  Never raises — all failures are logged + swallowed.

    Args:
        repo_path: Repository root (must contain .git to be acted on).
        cognition_dir: .cognition/ directory (flag lives here).
    """
    if _opt_out():
        return

    git_root = repo_path / ".git"
    if not git_root.exists():
        return

    flag_version = _read_flag(cognition_dir)
    if flag_version is not None and flag_version >= GIT_HYGIENE_VERSION:
        return

    gitattributes_path = repo_path / ".gitattributes"
    ga_ok = True
    gi_ok = True

    if _needs_gitattributes(gitattributes_path):
        ga_ok = _write_gitattributes(gitattributes_path, cognition_dir)
    # else already present — counts as resolved

    gi_ok = _write_gitignore(cognition_dir)

    if ga_ok and gi_ok:
        try:
            _write_flag(cognition_dir)
        except OSError as exc:
            logger.debug("git-hygiene: cannot write flag: %s", exc)


def check_hygiene_state(repo_path: Path, cognition_dir: Path) -> dict:
    """Read-only check of what git-hygiene rules are in place.  For prime.py announce.

    Returns a dict with boolean keys:
      - gitattr_configured: our marker is present in .gitattributes
      - gitignore_configured: chromadb/ is present in .cognition/.gitignore
    Never raises.
    """
    result = {"gitattr_configured": False, "gitignore_configured": False}
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
                if stripped in (_GITIGNORE_CHROMADB, "chromadb"):
                    result["gitignore_configured"] = True
                    break
    except OSError:
        pass
    return result


def format_hygiene_announce(state: dict) -> str:
    """Format a one-line announce string from check_hygiene_state output, or empty string."""
    parts = []
    if state.get("gitattr_configured"):
        parts.append("journal union-merge (.gitattributes)")
    if state.get("gitignore_configured"):
        parts.append("chromadb ignore (.cognition/.gitignore)")
    if not parts:
        return ""
    return "vibe-cognition configured: " + ", ".join(parts) + "."
