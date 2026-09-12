# WP-Identity-Profiles — IMPLEMENTATION PLAN

**Status:** rev 2 — peer-reviewed, two blockers fixed. Colton ruled ONE release
with the blockers fixed first (the reviewer recommended splitting; overridden
knowingly). Design settled in `docs/wp-identity-profiles-plan.md` rev 3.
**Scope:** two changes shipped together so users migrate once, not twice.
**Target:** v0.38.0.

---

## STEP 0 — shared local path accessor (BLOCKER FIX, before anything else)

**The bug this prevents is an email leak into the repo, not a tidiness issue.**
Six files' paths are built in five different modules, none of which
`git_hygiene.py` can reach:

| path built in | file |
|---|---|
| `identity.py` `identity_path()` | `identity.json` |
| `prime.py` (3 sites + a `.lock` sibling) | `onboard-declined`, `last-seen.json` |
| `storage.py` | `.last-rehydrate.json` |
| `backfill_identity.py` | `backfill-identity-map.skeleton.json` |
| `git_hygiene.py` `_read_flag`/`_write_flag` | `.git-hygiene-managed` |

Left unchanged while Part A moves files and drops the per-file ignore globs: the
move happens, `identity.py` reads the now-empty legacy path and reports
unconfirmed, the human re-answers, `write_confirmed_identity` recreates the file
**at the legacy path** — which the trimmed three-entry ignore list no longer
covers — and the next `git add .cognition/` commits a name and email. Same class
for `last-seen.json`, `onboard-declined`, `.last-rehydrate.json`.

**Build:** one accessor, `local_path(cognition_dir, filename)` — read prefers
`local/`, falls back to legacy; writes always go to `local/`. Repoint all six
sites. This ships and is tested BEFORE any move logic exists, so the fallback path
is proven while both locations are still legal.

### STEP 0b — the hygiene flag goes local-aware first (BLOCKER FIX)

`.git-hygiene-managed` both decides whether the versioned pass has run AND is one
of the files the pass moves. If its own read is not local-aware before the move
logic runs, every startup re-decides "not migrated" and re-attempts the move
forever; if its write lands legacy, it is unignored under the trimmed list. Its
read/write must go through STEP 0's accessor before anything else in Part A.

## A. `.cognition/local/` — one ignore entry instead of eight

### Why

Today every machine-local file needs its own ignore entry, a
`GIT_HYGIENE_VERSION` bump, and SVN users to re-copy globs. The list only ever
covers names someone remembered.

**Measured in the lab, not assumed.** With the current committed SVN ignore list,
`svn add --force` swept `identity.json` and a brand-new unlisted file straight
into version control, while `last-seen.json` and `onboard-declined` were skipped —
because only the latter were enumerated. Re-run with a single `local` entry:
`.cognition/local` marked `I`, and `svn add --force` added **nothing** from inside
it, including the unlisted file.

### What moves

Into `.cognition/local/`: `last-seen.json`(+`.tmp`), `identity.json`(+`.tmp`),
`onboard-declined`, `.last-rehydrate.json`, `.git-hygiene-managed`,
`backfill-identity-map.skeleton.json`.

**Not moved:** `*.lock` (written beside what they lock; changing lock paths mid-
upgrade means two versions using different paths, so the lock stops being mutually
exclusive — a real hazard for a cosmetic gain) and `chromadb/` (legacy compat).

Ignore list becomes three entries: `local/`, `*.lock`, `chromadb/`. A future
machine-local file needs **no ignore change at all**.

### Migration (`GIT_HYGIENE_VERSION` -> 9)

Path resolution is STEP 0's accessor, not logic inside the pass. The pass only
MOVES, and must do so safely:

- **Under one dedicated lock** (`_acquire_lock`, as the existing writers use), so
  two server processes starting together cannot both move.
- **Atomic rename/replace, never copy-then-delete**, so an interruption cannot
  leave a half-written destination.
- **Skip when the destination already exists and is non-empty** — never clobber a
  newer copy a faster process already wrote (`last-seen.json` may have been
  re-stamped in the interim).
- **Defer on `PermissionError`** (Windows file-in-use) — leave the source, retry
  next pass, rather than truncating a write in flight.

Losing STEP 0's legacy-read fallback would reset `last-seen` (spurious "Since You
Were Gone"), `onboard-declined` (re-prompts), and the hygiene flag (pass re-runs)
for every existing install — which is why the accessor ships before the move.

The SVN guidance in `readme.py` and README loses its "strip the trailing slash
from `chromadb/`" special case only if `chromadb` is re-emitted without the slash;
keep the slash for git and document the SVN form as `chromadb`. Simpler: the
guidance now lists three short globs, so transcribing them is no longer the
drift-prone act it was.

---

## B. Confirmed-identity gate and profiles

Build order, each step independently testable:

### B1. Profile store (`cognition/profiles.py`, new)

**Refined from the design's "same file" after review — flag for Colton.** Profiles
go in a SEPARATE file beside the facts file: `.cognition/people/<slug>.profile.jsonl`
alongside the existing `<slug>.jsonl`. Same directory, same `email_slug()`, same
`merge=union` (glob extended). This still satisfies the ruling that a person's
identity lives in their own per-person file; only the physical split differs.

Why: `people_facts.py`'s docstring states that self-only writes are "what makes
`merge=union` safe" for that file. Profiles are ruled trust-based MULTI-writer, so
interleaving them into the same file falsifies a documented invariant a future
maintainer will reason from. Separate files keep single-writer and multi-writer
policies physically apart.

The directory-scanning/catch-up engine is **factored out and shared** by both
registries, parameterised by action vocabulary and fold callback — not
copy-pasted. The Windows racy-mtime guard was found once by CI the hard way;
writing it twice means two chances to get it wrong, and two directory sweeps per
cycle instead of one.

Profiles use **their own two-level fold**
(`email -> field -> value`) — `people_facts._fold()` whitelists only `fact_*` and
silently drops anything else, so profiles cannot ride its fold.

Records: `profile_set` (field, value, by, at), `profile_unset` (field, by, at).
An explicit unset is required so "never written" and "deliberately cleared" are
distinguishable.

Fold is last-write-wins per field. Registry mirrors `PeopleFactsRegistry`'s
catch-up discipline: stat-gated re-read, complete-line-only parsing for torn
appends, truncation re-fold.

**One project-wide roster object** backs every consumer, with a reverse index
(email -> direct reports). Building it per-call would reintroduce the O(N²) roster
bug `dashboard/api.py` already carries a comment about.

### B2. Gate

Allowed only when: `identity.json` names an email; a profile exists for it; and
that profile has `role`, `seniority`, `reports_to` set (`"nobody"` valid). The
refusal names which of the three is missing.

**Read-only checkout** (ruling Q4): when `identity.json` cannot be written, refuse
with a *different* message saying confirmation can never succeed here — not the
actionable five-question prompt.

Bootstrap exemption is a closed list: `cognition_set_identity`, the four person
tools, and pure reads.

### B3. `cognition_set_identity(name, email, role, seniority, reports_to)`

Writes the profile records and the local pointer in one call. `reports_to`
accepts an email or the literal `"nobody"`; empty string is rejected.

### B4. Person tools repointed

Four names kept, now reading/writing profiles. Docstrings stop saying "person
node". Trust-based multi-writer (ruling Q1) — `register_person` still writes
someone else's profile.

### B5. Profile tamper alert

A profile record for YOUR email authored by someone else after your `last-seen`
stamp raises a loud session-start alert naming who changed which field, from what
to what. Seniority changes get prominence — they reweight your search results.

### B6. Consumers

`prime.py` (`_RoleContext`, manager chain, "Your Team", the `auto` personalize
heuristic), `_person_seniority_map` (**search ranking** — a missed profile
silently changes results), `_find_person_by_email`, `_resolve_person`,
`_reports_to_cycle`, `dashboard/api.py`, `backfill_identity.py`. All read the B1
roster.

### B7. Migration of person nodes

**Phase 1 runs AUTOMATICALLY at startup, versioned like the hygiene pass**, so it
completes before the gate is ever evaluated. If it were manual, the window between
upgrade and someone remembering to run it would leave `_person_seniority_map` with
ZERO profiles — silently removing all seniority weighting from search ranking —
and would re-ask all five questions of people whose person node was already
complete, which is exactly what migration exists to prevent.

Two phases, deliberately separate:

1. **Write profiles** from every person node, carrying `profile_history` across,
   translating `reports_to_email: "" -> "nobody"` (without this, every migrated
   solo owner fails the gate immediately — the case the ruling calls expected).
2. **Remove nodes**, separately triggered, via `delete_cognition_node` so
   embeddings are purged. A bare `storage.remove_node` leaves stale vectors that
   surface dead ids in search and 404 on `get_node`.

Splitting the phases means an interruption leaves duplicates (harmless) rather
than a half-deleted roster.

Edge cases: two person nodes sharing an email, a person node with no email,
migration racing a live session.

`PERSON` enum retirement is replay-safe (`_replay_entry` stores `type` as a raw
string) but `_parse_node_type` will reject `"person"`, so any still-typed node
becomes unfilterable. Error with "type retired", not "invalid".

### B8. Docs, surface, release

README + `readme.py` (identity section, SVN guidance, the new three-glob ignore
list), `whats-new.json`, CHANGELOG, three manifests, `uv lock`, parity rows if the
tool surface changes, and **the HARD RULE tool-surface audit** — named here
because v0.36.0 shows it gets missed when it is not.

---

## C. Gates before pushing

`pyright` 0 (the baseline that was red for a month), `ruff`, both render `--check`,
full suite **with git and SVN identity stripped from the environment** so the run
does not depend on this machine, plus the repeat-run check on any test touching
concurrency.

---

## D. Risk register

| risk | mitigation |
|---|---|
| Every project blocked on upgrade | Ruling 6, deliberate. Banner must name all five fields and `"nobody"` |
| Legacy machine-local files orphaned | Read-both-locations fallback in A |
| Migration interrupted | Phases split; profiles written before any removal |
| Search ranking silently changes | Roster covers every consumer of seniority; test asserts ranking parity |
| Lock path change breaks mutual exclusion | `*.lock` deliberately not moved |
| Tool-surface drift | Audit in B8, gates in C |
