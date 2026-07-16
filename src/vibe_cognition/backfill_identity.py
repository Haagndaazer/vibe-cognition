"""Legacy identity backfill (task 962ab7b442d5, design doc rev 2 APPROVED,
doc:76a545c246ef, decision 833e9f67de4d). Stamps pre-P13n-1 nodes (which carry
only the free-text `author` field) with an INFERRED recorded_by/created_by so
existing projects benefit from the identity features already built for new
writes.

Ruling 1 (Colton, 2026-07-16): NO auto-stamping, even on an unambiguous exact
roster hit. Roster match and git-blame match are SUGGESTION GENERATORS only —
the only path that actually WRITES a stamp is a human-confirmed map file
(`--map-file`, emitted as a skeleton by a dry run, edited by the graph owner,
then re-supplied with `--apply`). `--map "Name=email"` entries are always
human-typed, so they count as confirmed on the spot.

Scope (design doc "NODE-TYPE SCOPE"): entity node types get `recorded_by`;
TASK nodes get `created_by`; DOCUMENT (no live stamp shape until 2858ae93bf17
shipped — it since has, but backfilling that document corpus is a v2 line
item, ruling 7) and PERSON (their recorded_by is the REGISTRAR's identity, not
the person's own — backfilling it only perturbs the auto-personalize
calculation for no reader benefit, ruling 3) are permanently out of v1 scope.

CLI: ``python -m vibe_cognition.backfill_identity <project-path> [--map
"Name=email"]... [--map-file <path>] [--apply] [--recompute-backfilled]``.
Dry-run (no ``--apply``) computes and prints the report, and (unless every
eligible name is already confirmed) writes a skeleton map file the user edits
before the confirmed apply run — it NEVER writes to the graph.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from .cognition.models import CognitionNodeType
from .cognition.storage import CognitionStorage

# ── Node-type scope (design doc "NODE-TYPE SCOPE") ───────────────────────────

_ENTITY_TYPES: frozenset[CognitionNodeType] = frozenset({
    CognitionNodeType.DECISION,
    CognitionNodeType.FAIL,
    CognitionNodeType.DISCOVERY,
    CognitionNodeType.ASSUMPTION,
    CognitionNodeType.CONSTRAINT,
    CognitionNodeType.INCIDENT,
    CognitionNodeType.PATTERN,
    CognitionNodeType.EPISODE,
    CognitionNodeType.WORKFLOW,
})

_NO_AUTHOR_KEY = "(no author)"

# Bulk-rewrite commit exclusion (design doc, scale-invariant per peer-review
# M7): a commit is excluded as a blame source when it accounts for BOTH more
# than this fraction of the file's line count AT THAT COMMIT, and more than
# the absolute floor -- never a bare percentage of today's (much larger) file.
_BULK_REWRITE_RATIO = 0.5
_BULK_REWRITE_FLOOR = 20


def _stamp_key_for_type(node_type: str | None) -> str | None:
    """The metadata key a node of this type is stamped under, or None if the
    type is out of v1 scope (document, person, or anything unrecognized)."""
    try:
        t = CognitionNodeType(node_type)
    except ValueError:
        return None
    if t in _ENTITY_TYPES:
        return "recorded_by"
    if t is CognitionNodeType.TASK:
        return "created_by"
    return None


def eligibility(node: dict[str, Any], *, recompute_backfilled: bool) -> tuple[bool, str]:
    """Whether ``node`` may be (re)stamped by this run, and why/why-not.

    Skip predicate (peer-review H2): "already stamped" means the stamp key
    holds a dict with a NON-EMPTY email -- a legacy-era empty-email stamp
    (resolve_git_identity returns "" when git had no configured email) is
    NOT considered stamped, and remains eligible. A server-resolved
    non-empty stamp is untouchable UNLESS it carries ``backfilled: true``
    (a prior run's inference) AND ``--recompute-backfilled`` was passed.

    Returns:
        (eligible, reason) -- reason is one of "out-of-scope", "server-stamped",
        "already-backfilled", "unstamped". "already-backfilled" is eligible
        only when recompute_backfilled is True.
    """
    stamp_key = _stamp_key_for_type(node.get("type"))
    if stamp_key is None:
        return False, "out-of-scope"
    meta = node.get("metadata") or {}
    stamp = meta.get(stamp_key)
    if isinstance(stamp, dict) and (stamp.get("email") or "").strip():
        if stamp.get("backfilled"):
            return recompute_backfilled, "already-backfilled"
        return False, "server-stamped"
    return True, "unstamped"


# ── Source 1: registered-person roster (SUGGESTION only) ────────────────────


def roster_suggestions(storage: CognitionStorage) -> dict[str, str]:
    """casefolded author-name -> email, for names that match EXACTLY ONE
    registered person (name match, casefolded). A name shared by two
    registered persons is a collision -- unmappable by this source, falls
    through (never picked arbitrarily)."""
    by_name: dict[str, set[str]] = {}
    for n in storage.get_nodes_by_type(CognitionNodeType.PERSON):
        person = (n.get("metadata") or {}).get("person") or {}
        name = (person.get("name") or "").strip()
        email = (person.get("email") or "").strip()
        if not name or not email:
            continue
        by_name.setdefault(name.casefold(), set()).add(email)
    return {name: next(iter(emails)) for name, emails in by_name.items() if len(emails) == 1}


# ── Source 2: journal git-blame (SUGGESTION only) ────────────────────────────


def _run_git(repo_path: Path, args: list[str]) -> str | None:
    """Run a read-only git command, capturing stdout. None on any failure --
    blame is a best-effort suggestion source, never a hard requirement.

    Decodes explicitly as UTF-8 (errors replaced) rather than `text=True`'s
    locale-default codec: on Windows that defaults to cp1252, which raises
    UnicodeDecodeError on any non-cp1252 byte in a real repo's commit
    messages/author names (observed live on this repo's own history) --
    exactly the kind of subprocess-adjacent crash this best-effort source
    must never surface.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _blame_journal_lines(repo_path: Path, journal_rel_path: str) -> list[dict[str, str]] | None:
    """One entry per line of the tracked journal.jsonl: {commit, author_name,
    author_email, author_time}. None if blame is unavailable (not a git repo,
    file untracked, git missing) -- caller degrades to no blame suggestions."""
    out = _run_git(repo_path, ["blame", "--line-porcelain", "--", journal_rel_path])
    if out is None:
        return None
    lines: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw in out.splitlines():
        if raw.startswith("\t"):
            lines.append(current)
            current = {}
            continue
        if raw.startswith("author "):
            current["author_name"] = raw[len("author "):]
        elif raw.startswith("author-mail "):
            current["author_email"] = raw[len("author-mail "):].strip("<>")
        elif raw.startswith("author-time "):
            current["author_time"] = raw[len("author-time "):]
        elif " " in raw and len(raw.split(" ", 1)[0]) == 40:
            current["commit"] = raw.split(" ", 1)[0]
    return lines


def _commit_line_count_at(repo_path: Path, commit: str, journal_rel_path: str) -> int | None:
    """Total line count of the journal file AS OF ``commit`` (scale-invariant
    bulk-rewrite denominator -- never today's file size)."""
    out = _run_git(repo_path, ["show", f"{commit}:{journal_rel_path}"])
    if out is None:
        return None
    return len(out.splitlines())


def _bulk_rewrite_commits(
    repo_path: Path, journal_rel_path: str, blame_lines: list[dict[str, str]],
) -> set[str]:
    """Commits that introduced so many journal lines at once, relative to the
    file's OWN size at that point in history, that they read as a bulk rewrite
    (a manager's flush-commit, a rehydrate dump) rather than organic per-node
    authorship -- excluded from blame's name-agreement gate entirely."""
    counts = Counter(line.get("commit", "") for line in blame_lines)
    bulk: set[str] = set()
    for commit, count in counts.items():
        if not commit or count < _BULK_REWRITE_FLOOR:
            continue
        total_at_commit = _commit_line_count_at(repo_path, commit, journal_rel_path)
        if total_at_commit and count / total_at_commit > _BULK_REWRITE_RATIO:
            bulk.add(commit)
    return bulk


def blame_suggestions(
    repo_path: Path,
    cognition_dir: Path,
    node_authors: dict[str, str],
) -> tuple[dict[str, str], set[str]]:
    """casefolded author-name -> suggested email, sourced from journal git
    history, plus the set of names flagged as EMAIL-DRIFT (blamed to more than
    one distinct email across the node set).

    Gated per name (design doc, both must hold):
      - name agreement: the blame author NAME casefold-matches the node's own
        `author` field (guards flusher-attribution -- a shared-worktree flush
        commits every line under the flusher's identity regardless of who
        actually wrote it).
      - (email consistency is not a gate anymore under ruling 6 -- drift no
        longer falls to manual, it becomes a most-recent-email suggestion
        with an annotation; nothing here writes without human confirmation
        regardless.)
    Bulk-rewrite commits (peer-review M7) are excluded before aggregation.

    Args:
        node_authors: node_id -> free-text author, for every node this run
            cares about (used only to build the name-agreement gate; blame
            itself works at the journal-line level, not the node level).
    """
    blame_lines = _blame_journal_lines(repo_path, str(cognition_dir.name) + "/journal.jsonl")
    if blame_lines is None:
        return {}, set()
    bulk = _bulk_rewrite_commits(repo_path, str(cognition_dir.name) + "/journal.jsonl", blame_lines)

    author_names_casefold = {a.casefold() for a in node_authors.values() if a}
    # name(casefold) -> list of (email, author_time) observed via blame, gated
    # by name-agreement and bulk-rewrite exclusion.
    observed: dict[str, list[tuple[str, str]]] = {}
    for line in blame_lines:
        if line.get("commit") in bulk:
            continue
        blamed_name = (line.get("author_name") or "").casefold()
        if blamed_name not in author_names_casefold:
            continue
        email = (line.get("author_email") or "").strip()
        if not email:
            continue
        observed.setdefault(blamed_name, []).append((email, line.get("author_time", "")))

    suggestions: dict[str, str] = {}
    drift: set[str] = set()
    for name, seen in observed.items():
        distinct_emails = {email for email, _ in seen}
        if len(distinct_emails) > 1:
            drift.add(name)
            # Ruling 6: most-recent email wins as the SUGGESTION (never an
            # auto-write -- safe only because nothing here writes without a
            # human-confirmed map file).
            suggestions[name] = max(seen, key=lambda pair: pair[1])[0]
        else:
            suggestions[name] = next(iter(distinct_emails))
    return suggestions, drift


# ── Source 3: explicit human-confirmed mapping (the ONLY write authority) ───

# The closed set backfill_source may ever carry (design doc's honesty-marking
# section). Anything else -- a typo, or skeleton()'s own "none" placeholder
# left untouched by a user who filled in the email but didn't think to edit
# this field (the common case: nobody edits a field they don't need to) --
# must never reach the graph verbatim (Vince's Train C review, finding 1).
_VALID_BACKFILL_SOURCES = frozenset({"roster", "git-history", "manual"})


def parse_map_args(entries: list[str]) -> dict[str, tuple[str, str]]:
    """``--map "Name=email"`` entries -> {casefolded name: (email, "manual")}.
    Always "manual" -- a CLI arg is always human-typed on the spot."""
    out: dict[str, tuple[str, str]] = {}
    for entry in entries:
        name, sep, email = entry.partition("=")
        if not sep:
            raise ValueError(f"--map entry must be 'Name=email', got: {entry!r}")
        name, email = name.strip(), email.strip()
        if not name or not email:
            raise ValueError(f"--map entry must be 'Name=email', got: {entry!r}")
        out[name.casefold()] = (email, "manual")
    return out


def parse_map_file(path: Path) -> dict[str, tuple[str, str]]:
    """Load a confirmed map file: MANY-ALIASES-TO-ONE-EMAIL shape (peer-review
    L10) -- ``[{"email": "...", "aliases": ["Name", "alt"], "source":
    "roster"|"git-history"|"manual"}, ...]``. Each alias, casefolded, maps to
    that entry's email. ``source`` is optional (defaults to "manual" -- an
    entry the user hand-added to the file, not one that started life as a
    skeleton suggestion), validated against the closed set (anything else,
    including skeleton()'s own "none" placeholder, coerces to "manual" --
    never smuggled into the graph verbatim), and carried through to the
    write's ``backfill_source`` marker (subject to BackfillPlan's own
    suggestion-match downgrade -- see its to_write construction)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"map file must be a JSON array of entries: {path}")
    out: dict[str, tuple[str, str]] = {}
    for entry in data:
        email = (entry.get("email") or "").strip()
        aliases = entry.get("aliases") or []
        source = entry.get("source") or "manual"
        if source not in _VALID_BACKFILL_SOURCES:
            source = "manual"  # e.g. skeleton()'s "none" placeholder, left untouched
        if not email or not aliases:
            continue  # unfinished skeleton row -- alias stays unmapped, never guessed
        for alias in aliases:
            alias = (alias or "").strip()
            if alias:
                out[alias.casefold()] = (email, source)
    return out


# ── Report / plan assembly ───────────────────────────────────────────────────


class BackfillPlan:
    """The full computed plan for one run: what would be written, what stays
    suggested-only, and the auto-personalize forecast. Shared by the dry-run
    report and the ``--apply`` writer so they can never disagree."""

    def __init__(
        self,
        storage: CognitionStorage,
        *,
        recompute_backfilled: bool,
        confirmed: dict[str, tuple[str, str]],
        repo_path: Path,
    ) -> None:
        self.storage = storage
        self.recompute_backfilled = recompute_backfilled
        self.confirmed = confirmed

        eligible_nodes: list[dict[str, Any]] = []
        node_authors: dict[str, str] = {}
        for node in storage.get_all_nodes():
            ok, _reason = eligibility(node, recompute_backfilled=recompute_backfilled)
            if ok:
                eligible_nodes.append(node)
                node_authors[node["id"]] = node.get("author") or ""
        self.eligible_nodes = eligible_nodes

        self.roster = roster_suggestions(storage)
        self.blame, self.drift_names = blame_suggestions(
            repo_path, storage.cognition_dir, node_authors,
        )

        # name(casefold) -> node count, grouping "(no author)" separately.
        by_name: Counter[str] = Counter()
        self.display_name: dict[str, str] = {}
        for node in eligible_nodes:
            author = (node.get("author") or "").strip()
            key = author.casefold() if author else _NO_AUTHOR_KEY
            by_name[key] += 1
            self.display_name.setdefault(key, author or _NO_AUTHOR_KEY)
        self.node_counts_by_name = by_name

        # to_write: node -> (email, source) for every eligible node whose
        # author name IS in the confirmed map -- these are the ONLY writes
        # `apply()` performs.
        #
        # Kept-vs-edited (Vince's Train C review, finding 2): a confirmed
        # entry's `source` is trustworthy ONLY when the confirmed email
        # matches what this run would have suggested for that name -- i.e.
        # the human kept the suggestion as-is. If they changed the email (or
        # there was never a suggestion to begin with), the file's source
        # field is downgraded to "manual" here regardless of what it says --
        # the marker must describe what actually happened, never trust a
        # human to keep a bookkeeping field in sync with an edit they made
        # for an unrelated reason.
        self.to_write: list[tuple[dict[str, Any], str, str]] = []
        for node in eligible_nodes:
            author = (node.get("author") or "").strip()
            key = author.casefold() if author else ""
            hit = confirmed.get(key)
            if hit is not None:
                email, source = hit
                suggestion = self.suggestion_for(key)
                if suggestion is None or suggestion[0].casefold() != email.casefold():
                    source = "manual"
                self.to_write.append((node, email, source))

    def suggestion_for(self, name_key: str) -> tuple[str, str] | None:
        """(email, source) suggestion for a casefolded name key, roster first
        (peer-review ordering: roster is the explicit source of truth)."""
        if name_key in self.roster:
            return self.roster[name_key], "roster"
        if name_key in self.blame:
            return self.blame[name_key], "git-history"
        return None

    def unconfirmed_names(self) -> list[str]:
        return [
            key for key in self.node_counts_by_name
            if key not in self.confirmed and key != _NO_AUTHOR_KEY.casefold()
        ]

    def skeleton(self) -> list[dict[str, Any]]:
        """One entry per unconfirmed name (suggested or not) -- the file the
        user edits before a confirmed `--apply` run."""
        rows = []
        for key in sorted(self.unconfirmed_names()):
            suggestion = self.suggestion_for(key)
            row: dict[str, Any] = {
                "aliases": [self.display_name[key]],
                "email": suggestion[0] if suggestion else "",
                "source": suggestion[1] if suggestion else "none",
                "node_count": self.node_counts_by_name[key],
            }
            if key in self.drift_names:
                row["drift"] = True
            rows.append(row)
        return rows

    def stamped_email_forecast(self) -> tuple[int, int]:
        """(current, post-backfill) count of DISTINCT non-empty stamped
        emails in the graph -- the auto-personalize signal `_should_personalize`
        reads (prime.py). Only the CONFIRMED writes (`to_write`) affect the
        post-backfill count; suggestions that were never confirmed change
        nothing."""
        current: set[str] = set()
        for node in self.storage.get_all_nodes():
            stamp_key = _stamp_key_for_type(node.get("type"))
            if stamp_key is None:
                continue
            stamp = (node.get("metadata") or {}).get(stamp_key)
            if isinstance(stamp, dict) and stamp.get("email"):
                current.add(stamp["email"].casefold())
        post = set(current)
        for _node, email, _source in self.to_write:
            if email:
                post.add(email.casefold())
        return len(current), len(post)

    def registered_person_count(self) -> int:
        return len({
            (n.get("metadata", {}).get("person", {}).get("email") or "").casefold()
            for n in self.storage.get_nodes_by_type(CognitionNodeType.PERSON)
        } - {""})


def render_report(plan: BackfillPlan) -> str:
    lines: list[str] = []
    lines.append(f"# Legacy identity backfill -- {len(plan.eligible_nodes)} eligible node(s)")
    lines.append("")

    if plan.to_write:
        lines.append("## Would stamp (confirmed mapping)")
        by_key: Counter[str] = Counter()
        email_for: dict[str, str] = {}
        source_for: dict[str, str] = {}
        for node, email, source in plan.to_write:
            author = (node.get("author") or "").strip()
            key = author.casefold() if author else ""
            by_key[key] += 1
            email_for[key] = email
            source_for[key] = source
        for key, count in sorted(by_key.items()):
            name = plan.display_name.get(key, key)
            lines.append(f"  {name} -> {email_for[key]} ({count} node(s), source={source_for[key]})")
        lines.append("")

    unconfirmed = plan.unconfirmed_names()
    if unconfirmed:
        lines.append("## Unmapped -- edit the skeleton map file and re-run with --map-file")
        for key in sorted(unconfirmed):
            suggestion = plan.suggestion_for(key)
            count = plan.node_counts_by_name[key]
            name = plan.display_name[key]
            if suggestion:
                drift_note = " [EMAIL DRIFT -- most-recent suggested]" if key in plan.drift_names else ""
                lines.append(f"  {name}: {count} node(s) -- suggested {suggestion[0]} (source={suggestion[1]}){drift_note}")
            else:
                lines.append(f"  {name}: {count} node(s) -- no suggestion, needs a manual email")
        lines.append("")

    if _NO_AUTHOR_KEY.casefold() in plan.node_counts_by_name:
        lines.append(f"## {_NO_AUTHOR_KEY}: {plan.node_counts_by_name[_NO_AUTHOR_KEY.casefold()]} node(s) -- left unstamped, no author string to map")
        lines.append("")

    journal_lines = len(plan.to_write)
    lines.append(f"Journal lines that would be appended (update_node events): {journal_lines}")
    lines.append("")

    cur, post = plan.stamped_email_forecast()
    registered = plan.registered_person_count()
    cur_auto = cur > 1 or registered > 1
    post_auto = post > 1 or registered > 1
    lines.append(
        f"Auto-personalize forecast: distinct stamped emails {cur} -> {post} "
        f"(registered people: {registered}) -- 'auto' mode would personalize: "
        f"{cur_auto} -> {post_auto}"
        + (" [FLIPS]" if cur_auto != post_auto else "")
    )
    return "\n".join(lines)


def apply_plan(plan: BackfillPlan) -> int:
    """Write every planned stamp via `update_node` (append-only journal event
    per node, C-4 journal-first mechanics already guaranteed by storage.py).
    Re-reads and re-checks eligibility on each node immediately before writing
    (peer-review H5 concurrency honesty -- narrows, never closes, the
    get_node/update_node lock gap). Returns the number of nodes actually
    stamped."""
    written = 0
    for node, email, source in plan.to_write:
        current = plan.storage.get_node(node["id"])
        if current is None:
            continue
        ok, _reason = eligibility(current, recompute_backfilled=plan.recompute_backfilled)
        if not ok:
            continue  # stamped by a live session since the plan was computed
        stamp_key = _stamp_key_for_type(current.get("type"))
        if stamp_key is None:
            continue
        meta = dict(current.get("metadata") or {})
        meta[stamp_key] = {
            "name": plan.display_name.get(
                (current.get("author") or "").strip().casefold() or _NO_AUTHOR_KEY.casefold(),
                current.get("author") or "",
            ),
            "email": email,
            "backfilled": True,
            "backfill_source": source,
        }
        plan.storage.update_node(node["id"], metadata=meta)
        written += 1
    return written


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vibe_cognition.backfill_identity",
        description=(
            "Stamp pre-P13n-1 legacy nodes with an inferred recorded_by/"
            "created_by so this project benefits from identity features on "
            "old history, not just new writes. Dry-run by default -- never "
            "writes without --apply and a confirmed mapping."
        ),
    )
    parser.add_argument("project_path", help="Path to the target project (containing .cognition/)")
    parser.add_argument("--map", action="append", default=[], metavar="Name=email",
                         help="Confirm one name -> email mapping (repeatable)")
    parser.add_argument("--map-file", metavar="PATH",
                         help="Confirmed map file (many-aliases-to-one-email JSON array)")
    parser.add_argument("--apply", action="store_true",
                         help="Actually write the confirmed mappings (default: dry-run only)")
    parser.add_argument("--recompute-backfilled", action="store_true",
                         help="Also permit overwriting stamps this tool itself wrote previously")
    parser.add_argument("--skeleton-out", metavar="PATH",
                         help="Where to write the unconfirmed-names skeleton (default: "
                              "<project>/.cognition/backfill-identity-map.skeleton.json)")
    args = parser.parse_args(argv)

    repo_path = Path(args.project_path).resolve()
    cognition_dir = repo_path / ".cognition"
    if not cognition_dir.exists():
        print(f"error: no .cognition/ directory at {repo_path}", file=sys.stderr)
        return 1

    # Precedence on an alias collision: --map wins over --map-file (applied
    # last, so it overwrites) -- a CLI arg is a more explicit, one-off
    # override of whatever the file says for that name (Vince's Train C
    # review, minor finding b).
    confirmed: dict[str, tuple[str, str]] = {}
    try:
        if args.map_file:
            confirmed.update(parse_map_file(Path(args.map_file)))
        confirmed.update(parse_map_args(args.map))
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    journal_path = cognition_dir / "journal.jsonl"
    mtime_before = journal_path.stat().st_mtime if journal_path.exists() else None

    storage = CognitionStorage(cognition_dir)
    plan = BackfillPlan(
        storage, recompute_backfilled=args.recompute_backfilled,
        confirmed=confirmed, repo_path=repo_path,
    )

    if args.apply:
        if journal_path.exists() and journal_path.stat().st_mtime != mtime_before:
            print(
                "error: journal changed since this run started (a live session "
                "may be writing) -- aborting without writing; re-run",
                file=sys.stderr,
            )
            return 3
        written = apply_plan(plan)
        print(f"Stamped {written} node(s).")
        return 0

    print(render_report(plan))
    unconfirmed = plan.unconfirmed_names()
    if unconfirmed:
        skeleton_path = Path(args.skeleton_out) if args.skeleton_out else (
            cognition_dir / "backfill-identity-map.skeleton.json"
        )
        skeleton_path.write_text(
            json.dumps(plan.skeleton(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        print(f"\nSkeleton map file written: {skeleton_path}")
        print("Edit it (fill/correct emails), then re-run with --map-file <path> --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
