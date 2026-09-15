# WP-Journal-Shards — retire the mono journal for per-user shards

**Status:** rev 4 — 2026-09-14. Rev 3 re-baselined against v0.40.0; its sonnet
adversarial review (4 blockers, 4 majors, 3 minors) is folded in as §11, which
OVERRIDES the sections it names. All rulings in hand; ready to implement.

**Rulings in force**
- 2026-09-11 (Colton): per-user journal shards replace `.cognition/journal.jsonl`; the
  mono journal is retired; a migration tool ships; ONE shard per USER — no per-agent,
  per-worktree or per-machine key. Worktree divergence between agents of the same user
  is accepted; the shared-worktree manual flush protocol stays for that topology.
- 2026-09-11 (discovery `b51f6e6c50bd`): confirmed identity is the prerequisite.
  Shipped v0.37.0–v0.40.0 — every write already requires a confirmed identity with a
  complete profile, bound to machine/account/folder. The empty-email blocker (rev 2
  §2a) is therefore gone: there is no write without a confirmed email.
- 2026-09-14 (decision `74a13e5998ed`): BOTH phases ship in ONE release. Projects
  AUTO-ADOPT shards on their first write after upgrading. Legacy-journal growth after
  adoption is ENFORCED with a loud warning on git AND SVN.

---

## 1. What changes on disk

```
.cognition/
  journal.jsonl                  legacy: frozen, read-only for this plugin, never rewritten
  journal/
    <email_slug>.jsonl           one shard per person, committed; append-only
  people/                        unchanged (profiles, env facts)
  local/                         unchanged (machine-local)
```

- Shards live in their own `journal/` directory, NOT in `people/`: both registries in
  `people/` fold every `*.jsonl` they own, and sharing that directory would need a
  third reserved suffix for no benefit.
- File name: `people_facts.email_slug(confirmed_email) + ".jsonl"` — the same
  deterministic, casefolded, percent-encoded slug the profiles use.
- **The legacy journal moves no data and is never split** (discovery `f161c4063636`:
  78% of its lines carry no identity). It stays a replay input forever.

What does NOT change: `local/` contents, the search index location, local-only
documents, profiles, env facts, the identity gate.

## 2. Shard line format

```json
{"action": "update_node", "data": {...}, "at": "2026-09-14T22:55:03.348593+00:00"}
```

- Identical to a legacy line plus `at` — the write time, microseconds, UTC.
- The writer is the file. No per-line `by`: the gate already stamps identity into
  node metadata where it matters, and the shard name is authoritative for routing.
- First line of every shard, written once when the file is created:
  `{"action": "shard_start", "data": {"legacy_bytes": N, "legacy_sha256": H,
  "plugin_version": V}, "at": ...}` — the adoption record (§6). Replay ignores it for
  the graph.

## 3. Replay engine: a file set, ordered by stamps not by arrival

### 3a. File set and per-file state

`CognitionStorage` replays `journal.jsonl` plus every `journal/*.jsonl`, each with its
own `FileState` (offset, prefix hasher, mtime) — the mechanism
`jsonl_dir_registry.FileState` already implements for `people/`. Directory discovery
reuses its dir-mtime gate including the 2-second racy window, and a process calls
`register_own_write` on its own shard so its first write never waits on discovery.

### 3b. Rehydrate detection per file, rebuild globally (rev 2 §4a blocker)

Detected per file, exactly today's three conditions, each scoped to ONE file:

1. the file shrank below its offset;
2. its prefix hash no longer matches what was replayed from it (C-3);
3. its offset is 0, the file is non-empty, and this process has already applied or
   appended something **from/to that file** (today's "offset 0, graph non-empty",
   narrowed from "the graph" to "this file's contribution").

A brand-new shard — offset 0, nothing contributed — is ordinary discovery and never
resets anything. When a condition fires, the store rebuilds from ALL files (graph,
index, every file state) and runs today's identity-based loss check. A full rebuild is
always correct because application is order-independent (§3c). With only the legacy
file present, behaviour is byte-for-byte today's — the existing suite is the proof.

### 3c. Deterministic application: last-writer-wins by stamp (rev 2 §4c blocker, widened)

Rev 2 proposed sorting shard names. That only fixes a cold start: a running process
catches up on whichever file changed first, so two processes still apply the same two
`update_node` lines in different orders and diverge permanently. `update_node` lines
today carry no time at all.

So every entry gets a **stamp**, and conflicting writes resolve by stamp, not arrival:

- stamp = `(tier, at, file_name, byte_offset)`; legacy lines are tier 0 (`at` empty,
  ordered by offset); shard lines are tier 1.
- **Node attributes**: a per-node, per-attribute stamp map. `add_node` and
  `update_node` set an attribute only when their stamp is ≥ the stored one (≥ keeps
  re-reads idempotent). An `add_node` read after a newer `update_node` does not undo it.
- **Node removal**: a tombstone stamp per node id. Entries for that id stamped below it
  are ignored (not deferred, not warned); an `add_node` stamped above it re-creates.
- **Edges**: a stamp per `(from, to, type)` for both presence and removal; add/remove
  apply only when newer.
- **Clock skew**: writers stamp `max(now, stored stamp for that attribute/edge + 1µs)`,
  the same rule `profiles._winning_at` already uses, so a teammate with a fast clock
  cannot make later edits silently lose.
- Deferral (WP-5) runs once over the whole pass across all files, as today.

Residual, stated: `metadata` is replaced as one attribute (today's semantics), so two
people changing different metadata keys of one node at the same moment keep only the
later write. The mono journal has this today; sharding does not worsen it.

## 4. Write routing

- Every journal write goes to the shard of the checkout's **confirmed identity**,
  resolved from `identity.read_confirmed_identity` at append time (cached, re-read on
  the identity file's mtime change so `cognition_set_identity` takes effect at once).
- **No confirmed identity → no journal write.** `_append_journal` raises a typed error.
  Tool paths are already gated, so users never see it. Internal writes with no human in
  the loop (the startup deterministic-edge sweep in an unconfirmed checkout) catch it
  and skip: deterministic edges are idempotent and the next confirmed session makes them.
- The plugin **never appends to the legacy journal again.**
- Writers per path (from the inventory): tool writes, curation edges and stamps,
  task transitions, dashboard deletes, deterministic edges, and the remap/backfill CLIs
  all run in a checkout and go to that checkout's confirmed person's shard. Nothing
  needs a new identity parameter: routing is by file, and attribution inside node
  metadata is unchanged.

## 5. Adoption and migration

- **Auto-adopt:** the first journal write after upgrading creates the person's shard
  with its `shard_start` line. No command needed; nothing is moved.
- **Migration tool** `vibe-cognition-journal` (also `python -m
  vibe_cognition.cognition.journal_shards`): `status` (shards, entries per shard,
  legacy size, adoption time, stragglers), `adopt` (create your shard now, for teams
  that want to switch before anyone writes). Moves no data, never rewrites the legacy
  journal.
- A project never touched by an upgraded plugin keeps working unchanged.

## 6. Enforcement: legacy growth after adoption (Q3)

- **Adoption time** = the earliest `shard_start.at` in the project; **adoption size** =
  that line's `legacy_bytes`.
- Legacy lines past the adoption size whose own timestamp (`add_node.timestamp`,
  `add_edge.timestamp`, a tombstone's `removed_at`) is **after** adoption time were
  written by a plugin that does not shard — a straggler. Lines without a timestamp, or
  timestamped before adoption, are pre-upgrade work that merged in late: replayed
  normally, not warned about.
- Surfaces: a session-start warning naming the count, the latest time and the authors
  found on those lines ("update vibe-cognition to vX"), and a `get_status` key.
  Same on git and SVN.
- Straggler lines are still replayed — nothing is lost — but rank below every shard
  write (tier 0).

## 7. Version control

- **git:** hygiene v10 adds `.cognition/journal/*.jsonl merge=union` (one person in two
  clones or worktrees still appends to one shard). The legacy rule stays.
- **SVN:** the automatic setup already schedules new files; shard files are ordinary
  committed files. A same-person conflict (two checkouts of one person) is resolved by
  `resolve_journal`, which already covers every `.cognition/**/*.jsonl`.
- Between-session loss detection (`journal_watch`) is graph-level and unchanged;
  `journal_source` extends to report the shard directory too.

## 8. Surfaces (from the 2026-09-14 inventory)

| surface | change |
|---|---|
| `storage.py` | file set, per-file state, stamps, routing (§3, §4) |
| `jsonl_dir_registry.py` | reuse `FileState` + discovery; no behaviour change for `people/` |
| `journal_io.py` | unchanged (already path-parameterised) |
| `snapshot_cli.py`, `snapshot_journal` | snapshot the whole set into a directory, each file under its own lock |
| `backfill_identity.py`, `remap_identity.py` | concurrency guard covers every journal file; blame covers shards |
| `cognition_load_project` | accept a project with `journal.jsonl` OR any shard |
| `journal_watch.py`, `svn_hygiene.py`, `prime.py` recovery text | name the shard directory |
| `git_hygiene.py` | v10 rule |
| `get_status` | new `journal` key (files, shards, writing_to, stragglers) — tool-surface audit |
| `cognition_reload` docstring, `readme.py`, README, `docs/topology-guide.md`, `agents-src/plan.md`, SKILL.md | describe legacy + shards |
| tests | shared helper reading all journal lines; 24 files touched |

## 9. Verification required before pin

1. Existing suite green with the legacy file as the only member (behaviour preserved).
2. New-shard discovery never resets another file's state or the graph.
3. Two processes applying the same cross-shard writes in opposite arrival orders
   converge to identical graphs (attributes, tombstones, edges), including a skewed
   clock.
4. A replaced or truncated shard triggers a rebuild and the identity-based loss alert.
5. Routing: writes land in the confirmed person's shard; switching identity switches
   shards; no identity writes nothing.
6. Enforcement: straggler lines warn; late-merged pre-adoption lines do not.
7. git merge of two clones of one person's shard; real-svn lab: two people, no
   conflict; one person in two checkouts, conflict resolved by `resolve_journal`.
8. End-to-end on real git and SVN with two teammates, one on the previous plugin.
9. Sonnet adversarial review; tool-surface audit (`get_status`); `pytest -m svn`.

## 11. Rev 4 — review findings folded in (overrides §3–§8 where named)

**B1 dashboard deletes (§4).** The dashboard is identity-free by design, so in an
unconfirmed checkout its delete would now raise. It returns a clean `{"error": ...}`
naming `cognition_set_identity` instead — the same write rule as the tools; no
identity-free pseudo-shard (that would break one-shard-per-user).

**B2 startup deterministic-edge sweep (§4; `server.py:145-177`, `:472`).** Skipped
entirely when the checkout has no confirmed identity; per-node guard so one failure
cannot abort the rest. `server.py` joins the surfaces table.

**B3 re-add past a tombstone (§3c).** The skew bump covers every stamped decision, not
only attributes: a writer re-adding a tombstoned id stamps
`max(now, tombstone + 1µs)`; same for edge re-add past an edge tombstone.

**B4 own writes (§3c).** Local application goes through the same stamp comparison as
replay — one code path. Because the writer stamps past everything its caught-up
graph holds (`_synced()` catches up first), its write wins against all it has seen
and can lose only to a genuinely concurrent write with a later stamp, which is correct
last-writer-wins. Tool results keep reporting success for the write they made; the
docstrings of `cognition_update_node` / `cognition_update_task` state the rule.

**M1 union merges must not rebuild the world (§3b).** Each file state also keeps the
set of line hashes applied from it. On a prefix-hash mismatch the file is re-read: if
every previously applied line is still present, the merge only INSERTED lines, so
only the new lines are applied (stamps make this order-free) — no reset. A global
rebuild plus loss check happens only when lines vanished, or for the legacy file
(tier-0 lines are ordered by position, so an insertion there still rebuilds, as
today). Consequence for §3c: the tie-breaker is the **line's content hash**, not its
byte offset, so a merge that shifts offsets cannot reorder decisions:
stamp = `(tier, at, file_name, line_sha256)` for shards, `(0, line_index)` for legacy.

**M2 cross-shard deferral (§3c).** Deferred entries stay in a pending queue across
catch-up passes instead of being dropped after one retry; each pass retries them. A
full hydration enumerates every file, so anything still pending after one is genuinely
missing: warned once, counted in `get_status`.

**M3 CLIs (§8).** The remap/backfill concurrency guard compares a signature over every
journal file (legacy + all shards: name, size, mtime), before planning and right
before applying. Both catch the no-identity error and print "confirm your identity in
this checkout first".

**M4 id collision (§3c).** An `add_node` for a LIVE id whose `type`, `timestamp` or
`author` differ from the stored node is a collision, not a re-add: the earlier-stamped
node is kept whole (no field-by-field merge), the collision is logged and counted in
`get_status`. Residual, stated: later `update_node` lines for that id apply to the
surviving node.

**m1** `svn_hygiene` exclusion check covers `journal/` and each shard. **m2**
`journal_watch.journal_source` keys on the `journal/` directory once shards exist;
covered by a real-svn lab test after months-frozen legacy is simulated. **m3** the
writer identity is resolved once per `_synced()` operation, not per appended line.

## 10. Out of scope

Re-authoring history, backfilling identity onto the legacy 78%, splitting or rewriting
the legacy journal, per-worktree shard keys, per-key metadata merge.
