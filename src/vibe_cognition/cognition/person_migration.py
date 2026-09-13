"""Migrate legacy `person` nodes to committed profiles.

Two phases, deliberately separate.

PHASE 1 (automatic, versioned, at startup) writes a profile for every person node
that has no profile yet. It has to be automatic: the write gate reads PROFILES, not
the roster, so until a person node becomes a profile its owner is asked all five
onboarding questions again — exactly what migration exists to prevent. Anything
manual leaves a window between upgrading and someone remembering to run it.

PHASE 2 (manual) removes the nodes. It stays separate because an interruption then
leaves duplicates, which are harmless — the roster prefers the profile — instead of
a half-deleted roster. Removal also has to purge embeddings, which is the tool
layer's job (`cognition_remove_node`), not this module's: a bare
`storage.remove_node` leaves stale vectors that surface dead ids in search and 404
on get_node.

`reports_to_email: ""` becomes `"nobody"`. Without that translation every migrated
solo owner fails the gate the moment they upgrade, which is the case the ruling
calls the expected answer.

Never raises. A migration that crashes a session start is worse than one that
reports it did nothing.
"""

import logging
from pathlib import Path
from typing import Any

from .local_paths import read_path, write_path
from .models import CognitionNodeType
from .profiles import NO_MANAGER, SENIORITY_LEVELS

logger = logging.getLogger(__name__)

PERSON_MIGRATION_VERSION = 1

MIGRATION_FLAG_FILENAME = ".person-migration"


def _read_flag(cognition_dir: Path) -> int | None:
    try:
        raw = read_path(cognition_dir, MIGRATION_FLAG_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _write_flag(cognition_dir: Path) -> None:
    write_path(cognition_dir, MIGRATION_FLAG_FILENAME).write_text(
        str(PERSON_MIGRATION_VERSION), encoding="utf-8"
    )


def _person_nodes(storage: Any) -> list[dict[str, Any]]:
    try:
        return list(storage.get_nodes_by_type(CognitionNodeType.PERSON))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("person-migration: cannot list person nodes: %s", exc)
        return []


def _fields_from_node(node: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """(email, profile fields) for one person node, or None if unusable.

    A node with no email cannot become a profile: email IS the identity key, and
    inventing one would attribute someone's history to an address they never used.
    Such a node is reported and left alone for a human to fix.
    """
    info = (node.get("metadata") or {}).get("person") or {}
    email = str(info.get("email") or "").strip().casefold()
    if not email:
        return None

    fields: dict[str, Any] = {"email": email}
    if name := str(info.get("name") or "").strip():
        fields["name"] = name
    if role := str(info.get("role") or "").strip():
        fields["role"] = role
    seniority = str(info.get("seniority") or "").strip().casefold()
    if seniority in SENIORITY_LEVELS:
        fields["seniority"] = seniority
    manager = str(info.get("reports_to_email") or "").strip().casefold()
    # "" meant top-of-chain on a node. Carried across verbatim it would read as
    # "never answered" and hold the gate closed on every migrated solo owner.
    fields["reports_to"] = manager or NO_MANAGER
    if detail := str(node.get("detail") or "").strip():
        fields["detail"] = detail
    return email, fields


def migrate_person_nodes(storage: Any) -> dict[str, Any]:
    """Phase 1. Write a profile for every person node that lacks one.

    Idempotent: an email that already has a profile is skipped, so a second run
    finds nothing to do and a node left behind by an interrupted phase 2 does not
    resurrect anything. Returns a report; never raises.
    """
    report: dict[str, Any] = {
        "migrated": [], "skipped_existing": [], "skipped_no_email": [], "errors": [],
    }
    nodes = _person_nodes(storage)
    if not nodes:
        return report

    try:
        existing = set(storage.profile_emails())
    except Exception as exc:  # pragma: no cover - defensive
        report["errors"].append(f"cannot read profiles: {exc}")
        return report

    for node in nodes:
        parsed = _fields_from_node(node)
        if parsed is None:
            report["skipped_no_email"].append(node.get("id"))
            continue
        email, fields = parsed
        if email in existing:
            report["skipped_existing"].append(email)
            continue
        # `by` is the person themselves: this is their own node being carried
        # across, not someone editing their profile. Attributing it to whoever
        # happened to start the session would put a stranger in their audit trail.
        by = {"name": fields.get("name") or email, "email": email}
        try:
            result = storage.set_profile_fields(email, fields, by, from_agent=False)
        except Exception as exc:
            report["errors"].append(f"{email}: {exc}")
            continue
        if "error" in result:
            report["errors"].append(f"{email}: {result['error']}")
            continue
        existing.add(email)
        report["migrated"].append(email)

    _carry_history(storage, report["migrated"], nodes)
    return report


def _carry_history(storage: Any, migrated: list[str], nodes: list[dict[str, Any]]) -> None:
    """Copy each migrated node's profile_history across as a record.

    The old shape is `{changed: {field: {from, to}}, at, by}` — one entry per
    update call, not per field — so it cannot be replayed as profile_set records
    without inventing timestamps that would then compete with real ones in the
    last-write-wins fold. It is preserved verbatim under a `profile_history_legacy`
    action instead: readable, never folded, and never able to overwrite a current
    value.
    """
    if not migrated:
        return
    wanted = set(migrated)
    for node in nodes:
        info = (node.get("metadata") or {}).get("person") or {}
        email = str(info.get("email") or "").strip().casefold()
        if email not in wanted:
            continue
        entries = (node.get("metadata") or {}).get("profile_history") or []
        if not entries:
            continue
        try:
            storage.append_legacy_profile_history(email, entries, node.get("id") or "")
        except Exception as exc:
            logger.debug("person-migration: cannot carry history for %s: %s", email, exc)


def ensure_person_migration(storage: Any) -> dict[str, Any] | None:
    """Run phase 1 once per graph. Returns the report when it ran, else None.

    Versioned like the git-hygiene pass so it cannot re-run every startup, and
    flag-gated BEFORE the node scan so a migrated graph costs one small file read.
    """
    cognition_dir = Path(storage.cognition_dir)
    flag = _read_flag(cognition_dir)
    if flag is not None and flag >= PERSON_MIGRATION_VERSION:
        return None

    try:
        report = migrate_person_nodes(storage)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("person-migration: failed: %s", exc)
        return None

    # The flag is written even when nothing was migrated: "no person nodes" is a
    # completed migration, not a pending one. It is NOT written if anything
    # errored, so the next startup retries only the genuinely unfinished case.
    if not report["errors"]:
        try:
            _write_flag(cognition_dir)
        except OSError as exc:
            logger.debug("person-migration: cannot write flag: %s", exc)
    return report


def format_migration_announce(report: dict[str, Any]) -> str:
    """The session-start notice. Empty string when there is nothing to say.

    Loud on purpose: this wrote committed files the user did not ask for, and the
    leftover nodes need a human decision.
    """
    if not report:
        return ""
    migrated = report.get("migrated") or []
    errors = report.get("errors") or []
    no_email = report.get("skipped_no_email") or []
    if not (migrated or errors or no_email):
        return ""

    lines = ["## Roster migrated to committed profiles"]
    if migrated:
        lines.append(
            f"- Wrote {len(migrated)} profile(s) under `.cognition/people/` from the "
            f"old person nodes: {', '.join(sorted(migrated))}. These are NOT committed "
            "yet — review and commit them with your next change."
        )
        lines.append(
            "- The old `person` nodes are still in the graph. They are harmless (the "
            "roster prefers the profile) but they still appear in graph listings. "
            "Remove them with `cognition_remove_node` once you are happy with the "
            "profiles."
        )
    if no_email:
        lines.append(
            f"- {len(no_email)} person node(s) have NO email and could not be "
            f"migrated: {', '.join(str(n) for n in no_email)}. Email is the identity "
            "key, so these need a human to say who they are — re-register them with "
            "`cognition_register_person`."
        )
    if errors:
        lines.append(
            f"- {len(errors)} error(s), so the migration will be retried next "
            f"session: {'; '.join(errors)}"
        )
    return "\n".join(lines)
