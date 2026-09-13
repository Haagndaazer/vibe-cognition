"""Per-person committed profiles — name, role, seniority, reporting line.

Replaces person NODES. A profile is the shared, committed answer to "who is this
person"; `.cognition/local/identity.json` is the separate machine-local answer to
"which of them is driving this checkout".

Stored at `.cognition/people/<email-slug>.profile.jsonl`, beside the env-facts
file for the same identity. A SEPARATE file, not the same one, for two reasons:
env facts are keyed email -> machine -> key (machine-scoped, which profile fields
are not), and env facts are self-only-write while profiles are deliberately
trust-based multi-writer. people_facts.py's own docstring names single-writer as
what makes `merge=union` safe for its file; interleaving multi-writer records
into it would falsify that for a future maintainer.

Append-only with a last-write-wins fold. `profile_unset` exists so "never
written" and "deliberately cleared" stay distinguishable — the write gate turns
on that difference, and a falsy-string check cannot express it.

REPORTS_TO: the literal "nobody" is a valid, expected answer on a solo project.
An empty string is NOT accepted, because a skippable field gets skipped.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .journal_io import append_journal_line
from .jsonl_dir_registry import FileState, JsonlDirRegistry
from .models import SENIORITY_LEVELS
from .people_facts import PEOPLE_DIRNAME, email_slug

logger = logging.getLogger(__name__)

PROFILE_SUFFIX = ".profile.jsonl"

#: Fields the write gate requires before any graph write is allowed.
REQUIRED_FIELDS: tuple[str, ...] = ("name", "email", "role", "seniority", "reports_to")

#: Every field a profile may carry.
PROFILE_FIELDS: tuple[str, ...] = (*REQUIRED_FIELDS, "detail")

NO_MANAGER = "nobody"


def _casefold(value: str) -> str:
    return (value or "").strip().casefold()


def profile_filename(email: str) -> str:
    return f"{email_slug(email)}{PROFILE_SUFFIX}"


class ProfileRegistry(JsonlDirRegistry):
    """Profiles folded from `.cognition/people/*.profile.jsonl`.

    NOT thread-safe on its own — callers hold CognitionStorage._lock, as the
    graph and the env-fact registry do.
    """

    FILE_SUFFIX = PROFILE_SUFFIX

    def __init__(self, cognition_dir: Path):
        super().__init__(Path(cognition_dir) / PEOPLE_DIRNAME)
        # email -> field -> value
        self._profiles: dict[str, dict[str, Any]] = {}
        # email -> list of records, newest last (audit trail + tamper alert)
        self._history: dict[str, list[dict[str, Any]]] = {}
        # email -> field -> winning record's `at`, so folding is order-independent
        self._stamps: dict[str, dict[str, str]] = {}

    # ── Engine hooks ────────────────────────────────────────────────────

    def _fold_entry(self, fs: FileState, entry: dict[str, Any]) -> None:
        action = entry.get("action")
        email = _casefold(str(entry.get("email") or ""))
        if not email:
            return
        if action == "profile_history_legacy":
            # Readable, never folded: it carries a migrated person node's old
            # per-CALL history, which has no single field/value to apply.
            fs.keys.add(email)
            self._history.setdefault(email, []).append(entry)
            return
        if action not in ("profile_set", "profile_unset"):
            return
        field = str(entry.get("field") or "")
        if field not in PROFILE_FIELDS:
            return
        fs.keys.add(email)
        # Last-write-wins by TIMESTAMP, not by position in the file. Profiles are
        # multi-writer and `merge=union` does not guarantee chronological order:
        # the interleaving depends on merge direction, so position-ordered folding
        # would let the same two commits produce DIFFERENT final values depending
        # on which machine did the merge. Ties (identical `at`) fall back to
        # position, which is deterministic within a single file.
        at = str(entry.get("at") or "")
        stamps = self._stamps.setdefault(email, {})
        if at < stamps.get(field, ""):
            self._history.setdefault(email, []).append(entry)
            return
        stamps[field] = at
        fields = self._profiles.setdefault(email, {})
        if action == "profile_set":
            fields[field] = entry.get("value")
        else:
            fields.pop(field, None)
        self._history.setdefault(email, []).append(entry)
        if not fields:
            self._profiles.pop(email, None)

    def _drop_contribution(self, fs: FileState) -> None:
        """Undo this file's contribution, then re-fold any OTHER file that also
        touched the same emails.

        people_facts carries this defense for a case it calls pathological (one
        file per email, normally). For profiles it is the NORMAL case: they are
        trust-based multi-writer, so a manager's pre-registration and the
        person's own confirmation legitimately live in different files for one
        email. Without the re-fold, rewriting one file silently and permanently
        discards the other's fields.
        """
        affected = set(fs.keys)
        for email in affected:
            self._profiles.pop(email, None)
            self._history.pop(email, None)
            self._stamps.pop(email, None)
        fs.keys = set()
        for other in self._files.values():
            if other is fs or not (other.keys & affected):
                continue
            other.reset()
            other.mtime_ns = None

    def _clear_all(self) -> None:
        self._profiles.clear()
        self._history.clear()
        self._stamps.clear()

    # ── Read surface ────────────────────────────────────────────────────

    def get(self, email: str) -> dict[str, Any] | None:
        """One profile, or None. Always carries `email` so callers need not re-derive it."""
        folded = _casefold(email)
        fields = self._profiles.get(folded)
        if not fields:
            return None
        return {"email": folded, **fields}

    def all_emails(self) -> list[str]:
        return sorted(self._profiles)

    def all_profiles(self) -> list[dict[str, Any]]:
        return [self.get(e) or {} for e in self.all_emails()]

    def history_for(self, email: str) -> list[dict[str, Any]]:
        return list(self._history.get(_casefold(email), []))

    def direct_reports(self, email: str) -> list[str]:
        """Emails reporting to this one. One pass, so callers never rescan per row."""
        folded = _casefold(email)
        return sorted(
            e for e, f in self._profiles.items()
            if _casefold(str(f.get("reports_to") or "")) == folded
        )

    def missing_required(self, email: str) -> list[str]:
        """Which required fields are absent — the gate's refusal names these."""
        fields = self._profiles.get(_casefold(email)) or {}
        return [f for f in REQUIRED_FIELDS if not str(fields.get(f) or "").strip()]

    def is_complete(self, email: str) -> bool:
        return not self.missing_required(email)

    # ── Write surface ───────────────────────────────────────────────────

    def set_fields(
        self, email: str, fields: dict[str, Any], by: dict[str, str], from_agent: bool = True
    ) -> dict[str, Any]:
        """Append one record per changed field. Returns {written, skipped}.

        Unchanged values journal NOTHING, so a re-confirmation on an unchanged
        profile is free rather than appending noise every session.
        """
        folded = _casefold(email)
        if not folded:
            return {"error": "email must not be blank"}
        current = self._profiles.get(folded) or {}
        written, skipped = [], []
        now = datetime.now(UTC).isoformat()
        for field, value in fields.items():
            if field not in PROFILE_FIELDS:
                continue
            if value is None:
                continue
            if field == "seniority" and _casefold(str(value)) not in SENIORITY_LEVELS:
                return {"error": f"seniority must be one of {list(SENIORITY_LEVELS)}, got {value!r}"}
            if current.get(field) == value:
                skipped.append(field)
                continue
            self._append(folded, {
                "action": "profile_set", "email": folded, "field": field,
                "value": value, "by": by, "from_agent": from_agent, "at": now,
            })
            written.append(field)
        return {"written": written, "skipped": skipped}

    def unset_field(self, email: str, field: str, by: dict[str, str]) -> dict[str, Any]:
        folded = _casefold(email)
        if not folded:
            return {"error": "email must not be blank"}
        if field not in PROFILE_FIELDS:
            return {"error": f"unknown profile field {field!r}"}
        self._append(folded, {
            "action": "profile_unset", "email": folded, "field": field,
            "by": by, "at": datetime.now(UTC).isoformat(),
        })
        return {"unset": field}

    def append_legacy_history(
        self, email: str, entries: list[dict[str, Any]], node_id: str
    ) -> dict[str, Any]:
        """Preserve a migrated person node's `profile_history` verbatim.

        A separate action from profile_set/profile_unset so `_fold_entry` ignores
        it: the old shape is one entry per update CALL listing several fields, and
        replaying it as sets would need invented timestamps that then compete with
        real ones in the last-write-wins fold. Readable, never folded, never able
        to overwrite a current value.
        """
        folded = _casefold(email)
        if not folded:
            return {"error": "email must not be blank"}
        if not entries:
            return {"written": 0}
        self._append(folded, {
            "action": "profile_history_legacy", "email": folded,
            "from_node": node_id, "entries": entries,
            "at": datetime.now(UTC).isoformat(),
        })
        return {"written": len(entries)}

    def _append(self, email: str, entry: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        name = profile_filename(email)
        append_journal_line(self._dir / name, json.dumps(entry))
        # Our own writes must not depend on timestamp-racy dir discovery.
        self.register_own_write(name)
