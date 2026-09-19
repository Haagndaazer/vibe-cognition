# WP-Append-Only-Replay — a shorter journal never destroys live nodes

Status: rev 3 APPROVED for build, redesigned after FOUR peer reviews (engine + ops on rev 1, mirror
integrity + gates/tests on rev 2). Ruling: Colton, 2026-09-18 — full append-only replay, and
repair the person's own file automatically. Target: v0.44.0.

Reported by Violet (vibe-memory) 2026-09-18; incident df1dd916a2fa there, with earlier
occurrences c78351233969, 3483de902c18, 900c43fbe51c and one older event.

## 1. Problem

`_rebuild` wipes the graph (`_reset_replay_state`, storage.py:1268) and replays every journal
file from the top, so whatever is on disk wins — including a file older than the live graph.
`_scan_file` returns "rebuild" whenever a file shrank or a line it applied is gone
(storage.py:1349-1376).

An ordinary `git checkout -- .cognition` put a committed 5KB copy of a live 46KB shard on
disk; the next rehydrate reset the graph down to it. 15 live nodes destroyed. Across five
recorded events: 1, 2, 15, 23 and 93 nodes; two never recovered.

**Why converging down is never right:** a deliberate deletion is an APPENDED `remove_node`
tombstone, so a node can only vanish from a file's contents if the file was replaced,
truncated or rolled back.

**Not claimed:** incident 3483de902c18 (two episodes vanished mid-curation, no git command
recorded) is NOT diagnosed as this bug. Curation analyzers are read-only, so its mechanism is
unknown. This WP does not claim to fix it; it stays open. Earlier revisions of this plan
asserted otherwise, which a review caught.

## 2. Two rejected designs, and why

**Rev 1 — keep the graph in memory across a rebuild.** Retention is per-process, so the
reported sequence (end session, `git checkout`, reopen) has no witness and gets nothing. It
also splits the graph between a long-running and a fresh process, and blinds
`_record_rehydrate`, whose loss signal is exactly "nodes left the graph".

**Rev 2 — a machine-local mirror of every applied line of every journal file.** Rejected on
four counts, all from review:
- Legacy stamps are positional, so a union of "mirror order then file order" can pick a
  DIFFERENT last-writer than a clean replay of the same bytes elsewhere, whenever a rewrite
  both drops a line and splices one in.
- A file-level divergence report naming missing node ids sits BELOW the visibility choke
  point, leaking the existence of teammates' personal constraints (contradicting
  wp-personal-constraints §2.3).
- It mirrors every teammate's replayed content onto every machine, forever, with no pruning.
- It gated the read-only escape hatch while leaving the disk-writing heal ungated, and left
  no way to deliberately redact your own shard.

## 3. Design — two separate, smaller mechanisms

The two jobs are different and only one of them needs raw lines.

### 3.1 Detection: extend the existing id-level loss check

`journal_watch.py` already answers "did something I had seen vanish": a machine-local
`known-node-ids.json` snapshot plus a per-node log, checkout-bound via `checkout_binding.py`
so a record that travelled to another machine/account/folder is ignored rather than trusted.
It is id-level, not content-level — no teammate content is duplicated anywhere.

Changes:
- It currently runs at startup only and is scoped to SVN working copies
  (journal_watch.py:16). Extend it to run on any VCS, and to run mid-session as part of
  catch-up when a file's scan verdict is "rebuild", not only at construction.
- Its verdict feeds the retention decision below.

Nothing about ids leaves the machine, and the existing binding check comes along for free —
two of rev 2's blockers disappear because the mechanism is id-based and already bound.

### 3.2 Retention: replay stops destroying, on the evidence of the check

When a file's scan says "rebuild" and the id-level check says entries this checkout had
verifiably seen are now absent from disk, `_rebuild` keeps those nodes rather than dropping
them: the graph, stamps, tombstones and reference index survive the rebuild, and the file's
per-file state alone is reset. Removal still happens only through a replayed `remove_node`.

Legacy positional stamps are NOT reconstructed from any cache: retained nodes keep the stamps
they already have, and disk lines keep their own file order. There is no union-ordering
problem because nothing re-derives an order that disk no longer has.

### 3.3 Healing: a ledger of MY OWN appends, written at append time

The only file this checkout may repair is the shard it writes, and the only lines it needs are
the ones it wrote itself. So: a machine-local, checkout-bound ledger of this checkout's own
appended lines, verbatim.

- **Written synchronously inside `_append_journal`**, at the moment the line is appended —
  NOT on the next catch-up. A review found this is the crux: today a self-written line is only
  re-read on a subsequent catch-up (storage.py:1246-1264), so a ledger populated at read time
  would miss exactly the reported case (write, session ends, nothing reads that shard again).
- Holds every kind of line — `add_node`, `update_node`, `remove_node`, edges — never a
  per-node summary, so a healed file can never re-add a node without its tombstone.
- On detecting that the own shard lost lines this ledger holds, re-append them verbatim, in
  ledger order. Append-only: nothing on disk is destroyed even if the diagnosis is wrong, and
  replay dedups shard lines by content hash (storage.py:1403), so a double heal is harmless.
- Never appends a `shard_start` line (only a file's FIRST line is read for adoption; a tail
  copy would break adoption and straggler detection). If that line is missing, skip and alert.
- Skipped entirely when the file is mid-conflict: `resolve_journal.find_conflicts` reports it,
  `.git/MERGE_HEAD` / `rebase-merge` / `rebase-apply` exists, or the file carries conflict
  markers.
- Runs inside `_rebuild` after the per-file reset, never as a separate pass, so a re-appended
  line is not skipped as an already-seen duplicate (which would pin `own_unread` and force a
  rebuild forever).
- Degrades, never raises: a read-only checkout or a locked file is counted and alerted, not
  propagated, because this path runs on nearly every operation.
- Ledger size is bounded by what THIS checkout wrote, and it may be pruned to lines still
  absent from disk once healing has succeeded.

Teammates' shards and the frozen legacy journal are never written — retained and alerted only.

### 3.4 One alert, not three

`_record_rehydrate` and `check_between_sessions` already share ONE flag file and ONE
session-start slot, dispatched by a `kind` field (prime.py:1098-1145). This work adds a new
`kind` to that same slot rather than a third alert, and a test asserts exactly one alert per
loss event, on git and on SVN.

Behaviour changes to that slot:
- It is READ WITHOUT BEING DELETED while the condition persists. The current flag is
  read-once-and-delete, which is why the reported incident went unnoticed for 90 minutes.
- Text by case: *healed* ("your own journal was rolled back; the entries were appended back —
  commit them"), *retained only* ("a teammate's shard / the legacy journal lost N entries this
  checkout had seen; they are still readable here but not on disk").
- The existing texts that tell the reader to hunt `git log` / `svn cat` for permanently lost
  data are reworded: under this design that is no longer what happened.
- Nag discipline: full text while new, then collapsed to one line, with a non-destructive
  acknowledge (the same latch idiom as the straggler warning, prime.py:1151-1166) so the
  retained-only case — a teammate who may never fix their file — cannot become a permanent
  banner. The counter is per digest render, and new divergence re-expands it.

### 3.5 accept-disk — an audited escape hatch (Colton ruling, 2026-09-18)

A deliberate rollback must remain possible: `vibe-cognition-journal accept-disk [project]`
prunes the ledger and the retained entries to exactly what is on disk, rebuilds, and clears
the alert.

No technical gate (ruling: make it undeniable rather than fake prevention — a token shown in
the session-start text is readable by the agent it would gate, and this codebase already
relies on instruction plus provenance for human-intent boundaries). Instead:
- Every run records a durable audit entry in the graph naming what was dropped (count, ids,
  file) and the resolved identity that ran it, so silencing a data-loss warning is permanently
  visible afterwards.
- The command prints what it will drop before doing it and asks for confirmation, for the
  human case.
- Its docstring and the alert text both say plainly that this is a data-dropping command and
  that an agent must ask the human before running it.

### 3.6 Unchanged

Stamps and last-writer-wins, shard merging and the insert-only path, tombstone semantics,
identity/visibility rules, the legacy journal staying read-only, `rehydrate_events`.
`id_collisions` and `glued_lines` stay per-rebuild counters.

## 4. Residual limits

- A line written and truncated before this checkout's ledger recorded it is unrecoverable. The
  ledger is synchronous with the append, so the window is a single write, not a session.
- Another person's lost lines are retained and reported here, never repaired from here.
- A teammate who never restores their file leaves a standing (collapsed, acknowledgeable)
  notice.
- Viewing an old checkout while a server runs against it will heal your own shard back to
  current; the alert names it and accept-disk settles it.

## 5. Tests

Named inversions (these two currently ENCODE the bug and must flip, with a note saying why):
`tests/test_journal_shards.py::test_my_own_unread_lines_vanishing_before_i_read_them_back_is_a_loss`
and `::test_lines_vanishing_from_a_shard_rebuild_and_raise_the_loss_alert`.

New:
- **Cold start, the reported incident:** exactly ONE write call, then the process is destroyed
  with NO further catch-up on that shard; the file is truncated; a NEW process starts. Nodes
  survive, the file is healed, one alert is raised. Worded to leave no room for an extra
  catch-up or an out-of-band ledger seed.
- Live process: same truncation with a server running.
- Teammate shard truncated: retained, their file untouched, alert says retained-only.
- Legacy journal truncated: retained, untouched.
- `remove_node` still removes — before a rebuild, after a rebuild, and after a heal (the
  healed file carries the tombstone).
- Missing `shard_start`: heal skipped, adoption and straggler detection unaffected.
- Mid-conflict (markers, and `.git/MERGE_HEAD`): heal skipped, alert.
- Read-only file: heal fails, counted, alerted, no tool call raises.
- Two processes on one shard both heal: duplicates absorbed, one graph.
- Convergence: two processes, interleaved writes, one truncated and healed mid-flight.
- Exactly ONE alert per loss event, on git and on SVN.
- The alert survives session boundaries, is not consumed by reading, clears on restore and on
  accept-disk, and can be acknowledged without being cleared.
- Ledger is checkout-bound: a ledger copied from another machine/folder is ignored.
- Perturbation: restore the graph wipe and the truncation tests must fail; delete the ledger
  and the cold-start test must fail.

## 6. Release gates

ruff, render/parity checks, full pytest, `pytest -m svn` (the replay engine is in that gate's
list), tool-surface audit if a tool's shape changes, TWO rounds of multi-reviewer peer review
after the work is done (Colton's process), and — MANDATORY, not optional — a field test in
Violet's project, which has the only real reproduction and a known-good recovery procedure.
Only then the Loki pin.

## 7. Rulings folded in

- Full append-only replay, and repair the person's own file automatically (2026-09-18).
- accept-disk is audited, not gated (2026-09-18, §3.5).
