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
driving THIS checkout, not who exists in the project. The shared roster is the
committed profiles under `.cognition/people/`.

Only VCS sources may be *suggested* to the user; nothing here ever stamps an
inferred identity on its own (decision 833e9f67de4d).
"""

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from .checkout_binding import (
    MISMATCH_ACCOUNT,
    MISMATCH_FOLDER,
    binding_mismatch,
    current_binding,
    is_bound,
)
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
SOURCE_IDENTITY_FILE = "identity-file"

#: What an identity file on disk turned out to be. Only BOUND_OK is trusted.
FILE_ABSENT = "absent"
FILE_INVALID = "invalid"
FILE_BOUND_OK = "ok"
FILE_UNBOUND = "unbound"
FILE_FOREIGN = "foreign"


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


def inspect_confirmed_identity(cognition_dir: Path | str) -> dict[str, Any]:
    """What the identity file on disk is, and whether it belongs to THIS checkout.

    The file is machine-local only by ignore-rule convention, and conventions leak:
    SVN never reads .gitignore, so `svn add --force` commits it, and anyone
    copying a project folder copies it. A teammate who then updates would write AS
    the file's author, stamped confirmed -- worse than the empty-address bug this
    module exists to fix, because the misattribution looks verified.

    So the file records the machine, OS account and checkout folder it was written
    for (see checkout_binding), and is trusted only where all three still match.
    Never raises.

    Returns `{"status", "identity", "machine", "mismatch"}`: `status` is one of
    FILE_ABSENT, FILE_INVALID, FILE_BOUND_OK (the only trusted state), FILE_UNBOUND
    (written before binding existed, so unverifiable) or FILE_FOREIGN (written for
    a different machine, account or folder); `identity` is the `{name, email}` it
    names, when readable; `machine` is the machine it was written on, when
    recorded; `mismatch` is which part differs, for FILE_FOREIGN.
    """
    result: dict[str, Any] = {
        "status": FILE_ABSENT, "identity": None, "machine": None, "mismatch": None,
    }
    try:
        raw = identity_path(Path(cognition_dir)).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return result
    try:
        data = json.loads(raw.lstrip("﻿"))
    except (json.JSONDecodeError, ValueError):
        logger.debug("identity: %s is not valid JSON; ignoring", IDENTITY_FILENAME)
        result["status"] = FILE_INVALID
        return result
    if not isinstance(data, dict):
        result["status"] = FILE_INVALID
        return result
    email = _casefold_email(str(data.get("email", "")))
    name = str(data.get("name", "")).strip()
    if not email or not name:
        result["status"] = FILE_INVALID
        return result
    result["identity"] = {"name": name, "email": email}

    if not is_bound(data):
        result["status"] = FILE_UNBOUND
        return result
    result["machine"] = data["machine"]
    mismatch = binding_mismatch(data, cognition_dir)
    result["status"] = FILE_BOUND_OK if mismatch is None else FILE_FOREIGN
    result["mismatch"] = mismatch
    return result


def read_confirmed_identity(cognition_dir: Path) -> dict[str, str] | None:
    """The confirmed identity for THIS checkout, or None. Never raises.

    None also for a file that names someone but belongs to another machine or
    folder, or predates binding -- see inspect_confirmed_identity.
    """
    info = inspect_confirmed_identity(cognition_dir)
    return info["identity"] if info["status"] == FILE_BOUND_OK else None


def write_confirmed_identity(cognition_dir: Path, name: str, email: str) -> dict[str, Any]:
    """Persist the confirmed identity. Returns an error dict on failure, else the identity."""
    name = (name or "").strip()
    email = _casefold_email(email)
    if not name:
        return {"error": "name must not be blank"}
    if not is_valid_email(email):
        return {"error": f"email must be a valid address, got {email!r}"}
    path = identity_write_path(cognition_dir)
    payload = {"name": name, "email": email, **current_binding(cognition_dir)}
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
    return read_confirmed_identity(cognition_dir) or {"name": name, "email": email}


def resolve_identity(repo_path: Path | str, cognition_dir: Path | str) -> dict[str, Any]:
    """Resolve the acting identity. NEVER shells out, NEVER raises.

    Returns ``{"name", "email", "source", "confirmed"}``. ``email`` may be "" when
    nothing resolvable was found -- callers that write MUST gate on that via
    ``require_identity``. When an identity file exists but is not trusted here, an
    ``identity_file`` key says why (`status`, and who it names).
    """
    cognition_dir = Path(cognition_dir)

    info = inspect_confirmed_identity(cognition_dir)
    if info["status"] == FILE_BOUND_OK:
        return {**info["identity"], "source": SOURCE_CONFIRMED, "confirmed": True}
    untrusted: dict[str, Any] = {}
    if info["status"] in (FILE_UNBOUND, FILE_FOREIGN):
        untrusted = {"identity_file": {
            "status": info["status"], **info["identity"],
            "machine": info["machine"], "mismatch": info["mismatch"],
        }}

    git = resolve_git_identity(repo_path)
    git_email = _casefold_email(git.get("email", ""))
    if git_email:
        return {
            "name": git.get("name") or git_email,
            "email": git_email,
            "source": SOURCE_GIT,
            "confirmed": False,
            **untrusted,
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
        **untrusted,
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

    info = inspect_confirmed_identity(Path(cognition_dir))
    if info["status"] == FILE_UNBOUND and info["identity"]:
        # Most likely the same person who confirmed before binding existed, so
        # offered first -- but still only a candidate: it could also have leaked.
        _add(info["identity"]["name"], info["identity"]["email"], SOURCE_IDENTITY_FILE)

    git = resolve_git_identity(repo_path)
    if git.get("email"):
        _add(git.get("name", ""), git["email"], SOURCE_GIT)
    if info["status"] == FILE_FOREIGN and info["identity"]:
        # Offered LAST: written for another machine or folder, so more likely a
        # teammate's file that travelled than this person's own.
        _add(info["identity"]["name"], info["identity"]["email"], SOURCE_IDENTITY_FILE)
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


def _stop_it_travelling(repo_path: Path | str) -> str:
    """The exact command that takes the identity file out of version control."""
    rel = ".cognition/local/identity.json"
    svn = f"`svn rm --keep-local {rel}`"
    git = f"`git rm --cached {rel}`"
    root = Path(repo_path)
    if is_svn_working_copy(root):
        how = f"run {svn}"
    elif (root / ".git").exists():
        how = f"run {git}"
    else:
        how = f"run {svn} (SVN) or {git} (git)"
    return (
        f"If it is under version control, {how} so it stops travelling, and make sure "
        ".cognition/local is ignored."
    )


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

    travelled = ident.get("identity_file") or {}
    if not confirmed and travelled.get("status") == FILE_FOREIGN:
        named = f"{travelled.get('name')} <{travelled.get('email')}>"
        where = {
            MISMATCH_FOLDER: (
                "for a DIFFERENT FOLDER on this machine (the project was copied, or a "
                "second checkout received it through version control)"
            ),
            MISMATCH_ACCOUNT: "by a DIFFERENT OS ACCOUNT on this machine",
        }.get(
            str(travelled.get("mismatch")),
            f"on a DIFFERENT MACHINE ({travelled.get('machine')})",
        )
        what = (
            "GRAPH IDENTITY NOT CONFIRMED -- refusing to write. This checkout has an "
            f"identity file naming {named}, but it was written {where}, so it is NOT "
            "trusted here: an identity file that arrived through version control or a "
            "copied folder would otherwise make everyone who receives it write as that "
            f"person. {_stop_it_travelling(repo_path)} Then confirm who is ACTUALLY "
            f"driving this checkout -- which may or may not be {named}. If it is them "
            "and their profile is already complete, name and email alone are enough."
            f"{hint}"
        )
    elif not confirmed and travelled.get("status") == FILE_UNBOUND:
        named = f"{travelled.get('name')} <{travelled.get('email')}>"
        what = (
            "GRAPH IDENTITY NEEDS RE-CONFIRMING -- refusing to write. This checkout's "
            f"identity file names {named} but predates machine binding, so it cannot "
            "be verified as belonging to this checkout rather than having arrived "
            "through version control. This is a one-time step after upgrading. If "
            f"{named} really is who is driving, confirm it once; when their profile "
            "is already complete, name and email alone are enough and the result "
            "says if anything is still missing."
            f"{hint}"
        )
    elif not confirmed:
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
