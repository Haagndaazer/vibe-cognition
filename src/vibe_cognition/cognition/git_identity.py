"""Server-side git identity resolution for task attribution.

A ``task`` node's ``created_by`` (and each status transition's ``by``) MUST be
resolved server-side from the repo-local git config — NEVER trusted from a
client-supplied value (decision d1192f7e7bf8). There is no ``created_by`` tool
parameter; the client cannot override it.

This is net-new code. ``git_hygiene.py`` is pure-filesystem (zero subprocess), so
it is NOT the precedent — the git-shelling precedent is ``hooks/post-commit.py``,
which runs ``git -C <repo> ...`` with ``capture_output``/``text``/``encoding`` and a
short timeout, and never raises. We mirror that here.
"""

import getpass
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Match the post-commit hook's git timeout — a hung git must never block a tool call.
_GIT_TIMEOUT_S = 5


def _git_config(repo_path: Path | str, key: str) -> str:
    """Read one repo-local git config value, or "" on any failure.

    ``git -C <repo> config <key>`` reads the repo-local identity (the value that would
    author a commit here), which is exactly what we want. Returns "" for an unset key,
    a non-zero exit, or any subprocess error — never raises (mirrors post-commit.py).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "config", key],
            capture_output=True, text=True, encoding="utf-8", timeout=_GIT_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - never let git failure surface to the caller
        logger.debug("resolve_git_identity: git config %s failed: %s", key, exc)
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def resolve_git_identity(repo_path: Path | str) -> dict[str, str]:
    """Resolve the committer identity for task attribution. NEVER trusts a client value.

    Fallback chain (never hard-fails):
      1. git config ``user.name`` / ``user.email`` (repo-local)
      2. name unset → OS user (``getpass.getuser()``)
      3. total failure → ``"unknown"``

    Args:
        repo_path: The repo root (``REPO_PATH`` / ``CLAUDE_PROJECT_DIR``, already
            resolved in config.py; in the tool we derive it from the storage dir).

    Returns:
        ``{"name": <str>, "email": <str>}`` — ``name`` is always non-empty; ``email``
        may be "" when git has no email configured (the OS user has no email).
    """
    name = _git_config(repo_path, "user.name")
    email = _git_config(repo_path, "user.email")
    if not name:
        try:
            name = getpass.getuser()
        except Exception as exc:  # noqa: BLE001 - getpass can raise if no user env is set
            logger.debug("resolve_git_identity: getpass.getuser() failed: %s", exc)
            name = ""
    return {"name": name or "unknown", "email": email or ""}
