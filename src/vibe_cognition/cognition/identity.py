"""Unified graph identity: resolution, confirmation, and the write gate.

Attribution used to come straight from git config. That breaks for the case this
module exists to fix: a Subversion working copy on a machine with no git identity
resolves to ``email=""``, so every such user attributes to an empty address and
is indistinguishable from every other one.

Resolution order (first hit wins):
  1. CONFIRMED -- ``.cognition/identity.json``, written by the user answering once
  2. git config files
  3. OS user (name only, never an email -- so writes stay gated)

SVN credentials are SUGGESTION-ONLY and never stamp a write: the auth cache is
machine-wide and realm-keyed, and correlating a realm to this working copy is not
attempted, so trusting it could attribute this repo's history via another
project's credential. SVN users confirm once with ``cognition_set_identity``.

The confirmed file is MACHINE-LOCAL and must never be committed: it says who is
driving THIS checkout, not who exists in the project. The shared roster stays the
person nodes in the graph.

Only VCS sources may be *suggested* to the user; nothing here ever stamps an
inferred identity on its own (decision 833e9f67de4d).
"""

import json
import logging
from pathlib import Path
from typing import Any

from .git_identity import resolve_git_identity
from .svn_identity import is_svn_working_copy, svn_username_candidates

logger = logging.getLogger(__name__)

IDENTITY_FILENAME = "identity.json"

SOURCE_CONFIRMED = "confirmed"
SOURCE_GIT = "git"
SOURCE_SVN = "svn"
SOURCE_OS_USER = "os-user"


def _casefold_email(value: str) -> str:
    return (value or "").strip().casefold()


def identity_path(cognition_dir: Path) -> Path:
    return Path(cognition_dir) / IDENTITY_FILENAME


def read_confirmed_identity(cognition_dir: Path) -> dict[str, str] | None:
    """The confirmed identity for this checkout, or None. Never raises."""
    try:
        raw = identity_path(cognition_dir).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.debug("identity: %s is not valid JSON; ignoring", IDENTITY_FILENAME)
        return None
    if not isinstance(data, dict):
        return None
    email = _casefold_email(str(data.get("email", "")))
    name = str(data.get("name", "")).strip()
    if not email or not name:
        return None
    return {"name": name, "email": email}


def write_confirmed_identity(cognition_dir: Path, name: str, email: str) -> dict[str, Any]:
    """Persist the confirmed identity. Returns an error dict on failure, else the identity."""
    name = (name or "").strip()
    email = _casefold_email(email)
    if not name:
        return {"error": "name must not be blank"}
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        return {"error": f"email must be a valid address, got {email!r}"}
    path = identity_path(cognition_dir)
    payload = {"name": name, "email": email}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return {"error": f"could not write {IDENTITY_FILENAME}: {exc}"}
    return payload


def resolve_identity(repo_path: Path | str, cognition_dir: Path | str) -> dict[str, Any]:
    """Resolve the acting identity. NEVER shells out, NEVER raises.

    Returns ``{"name", "email", "source", "confirmed"}``. ``email`` may be "" when
    nothing resolvable was found -- callers that write MUST gate on that via
    ``require_identity``.
    """
    cognition_dir = Path(cognition_dir)

    confirmed = read_confirmed_identity(cognition_dir)
    if confirmed is not None:
        return {**confirmed, "source": SOURCE_CONFIRMED, "confirmed": True}

    git = resolve_git_identity(repo_path)
    git_email = _casefold_email(git.get("email", ""))
    if git_email:
        return {
            "name": git.get("name") or git_email,
            "email": git_email,
            "source": SOURCE_GIT,
            "confirmed": False,
        }

    # SVN is deliberately SUGGESTION-ONLY and never stamps a write. Its auth cache
    # is machine-wide and realm-keyed, and nothing here correlates a realm to this
    # working copy's repository -- so someone who touches several SVN servers would
    # otherwise have this repo's history silently attributed via another project's
    # credential. A plausible-looking misattribution is worse than the empty-address
    # bug this module exists to fix, so an SVN user confirms once via
    # cognition_set_identity and is authoritative from then on. See
    # identity_suggestions(), which DOES surface SVN candidates for that prompt.
    return {
        "name": git.get("name") or "unknown",
        "email": "",
        "source": SOURCE_OS_USER,
        "confirmed": False,
    }


def identity_suggestions(repo_path: Path | str, cognition_dir: Path | str) -> list[dict[str, str]]:
    """Candidate identities to offer the human, best first. Suggestions only."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(name: str, email: str, source: str) -> None:
        key = _casefold_email(email) or f"name:{name}"
        if key in seen or not (name or email):
            return
        seen.add(key)
        out.append({"name": name, "email": _casefold_email(email), "source": source})

    git = resolve_git_identity(repo_path)
    if git.get("email"):
        _add(git.get("name", ""), git["email"], SOURCE_GIT)
    if is_svn_working_copy(repo_path):
        for cand in svn_username_candidates():
            username = cand["username"]
            if "@" in username:
                _add(username.split("@", 1)[0], username, SOURCE_SVN)
            else:
                _add(username, "", SOURCE_SVN)
    return out


def require_identity(repo_path: Path | str, cognition_dir: Path | str) -> dict[str, Any] | None:
    """Gate for every graph WRITE. Returns None when allowed, else an error dict.

    Blocks when no email resolves -- the state which silently attributes work to
    nobody. An unconfirmed GIT email passes (blocking it would break every working
    install on upgrade for no correctness gain). An SVN-only machine does NOT pass:
    its credentials are suggestion-only, so those users confirm once.
    """
    ident = resolve_identity(repo_path, cognition_dir)
    if ident.get("email"):
        return None

    suggestions = identity_suggestions(repo_path, cognition_dir)
    hint = ""
    if suggestions:
        names = ", ".join(
            f"{s['name']}{' <' + s['email'] + '>' if s['email'] else ''} (from {s['source']})"
            for s in suggestions
        )
        hint = f" Candidates found on this machine: {names}."
    return {
        "error": (
            "GRAPH IDENTITY NOT SET -- refusing to write. Every memory must be "
            "attributable to a person, and no email address could be resolved from "
            "git config, and SVN credentials are suggestion-only."
            f"{hint}"
            " ASK THE HUMAN for their name and work email, then call "
            "cognition_set_identity(name=..., email=...). Do NOT guess on their "
            "behalf and do NOT invent an address."
        ),
        "identity_required": True,
        "resolved": ident,
        "suggestions": suggestions,
    }
