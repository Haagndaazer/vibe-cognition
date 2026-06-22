"""One-time git hygiene pass for .cognition/ — stdlib-only, no deps.

Runs on every CognitionStorage startup; gated by a content-versioned, git-ignored
sidecar flag so the pass executes exactly ONCE per working copy (idempotent),
except when the schema version is bumped for future writers (triggers one re-run).

Two writes (both idempotent, locked, crash-proof):
  1. repo-root .gitattributes  — .cognition/journal.jsonl merge=union
  2. .cognition/.gitignore     — chromadb/ and .git-hygiene-managed

`merge=union` is a MERGE-DRIVER attribute — it only changes 3-way merge resolution
and never participates in checkout/checkin filtering, so adding it does NOT
re-smudge or byte-rewrite the committed journal blob.  This is the exact reason it
is safe where -text (an EOL/filter attribute) is NOT: -text reactivates the C-3
byte-rewrite + duplication scar (nodes 90ee3c1b968c, 54304ecf567c).  The writer
emits ONLY merge=union, never -text.

Opt-out: set VIBE_COGNITION_NO_GIT_HYGIENE to any truthy value to skip the whole
pass (the flag is not written; the pass retries on next start when the env is
cleared).

Re-arm: delete .cognition/.git-hygiene-managed to make the pass re-run (re-adds
any rule that was removed).
"""

import contextlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Bump this integer when a NEW writer is added to ensure every working copy
# re-runs the pass exactly once more to pick up the new rule.
GIT_HYGIENE_VERSION = 1

_GITATTRIBUTES_MARKER = "# vibe-cognition: append-only journal union-merge (safe to remove)"
_GITATTRIBUTES_RULE = ".cognition/journal.jsonl merge=union"
_GITIGNORE_CHROMADB = "chromadb/"
_GITIGNORE_FLAG = ".git-hygiene-managed"
_FLAG_FILENAME = ".git-hygiene-managed"


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


def _lock_path(target: Path) -> Path:
    return target.parent / (target.name + ".lock")


def _acquire_lock(lock_path: Path) -> bool:
    """Try to create the lock file exclusively. Returns True if acquired."""
    try:
        lock_path.open("x").close()
        return True
    except (FileExistsError, OSError):
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


def _write_gitattributes(gitattributes_path: Path) -> bool:
    """Append the marker + rule block to .gitattributes.  Returns True on success."""
    lock = _lock_path(gitattributes_path)
    if not _acquire_lock(lock):
        return False
    try:
        existing = ""
        if gitattributes_path.exists():
            try:
                existing = gitattributes_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.debug("git-hygiene: cannot read .gitattributes: %s", exc)
                return False
        prefix = ""
        if existing and not existing.endswith("\n"):
            prefix = "\n"
        block = f"{prefix}{_GITATTRIBUTES_MARKER}\n{_GITATTRIBUTES_RULE}\n"
        try:
            with gitattributes_path.open("a", encoding="utf-8") as fh:
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
    """Ensure .cognition/.gitignore contains chromadb/ and .git-hygiene-managed.
    Returns True if the file is correct after the call (success or already-present)."""
    gitignore_path = cognition_dir / ".gitignore"
    lock = _lock_path(gitignore_path)
    if not _acquire_lock(lock):
        return False
    try:
        need_chromadb = _needs_gitignore_entry(gitignore_path, _GITIGNORE_CHROMADB, "chromadb")
        need_flag = _needs_gitignore_entry(gitignore_path, _GITIGNORE_FLAG, _GITIGNORE_FLAG)
        if not need_chromadb and not need_flag:
            return True

        if not gitignore_path.exists():
            lines_to_add = ["# vibe-cognition managed — do not remove"]
            if need_chromadb:
                lines_to_add.append(_GITIGNORE_CHROMADB)
            if need_flag:
                lines_to_add.append(_GITIGNORE_FLAG)
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

        prefix = ""
        if existing and not existing.endswith("\n"):
            prefix = "\n"
        addition = prefix + "\n".join(lines_to_add) + "\n"
        try:
            with gitignore_path.open("a", encoding="utf-8") as fh:
                fh.write(addition)
        except OSError as exc:
            logger.debug("git-hygiene: cannot append to .cognition/.gitignore: %s", exc)
            return False
        return True
    finally:
        _release_lock(lock)


def ensure_git_hygiene(repo_path: Path, cognition_dir: Path) -> None:
    """Run the one-time git hygiene pass.  Never raises — all failures are logged + swallowed.

    Args:
        repo_path: Repository root (must contain .git to be acted on).
        cognition_dir: .cognition/ directory (flag lives here).
    """
    if os.environ.get("VIBE_COGNITION_NO_GIT_HYGIENE", "").strip():
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
        ga_ok = _write_gitattributes(gitattributes_path)
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
