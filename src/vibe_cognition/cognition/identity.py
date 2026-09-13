"""Unified graph identity: resolution, confirmation, and the write gate.

Attribution used to come straight from git config. That breaks for the case this
module exists to fix: a Subversion working copy on a machine with no git identity
resolves to ``email=""``, so every such user attributes to an empty address and
is indistinguishable from every other one.

A write is allowed only when identity is CONFIRMED and the person's committed
profile is COMPLETE (name, email, role, seniority, reports_to -- where "nobody"
is a valid reports_to and the expected answer on a solo project). An unconfirmed
git email is NOT enough: that is the shared-build-account case, where every human
on one machine attributes to whatever address happens to be in git config.

Resolution order (first hit wins):
  1. CONFIRMED -- ``.cognition/local/identity.json``, written by answering once
  2. git config files -- resolves, but does NOT satisfy the gate
  3. OS user (name only, never an email)

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

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from .git_identity import resolve_git_identity
from .local_paths import local_dir, read_path, write_path
from .people_facts import fold_email
from .profiles import SENIORITY_LEVELS
from .svn_identity import _looks_like_email, is_svn_working_copy, svn_username_candidates

logger = logging.getLogger(__name__)

IDENTITY_FILENAME = "identity.json"

SOURCE_CONFIRMED = "confirmed"
SOURCE_GIT = "git"
SOURCE_SVN = "svn"
SOURCE_OS_USER = "os-user"


def _casefold_email(value: str) -> str:
    return fold_email(value)


def is_valid_email(value: str) -> bool:
    """One shared definition of a usable address.

    Suggestions and confirmation must agree: a looser check on one side lets a
    malformed SVN username (``"a b@c.com"``) be offered and then permanently
    confirmed, after which it stamps every future write.
    """
    return _looks_like_email((value or "").strip())


def identity_path(cognition_dir: Path) -> Path:
    """Where to READ the confirmed identity (local/, else legacy)."""
    return read_path(cognition_dir, IDENTITY_FILENAME)


def identity_write_path(cognition_dir: Path) -> Path:
    """Where to WRITE it. Always local/, which one ignore entry covers."""
    return write_path(cognition_dir, IDENTITY_FILENAME)


def read_confirmed_identity(cognition_dir: Path) -> dict[str, str] | None:
    """The confirmed identity for this checkout, or None. Never raises."""
    try:
        raw = identity_path(cognition_dir).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw.lstrip("﻿"))
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
    if not is_valid_email(email):
        return {"error": f"email must be a valid address, got {email!r}"}
    path = identity_write_path(cognition_dir)
    payload = {"name": name, "email": email}
    # Per-process temp name: a fixed one lets two concurrent callers clobber each
    # other's staged file, after which one reports success while the other's
    # content is what actually landed. Covered by the identity.json* ignore glob.
    tmp = path.with_name(f"{IDENTITY_FILENAME}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return {"error": f"could not write {IDENTITY_FILENAME}: {exc}"}
    # Report what is actually on disk, not what we meant to write.
    return read_confirmed_identity(cognition_dir) or payload


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
            if _looks_like_email(username):
                _add(username.split("@", 1)[0], username, SOURCE_SVN)
            else:
                _add(username, "", SOURCE_SVN)
    return out


def can_persist_identity(cognition_dir: Path | str) -> bool:
    """Whether identity could ever be confirmed here.

    A read-only checkout cannot persist the pointer, so no answer from a human
    would help -- the refusal must say that rather than asking five questions
    whose answer cannot be saved (ruling Q4).
    """
    target = local_dir(Path(cognition_dir))
    probe = target / f".identity-probe.{os.getpid()}"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe.touch()
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()
    return True


def require_identity(
    repo_path: Path | str,
    cognition_dir: Path | str,
    missing_profile_fields: list[str] | None = None,
    removed_from_roster: bool = False,
) -> dict[str, Any] | None:
    """Gate for every graph WRITE. Returns None when allowed, else a refusal.

    Three conditions, all required:
      1. identity.json names an email (this checkout is claimed), AND
      2. a committed profile exists for it, AND
      3. that profile is complete -- name, email, role, seniority, reports_to,
         where "nobody" is a valid reports_to.

    An unconfirmed git email does NOT pass; see this module's docstring.

    Args:
        repo_path: repository root, for VCS identity suggestions.
        cognition_dir: the .cognition/ directory.
        missing_profile_fields: which required profile fields are absent, from
            the caller's ProfileRegistry (passed in rather than read here, so
            this module stays free of storage). None means "not checked".
        removed_from_roster: True when this identity was deliberately taken off
            the roster. Same refusal, different message: "your profile is
            incomplete" would be a baffling thing to read when what actually
            happened is that a teammate removed you.

    Returns:
        None when writing is allowed. Otherwise a refusal dict, handed straight
        back to the calling agent by every gated tool:
          `error` -- what is missing and how to fix it, including the exact
              cognition_set_identity call to make.
          `identity_required` -- always True.
          `resolved` -- the identity that DID resolve, with its `source`.
          `suggestions` -- candidates from git config and cached SVN
              credentials; empty on the read-only branch.
          `confirmed` -- bool, whether the checkout is claimed at all. Absent on
              the read-only branch.
          `missing_profile_fields` -- the list passed in, echoed so the agent can
              ask for exactly those. Absent on the read-only branch.
          `removed_from_roster` -- bool, True when this identity was deliberately
              removed rather than never completed. Absent on the read-only branch.
          `read_only` -- True, and ONLY present, when .cognition/ cannot be
              written; that branch asks the human for nothing.
    """
    ident = resolve_identity(repo_path, cognition_dir)
    confirmed = bool(ident.get("confirmed")) and bool(ident.get("email"))
    complete = not missing_profile_fields

    if confirmed and complete:
        return None

    if not can_persist_identity(cognition_dir):
        # Ruling Q4: not "blocked until a human answers" -- unfixable from here.
        return {
            "error": (
                "GRAPH IDENTITY CANNOT BE CONFIRMED IN THIS CHECKOUT -- refusing to "
                "write. .cognition/ is not writable, so the identity file can never "
                "be saved and no answer from a human would help. Recording is "
                "unavailable here; reads still work. Do NOT ask the human to set "
                "an identity -- nothing could persist it."
            ),
            "identity_required": True,
            "read_only": True,
            "resolved": ident,
            "suggestions": [],
        }

    suggestions = identity_suggestions(repo_path, cognition_dir)
    hint = ""
    if suggestions:
        names = ", ".join(
            f"{s['name']}{' <' + s['email'] + '>' if s['email'] else ''} (from {s['source']})"
            for s in suggestions
        )
        hint = f" Candidates found on this machine (confirm, do not assume): {names}."

    if not confirmed:
        what = (
            "GRAPH IDENTITY NOT CONFIRMED -- refusing to write. Every memory must be "
            "attributable to a real person, and this checkout has no confirmed "
            "identity."
            f"{hint}"
        )
    elif removed_from_roster:
        what = (
            f"YOU WERE REMOVED FROM THE ROSTER -- refusing to write. {ident.get('email')} "
            "is confirmed as the identity driving this checkout, but someone took that "
            "person off the project roster (possibly by mistake, possibly because they "
            "left and this checkout is being reused). Nothing was deleted: the record "
            "trail is intact and re-registering restores it."
        )
    else:
        what = (
            "GRAPH PROFILE INCOMPLETE -- refusing to write. Identity is confirmed as "
            f"{ident.get('email')}, but the profile still needs: "
            f"{', '.join(missing_profile_fields or [])}."
        )

    return {
        "error": (
            f"{what} ASK THE HUMAN for their name, work email, role, seniority "
            f"({'|'.join(SENIORITY_LEVELS)}), and who they report to -- \"nobody\" is a "
            "valid and expected answer on a solo project. Then call "
            "cognition_set_identity(name=..., email=..., role=..., seniority=..., "
            "reports_to=...). Do NOT guess on their behalf, do NOT invent an "
            "address, and NEVER use your own agent name."
        ),
        "identity_required": True,
        "confirmed": confirmed,
        "removed_from_roster": removed_from_roster,
        "missing_profile_fields": list(missing_profile_fields or []),
        "resolved": ident,
        "suggestions": suggestions,
    }
