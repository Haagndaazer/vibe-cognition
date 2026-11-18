"""Server-side SVN identity resolution, file-read only.

Mirrors git_identity.py's hard rule: NEVER shell out. `svn.exe` in a detached
MCP server is the same hang risk that cost us v0.12.1 (git_identity.py's WHY NO
SUBPROCESS note) -- a piped stdout that never closes blocks subprocess forever
and the timeout cannot fire. Everything here is a filesystem read.

SVN has no identity config equivalent to git's `[user] email`. What it has is a
cached CREDENTIAL per realm, whose username may or may not be an email address.
That makes SVN a SUGGESTION source only, never authoritative -- consistent with
the standing no-auto-stamp ruling (decision 833e9f67de4d).
"""

import contextlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_AUTH_SUBDIR = ("auth", "svn.simple")
_MAX_CRED_FILES = 50
_MAX_CRED_BYTES = 64 * 1024


def _config_root() -> Path | None:
    """SVN's per-user config dir, or None. Honors SVN_CONFIG_DIR (svn's --config-dir env)."""
    override = os.environ.get("SVN_CONFIG_DIR")
    if override:
        return Path(override)
    try:
        home = Path.home()
    except Exception as exc:  # noqa: BLE001 - Path.home() raises with no home env
        logger.debug("svn_identity: Path.home() failed: %s", exc)
        return None
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Subversion"
    return home / ".subversion"


def _parse_hash_dump(text: str) -> dict[str, str]:
    """Parse svn's hash-dump credential format into a dict.

    Records are `K <len>\\n<key>\\nV <len>\\n<value>\\n`, terminated by `END`.
    Lengths are advisory here: we read whole lines, which is correct for the
    single-line keys and values svn writes, and cannot desync on a malformed
    file the way offset arithmetic would. Tolerant: garbage is skipped.
    """
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line == "END":
            break
        if line.startswith("K ") and i + 3 < len(lines) and lines[i + 2].startswith("V "):
            out[lines[i + 1]] = lines[i + 3]
            i += 4
            continue
        i += 1
    return out


def _read_credential(path: Path) -> dict[str, str]:
    """Read one cached credential file. Unreadable/oversized -> {}. Never raises."""
    try:
        if path.stat().st_size > _MAX_CRED_BYTES:
            return {}
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    try:
        return _parse_hash_dump(text)
    except Exception as exc:  # noqa: BLE001 - defensive: a parse must never surface
        logger.debug("svn_identity: parsing %s failed: %s", path, exc)
        return {}


def is_svn_working_copy(repo_path: Path | str) -> bool:
    """True when repo_path is an SVN working copy root (.svn exists only there since 1.7)."""
    try:
        return (Path(repo_path) / ".svn").exists()
    except OSError:
        return False


def svn_username_candidates() -> list[dict[str, str]]:
    """Every cached SVN username on this machine, newest credential first.

    Returns dicts of ``{"username": str, "realm": str}``. Empty list when SVN is
    not configured here. Never raises.
    """
    root = _config_root()
    if root is None:
        return []
    auth_dir = root.joinpath(*_AUTH_SUBDIR)
    try:
        entries = [p for p in auth_dir.iterdir() if p.is_file()]
    except OSError:
        return []
    with contextlib.suppress(OSError):
        entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, str]] = []
    for path in entries[:_MAX_CRED_FILES]:
        fields = _read_credential(path)
        username = (fields.get("username") or "").strip()
        if username:
            out.append({"username": username, "realm": (fields.get("svn:realmstring") or "").strip()})
    return out


def resolve_svn_identity(repo_path: Path | str) -> dict[str, str]:
    """Best-effort SVN identity for a working copy. NEVER shells out, NEVER raises.

    Returns ``{"name": str, "email": str}`` with either possibly empty. An SVN
    username is only treated as an email when it actually looks like one; a bare
    login (``jsmith``) yields a name with no email, which the caller must treat as
    insufficient rather than inventing an address.

    Scans ALL cached credentials for one that looks like an email rather than
    judging only the most recent: a machine whose newest credential is a bare
    login would otherwise report "no identity" while a usable address sat in an
    older entry.
    """
    if not is_svn_working_copy(repo_path):
        return {"name": "", "email": ""}
    candidates = svn_username_candidates()
    for cand in candidates:
        username = cand["username"]
        if _looks_like_email(username):
            return {"name": username.split("@", 1)[0], "email": username}
    if candidates:
        return {"name": candidates[0]["username"], "email": ""}
    return {"name": "", "email": ""}


def _looks_like_email(value: str) -> bool:
    """Conservative: exactly one @, non-empty both sides, a dot in the domain, no spaces."""
    if value.count("@") != 1 or any(c.isspace() for c in value):
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")
