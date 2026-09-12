# WP-Journal-Shards — retire the mono journal for per-user shards

**Status:** rev 2 — adversarially peer-reviewed, two blockers found. **One of them
requires a ruling from Colton before this WP can proceed** (§6 Q1).
**Origin:** Colton's ruling 2026-09-11, following the WP-SVN-Hygiene lab which
proved SVN cannot union-merge, so conflict *recovery* is the ceiling for SVN teams
unless the conflict stops existing.

---

## 0. Colton's ruling, and where review says it does not reach

1. Per-user journal shards replace the single `.cognition/journal.jsonl`.
2. The mono journal is **retired entirely**.
3. A **migration tool ships** with it.
4. **Shards are per USER, not per agent.** Agents in worktrees do not get their
   own shards — "that gets too messy."

Points 1–3 survive review intact. **Point 4, read as "no per-worktree shards
either", leaves the most-used topology in this codebase unfixed** — see §6 Q1.
The distinction that matters and was not drawn when the ruling was given:
*per-agent* and *per-working-copy* are different axes.

---

## 1. What sharding buys — corrected

| | today (mono + `merge=union`) | sharded |
|---|---|---|
| git, separate clones | union-merge resolves silently | no conflict exists |
| git, shared checkout + worktrees | manager-flush protocol, by hand | **UNCHANGED — see §6 Q1** |
| **SVN, separate working copies** | **conflicts; manual resolver is the ceiling** | **no conflict exists** |
| whole-file rewrite under byte-offset replay (C-3) | every merge touches it | rarer, not gone |

**Rev 1 claimed the shared-worktree manager-flush constraint retires. It does
not.** Review demonstrated why (§6 Q1). Two liabilities do retire: the
`merge=union` dependency and most of the SVN story.

**What sharding does NOT solve**, stated plainly so nothing is over-promised:

- concurrent appends by several agents running as the same user **in one working
  copy** — they share that shard file; `journal_io`'s `O_APPEND` + lock machinery
  stays and does not become removable
- divergence between **separate checkouts that resolve to the same shard name**
  (the blocker, §6 Q1)

---

## 2. The migration is NOT a split — confirmed sound by review

Measured on this repo's journal (3,261 lines):

| identity present | lines | share |
|---|---|---|
| `recorded_by` / `created_by` | **0** | 0% |
| free-text `author` only | 722 | 22.1% |
| **nothing at all** | **2,539** | **77.9%** |

The 2,539 are exactly the non-node actions — `add_edge` 1511, `update_node` 1011,
`remove_node` 16, `remove_edge` 1. Review verified this against the code:
`CognitionEdge` carries no identity field at all, and `update_node`'s line is
`{"id": node_id, **kwargs}` with identity only if a caller happens to pass it.

The 722 `author` values are agent names, not people: `Colton Dyck` 429, `Vince`
151, `Vorpid` 75, `Vince (manager, for Colton)` 18, `vince` 15, `curate-orchestrator`
6, `Claude/Fable` 5.

**Review explicitly checked whether this plan over-applies the `backfill_identity`
no-auto-stamp ruling (2026-07-16, decision `833e9f67de4d`) and found it does
not** — inferring identity for historical authors and stamping it onto node
metadata (forbidden) is a different act from routing a *new* line to a file by the
writing process's own resolved identity (proposed here).

### The lawful migration: freeze, don't split

`.cognition/journal.jsonl` becomes a **read-only legacy shard** — stops growing,
never rewritten, never re-authored. New writes go to a per-identity shard file.
Replay reads the legacy journal first, then every shard. A project that never
migrates keeps working. The migration tool moves **no data**.

### 2a. BLOCKER-adjacent: the empty-email case breaks the guarantee for SVN users

`resolve_git_identity` reads only git config **files** and, finding none, degrades
to `{"name": getpass.getuser(), "email": ""}`. And `email_slug("")` returns `""`.

**A pure SVN working copy has no `.git` at all.** If the machine also lacks a
`~/.gitconfig` with `user.email` — entirely normal for an SVN shop — every user on
that machine collides into the same degenerate shard filename, silently defeating
"no two people ever write the same file" for **exactly the population this WP
exists to serve.**

Not optional to resolve. Options: hard-require a configured email before shard
writes are allowed (fail loud, name the fix); or fall back to an OS-user+machine
key with loud disclosure. **Silent collision is not on the menu.**

---

## 3. Reuse already on the shelf — confirmed

- **`people_facts.email_slug(email)`** — shipped, deterministic, filesystem-safe.
- **`.cognition/people/*.jsonl`** — the per-identity-file pattern, already
  committed and union-merged since hygiene v6.
- **`journal_io`** — append/lock machinery is path-parameterized and transfers
  cleanly per review.
- **`people_facts._FileState`** — and see §4, it already solves the trap the
  replay engine is about to fall into.

---

## 4. The replay engine — one blocker, one unnamed correctness class

### 4a. BLOCKER: rehydrate detection does not generalize by duplication

`_catch_up` contains:

```python
elif self._offset == 0 and self._graph.number_of_nodes() > 0:
    rehydrate = True
```

Sound today: `self._graph` is populated *entirely* by that one file, so "graph
non-empty at offset 0" is real evidence that file was replaced.

**Under sharding the graph is populated by every shard, so this becomes the normal
case, not an anomaly.** The first time any process opens a *new* shard — a new
contributor's first write, or simply a file this process has not seen — that
file's offset is legitimately 0 while the shared graph is already non-empty from
others. As written it fires `_rehydrate_reset()`, wiping **the entire graph,
offset and hasher for the whole store**, and risks a spurious loss-visibility
WARNING plus sidecar flag via `_record_rehydrate`.

**Rev 1's claim that "every one of these becomes per-shard state" is wrong for
this check.** It needs a *semantic* rewrite, not a mechanical per-file copy —
per-file prefix-hash detection plus an explicit "has this file ever contributed"
record, which is exactly what `people_facts._FileState.emails` /
`_drop_contribution` already do correctly. Required test: **discovering a
brand-new shard must never reset another shard's state or the shared graph.**

### 4b. Dependency deferral: sound, and sufficient

Review confirmed every dependent action (`add_edge`, `update_node`,
`remove_node`, `remove_edge`) depends at most **one hop** on a prior unconditional
`add_node`, so a single global deferred-retry pass genuinely covers the
missing-target case. Rev 1 was right here.

### 4c. NEW: concurrent same-field updates can diverge permanently

One file gives every process an identical byte-order total order. **N files give
no natural total order at all.** Two `update_node` calls on the same field of the
same node arriving from different shards are last-write-wins *by replay
application order* — and if two processes enumerate shards in different orders
(directory listing order is not guaranteed stable or identical across processes or
OSes), they converge to **different, self-consistent, permanently divergent**
values, with neither detecting it.

This is a correctness class the single-journal design structurally cannot have,
and rev 1 did not name it.

**Required:** a deterministic canonical cross-shard application order (sorted
shard filenames, applied in that fixed order on every pass in every process),
stated as a hard requirement and tested. Plus an audit of which write paths can be
mutated by more than one identity — curation, `cognition_mark_curated`, task
status transitions, dashboard edits — since those are where this bites.

---

## 5. Surfaces this touches

- **`backfill_identity.py`** — hardcodes `cognition_dir / "journal.jsonl"` and
  gates `--apply` on that one file's mtime being unchanged since the dry run.
  Under sharding a concurrent write to any *shard* races `--apply` undetected —
  the exact bug class the guard exists to prevent, silently reintroduced.
- **`journal_io.snapshot_journal` / `snapshot_cli.py`** — locks-then-copies **one**
  file. There is no defined semantics for "capture N shards at a mutually
  consistent instant"; a snapshot can straddle shard A at T1 and shard B at T2.
  Idempotent replay makes this survivable, but it is an unstated design gap.
- **Test surface** — 16 existing test files reference `journal.jsonl` /
  `JOURNAL_FILENAME` directly. Decide per file: degenerate N=1 shard set, or a
  per-shard variant.
- **Git hygiene** (`merge=union` for shards, `GIT_HYGIENE_VERSION` bump),
  **SVN hygiene**, **`readme.py`** (its "Team setup (git)" premise changes),
  **`get_status`** (tool-surface audit HARD RULE if the shape changes),
  **dashboard**, **`prime.py`**, **hooks**, plus README / CHANGELOG /
  whats-new / three-manifest bump.

---

## 6. Open questions for Colton

### Q1 — BLOCKING. Does the shard key need a per-working-copy dimension?

**The problem.** A git worktree has its own physical copy of `.cognition/`,
sharing only `.git/objects`. `resolve_git_identity` **cannot read local git config
in a worktree** — `_local_config_path` returns `None` when `.git` is a file
(gitlink), by explicit design — so it falls through to global config, which is
identical across every worktree on the machine. `platform.node()` is identical
too. So under "per user" (with or without a machine dimension), **manager and
subordinate agents in different worktrees resolve to the same shard filename while
holding separate physical files** — they diverge exactly as they diverge on
`journal.jsonl` today, and reconciling them needs the identical manual flush
protocol.

**This is the manager/subordinate-in-worktree pattern this codebase runs on
daily.** Not an edge case.

**The distinction not drawn when the ruling was given:** *per-agent* and
*per-working-copy* are different axes. A per-clone key does **not** give each agent
its own shard — several agents in one worktree still share one file. It gives each
*checkout* its own file, which is where the divergence actually is.

Options in §7.

### Q2 — What happens to a project that never migrates?
Proposal: nothing breaks; the legacy journal is still read and the pass starts
writing shards on first run after upgrade. Confirm silent auto-adoption is wanted
versus explicit opt-in via the migration CLI.

### Q3 — HARDENED. Stragglers on old plugin versions: accept, or enforce?
Rev 1 treated this as a shrug. Review showed **the safety net is asymmetric**: for
git, a teammate still appending to `journal.jsonl` is safe because `merge=union`
stays on that file. **For SVN there is no backstop at all.** So on an SVN team, one
teammate on an old plugin reproduces the original unsolved problem for as long as
migration is incomplete — and completeness is entirely voluntary under Q2's silent
auto-adoption. For the one VCS this WP cannot afford to be soft about, this should
lean toward enforcement: detect an SVN project whose `journal.jsonl` mtime advanced
*after* a sharded-project marker was stamped, and warn loudly or refuse.

---

## 7. Phasing — review's recommendation, and I endorse it

**This should not ship as one WP.** The two blockers live in different places, and
the point of no return is not "code shipped" but **"first committed multi-shard
state"** — after that, un-sharding needs exactly the history rewrite §2 rules out.

- **Phase 1 — replay generalization only.** Refactor `_catch_up` / the append path
  to operate over a *file set*, with the existing mono journal as the sole member.
  Behavior-preserving, proven by the existing suite as the N=1 case, zero
  user-visible change, cheaply revertible because no on-disk format changes. §4a
  and §4c get fixed and tested here.
- **Phase 2 — write-side fan-out.** Shard routing, the migration CLI, VCS hygiene,
  docs. Only after Phase 1 has soaked and Q1 is ruled, including a
  worktree-collision test and a cross-shard concurrent-update-ordering test.

---

## 8. Out of scope

Re-authoring history, backfilling identity onto the 78%, rewriting the legacy
journal, and removing `journal_io`'s local locking.
