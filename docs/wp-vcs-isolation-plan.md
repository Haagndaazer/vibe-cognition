# WP-VCS-Isolation — an SVN user sees nothing git, a git user sees nothing SVN

**Status:** rev 2, 2026-10-28, IMPLEMENTED in v0.46.0. Sonnet peer review of rev 1 done
(two blockers, seven should-fix, three nits — folded in below and marked `[R]`). Colton's
rulings (§6, recorded as cognition decision 728eb8888c6c): Q1 (b) leave a committed
`.cognition/.gitignore` alone (so the `svn rm` design in §2c was NOT built; unversioned ones
are hidden by the `.gitignore` glob), Q2 static text may name both, Q3 none stays
git-leaning, Q4 SVN wins ties. Q5 (Codex Git Bash) was not put to Colton and stays an
accepted shell dependency until he says otherwise.
**Origin:** a visasoccer (SVN) teammate reported a Git Credential Manager popup. The audit
(cognition node 6873845e4b52) cleared the plugin of the popup — nothing on the startup or
tool path runs `git`, and nothing anywhere contacts a git remote — but found the plugin
does leak the *other* VCS into a user's experience in several places. Colton's rule, stated
2026-10-28: on an SVN project nothing git-related is invoked, attempted, written or shown;
on a git project nothing SVN-related is read, run or shown.

## 1. Inventory — every cross-VCS leak, by direction

Verified against the source on 2026-10-28. "Visible" means it reaches the human or the
agent's context (prime digest, tool result, error text). "Under the hood" means a file read,
file write or process spawn the human never sees.

### SVN project sees git

| # | Where | What leaks | Kind |
|---|---|---|---|
| S1 | `identity.py::resolve_identity` → `git_identity.resolve_git_identity` | Reads `~/.gitconfig` (and XDG/`GIT_CONFIG_GLOBAL`) on every project. On an SVN checkout with a stale global git email, the *unconfirmed* identity is stamped `source: "git"`, the identity banner is suppressed and the prime digest is personalised to that stale email `[R]`. SVN usernames are deliberately suggestion-only, so git is trusted more than the project's own VCS. | under the hood → attribution and prime before confirmation |
| S2 | `identity.py::identity_suggestions` | Offers the `~/.gitconfig` email as a candidate on SVN checkouts (`source: "git"` in the `cognition_set_identity` refusal payload). | visible (tool result) |
| S3 | `git_hygiene.py::ensure_git_hygiene` | Writes `.cognition/.gitignore` under any VCS (deliberate since v7, episode 4beb40e3cbe2). The SVN pass mirrors it into `svn:global-ignores` and `svn add`s it, so it is **committed to the SVN repository** as a git-named file. | visible (file in repo) |
| S4 | `documents.py` local-only manifest `documents/.gitignore` | Written when a document is stored `local_only`. On SVN it is excluded from adds but not ignored, so it sits as a permanent `?  .cognition/documents/.gitignore` row in `svn status`. | visible (`svn status`) |
| S5 | `prime.py::IDENTITY_REQUIRED_NOTICE` | "No email address resolves from git config or SVN credentials…" regardless of VCS. | visible (prime) |
| S6 | `prime.py::_repair_notice` (healed journal) | "the usual cause is `git checkout`, `git restore`, `git stash` or `svn revert`" regardless of VCS. | visible (prime) |
| S7 | `prime.py::_loss_notice`, rehydrate kind | "check `git log -- .cognition/journal …` or a teammate's clone" shown to SVN users. | visible (prime) |
| S8 | `git_hygiene.py::format_hygiene_announce` | Reached when the SVN pass returned `None` (opt-out, or `.svn` without `wc.db`); its SVN wording branch is otherwise dead. Folded into S3/G5. | visible (prime, edge case) |
| S9 | `backfill.py` CLI and `skills-src/vibe-backfill/SKILL.md` | `_get_recent_commits`/`_get_changed_files` run `git -C <dir> log`/`diff-tree` without checking the VCS; the skill tells the agent to run `git log main …`. Fails fast, no popup, but **invokes git on an SVN project**. | under the hood (spawn) |
| S10 | `skills-src/vibe-cognition/SKILL.md` line 84; `cognition_tools.py` docstrings at ~231, 267, 3155, 3403, 3423, 3462, 3796, 5456 | "resolved from this repo's git config", "ignored by git and SVN", "overrides git config and SVN credentials". Static prose; §5 boundary. | visible (static docs) |
| S11 `[R]` | `cognition_tools.py::_materialize_blob` ~1141–1146 | `cognition_store_document` warnings say "exceeds GitHub push limit" and "git history retains it" to SVN users. | visible (tool result) |
| S12 `[R]` | `backfill_identity.py::_run_git` ~128–155 | `python -m vibe_cognition.backfill_identity` spawns `git blame`/`git show` on any project. Suggestion source only, but a git spawn on SVN. | under the hood (spawn) |
| S13 `[R]` | `journal_watch.checkout_source` → `_git_source`/`git_dir`; `storage._file_is_contested` → `git_dir` | On an SVN project, `checkout_source` falls through to reading an ancestor `.git/HEAD` when `wc.db` is unreadable, and `_file_is_contested` stats `.git/MERGE_HEAD` and friends on every project. File reads, never spawns. | under the hood (reads) |

### git project sees SVN

| # | Where | What leaks | Kind |
|---|---|---|---|
| G1 | `prime.py::_loss_notice`, between-sessions kind | "The usual cause on Subversion is… `svn log` … `svn cat`" shown to **git** users. `journal_watch.applies_to` became every-project in WP-Append-Only-Replay; the wording stayed SVN-only. | visible (prime) |
| G2 | `identity.py::_stop_it_travelling` | Names both `svn rm` and `git rm` when neither marker is at `repo_path` exactly — a git project whose `.git` is a parent gets both. | visible (tool error text) |
| G3 | `svn_identity.svn_username_candidates` | Gated on `is_svn_working_copy`. **No leak**; listed as checked. | — |
| G4 | `svn_hygiene.ensure_svn_hygiene` | Gated on `svn_root` (`.svn/wc.db`, stops at `.git`). Never spawns `svn` on git. **No leak.** | — |
| G5 `[R]` | `git_hygiene.format_hygiene_announce` ~478–485 | When `.gitattributes` is not configured it appends "This VCS has no union-merge equivalent… See 'Team setup (svn)' via cognition_readme". `ensure_git_hygiene` only writes `.gitattributes` when `.git` is **at** the project root, so an ordinary monorepo layout (project under a git root) shows a git user an SVN pointer every session. | visible (prime) |
| G6 `[R]` | `journal_watch._wc_root` | Stats `.svn` on every ancestor of a git project before finding `.git`. Reads only. | under the hood (reads) |
| G7 `[R]` | Codex adapter `find-git-bash.cmd`, `session-start.cmd`, `reinject.cmd` | On Windows the Codex hooks need **Git Bash** to run and tell any user, SVN-only included, to install Git for Windows. This is a dependency on the shell, not on git the VCS; no git command runs. | visible (Codex Windows only) — ruling Q5 |

### Detection drift (root cause of G2, S8, G5)

Four "which VCS" checks with different rules:

- `git_hygiene.ensure_git_hygiene`: `(repo_path / ".git").exists()`, root only.
- `git_hygiene.check_hygiene_state`: walks parents; `.svn` *dir* (no `wc.db`); stops at `.git`.
- `svn_identity.is_svn_working_copy`: `.svn` at `repo_path` only, no walk, no `wc.db`, no `resolve()`.
- `svn_hygiene.svn_root` and `journal_watch._wc_root`: walk parents; `svn_root` requires `wc.db` and resolves the path. **Both test svn before git at the same level** `[R]`.

Ledger rule 21 drift: WP-SVN-Hygiene settled "walk up, require `wc.db`" and the identity
module never picked it up.

## 2. Design — one detector, consumers gated on it

### 2a. `cognition/vcs.py` (new, stdlib-only, imports nothing from the package)

```python
VCS_GIT = "git"; VCS_SVN = "svn"; VCS_NONE = "none"

def svn_root(start) -> Path | None   # moved from svn_hygiene; identical rule and resolve()
def git_root(start) -> Path | None   # first ancestor (inclusive) with .git — dir OR gitlink file
def detect_vcs(repo_path) -> str     # see tie-break below
```

- `detect_vcs` resolves the path first `[R]`, then walks upward and returns the first level
  with a marker. **At one level with both `.svn/wc.db` and `.git`, SVN wins** — that is the
  existing `svn_root`/`_wc_root`/`check_hygiene_state` order, kept so the SVN hygiene pass
  keeps running on git-svn trees `[R]`. Documented in the docstring and pinned by a test.
  Colton may overrule (Q4).
- `.git` counts as git whether directory or gitlink file (worktrees, submodules), matching
  `journal_watch.git_dir`; `git_identity._local_config_path` keeps honouring only a
  directory, unchanged `[R]`.
- `git_root` is consumed by G5's fix (announce) and by `_stop_it_travelling` `[R]`.
- No cycle: `git_hygiene` imports `local_paths` only, `svn_hygiene` imports `git_hygiene`;
  `vcs.py` imports nothing. `embeddings/storage.py` keeps importing `git_hygiene`'s lock
  helpers untouched `[R]`.
- `svn_hygiene.svn_root` becomes `from .vcs import svn_root` so `resolve_journal`'s
  `svn_hygiene.svn_root` attribute and the svn tests keep working. No test monkeypatches
  it `[R]`.
- **`is_svn_working_copy` stays root-only and unchanged** `[R]` — `journal_watch._wc_root`
  returns the first ancestor for which it is true and reads `<that>/.svn/wc.db`; a
  walk-up predicate would make it return the project folder and silently disable nested
  SVN source detection (`test_the_nested_journal_source_is_found_relative_to_the_checkout_root`).
  Callers that need the walk (identity, `_stop_it_travelling`, `check_hygiene_state`)
  switch to `detect_vcs`; `_wc_root` and `svn_identity.resolve_svn_identity` are not
  touched. The pre-1.7 `.svn`-without-`wc.db` case therefore reads as `none` for the new
  callers and as before for the old ones; the plugin already requires SVN 1.8+ for
  inheritable properties, so nothing supported is lost.
- Cost: identity resolution runs per gated tool call, so `detect_vcs` adds 10–20 stats per
  call. Accepted; local disks only in practice. No memoisation (stale-cache bugs cost more
  than the stats) `[R]`.

### 2b. Fix S1/S2 — identity never consults git on an SVN checkout

`resolve_identity` and `identity_suggestions` compute `vcs = detect_vcs(repo_path)`. On
`VCS_SVN` they skip `resolve_git_identity` entirely; the name comes from
`getpass.getuser()` called directly (the git module was the one doing that fallback, so
skipping it would otherwise yield `"unknown"` `[R]`), email stays empty, `source` is
`os_user`. On `git` and `none`, unchanged.

Blast radius: only the *unconfirmed* path; the write gate requires `confirmed` and is
unchanged. **Visible behaviour change** `[R]`: an SVN user with a stale git email who never
confirmed used to get a silently personalised digest and no banner; now they get the
SVN-worded identity banner and no personalisation until they confirm. That is the intent.
The `identity_file` FILE_UNBOUND/FILE_FOREIGN information still attaches on the SVN path,
so that messaging is intact. No tool parameter, return key or error shape changes.

### 2c. Fix S3/S4/S8/G5 — no git-named file is written or left visible on SVN; the git announce never points at SVN

- `ensure_git_hygiene`: the `.cognition/.gitignore` writer runs only when
  `detect_vcs(repo_path) != VCS_SVN` (git **and** none; "none" is the pre-`git init` state).
  Relocation and the flag still run on SVN. No `GIT_HYGIENE_VERSION` bump.
- `svn_hygiene.ignore_globs` already unions `_GITIGNORE_ENTRIES` and the legacy names from
  code, so the property is complete without the file; it keeps reading the file if present
  so a hand-added glob on an existing checkout survives. It adds the glob `.gitignore` so an
  unversioned `documents/.gitignore` (S4) or a re-created `.cognition/.gitignore` never
  shows in `svn status`. `local_only_paths` keeps excluding `documents/.gitignore` from adds.
- **Removal of an already-committed `.cognition/.gitignore`** (ruling Q1; recommended (a)),
  done inside `_run_pass` so both prime and `add_new_files_in_background` behave the same
  `[R]`, in this order: (1) read the file and decide "managed-only" (line 1 is our marker,
  every other non-comment line is in `_GITIGNORE_ENTRIES` or the pre-v9 enumerated names);
  (2) ensure the property carries every glob including `.gitignore`; (3) only then, if the
  file is versioned (status `normal`), unmodified, and managed-only, run `svn rm` with
  `--targets`. A locally modified file, a file with a foreign line, or a file with its own
  properties is left alone `[R]`. After `svn rm` the status item is `deleted`, which
  `add_new_files` never re-adds, and the working file is gone, so nothing in the same run
  recreates it. The report gains `removed_now: list[str]` and the announce says what was
  scheduled for deletion. A foreign-line file is **not** reported (the pass is stateless by
  design; a once-only note needs persisted state it deliberately lacks) `[R]`.
  Mixed-version teams: an older plugin on a fresh checkout re-creates the file
  unversioned; it stays unversioned (and hidden) once the `.gitignore` glob is committed in
  the property. The property change and the delete should be committed together; the
  announce says so.
- `format_hygiene_announce` `[R]`: takes the `vcs` from `check_hygiene_state`. The
  "no union-merge equivalent… Team setup (svn)" sentence renders **only** on `svn`; on `git`
  with `.gitattributes` unconfigured (git root above the project) it says nothing extra; on
  `none` it says nothing extra. The `is_svn` key is replaced by `vcs`.

### 2d. Fix S5/S6/S7/G1/G2/S11 — every dynamic notice is worded for the detected VCS only

`IDENTITY_REQUIRED_NOTICE` becomes `identity_required_notice(vcs)` (no test imports the
constant `[R]`). `_repair_notice(info, vcs)` and `_loss_notice(info, vcs)` take the VCS,
which prime derives as `detect_vcs(cognition_dir.parent)` at render time `[R]`. The phrases
existing tests pin ("appended back automatically", "COMMIT", "tell the user", and "check
`git log`" absent from the healed note) stay in every variant `[R]`.

| notice | git | svn | none |
|---|---|---|---|
| identity required | "No email address resolves from git config on this machine" | "No email address resolves on this machine (SVN usernames are offered as suggestions only)" | "No email address resolves on this machine" |
| healed journal cause | "`git checkout`, `git restore` or `git stash`" | "`svn revert` or `svn update` to an older revision" | "an older copy of .cognition being put back on disk" |
| rehydrate loss recovery | "check `git log -- …` or a teammate's clone" | "check `svn log …` or a teammate's working copy" | "check a teammate's copy" |
| between-sessions loss | "a merge or rebase that resolved a journal conflict by taking one side… `git log`/`git show` on the file has the lines" | existing SVN text, unchanged | "a journal conflict resolved by taking one side…" |

`_stop_it_travelling` uses `detect_vcs` and names one command, or none on `none`.
S11: the two `_materialize_blob` warnings drop "GitHub" and "git history" ("exceeds the
hosting push limit", "version-control history retains it").

### 2e. Fix S9/S12 — no git spawn off a git project

`backfill._get_recent_commits`/`_get_changed_files` and `backfill_identity._run_git` return
their empty value **before** `subprocess` when `detect_vcs(repo_path) != VCS_GIT`; each
`main()` prints one line that the command is for git repositories. `vibe-backfill`
SKILL.md gains a first step: "If `get_status`'s `repo_path` has no `.git` at or above it,
tell the user this skill is git-only and stop" — before any git command. Rendered via
`tools/render_harness.py`.

### 2f. Fix S13/G6 — silent reads of the other VCS `[R]`

`journal_watch.checkout_source` consults `journal_source` only when `detect_vcs` is
`svn` and `_git_source` only when it is `git`; `storage._file_is_contested` calls
`git_dir` only when `detect_vcs` is `git`. `_wc_root` is only called from `journal_source`,
so once that is gated it never stats `.svn` on a git project. Behaviour on `none`: both
return `None`/`False` as today.

### 2g. S10 — static prose

The SKILL.md line and the listed docstrings change to "resolved server-side from the
confirmed identity for this checkout" / "kept out of version control". `Args:`/`Returns:`
untouched. `cognition_readme` keeps both "Team setup" sections (§5, Q2).

## 3. Tests (each must fail on the reverted code — ledger 12/20) `[R]`

Shared fixture `svn_wc(root)` creating `.svn/wc.db` (the existing `_fake_wc` in
`test_svn_hygiene.py`), used by every new SVN test. **Fixture migration**: the bare-`.svn`
fixtures in `tests/test_identity.py` (lines ~113–168, 323, 338, 687, 757) and
`tests/test_git_hygiene.py` (~567–605) move to `svn_wc`, since the callers they exercise
now use `detect_vcs`. `test_resolve_prefers_git_over_svn` and
`test_suggestions_are_deduped_and_sourced` encode the opposite of S1/S2 and are **inverted**
(git must lose on an SVN tree). `test_a_git_repo_inside_an_svn_checkout_is_not_svn` is
kept and re-pointed at `detect_vcs` so it passes for the right reason.
`tests/test_journal_watch.py` bare-`.svn` fixtures stay (that module is unchanged).

New `tests/test_vcs.py`: git dir; gitlink file; `.svn/wc.db`; `.svn` without `wc.db` →
none; project under an SVN root; `.git` below `.svn` → git; both at one level → svn;
relative path resolves; neither → none.

Per fix:

- S1: svn fixture + `GIT_CONFIG_GLOBAL` holding an email → `resolve_identity` gives
  `email == ""`, `source == "os_user"`, `name == getpass.getuser()`; git fixture → the email.
- S2: svn fixture → no suggestion with `source == "git"`; git fixture → one.
- S3: svn fixture → `ensure_git_hygiene` leaves no `.cognition/.gitignore`, still writes the
  flag and relocates; git and none → file written (existing tests).
- S3 removal: fake-`svn` tests (the `_run` monkeypatch pattern) assert `rm` is issued for a
  versioned managed-only file; NOT for a foreign-line file; NOT for a modified file; and
  that the property set precedes the rm in the recorded call order. One `-m svn` lab test
  against the real client: the file ends `D` in `svn status --xml` and the next run issues
  no second rm.
- S4: `ignore_globs` contains `.gitignore`; `matches_ignore(".cognition/documents/.gitignore", …)`
  is true; lab test: `svn status` shows no `?` row for it.
- S5–S7, G1: parametrised; assertions are **case-insensitive** and check both `"svn"` and
  `"subversion"` absent from the git variant, `"git"` absent from the svn variant, both
  absent from none; plus the pinned phrases present in all three.
- G5: git fixture with `.git` in the parent → announce contains neither "svn" nor
  "Team setup".
- G2: same fixture → `_stop_it_travelling` names only `git rm`.
- S9/S12: svn fixture with `subprocess.run`/`Popen` replaced by a **call-recording** mock →
  the recorded call count is 0 (an exception-raising mock passes today because the
  functions swallow exceptions).
- S13/G6: svn fixture with an ancestor `.git` and a recording `Path.read_text`/`stat`
  wrapper → no path under `.git` is touched by `checkout_source` or `_file_is_contested`;
  git fixture with an ancestor `.svn` → no `.svn` path touched by `journal_source` callers.
- S8/none: a bare `.svn` without `wc.db` renders the `none` announce (no "svn", no "git").
- `test_doc_drift.py` and `render_harness.py --check` cover the skill prose.

## 4. Release

- User-facing → **0.46.0** in the three manifests, `uv lock`, CHANGELOG section,
  `whats-new.json` bullets. The bullets avoid the bare tokens "git"/"svn" where possible so
  the field check below is not defeated by the release note itself `[R]`.
- Gates: `ruff`, `render_harness.py --check`, `render_parity.py --check`, full pytest,
  **`uv run pytest -m svn`** (svn_hygiene.py touched; client 1.14.5 is on this machine),
  sonnet code review.
- Tool-surface audit: no parameter, return key or error changes. Docstring wording only;
  audit items 1–5 re-run anyway and the pin request to Loki says so. `parity.json`: no new
  rows.
- Field check on Colton's machine after `/plugin update`: a session in visasoccer (SVN)
  shows no "git" token in the prime outside the digest body and the what's-new note; a
  session in vibe-cognition (git) shows no "svn"/"Subversion" token.

## 5. Boundary — what stays dual-VCS on purpose

Static reference text describes both VCSes because one copy serves every reader:
`cognition_readme`'s "Team setup (git)" / "Team setup (svn)" sections, `README.md`,
`CHANGELOG.md`, `docs/`, and the `cognition_set_identity`/`cognition_store_document`
docstrings that explain the `local_only`/identity model. The rule is applied to everything
*dynamic*: prime digest, announces, tool results, error text, suggestions, files written,
files read, processes spawned. Module and constant names (`git_hygiene.py`,
`_GITIGNORE_ENTRIES`, the `.git-hygiene-managed` flag under the ignored `local/`) are
internal and never surface. Codex-on-Windows's Git Bash requirement (G7) is a shell
dependency and stays unless Colton rules otherwise (Q5).

## 6. Rulings needed from Colton

- **Q1 — the already-committed `.cognition/.gitignore` on SVN checkouts.** (a) Stop writing
  it and schedule `svn rm` for a versioned, unmodified, plugin-managed-only file
  (recommended: the only path that removes the git artifact from the SVN repo; never
  commits; shows as `D` beside the `A`s); (b) stop writing it and leave existing files
  alone, hiding only unversioned ones via the ignore glob; (c) keep writing it (status quo).
- **Q2 — the static boundary (§5).** Confirm reference text may mention both VCSes, or
  rule that `cognition_readme` must branch on the detected VCS.
- **Q3 — "none" VCS.** Confirm a folder with no VCS keeps today's git-leaning behaviour
  (read `~/.gitconfig`, write `.cognition/.gitignore`), the pre-`git init` state.
- **Q4 — tie-break when `.git` and `.svn/wc.db` share a level (git-svn).** Keep "SVN wins"
  (today's order in three of four checks; recommended, no behaviour change), or "git wins".
- **Q5 — Codex on Windows needs Git Bash (G7).** Accept as a shell dependency (recommended:
  replacing it means rewriting the hooks in `.cmd`/PowerShell), or file a follow-up task.

## 7. Peer-review disposition (rev 1 → rev 2)

Blockers: `_wc_root` dependence on root-only `is_svn_working_copy` (fixed: predicate
unchanged, new callers use `detect_vcs`); bare-`.svn` test fixtures and two inverted tests
(fixed: §3 migration list). Should-fix: S11, S12, S13/G6, G5 added; tie-break made an
explicit ruling (Q4); S9 test made call-counting; notice assertions case-insensitive; false
claim about `IDENTITY_REQUIRED_NOTICE` test imports removed; removal safety spelled out
(order, modified-file skip, no once-only note, background thread). Nits: `resolve()` in
`detect_vcs`; `git_root` now consumed; S8 opt-out/bare-`.svn` case covered.
