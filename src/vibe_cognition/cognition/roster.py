"""Who is on this project — the ONE view every consumer reads.

Before this, eight modules each did their own `get_nodes_by_type(PERSON)` scan and
reached into `metadata["person"]` by hand. Search ranking, the prime digest's
manager rollup, the dashboard, the identity backfill and four MCP tools all
carried their own copy of "find the person with this email", so moving the roster
from person NODES to committed PROFILES would have meant thirty independent edits
with no way to tell which had been missed. A missed one is silent: a seniority
that stops reaching `_person_seniority_map` does not error, it just quietly
changes what search returns.

Source of truth is `.cognition/people/<email>.profile.jsonl`. Legacy person nodes
are folded in for any email with NO profile yet, so a graph that has not been
migrated still works and the migration is idempotent — run it twice and the
second pass finds nothing to do. That fallback is permanent rather than
transitional: a clone that predates the migration can appear at any time.

Snapshot, not a live view: built once per operation from a storage read, so a
caller doing several lookups gets a consistent answer and one scan.
"""

from dataclasses import dataclass, field
from typing import Any

from .models import CognitionNodeType
from .people_facts import fold_email
from .profiles import NO_MANAGER

#: Where a roster row came from. Legacy rows are what the migration consumes.
SOURCE_PROFILE = "profile"
SOURCE_NODE = "node"


def _fold(value: Any) -> str:
    return fold_email(str(value or ""))


@dataclass(frozen=True)
class Person:
    """One human on the project.

    `reports_to` is an email, or `""` for top-of-chain. The literal "nobody" a
    profile stores is normalized to `""` here so every consumer tests one thing;
    `answered_reports_to` distinguishes "said nobody" from "never asked", which is
    what the write gate turns on.
    """

    email: str
    name: str = ""
    role: str = ""
    seniority: str = ""
    reports_to: str = ""
    detail: str = ""
    source: str = SOURCE_PROFILE
    #: Legacy person node id, present only for rows still backed by a node.
    node_id: str | None = None
    answered_reports_to: bool = False

    @property
    def has_manager(self) -> bool:
        return bool(self.reports_to)

    @property
    def reports_to_display(self) -> str:
        """What to SHOW for the reporting line.

        `reports_to` is normalized to "" for both "said nobody" and "never asked",
        which every chain-walking consumer wants; a reader needs them apart. Two
        consumers derived this separately and had already drifted.
        """
        if self.reports_to:
            return self.reports_to
        return NO_MANAGER if self.answered_reports_to else ""


@dataclass
class Roster:
    """Snapshot of the project roster, profiles first, legacy nodes as fallback."""

    people: dict[str, Person] = field(default_factory=dict)
    #: email -> legacy person-node id, for EVERY person node, including emails
    #: that already have a profile. Callers need to know a node still exists even
    #: when the profile is what they are reading — removing someone, for one.
    legacy_nodes: dict[str, str] = field(default_factory=dict)

    # ── Construction ────────────────────────────────────────────────────

    @classmethod
    def load(cls, storage: Any) -> "Roster":
        people: dict[str, Person] = {}
        legacy_nodes: dict[str, str] = {}
        removed = storage.removed_profile_emails()
        for profile in storage.all_profiles():
            email = _fold(profile.get("email"))
            if not email:
                continue
            raw_manager = str(profile.get("reports_to") or "").strip()
            folded_manager = raw_manager.casefold()
            people[email] = Person(
                email=email,
                name=str(profile.get("name") or ""),
                role=str(profile.get("role") or ""),
                seniority=_fold(profile.get("seniority")),
                reports_to="" if folded_manager in ("", NO_MANAGER) else folded_manager,
                detail=str(profile.get("detail") or ""),
                source=SOURCE_PROFILE,
                answered_reports_to=bool(raw_manager),
            )

        for node in storage.get_nodes_by_type(CognitionNodeType.PERSON):
            info = (node.get("metadata") or {}).get("person") or {}
            email = _fold(info.get("email"))
            if email:
                legacy_nodes.setdefault(email, str(node.get("id") or ""))
            # A profile always wins: it is the newer, committed answer, and the
            # migration writes one per node. Duplicate nodes sharing an email
            # (a journal-replay shape) collapse to the first seen, as before.
            # A tombstoned email is skipped outright, or removing someone who
            # predates profiles would be undone by this very loop.
            if not email or email in people or email in removed:
                continue
            manager = _fold(info.get("reports_to_email"))
            people[email] = Person(
                email=email,
                name=str(info.get("name") or ""),
                role=str(info.get("role") or ""),
                seniority=_fold(info.get("seniority")),
                reports_to="" if manager == NO_MANAGER else manager,
                detail=str(node.get("detail") or ""),
                source=SOURCE_NODE,
                node_id=node.get("id"),
                answered_reports_to=bool(manager),
            )
        return cls(people, legacy_nodes)

    # ── Lookup ──────────────────────────────────────────────────────────

    def get(self, email: str) -> Person | None:
        return self.people.get(_fold(email))

    def resolve(self, email_or_id: str) -> Person | None:
        """By email, or by legacy person-node id while any node still exists.

        Node ids stop resolving once the migration removes the nodes. Email is
        the identity key and always has been; the id path is compatibility only.
        """
        found = self.get(email_or_id)
        if found is not None:
            return found
        needle = (email_or_id or "").strip()
        if not needle:
            return None
        for person in self.people.values():
            if person.node_id == needle:
                return person
        return None

    def emails(self) -> set[str]:
        return set(self.people)

    def all(self) -> list[Person]:
        return sorted(self.people.values(), key=lambda p: (p.name or p.email).casefold())

    def names(self) -> dict[str, str]:
        return {e: p.name for e, p in self.people.items()}

    def seniority_map(self) -> dict[str, str]:
        """email -> seniority, omitting anyone with none recorded.

        Feeds search RANKING. An email missing here silently loses its weighting
        rather than erroring, which is why every consumer reads this one map.
        """
        return {e: p.seniority for e, p in self.people.items() if p.seniority}

    def name_for(self, email: str) -> str:
        person = self.get(email)
        return person.name if person else ""

    # ── Reporting line ──────────────────────────────────────────────────

    def direct_reports(self, email: str) -> list[Person]:
        target = _fold(email)
        if not target:
            return []
        return sorted(
            (p for p in self.people.values() if p.reports_to == target),
            key=lambda p: (p.name or p.email).casefold(),
        )

    def manager_of(self, email: str) -> Person | None:
        person = self.get(email)
        return self.get(person.reports_to) if person and person.reports_to else None

    def manager_chain(self, email: str) -> list[Person]:
        """Managers upward, nearest first. Stops at the top, at a dangling email,
        or on a cycle — a hand-edited or merged graph can contain one."""
        chain: list[Person] = []
        seen = {_fold(email)}
        current = self.get(email)
        while current is not None and current.reports_to and current.reports_to not in seen:
            seen.add(current.reports_to)
            nxt = self.get(current.reports_to)
            if nxt is None:
                break
            chain.append(nxt)
            current = nxt
        return chain

    def reports_to_cycle(self, self_email: str, reports_to_email: str) -> bool:
        """True if setting this manager would make someone their own transitive
        manager. A dangling email terminates the walk gracefully — a manager may
        simply not be on the roster yet."""
        me = _fold(self_email)
        current = _fold(reports_to_email)
        seen: set[str] = set()
        while current and current not in seen:
            if current == me:
                return True
            seen.add(current)
            person = self.get(current)
            if person is None:
                return False
            current = person.reports_to
        return False

    def is_registered(self, email: str) -> bool:
        return self.get(email) is not None

    def legacy_node_id(self, email: str) -> str | None:
        """The person node still backing this email, if one exists. Present even
        when a profile shadows it — that is exactly when it is easy to forget."""
        return self.legacy_nodes.get(_fold(email)) or None
