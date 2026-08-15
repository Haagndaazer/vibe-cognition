"""Per-person environment-fact files (WP-EnvFacts-A).

One committed JSONL file per identity under ``.cognition/people/``, holding ONLY
environment-fact delta lines (``fact_set`` / ``fact_delete`` / ``fact_clear``).
The person NODE (graph identity, profile fields, profile_history) stays in the
main journal — this module never touches the graph. Facts hydrate into a plain
per-email registry dict (precedent: storage's ``_reference_index`` — non-graph
state built from replay), so a person file with no registration node is a
first-class legal state, never a dangling reference.

Why deltas, not snapshots: the main journal's ``update_node`` convention writes
the ENTIRE metadata dict per call, which makes accumulating per-fact history
O(n²) in file growth. Here every line is exactly one change and the file IS the
audit trail — the O(n²) shape is structurally impossible, not just avoided.

Catch-up discipline (peer-review HIGH): the hot path never lists the directory
unless the directory's own mtime changed (file creation/removal touches dir
mtime on NTFS and POSIX; appends do not). Known files are individually
stat-gated exactly like the main journal (size+mtime unchanged -> one stat, no
read). A no-change pass therefore costs one dir stat plus one stat per known
file and ZERO reads/listdirs — reads only ever cover appended bytes of files
that actually changed.

Single-writer note: tool-layer policy (self-only writes, decision 29faeeca5190)
makes each file single-writer-per-identity, which is what makes the
``merge=union`` .gitattributes rule (hygiene v6) and any future compaction safe.
This module does not enforce that policy — it is enforced where identity is
resolved (the tools layer).
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .journal_io import append_journal_line

logger = logging.getLogger(__name__)

PEOPLE_DIRNAME = "people"

# Slug bytes kept verbatim (post-casefold, so no uppercase ASCII survives).
_SLUG_SAFE = frozenset(b"abcdefghijklmnopqrstuvwxyz0123456789._-")
# Encoded slugs longer than this collapse to prefix + hash suffix (Windows
# MAX_PATH headroom; the sha256 suffix carries uniqueness, the prefix is only
# for human readability).
_SLUG_MAX = 40
_SLUG_PREFIX_KEEP = 24
_SLUG_HASH_LEN = 12

# fact_set/fact_delete may not target these many distinct machines per person.
# At the cap a NEW machine is REJECTED (retryable error naming the remedy) —
# never silently LRU-evicted, which would be data loss for a still-real machine.
DEFAULT_MACHINE_CAP = 10


def email_slug(email: str) -> str:
    """Deterministic filesystem-safe slug for a (raw) identity email.

    Casefold FIRST (topology-dependent-collision guard: ``Alice@X`` and
    ``alice@x`` must be ONE file on case-sensitive filesystems too), then
    percent-encode every UTF-8 byte outside ``[a-z0-9._-]`` with LOWERCASE hex
    digits — pinned explicitly, because encoder libraries legally differ on hex
    case and an unpinned case would let two implementations mint two files for
    one person. Lossy sanitize-and-strip is banned: ``alice+a@x`` and
    ``alice+b@x`` colliding into one file would be a silent data merge.

    Over-length encodings collapse to ``prefix + '-' + sha256[:12]`` (lowercase
    hex) so a very long address can never push the path toward MAX_PATH while
    uniqueness rides on the hash, not the readable prefix.
    """
    folded = email.strip().casefold()
    out: list[str] = []
    for b in folded.encode("utf-8"):
        if b in _SLUG_SAFE:
            out.append(chr(b))
        else:
            out.append(f"%{b:02x}")
    encoded = "".join(out)
    if len(encoded) > _SLUG_MAX:
        digest = hashlib.sha256(folded.encode("utf-8")).hexdigest()[:_SLUG_HASH_LEN]
        encoded = f"{encoded[:_SLUG_PREFIX_KEEP]}-{digest}"
    return encoded


@dataclass
class _FileState:
    """Per-file replay state — the main journal's offset/hasher/mtime trio."""

    offset: int = 0
    hasher: "hashlib._Hash" = field(default_factory=hashlib.sha256)
    mtime_ns: int | None = None
    # Emails whose folded facts came from this file (normally exactly one, but
    # folding trusts the LINES, not the filename — defensive against a
    # hand-edited or mis-slugged file).
    emails: set[str] = field(default_factory=set)

    def reset(self) -> None:
        self.offset = 0
        self.hasher = hashlib.sha256()
        self.emails = set()


class PeopleFactsRegistry:
    """Environment facts folded from ``.cognition/people/*.jsonl`` delta files.

    NOT thread-safe on its own — every method is called under
    ``CognitionStorage._lock`` (via ``_synced``), mirroring how the graph and
    ``_reference_index`` are guarded.
    """

    def __init__(self, cognition_dir: Path):
        self._people_dir = cognition_dir / PEOPLE_DIRNAME
        # email -> machine_key -> fact_key -> value
        self._facts: dict[str, dict[str, dict[str, Any]]] = {}
        self._files: dict[str, _FileState] = {}
        self._dir_mtime_ns: int | None = None

    @property
    def people_dir(self) -> Path:
        return self._people_dir

    # ── Read surface ────────────────────────────────────────────────────

    def facts_for(self, email: str) -> dict[str, dict[str, Any]]:
        """Deep copy of one identity's facts (machine -> key -> value)."""
        folded = (email or "").strip().casefold()
        machines = self._facts.get(folded, {})
        return {m: dict(kv) for m, kv in machines.items()}

    def all_emails(self) -> list[str]:
        """Every email with at least one folded fact (registered or not)."""
        return sorted(self._facts)

    # ── Write surface (called by CognitionStorage under its lock) ───────

    def set_fact(
        self,
        email: str,
        machine: str,
        key: str,
        value: Any,
        by: dict[str, str],
        from_agent: bool,
        machine_cap: int = DEFAULT_MACHINE_CAP,
    ) -> dict[str, Any]:
        """Append one fact_set delta; returns {written, old, ...}.

        No-op suppression: an identical value journals NOTHING (the anti-churn
        contract — session-frequency writers must be free). A NEW machine at
        the cap raises ValueError (retryable; names the remedy) — existing
        machines keep writing at cap.
        """
        folded = self._require_email(email)
        machine_key = self._require_key(machine, "machine")
        fact_key = self._require_key(key, "key")

        machines = self._facts.get(folded, {})
        old = machines.get(machine_key, {}).get(fact_key)
        if machine_key in machines and old == value:
            return {"written": False, "noop": True, "old": old}
        if machine_key not in machines and len(machines) >= machine_cap:
            raise ValueError(
                f"machine cap reached ({machine_cap}) for '{folded}' — prune a "
                f"retired machine first with cognition_clear_env_facts(machine=...)"
            )

        self._append(
            folded,
            {
                "action": "fact_set",
                "email": folded,
                "machine": machine_key,
                "key": fact_key,
                "value": value,
                "old": old,
                "at": datetime.now(UTC).isoformat(),
                "by": by,
                "from_agent": from_agent,
            },
        )
        return {"written": True, "noop": False, "old": old}

    def delete_fact(
        self, email: str, machine: str, key: str, by: dict[str, str], from_agent: bool
    ) -> dict[str, Any]:
        """Append one fact_delete delta; deleting an absent key is a no-op."""
        folded = self._require_email(email)
        machine_key = self._require_key(machine, "machine")
        fact_key = self._require_key(key, "key")

        machines = self._facts.get(folded, {})
        if fact_key not in machines.get(machine_key, {}):
            return {"written": False, "noop": True, "old": None}
        old = machines[machine_key][fact_key]

        self._append(
            folded,
            {
                "action": "fact_delete",
                "email": folded,
                "machine": machine_key,
                "key": fact_key,
                "old": old,
                "at": datetime.now(UTC).isoformat(),
                "by": by,
                "from_agent": from_agent,
            },
        )
        return {"written": True, "noop": False, "old": old}

    def clear_facts(
        self,
        email: str,
        machine: str | None,
        by: dict[str, str],
        from_agent: bool,
    ) -> dict[str, Any]:
        """Append one fact_clear delta (bulk removal — the removal-on-request
        ruling made concrete). ``machine`` scopes to one machine; None clears
        ALL of the identity's facts. ``cleared_keys`` are enumerated in the
        line so the audit trail shows exactly what was removed — never an
        opaque wipe. Clearing an already-empty state journals nothing.
        """
        folded = self._require_email(email)
        machines = self._facts.get(folded, {})
        if machine is not None:
            machine_key = self._require_key(machine, "machine")
            cleared = sorted(machines.get(machine_key, {}))
        else:
            machine_key = None
            cleared = sorted(
                f"{m}/{k}" for m, kv in machines.items() for k in kv
            )
        if not cleared:
            return {"written": False, "noop": True, "cleared_keys": []}

        self._append(
            folded,
            {
                "action": "fact_clear",
                "email": folded,
                "machine": machine_key,
                "cleared_keys": cleared,
                "at": datetime.now(UTC).isoformat(),
                "by": by,
                "from_agent": from_agent,
            },
        )
        return {"written": True, "noop": False, "cleared_keys": cleared}

    # ── Catch-up ────────────────────────────────────────────────────────

    def catch_up(self) -> int:
        """Fold deltas appended since the last pass; return folded line count.

        Cost contract (peer-review HIGH): one dir stat gates the listdir; each
        known file is stat-gated before any read; a no-change pass does ZERO
        reads and ZERO listdirs. Truncation/rewrite of ONE file re-folds that
        file only — never a global reset, never touching the graph.
        """
        try:
            dir_stat = self._people_dir.stat()
        except OSError:
            # Missing/unreadable dir: drop anything we had (dir deleted under
            # us) and go quiet — a fresh project simply has no people/ yet.
            if self._files:
                self._facts.clear()
                self._files.clear()
                self._dir_mtime_ns = None
            return 0

        if dir_stat.st_mtime_ns != self._dir_mtime_ns:
            self._dir_mtime_ns = dir_stat.st_mtime_ns
            try:
                names = {
                    p.name for p in self._people_dir.iterdir()
                    if p.name.endswith(".jsonl")
                }
            except OSError:
                return 0
            for gone in set(self._files) - names:
                self._drop_file(gone)
            for new in names - set(self._files):
                self._files[new] = _FileState()

        folded = 0
        for name in list(self._files):
            folded += self._catch_up_file(name)
        return folded

    def _catch_up_file(self, name: str) -> int:
        fs = self._files[name]
        path = self._people_dir / name
        try:
            st = path.stat()
        except OSError:
            self._drop_file(name)
            return 0

        if st.st_size == fs.offset and st.st_mtime_ns == fs.mtime_ns:
            return 0
        fs.mtime_ns = st.st_mtime_ns

        try:
            data = path.read_bytes()
        except OSError:
            return 0

        if st.st_size < fs.offset or (
            fs.offset > 0
            and hashlib.sha256(data[: fs.offset]).digest() != fs.hasher.digest()
        ):
            # Truncated or divergently rewritten (e.g. a merge) — re-fold THIS
            # file only, from the top.
            self._drop_contribution(fs)
            fs.reset()

        raw = data[fs.offset:]
        last_nl = raw.rfind(b"\n")
        if last_nl == -1:
            return 0  # torn tail — park before it, re-read once complete
        complete = raw[: last_nl + 1]
        fs.offset += len(complete)
        fs.hasher.update(complete)

        count = 0
        for line in complete.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                self._fold(fs, entry)
                count += 1
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
                logger.warning("Skipping malformed people-file line (%s): %s", name, e)
        return count

    # ── Internals ───────────────────────────────────────────────────────

    def _fold(self, fs: _FileState, entry: dict[str, Any]) -> None:
        action = entry.get("action")
        email = str(entry.get("email") or "").casefold()
        if not email or action not in ("fact_set", "fact_delete", "fact_clear"):
            return
        fs.emails.add(email)
        machines = self._facts.setdefault(email, {})
        if action == "fact_set":
            machine = str(entry["machine"])
            machines.setdefault(machine, {})[str(entry["key"])] = entry.get("value")
        elif action == "fact_delete":
            machine = str(entry["machine"])
            machines.get(machine, {}).pop(str(entry["key"]), None)
            if machine in machines and not machines[machine]:
                del machines[machine]
        else:  # fact_clear
            machine_val = entry.get("machine")
            if machine_val is None:
                machines.clear()
            else:
                machines.pop(str(machine_val), None)
        if not machines:
            self._facts.pop(email, None)

    def _append(self, email: str, entry: dict[str, Any]) -> None:
        # append_journal_line creates the FILE (O_CREAT) but not parents —
        # a fresh project has no people/ dir until the first write.
        self._people_dir.mkdir(parents=True, exist_ok=True)
        path = self._people_dir / f"{email_slug(email)}.jsonl"
        append_journal_line(path, json.dumps(entry))
        # The write lands in folded state via the normal catch-up path (the
        # per-file stat sees the append) — same C-6 discipline as the main
        # journal: appends never advance our own offset.

    def _drop_file(self, name: str) -> None:
        fs = self._files.pop(name, None)
        if fs is not None:
            self._drop_contribution(fs)

    def _drop_contribution(self, fs: _FileState) -> None:
        """Remove every email this file contributed, then re-fold the OTHER
        files that also touched those emails (defensive: normally one file ==
        one email, but folding trusts lines, not filenames)."""
        affected = set(fs.emails)
        for email in affected:
            self._facts.pop(email, None)
        fs.emails = set()
        for other in self._files.values():
            if other is fs or not (other.emails & affected):
                continue
            # Cheap full re-fold of the overlapping file on the rare
            # hand-edited-overlap path: reset and let the next catch_up pass
            # re-read it (offset 0 forces a full re-read via size != offset).
            other.reset()
            other.mtime_ns = None

    @staticmethod
    def _require_email(email: str) -> str:
        folded = (email or "").strip().casefold()
        if not folded:
            raise ValueError("email must be non-empty (server-resolved identity)")
        return folded

    @staticmethod
    def _require_key(value: str, label: str) -> str:
        out = (value or "").strip()
        if not out:
            raise ValueError(f"{label} must be a non-empty string")
        if label == "machine":
            out = out.casefold()
        return out
