# WP-Identity-Profiles — confirmed identity gate, profiles replace person nodes

**Status:** rev 3 — adversarially peer-reviewed, three blockers fixed, all
rulings in. Ready for implementation. Nothing implemented yet.
**Origin:** Colton's rulings 2026-09-12, tightening v0.37.0's identity work.
**Supersedes:** v0.37.0's "an unconfirmed git email is good enough to write"
threshold, which I chose and Colton has overridden.

---

## 0. The rulings

1. **Writes refused until identity is CONFIRMED** — not merely resolvable.
2. **Role and seniority confirmed too**, not just name and email.
3. **Profiles live in a committed per-person file; person nodes are retired.**
4. **A tiny machine-local pointer** says which profile drives this checkout.
5. **Required fields:** name, email, role, seniority, reports-to — where
   **"nobody" is valid**, and expected on solo projects.
6. **Block everyone on upgrade**, existing projects included, no escape hatch.
7. **Existing person nodes migrated, then removed.**
8. **The four person tool names kept**, repointed at profile files.

Ruling 3 first read "store both in the machine-local file, and make it source
controlled". Those cannot both hold: a committed file cannot answer "who is
driving THIS checkout" — a shared box or second machine would attribute everyone
to whoever committed first, the exact bug this work exists to kill. Resolved to
the split in rulings 3+4.

---

## 1. Storage

### 1a. `.cognition/people/<email-slug>.jsonl` — COMMITTED, per person

**Corrected in rev 2.** Rev 1 called profiles "a second record kind in the same
file", implying the existing machinery absorbs them. It does not:

- `people_facts._fold()` hard-whitelists `fact_set` / `fact_delete` /
  `fact_clear` and **silently drops anything else**, so profile records would
  vanish on fold.
- Env facts are keyed `email -> machine_key -> fact_key -> value`. That third
  level exists *because facts are machine-scoped*. **Profile fields are not
  machine-scoped**, so they need a two-level fold (`email -> field -> value`) and
  their own action vocabulary.

What is genuinely reused is the **file location and merge policy** — per-person
path, `email_slug()` naming, append-only JSONL, `merge=union` from hygiene v6 —
**not the fold logic**, which is a new profile registry.

`email_slug()` was checked and is sound for this: casefold-first, byte-level
percent-encoding with pinned lowercase hex, hash-suffix collapse past 40 chars.
Collision-free and deterministic for any address a human can type.

Per-person files remain load-bearing: two teammates write two different files, so
there is nothing to merge — which matters most on SVN, where there is no
union-merge at all and a shared roster would conflict on every commit.

**Fields need an explicit unset action** (mirroring `fact_delete`), not falsy
strings. "role was never written" and "role was deliberately cleared" must be
distinguishable, or §2's gate and §7 Q3 cannot be answered.

### 1b. `.cognition/identity.json` — MACHINE-LOCAL, never committed

One job: which profile is me on this checkout. Email plus name for display.
Already in the managed ignore list (hygiene v8) and in the SVN ignore guidance.

---

## 2. The gate

A write is allowed only when **all** hold:

1. `identity.json` names an email, **and**
2. a profile exists for that email, **and**
3. that profile has `role`, `seniority` and `reports_to` set — `reports_to` may be
   the literal `"nobody"`.

Refusal names *which* of the three is missing, so an agent asks one targeted
question rather than re-running setup.

**This runs on every write, where today there is zero I/O.** The profile reader
must therefore reuse `people_facts.py`'s catch-up discipline — stat-gated
re-reads, complete-line-only parsing for torn appends, truncation re-fold — not a
naive read-and-parse per call.

**Bootstrap exemption is a CLOSED LIST**, not a fuzzy category: `cognition_set_identity`,
the four person tools, and pure reads. Everything else is gated.

---

## 3. Tools

`cognition_set_identity(name, email, role, seniority, reports_to)` writes the
profile record **and** the local pointer in one call. `reports_to="nobody"` is
accepted and documented for solo projects; empty string is not, because a
skippable field gets skipped.

The four names are kept, repointed at profile files: `cognition_register_person`
writes someone's profile, `cognition_update_person` appends a change,
`cognition_get_person` reads a profile plus its env facts, `cognition_list_people`
enumerates profiles. Their docstrings must stop saying "person node".

Yes, `register_person` can still write someone else's profile — §7 Q1 is ruled
trust-based multi-writer, so this table stands as written.

### 3a. Profile tamper alert

Trust-based means anyone can correct anyone; the safeguard is that it cannot
happen invisibly. When a profile record for YOUR email was authored by SOMEONE
ELSE since your last session, the session-start digest says so loudly — naming who
changed which field, from what to what. A seniority change gets particular
prominence, because it silently reweights your own search results.

---

## 4. Migration

Per existing person node: write its fields to a profile, carry its
`profile_history` across, then remove the node.

Colton chose "migrate and remove" having been told removal loses the audit trail.
Carrying `profile_history` first means removal discards nothing uncopied.

**Three corrections from review:**

- **`reports_to_email: "" -> "nobody"` is a NAMED MIGRATION RULE.** Today "no
  manager" is stored as `""`; the new gate treats `""` as *unset*. Without this
  translation every migrated solo-project owner — the case ruling 5 calls
  expected — fails the gate immediately after migrating and is forced through the
  full prompt. This was the single most likely silent failure in rev 1.
- **Removal must go through `delete_cognition_node`, not `storage.remove_node`.**
  Person nodes are embedded; the bare graph method leaves stale vectors that
  surface dead ids in `cognition_search` and then 404 on `get_node`.
- **Removal is deferred to a second, separately-triggered step.** A migration that
  deletes cannot be idempotent-on-interrupt the way the hygiene pass is. Write all
  profiles first, verify, then remove — so an interruption leaves duplicates
  (harmless, resolvable) rather than a half-deleted roster.

Edge cases to handle explicitly: two person nodes sharing an email, a person node
with no email, migration racing a live session.

---

## 5. Consumers

**A single project-wide in-memory profile roster** must back all of these. Review
found `dashboard/api.py` carries a comment about a previously-fixed O(N²) bug in
`_list_people`/`_reports_to_registered`; porting these to per-file reads would
reintroduce it with real disk I/O per cell.

- `prime.py` — **understated in rev 1.** It builds `_RoleContext` (direct reports,
  manager-chain walk, "Your Team", "Your Manager's Recent Decisions", the identity
  header, and the `auto` personalize heuristic keyed on registered-person count)
  from **one in-memory PERSON scan per run**. File-backed profiles need a reverse
  index (email → subordinates) and a full-roster view — a purpose-built index, not
  a swap.
- `cognition_tools.py` — `_person_seniority_map` (**drives search ranking**, so a
  missed profile silently changes results), `_find_person_by_email`,
  `_resolve_person`, `_reports_to_cycle`
- `dashboard/api.py`, `backfill_identity.py`, `storage.py`/`models.py`, `config.py`

**Retiring the `PERSON` enum member:** replay is safe (`_replay_entry` stores
`type` as a raw string, no enum validation), but `_parse_node_type` and
`cognition_record`'s inline check both do `CognitionNodeType(node_type)` and will
reject `"person"` once the member is gone — so a node still typed `person`
(mid-migration, or a rolled-back version) becomes unfilterable through search and
record even though it physically exists. Acceptable if migration is near-instant;
must be stated, and ideally errors with "type retired" rather than "invalid".

---

## 6. Upgrade blast radius

**Every existing project stops recording on the first write after upgrade** —
Colton's own included — until a human answers five questions, per machine, per
teammate. No escape hatch, by ruling 6.

The session-start banner must name all five fields **and** the `"nobody"` option,
or the first post-upgrade experience is a guessing game.

Cross-project reads (`cognition_load_project`) read another project's people —
that project may be unmigrated, so the roster reader must tolerate both shapes
during the transition.

**Release gate:** this changes four tools' semantics and docstrings and adds
required params to `cognition_set_identity`, so CLAUDE.md's HARD RULE
tool-surface audit applies. Named here because the project's own history
(v0.36.0's `curation_token` gap) shows it gets missed when it is not.

---

## 7. Rulings needed before implementation

**Q1 — RESOLVED (Colton, 2026-09-12): profiles are TRUST-BASED MULTI-WRITER, with
a tamper alert.** "There is an aspect of trust in any team, it is acceptable." So
`register_person` keeps working for a manager pre-registering a teammate, §3's
tool table stands, and profiles match today's person-node posture rather than env
facts' self-only rule.

The accompanying safeguard, also Colton's: **if someone else modified your profile
since you last saw it, alert you loudly.** That converts the residual risk from
silent to visible, which is the right trade for a trust-based model — nobody is
prevented from correcting your role, but nobody can do it without you finding out.

Design (§3a) — the machinery already exists:
- Profile records are append-only and carry who wrote them, so "changed by someone
  other than me" is directly readable, not inferred.
- `last-seen.json` already stamps a per-email last-session timestamp (WP-TC14).
  Any profile record for YOUR email, authored by someone else, after that stamp,
  is an alert.
- The alert names who changed which field, from what to what. A seniority change
  deserves particular prominence because it silently reweights your search results.

Note this does reintroduce the concurrent-write case that self-only avoided: two
people appending to one profile file. On git, `merge=union` covers it (hygiene v6
already union-merges `people/*.jsonl`). **On SVN it does not** — pre-registration
plus self-confirmation on the same profile can conflict, resolved the same
keep-both-sides way as the journal. Worth stating in the SVN guidance rather than
discovering.

**Q2 — RESOLVED, adopting now.** A profile missing for a named identity is treated
as unconfirmed and re-prompts. Low-risk either way; the alternative is writing
memory under a profile nobody can read.

**Q3 — downstream of Q1 and §1a's unset action.** Whether the seniority fold
trusts the newest record unconditionally cannot be settled until those are.

**Q4 — RESOLVED (Colton, 2026-09-12): refuse, and say plainly it can never work
here.** No exception to ruling 6; the difference is in the message, not the
behaviour. A read-only checkout cannot persist `identity.json`, so the
bootstrap-exempt tool itself cannot succeed and no answer from a human would help.

The refusal must therefore distinguish two states that would otherwise read
identically:

- *identity not confirmed* — "ask the human these five fields, then call
  `cognition_set_identity`". Actionable.
- *checkout is read-only* — "identity can never be confirmed here, so recording is
  unavailable in this checkout". NOT actionable from inside, and saying so stops
  an agent looping on a prompt whose answer cannot be saved.

Consequence accepted: a read-only CI job that records memories today stops doing
so. That is correct under ruling 6, and the message is what keeps it from looking
like a bug.

Rejected: allowing an unconfirmed resolved identity through when the repo is
read-only (read-only is trivial to arrange, so it becomes the documented bypass),
and relocating the pointer to the home directory (one machine's home would then
silently govern attribution across every project on it).
