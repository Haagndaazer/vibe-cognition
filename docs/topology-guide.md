# Choosing a Team Topology

Vibe Cognition's `.cognition/journal.jsonl` is a single append-only file that every
teammate's server reads and writes. How your team gets that file onto each machine —
**shared checkout** or **separate clones** — changes which protocol you need to follow.
Get this wrong and you can lose recorded nodes (see the incidents below); get it right
and both modes are safe.

## How to choose

| Your setup | Topology |
|------------|----------|
| One working directory, multiple agents/branches switched in and out of it (e.g. a manager + implementer sharing a single checkout) | **Shared checkout** |
| Each teammate has their own separate `git clone`, pulling/pushing independently | **Separate clones** |

If you're not sure which you have: separate clones is the default and the common case
(most teams). Shared checkout is a deliberate choice for tight manager/implementer
pairing in one tree — if you didn't set that up on purpose, you're on separate clones.

The two protocols are NOT interchangeable. Running the shared-checkout protocol's
destructive-op discipline in a separate-clones setup is unnecessary friction; running
separate-clones' relaxed "just commit and pull" habit in a shared checkout **will**
clobber live in-memory server state (see the incident below) — a `git checkout`/`reset`
in a shared tree rewrites the SAME `journal.jsonl` a running server has open.

## Shared checkout

**When to use:** multiple agents (e.g. a manager and one or more implementers) operate
in a single working directory, switching branches in and out of it as work proceeds.

**Why the protocol exists:** in this topology, `.cognition/journal.jsonl` is not just a
tracked file — it's a file a running MCP server has open and is appending to, live, in
the SAME working directory every branch switch touches. A branch switch, `reset`, or
`checkout --` doesn't just change tracked files; it rewrites the journal a server is
mid-session with. This actually happened:

> **Incident:** a branch switch (`checkout main` → `checkout fix/branch`) in the shared
> tree rewrote `journal.jsonl` back to an earlier commit, discarding two nodes recorded
> live after the last flush. The running server detected the rewrite and reloaded from
> the truncated file — the nodes were gone from disk AND from server memory. Recovery
> required reconstructing the lost nodes from conversation context.

A second incident showed the flush protocol alone isn't sufficient — it also has to
reach the actual merge base:

> **Near-miss:** a manager flushed WP nodes to *local* `main` correctly three times via
> temp worktrees, but only one of those flushes had been pushed to `origin/main` before
> a GitHub PR merged. The PR's merge base was the stale, unpushed `origin/main`, so the
> merge commit carried an outdated journal — local `main` had the full journal, `origin`
> had the code. No data was lost (the records survived in local commits and the live
> working file), but it required a reconciliation merge to fix. Lesson: a local-only
> flush protects against branch-switch clobber but NOT against a PR merging onto a
> stale origin base — push (or flush onto a branch that will actually reach origin)
> before merging.

**The protocol:**

1. **Nobody commits `.cognition/journal.jsonl` on a work branch.** Only the manager
   flushes it, and only onto `main` (never a WP/feature branch — see incident #2 above:
   a flush that never reaches the branch that gets merged doesn't count).
2. **The manager flushes via a temporary worktree**, not by touching the live tree:
   ```bash
   git worktree add /tmp/flush-wt main
   vibe-cognition-snapshot .cognition/journal.jsonl /tmp/flush-wt/.cognition/journal.jsonl
   git -C /tmp/flush-wt add .cognition/journal.jsonl
   git -C /tmp/flush-wt commit -m "journal flush: <what changed>"
   git -C /tmp/flush-wt push origin main   # <- do this every time; see incident #2
   git worktree remove /tmp/flush-wt
   ```
   `vibe-cognition-snapshot` (installed with the plugin) copies the journal while holding
   the same append lock a live writer would, so the copy can never land on a torn
   mid-append line — a plain `cp`/`copy` doesn't have this guarantee. Flush at natural
   checkpoints (a work package lands, before any risky git operation, before end of
   session) rather than letting live appends sit unflushed for long stretches.
3. **Destructive-op ban near the journal**, for anyone working in the shared tree:
   no `git reset --hard`, `checkout -- .`, `stash`, or `clean` that could touch
   `.cognition/journal.jsonl` without first confirming the manager has flushed. Regular
   `git add`/`commit` on your own files is fine — the ban is specifically about
   operations that rewrite files you didn't stage, since those can silently include the
   journal.
4. Before ANY branch switch, checkout, or reset in the shared tree: **confirm the
   journal tail is flushed first.** Coordinate this explicitly (e.g. via a message to
   the manager) rather than assuming — this is what incident #1 above skipped.
5. `.gitattributes merge=union` (see [Automatic Git Hygiene](../README.md#automatic-git-hygiene))
   stays configured as defense-in-depth even in this topology, in case a merge ever
   does happen — it doesn't replace the protocol above.

**Residual risk / EOL normalization on Windows:** the journal is replayed by byte
offset, so its on-disk bytes must never be rewritten by line-ending normalization. See
the README's [Troubleshooting](../README.md#troubleshooting) entry for the `-text`
cut-over note — same guidance applies here, not restated.

## Separate clones

**When to use:** the common case — each teammate has their own independent `git clone`
of the project and pulls/pushes on their own schedule.

**Why this is simpler:** nobody's running server has the SAME journal file open that
someone else's `git pull`/`merge` is about to rewrite — each clone's journal only
changes via that clone's own git operations, which a local server naturally re-reads
through its normal replay/catch-up path. The two failure modes above (live rewrite
under a running server, unpushed local flush) don't apply here.

**What's automatic:**

- **`.gitattributes merge=union`** is configured on first startup in a new project (see
  [Automatic Git Hygiene](../README.md#automatic-git-hygiene)). It makes concurrent
  journal appends from different clones/branches union-merge cleanly on a normal `git
  merge`/`pull` instead of producing a conflict — you don't need to resolve journal
  conflicts by hand.
- **Merge-shaped replay defense**: a union-merge can legally interleave two clones'
  journal tails so that an edge/remove/update line for a node arrives BEFORE that
  node's own `add_node` line, within the same replay batch (the two clones minted their
  lines in a different order than the merged file now has them in). The server detects
  this, defers the out-of-order line, retries once after the full batch has loaded, and
  only logs a WARNING if a line is still unresolved after that — self-healing in the
  normal case, loud in the genuine-loss case.
- **Episode duplicate detection**: if two clones each independently record an episode
  citing the same reference (e.g. both minted an episode for the same commit before
  either pulled the other's journal), recording the second one doesn't silently reuse
  or block it — both nodes are created (nothing is ever silently merged), but the
  response carries a `possible_duplicate_of` field naming the earlier one, so a curator
  can reconcile them manually (e.g. with a `supersedes` edge) instead of the duplication
  going unnoticed.

**What's still on you:**

- Pull/merge regularly. The auto-merge handles the FILE conflict; it doesn't change how
  often you should sync — a long-unpulled clone still means your teammates' work is
  invisible to your local searches until you pull.
- The defenses above catch and repair the interleaving/duplication SHAPES that are known
  to occur with this topology under normal use. They are not a substitute for the
  shared-checkout protocol's discipline if you actually have a shared-checkout setup —
  see "How to choose" above.
