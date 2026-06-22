# WP-Readme-GitAttr — journal union-merge setup in the readme tool

**Status:** spec peer-reviewed (sonnet, VERDICT revise → blockers resolved below). Ready for Vorpid.
**Owner (impl):** Vorpid. **Gate:** full WP protocol (SHA-pinned merge, CI green 3 legs, manager journal-flush). Vince does not write code.
**Release:** folds into the already-held v0.10.0 cut. No version bump beyond that.

## Goal

The readme/onboarding content the tool serves should teach users to make the
append-only journal **union-merge** in git, so concurrent team appends merge
cleanly instead of conflicting. The single correct line is:

```
.cognition/journal.jsonl merge=union
```

`union` is a **built-in** git merge driver (git >= 1.7.x). No `[merge "union"]`
config stanza is needed — the line alone is sufficient on any modern git.

## Constrained by project history (do NOT contradict)

- **Topology:** `merge=union` is correct for the **separate-clones** topology
  (each dev/agent has their own clone). A **single shared checkout** must NOT use
  it — it uses the manager-worktree-flush protocol and nobody commits the journal
  on branches. (nodes 4ed473ba9c75, 1f39e60c6d83)
- **Cut-over scar (high):** adding `.gitattributes` rules to an already-committed,
  grown journal can byte-rewrite the blob and union-duplicate lines across the
  boundary. Recommend ONLY `merge=union` (never `-text`), and set it EARLY — at
  setup, before the journal grows. (nodes 90ee3c1b968c, 54304ecf567c)

## Changes (content only — all in `src/vibe_cognition/cognition/readme.py`)

All ASCII-only, stdlib-only, no runtime file reads. No new files.

**A. COGNITION_GUIDE — primary home.** Add a new `## Team setup (git)` subsection
(place it after `## Cross-project reads`). Content:
  - If multiple people or agents share this repo as **separate clones**, add to
    the **repo-root** `.gitattributes`:
        `.cognition/journal.jsonl merge=union`
  - State: `union` is a built-in git merge driver; no extra git config is needed.
  - State: this makes the append-only journal union-merge so concurrent appends
    from different branches/clones survive a merge instead of conflicting.
  - **Warning (strong, per B3):** Do NOT add this in a single shared checkout
    (everyone in one clone) — that setup uses the worktree-flush protocol. Add it
    at setup, **before the journal grows**; retrofitting it onto a large committed
    journal can duplicate entries across the rewrite boundary.

**B. COGNITION_GETTING_STARTED — short pointer.** Add a brief step (fresh setup is
the right moment to set it early): one or two lines — "If teammates will share this
repo as separate clones, add `.cognition/journal.jsonl merge=union` to your repo-root
`.gitattributes` now, before the journal grows. See the Team setup section
(cognition_readme guide) for details." Do not duplicate the full warning here.

**C. ONBOARDING_BLOCK — one deferral clause.** Extend the INSTRUCTION so the
empty-graph alert ALSO mentions, as a sub-clause, that if the user shares the repo
with teammates there is a one-time `.gitattributes merge=union` setup for the
journal (point them at cognition_readme). Keep it a single conditional clause —
do NOT restate the full guide; solo users should not be burdened.

**D. tests/test_readme.py.** Keep all existing `.isascii()` assertions (the new
text is ASCII) and the `{guide, getting_started}` return-shape test unchanged. Add:
  - assert `"merge=union"` in COGNITION_GUIDE.
  - assert `"merge=union"` in COGNITION_GETTING_STARTED.
  - assert `"-text"` NOT in COGNITION_GUIDE (lock the scar lesson — never recommend
    `-text`).
Note: the "set it early" / shared-checkout-warning *framing* is manual-review only
(not mechanically testable); the asserts above just lock presence/absence of the
key tokens.

**E. Out of scope (explicit):** SKILL.md is agent-facing tool reference — gitattributes
advice is user-facing onboarding and does NOT belong there; do not touch it. The
doc-drift GUARD test does not cover readme.py content — do not try to satisfy it for
this change.

## Acceptance

- `uv run ruff check .` clean, `uv run pyright` clean (whole-repo, no path),
  `uv run pytest` green.
- CI green on all 3 legs (ubuntu 3.11, ubuntu 3.13, windows 3.11).
- `cognition_readme` returns the Team setup content in `guide`; empty-graph prime
  injects the one-clause team mention.
- Journal stays off the WP branch (manager flushes via worktree at merge).
