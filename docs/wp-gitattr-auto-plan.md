# WP-Git-Hygiene-Auto — auto-configure git hygiene for `.cognition/` on startup

**Status:** spec FINAL — two sonnet review rounds, all 6 blockers resolved (gitattributes
3 + git-hygiene-delta 3). Ready for Vorpid. Follow-on to WP-Readme-GitAttr (PR #26).
**Owner (impl):** Vorpid. **Gate:** full WP protocol. Vince does not write code.
**Sequencing:** branch off main AFTER PR #26 merges (builds on the readme Team-setup text).
**Release:** folds into the held v0.10.0.

## Goal

On startup, the server runs a one-time-ever **git-hygiene pass** for `.cognition/`,
each write idempotent + no-clobber, then prime.py announces what was set. Two writes:

1. **journal union-merge** → repo-root `.gitattributes`: `.cognition/journal.jsonl merge=union`
   (ONLY this line — never `-text`). Topology-sensitive → default-on, env opt-out.
2. **chromadb ignore** → `.cognition/.gitignore`: `chromadb/`. The ChromaDB store is a
   derived cache rebuilt from the journal (the journal is the source of truth) — never
   commit it. Universally correct (no topology dependence). Same env opt-out for symmetry.

Both are gated, idempotent, locked, crash-proof, and recorded via ONE write-once-ever
sidecar flag (see below). Default-on, single env opt-out `VIBE_COGNITION_NO_GIT_HYGIENE`.

## Why the union-merge line is byte-safe (document in a code comment)

`merge=union` is a **merge-driver** attribute — it only changes 3-way merge
resolution and never participates in checkout/checkin filtering, so adding it does
NOT re-smudge or byte-rewrite the committed journal blob. This is the exact reason
it is safe where `-text` (an EOL/filter attribute) is NOT — `-text` reactivates the
C-3 byte-rewrite + duplication scar (nodes 90ee3c1b968c, 54304ecf567c). The writer
must therefore emit ONLY `merge=union`.

## Why chromadb is ignored, not committed

Our own repo already does this (root `.gitignore` line 69, with the journal-committed /
cache-ignored comment): `.cognition/journal.jsonl` is the committed source of truth;
`.cognition/chromadb/` is a large, churning, rebuildable SQLite+index cache. Committing
it = noise + binary merge conflicts. Scoping the ignore to `.cognition/.gitignore`
(not the repo-root `.gitignore`) keeps our blast radius inside the dir we own — zero
clobber risk to the user's root ignore — and a `.gitignore` in a subdir applies to that
subdir and below, so `chromadb/` there correctly ignores `.cognition/chromadb/`. If the
user (or our repo) ALSO ignores it at root, the scoped rule is harmless redundancy.

## Why this is byte-safe (document in a code comment)

`merge=union` is a **merge-driver** attribute — it only changes 3-way merge
resolution and never participates in checkout/checkin filtering, so adding it does
NOT re-smudge or byte-rewrite the committed journal blob. This is the exact reason
it is safe where `-text` (an EOL/filter attribute) is NOT — `-text` reactivates the
C-3 byte-rewrite + duplication scar (nodes 90ee3c1b968c, 54304ecf567c). The writer
must therefore emit ONLY `merge=union`.

## Preconditions / behavior (ALL required)

1. **Git-root detection (REQUIRED — B2):** act only if `repo_path/.git` exists (covers
   the dir form AND the worktree/submodule file form). If `repo_path` is NOT the git
   root (no `.git` there — e.g. Claude opened in a subdir), **skip silently and do NOT
   walk upward**: a `.gitattributes` written below the git root would not match the
   journal path and would be wrong. Not a git repo at all → also skip; never create
   `.gitattributes` in a non-git dir. (The static readme text still covers manual setup
   for these cases.)
2. **Create-if-absent (REQUIRED):** if no `.gitattributes` exists at the git root,
   create it containing the marker + rule. If it exists, append (see idempotency).
3. **Write-once-ever via sidecar flag (REPLACES the first-init gate — B1):** run the
   pass on EVERY startup (not only on first `.cognition/` creation — otherwise every
   existing install and every fresh clone of an established repo, which already have
   `.cognition/`, would never get the rules). Gate BOTH writes on ONE persistent
   sidecar flag file `.cognition/.git-hygiene-managed`:
     - flag present → we have already done our one write; do NOTHING (this is what
       respects a user who deliberately deleted the rule — we never re-add it).
     - flag present AND its content >= current hygiene schema version → we have already
       done our pass at this version; do NOTHING (this is what respects a user who
       deliberately deleted a rule — we never re-add it).
     - flag absent OR content < current schema version → run BOTH idempotency checks
       below; perform whichever writes are needed.
   **Flag is content-VERSIONED** (file content = an integer schema version, currently
   `1`). Bumping the version when a future writer is added makes every project re-run
   the pass exactly once more — kills the "added a writer later, old flag short-circuits
   it" upgrade hazard.
   **Flag is git-IGNORED** (added to the `.cognition/.gitignore` we write, see 5b), NOT
   committed: the committed *rules* (`.gitattributes`, `.cognition/.gitignore`) are the
   shared team decision and travel to teammates; the flag is just this working copy's
   local "did-my-pass" mark. So every fresh clone self-heals: it re-runs once, finds the
   committed rules already present (idempotent → no writes), and stamps its own local flag.
   **(Q1) Conditional flag drop:** write the flag (current version) ONLY after BOTH
   writes RESOLVE — where "resolve" = succeeded OR already-present, NOT an error. If
   EITHER write raises (swallowed), do NOT write the flag, so the next startup retries
   the still-missing file. This makes the pass re-entrant until the repo is clean.
   This covers the existing user base on their next startup and respects revocation.
4. **`.gitattributes` idempotency / no-clobber (B3):** scan existing `.gitattributes` for a non-comment
   line whose first whitespace token is `.cognition/journal.jsonl` **AND that already
   carries a `merge=` attribute token**. If such a line exists → do not append (just
   drop the flag). If a journal-path line exists but has NO `merge=` token (e.g.
   `.cognition/journal.jsonl text`) → still append our `merge=union` line (a second
   matching line is legal; git accumulates attributes across matching lines). Never
   duplicate an existing `merge=` rule, never rewrite the user's line. Preserve all
   existing content verbatim; if the file is non-empty and lacks a trailing newline,
   add one before our block.
5. **Marker block appended:**
   ```
   # vibe-cognition: append-only journal union-merge (safe to remove)
   .cognition/journal.jsonl merge=union
   ```
   The marker comment lets the announce step detect our own prior write.
5b. **`.cognition/.gitignore` idempotency / no-clobber:** ensure `.cognition/.gitignore`
   ignores BOTH `chromadb/` (the rebuildable cache) AND `.git-hygiene-managed` (our local
   versioned flag — keep it untracked, per precondition 3). Create the file if absent
   (header comment + the two lines); if it exists, append each line only when no
   non-comment line already ignores it (scan for `chromadb/`/`chromadb`, and for
   `.git-hygiene-managed`). Preserve existing content verbatim; normalize trailing
   newline. We do NOT inspect the root `.gitignore` or shell to `git check-ignore` — a
   redundant scoped ignore is harmless and staying stdlib-only keeps it fast. The
   `.cognition/.gitignore` file IS git-tracked so the ignore travels to teammates.
   (Our own repo already ignores chromadb at root; this scoped file is harmless
   redundancy there, or suppress via opt-out.) **(Q2, accepted):** a subdir `.gitignore`
   wins over a root-level negation (`!.cognition/chromadb/`) for this path — that
   pathological case resolving to "ignored" is the correct outcome here, not a hazard.
6. **Env opt-out:** if `VIBE_COGNITION_NO_GIT_HYGIENE` is truthy, skip the WHOLE pass
   (before any write; do not drop the flag). Add to config.py and document in the readme
   Team-setup text. We set this in OUR shared-checkout repo (union-merge is the wrong
   topology there — worktree-flush protocol instead; chromadb is already root-ignored).
7. **Locked + crash-proof:** serialize each read-modify-write with a per-file **sidecar
   lock** (`.gitattributes.lock`, `.cognition/.gitignore.lock` — git's own convention,
   preferred over sentinel-byte locks on these short files) so concurrent starts can't
   double-write; each file's check + write happens inside its lock. The versioned flag
   is written only after BOTH writes resolve without an exception (per precondition 3's
   Q1 rule); a swallowed error in either leaves the flag unwritten so the next start
   retries. Wrap ONLY the git-hygiene operation in
   try/except, ordered AFTER `self._dir.mkdir(...)` so a failure here can never block
   journal init: ANY failure (permissions, IO, lock) logs a one-line stderr breadcrumb
   and is swallowed. Non-critical convenience.

## Announce (the "we did it" emit)

- Primary surface: `prime.py` (SessionStart). At emit time, check the git-root
  `.gitattributes` for our marker AND `.cognition/.gitignore` for `chromadb/`; emit a
  short line for whatever we configured, e.g. "vibe-cognition configured: journal
  union-merge (.gitattributes), chromadb ignore (.cognition/.gitignore)." Dynamic state
  belongs in prime.py where the empty-vs-populated logic already lives — NOT baked into
  the static readme.py constants. **N4:** prime.py runs as a CLI subprocess reading
  `REPO_PATH` from env (not from Settings) — construct paths the same way
  (`Path(os.environ.get("REPO_PATH", Path.cwd())) / ...`) and guard the reads
  (missing/error → no announce, swallow). **(Q7)** the announce is a READ-ONLY
  presence/content check — it must NOT call the writer or re-trigger any write.
- The static readme.py "Team setup (git)" section (from PR #26) gets a short note:
  the server auto-configures journal union-merge + chromadb ignore once on first use,
  manual setup applies for existing projects or when opted out
  (`VIBE_COGNITION_NO_GIT_HYGIENE`). Avoid over-promising "always automatic." ASCII/stdlib.
- **Repo `README.md` (human-facing, REQUIRED — Colton):** add a short subsection
  acknowledging the auto git-hygiene behavior — that on startup vibe-cognition will,
  once per project, (a) add `.cognition/journal.jsonl merge=union` to the repo-root
  `.gitattributes` (team-friendly journal merges) and (b) add `chromadb/` to
  `.cognition/.gitignore` (the cache is rebuildable, never committed) — and document
  the `VIBE_COGNITION_NO_GIT_HYGIENE` opt-out, what it suppresses, and that the writes
  are idempotent + non-destructive (existing files appended, never clobbered). This is
  the discoverable place a user learns the server touches their git config files. NOTE
  the doc-drift GUARD test reads README.md — keep any tool/edge-type tables it asserts
  intact; this is an additive section. **(Q5)** the section must state the re-arm path:
  delete `.cognition/.git-hygiene-managed` to make the pass re-run (re-adds any rule you
  removed).

## Implementation surface (pointers, Vorpid finalizes)

- Hook: `CognitionStorage.__init__` around storage.py:72. Order: `mkdir(...)` FIRST,
  then (inside its own try/except) call a new `ensure_git_hygiene(repo_path)` helper.
  No first-init capture needed — the sidecar flag handles write-once.
- New helper: small stdlib-only module `cognition/git_hygiene.py` with two internal
  writers (gitattributes union-merge; `.cognition/.gitignore` chromadb) + the flag/lock
  plumbing. No new deps.
- `repo_path`: from config (`Settings.repo_path`); helper takes it as an arg so it stays
  unit-testable without env. Sidecar flag at `repo_path/.cognition/.git-hygiene-managed`.
- config.py: add the `VIBE_COGNITION_NO_GIT_HYGIENE` read.

## Tests (new tests/test_git_hygiene.py)

**gitattributes:**
- creates `.gitattributes` with exactly marker+rule when absent in a git repo (tmp dir
  with a `.git` dir/file to simulate) + drops the sidecar flag.
- appends without clobbering: pre-seed unrelated rules → all preserved, block appended,
  trailing newline normalized.
- skip when an existing journal-path line ALREADY has a `merge=` token (untouched, no dup).
- **(B3)** append when a journal-path line exists WITHOUT a merge token (e.g.
  `.cognition/journal.jsonl text`) → our `merge=union` line still appended.
- written content contains `merge=union` and NOT `-text`.
- skip when not a git repo / `repo_path` not the git root (no `.git`) → `.gitattributes`
  NOT created, NO upward walk.

**gitignore:**
- creates `.cognition/.gitignore` with `chromadb/` when absent.
- appends `chromadb/` without clobbering a pre-seeded `.cognition/.gitignore`.
- skip-dup when `chromadb/` (or `chromadb`) already ignored there.

**shared (flag / opt-out / idempotency / announce):**
- run pass twice → no dup in either file (flag short-circuits 2nd).
- **(B1)** existing project: `.cognition/` present, no flag → pass still runs and writes.
- **(B1)** revocation: flag present, both rules deleted → pass does NOT re-add either.
- skip when `VIBE_COGNITION_NO_GIT_HYGIENE` set (no write, flag NOT dropped).
- **(Q1 partial-failure)** gitattributes write succeeds, gitignore writer raises (monkeypatched)
  → flag is NOT written; a subsequent run (writer un-patched) DOES write the gitignore.
- **(Q3 versioned re-run)** flag present but content < current schema version → pass
  re-runs (writes anything missing) and stamps the current version; flag content >=
  current → pass does nothing.
- flag is listed in `.cognition/.gitignore` (stays untracked).
- prime announce: configured → line names what was set; nothing configured → no line;
  announce performs NO writes (assert files unchanged after an announce-only call).

## CHANGELOG (post-peer-review revisions)

- **B1:** replaced the first-init gate (which excluded every existing install + fresh
  clone) with every-startup run + write-once-ever sidecar flag (`.cognition/.git-hygiene-managed`)
  that also encodes user-revocation.
- **B2:** explicit no-upward-walk; the gitattributes write acts only when `repo_path` IS
  the git root.
- **B3:** gitattributes idempotency tightened to "skip only if an existing journal-path
  line already has a `merge=` token"; otherwise append even when a non-merge line exists.
- **chromadb gitignore folded in (Colton):** WP renamed GitAttr-Auto → Git-Hygiene-Auto;
  added the `.cognition/.gitignore` `chromadb/` writer (scoped, not root `.gitignore`),
  unified flag + single `VIBE_COGNITION_NO_GIT_HYGIENE` opt-out. Pending focused review.
- **Repo README.md note added to scope (Colton):** human-facing acknowledgement of the
  auto git-hygiene + the opt-out env var, so users discover the server touches their
  git config files.
- Nits: per-file sidecar locks (git convention); try/except scoped to the write only,
  after mkdir; prime.py announce uses the REPO_PATH-env path pattern + guarded reads;
  softened readme wording.

## Acceptance

- `uv run ruff check .` clean, `uv run pyright` clean (whole-repo), `uv run pytest` green.
- CI green 3 legs. Journal stays OFF the branch — Vince flushes via worktree at merge.
- Manual sanity (human, since it touches real git files): fresh repo → first server
  start writes the rule + announce; second start no-ops; opt-out env suppresses.
