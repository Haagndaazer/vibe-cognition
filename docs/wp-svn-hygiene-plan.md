# WP-SVN-Hygiene — `.cognition/` hygiene for Subversion working copies

**Status:** rev 3 — re-baselined 2026-09-11 against a codebase that went
multi-harness since rev 2. Research complete, not implemented, pending peer review.
**Scope:** parity with the git-hygiene pass (`cognition/git_hygiene.py`, still at
`GIT_HYGIENE_VERSION = 6`, untouched since rev 1) for teams whose repo is SVN.
**Companions:** `docs/wp-svn-hygiene-lab.md` (validation lab, rev 2),
`docs/wp-gitattr-auto-plan.md` (the git pass this mirrors),
`F:\FRGVersionControl\ServerAdmin\` (our own field-proven SVN property tooling).

---

## 0. What changed since rev 2, and what it costs this WP

Ten days, eleven releases (v0.35.0 → v0.36.2). Four changes bear on this plan;
the fourth is the important one.

1. **The plugin is multi-harness** (Claude Code + Codex). Harness facts live in
   `src/vibe_cognition/harness.py`; skill/agent prose is templated in
   `skills-src/` + `agents-src/` and rendered by `tools/render_harness.py`;
   `parity.json` → `PARITY.md` is a per-harness feature registry with a
   `--check` gate in CI. **Impact on this WP: mostly free.** The hygiene pass
   lives in `storage.py` (server-side, harness-agnostic), so an SVN pass runs on
   both harnesses with no templating at all. The cost lands only if we add an
   agent-facing surface — see §9.
2. **A HARD RULE now gates every release** that adds or changes an MCP tool: the
   tool-surface self-sufficiency audit (docstring `Args:`/`Returns:`/errors,
   `get_status` shape, skill + README tool tables, `parity.json` rows on every
   harness). This is why §4's resolver should be a CLI, not a tool (§9).
3. **chromadb left the repo in v0.32.0** — it lives under the plugin data dir and
   is surfaced as `chromadb_path`. The `chromadb/` ignore is now *legacy-compat*
   (protecting repos where a teammate runs an older plugin), not the primary
   defense. The SVN ignore list keeps it for exactly that reason, but the
   announce and docs must not oversell it.
4. **A standing decision cuts against §3b — see §3c.** The git analogue of the
   EOL defense (`-text`) was deliberately *not* auto-written. It shipped as
   **disclosure only**. This is new information bearing on a ruling Colton
   already made, and it is the one thing in this rev that needs a fresh answer.

---

## 1. The headline finding (unchanged, still load-bearing)

**Subversion cannot do `merge=union`. There is no per-path merge configuration in
SVN at all, at any version, including 1.15 (Aug 2026).**

| git mechanism | SVN counterpart | Verdict |
|---|---|---|
| `.gitattributes` `merge=union` on a path | *none* | **impossible** |
| `.gitattributes` `-text` (no EOL rewrite) | `svn:eol-style` unset = already verbatim | free, but see §3b/§3c |
| `.cognition/.gitignore` | `svn:global-ignores` (inheritable, 1.8+) | **clean parity** |
| *(no git equivalent)* | `svn:auto-props` (inheritable, 1.8+) | useful — §3b |

Two escape hatches, both investigated and rejected:

- **`[helpers] diff3-cmd`** replaces SVN's internal 3-way merge, so it *could*
  union — but SVN passes it only the three **temp file paths** (args 9/10/11),
  never the working-copy path, so a wrapper cannot tell what it is merging. It is
  also one global client setting, so every merge in every repo on that machine
  would route through our code.
- **`[helpers] merge-tool-cmd`** *does* receive the WC path (5th arg), but only
  fires during **interactive** conflict resolution when a human chooses "launch
  tool". Unreachable from `svn update` or TortoiseSVN's default flow.

So the SVN story splits in two: **the ignore half**, fully solvable; and **the
conflict half**, not preventable by configuration, only made cheap to resolve.

---

## 2. Why SVN props are a different kind of thing than `.gitattributes`

1. **They are not files.** Props live in the working copy's `wc.db`. There is no
   file to append to, so **every write shells out to `svn.exe`** — a real
   departure from `git_hygiene.py`, which is stdlib-only and appends text.
2. **They are versioned content.** `svn propset` produces a *local modification*
   that does nothing for teammates until someone **commits** it.
3. **They inherit, nearest-ancestor wins per pattern** (1.8+). Setting props on
   `.cognition/` itself covers everything beneath it and overrides a repo-root
   policy for the same pattern, without needing repo-root access. Blast radius
   stays inside the directory we own — the same posture that scoped the gitignore
   to `.cognition/.gitignore`. **But see §3c: "per pattern" is unverified, and if
   it is actually wholesale shadowing the blast radius claim is false.**

---

## 3. What the SVN pass should write

All set on the **`.cognition/` directory**, not the repo root.

### 3a. `svn:global-ignores` — the `.gitignore` parity (uncontroversial)

Mirrors `.cognition/.gitignore`, one glob per line: `chromadb`,
`.git-hygiene-managed`, `*.lock`, `.last-rehydrate.json`, `onboard-declined`,
`last-seen.json*`, `backfill-identity-map.skeleton.json`, plus the SVN pass's own
version flag (§3d).

- `chromadb` not `chromadb/` — SVN ignore globs are fnmatch patterns matched
  against the entry name; a trailing slash does not mean "directory". **Lab E1
  verifies this**, including the Windows case-sensitivity trap (`ChromaDB\`).
- Inheritable, so it covers `.cognition/**` recursively. `svn:ignore` would not.
- **Same limitation as git:** ignores affect only *unversioned* items.
- Per §0.3, `chromadb` here is legacy-compat, not the main event. Say so.

### 3b. `svn:auto-props` — the EOL defense — **DEAD. Lab E2, 2026-09-11.**

> **This section is retained for the reasoning; the mechanism does not exist.**
> Lab E2 returned **COMBINATION**: a `svn:mime-type` rule on `.cognition/` does
> **not** displace an inherited `svn:eol-style` rule for the same pattern — both
> are applied. The full E2b fallback ladder was then exhausted:
>
> - empty value (`svn:eol-style=`) → `E135001`, `svn add` **refuses the file**
> - binary mime-type → `E200009: Can't set 'svn:eol-style': file has binary mime
>   type property`, `svn add` **fails**
> - same property, valid value (`svn:eol-style=CRLF`) → **works**, and is the only
>   thing that does
>
> So an inherited `svn:eol-style` can be *overridden* but never *removed*, and
> every valid value translates line endings. **There is no value meaning "do not
> translate."** The pass cannot defend `.cognition/` from a hostile root EOL
> policy. What remains is disclosure (§8) and, if the risk proves real for a team,
> the extension-change option in §3f.
>
> Silver lining from the same run: the `*.log` discriminator inherited the root
> rule normally, so our policy does **not** wholesale-shadow a team's other
> auto-props. §2's blast-radius claim is safe, and Q3 is answered.

Original reasoning follows.

SVN performs no EOL translation unless `svn:eol-style` is set, so `-text` is free
by default. The threat is the **team's own root policy**: a root `svn:auto-props`
carrying `*.md = svn:eol-style=native` (a common rule) would stamp it on our
content-addressed sidecars under `.cognition/documents/` at `svn add` time and
byte-rewrite them at every checkout — breaking the sha-named-content invariant.
Same bug class as the C-3 journal scar, and nobody would trace a broken document
store back to a two-line root ignore policy.

Proposed counter-policy on `.cognition/`:

```
*.jsonl = svn:mime-type=text/plain
*.txt   = svn:mime-type=text/plain
*.md    = svn:mime-type=text/plain
```

An explicit mergeable `text/*` type keeps files line-mergeable while displacing
the inherited `svn:eol-style` rule for the same pattern. **Never**
`application/octet-stream` on `journal.jsonl` — SVN treats a non-`text/*` type as
binary and refuses to merge it at all, turning every collision into a whole-file
conflict and destroying §4's resolver.

### 3c. RETRACTED IN REV 4 — the `-text` precedent does not transfer

Rev 3 argued that because the git-side `-text` defense shipped as **disclosure
only** (task `914e4a354031`, decision `9f13a8099e03`), §3b should too. **That
argument was wrong and is withdrawn.** Peer review caught it; the mechanisms are
not alike.

What made `-text` hazardous is specific and does not generalize: it is a
**filter** attribute, so *toggling* it retroactively re-normalizes the bytes of
already-committed, already-shared content at the next commit touching the file —
the cut-over ritual a live session observes as a replaced journal.
`git_hygiene.py`'s own docstring draws exactly this line: a merge-driver attribute
never participates in checkout/checkin filtering; `-text` does.

`svn:auto-props` has no such property. **Auto-props apply only at `svn add` /
`svn import` time.** Setting the policy on `.cognition/` has *zero* effect on any
file already under version control — it shapes only what gets stamped on the next
file added beneath that directory. No cut-over, no re-normalization, nothing a
live session would see change.

Independent corroboration from our own SVN work: FRG could not use auto-props to
protect content that was already committed, and had to ship a separate
`ServerAdmin\Backfill-NeedsLock.ps1` plus a retroactive commit (`r5 —
svn:needs-lock set on 8,702 lockable assets`) to do it. If auto-props applied
retroactively, neither would exist.

So structurally §3b's write is far closer to `merge=union` — forward-looking,
inert until triggered, and already auto-written without controversy — than to
`-text`. Rev 3 conflated the *threat* (an inherited `svn:eol-style` breaking
sha-named content, genuinely the C-3 bug class) with the *defense* (a template
rule that only fires on future adds, and is not).

**Consequence:** Colton's 2026-09-01 ruling — auto-write the props, never commit,
announce loudly — **stands unchanged**. There is no precedent-based reason to
re-litigate it. The real, mechanism-level hazard in this area is Q3 (wholesale vs
per-pattern shadowing), which is unaffected by who runs `propset` and is gated by
lab E2 either way.

### 3e. NEW IN REV 5 — the `svn add` ruling forces a strict write ORDER

Colton ruled the pass adds `.cognition/` **and its contents**. That is the most
useful outcome for users, and it creates one ordering hazard worth getting right.

`svn add` **skips ignored items** (absent `--no-ignore`). So the ignore policy
must be in place *before* the contents are added, or the pass sweeps `*.lock`,
`last-seen.json`, `onboard-declined` and the rest into version control — the exact
machine-local files `.cognition/.gitignore` exists to keep out, and the WP-TC14
gate-F1 failure class ("machine-local state syncing via version control") repeated
in a second VCS.

Required sequence — **verified working, lab E0b 2026-09-11**:

1. `svn add --depth empty .cognition` — register the directory alone, so a
   property can be set on it.
2. `svn propset svn:global-ignores -F <file> .cognition` — install the ignores.
3. ~~`svn propset svn:auto-props`~~ — dropped, §3b is dead.
4. **`svn add --force .cognition`** — now filtered by the policy from step 2.

**Step 4 needs `--force`.** Plain `svn add` refuses an already-versioned directory
(`W150002` / `E200009: Could not add all targets because some targets are already
versioned`). `--force` is what makes it recurse into the depth-empty-added
directory and pick up unversioned children.

**The uncommitted-property question is ANSWERED: yes.** `svn add --force` honours
an `svn:global-ignores` set but not yet committed, on a directory that is itself
only scheduled for addition — every junk file was marked `I` and none scheduled,
including one nested inside an unversioned-then-added subdirectory. **The
Python-side ignore-filtering fallback is not needed.**

**Announce consequence (§7):** under this ruling the user's first session in an
SVN project ends with a large pile of scheduled additions they did not ask for.
The announce must say so explicitly and name the count, or it reads as the plugin
having quietly committed them to something.

### 3d. `.cognition/.svn-hygiene-managed`

Content-versioned integer sidecar, same mechanism as `.git-hygiene-managed`, so
the pass runs once per working copy and re-runs once per schema bump. Listed in
both `svn:global-ignores` and `.cognition/.gitignore` (for the both-VCS case).

---

## 4. The conflict half — a `resolve-journal` CLI

Configuration cannot prevent the conflict, so make it a non-event.

An SVN text conflict on `journal.jsonl` leaves `journal.jsonl.mine`,
`journal.jsonl.rOLD`, `journal.jsonl.rNEW` beside a marker-laden working file.
Journal lines are append-only with **globally-unique node IDs**, so resolution is
mechanical and lossless: union the `.mine` and `.rNEW` line sets, dedupe by node
ID, preserve order, write, `svn resolve --accept working`. Exactly what
`merge=union` does for git, on demand instead of automatically.

Deliberately **not** automatic — it must never run unprompted during a checkout,
and the user should see the line counts. Post-resolution the graph must be
reloaded: union resolves the *text*, not the replay order (the same caveat the
`.gitattributes` comment already carries).

Also covers `.cognition/people/*.jsonl` — identical append-only single-writer
shape, union-merged in git since hygiene v6.

**Ships as a console script, not an MCP tool** — see §9.

---

## 5. Gotchas already paid for in `F:\FRGVersionControl`

Both were real, shipped, silent failures on `svn.frgdev.net`:

1. **BOM kills the first rule.** `svn propset -F <file>` reads a UTF-8 BOM as
   CP1252 and re-encodes it, so the value begins `ï»¿` and its **first line is
   silently dead** — on `test` r4 this killed both the leading auto-props rule and
   the leading global-ignore, with no error. Every `-F` file must be UTF-8
   **no-BOM**. (Python's `encoding="utf-8"` already is; the trap is PowerShell
   5.1's `Set-Content -Encoding UTF8`.)
2. **Windows CRT glob expansion.** `svn.exe` glob-expands a bare unquoted argv
   token containing `*` or `?` against the CWD before parsing; quoting does not
   help. **Always `-F <tempfile>`, never inline values**, and pass args as a list.

Third, ours: this repo has a standing subprocess-hygiene constraint from the
v0.12.1 git wedge (`git_identity.py` resolves identity from config files rather
than subprocess for exactly this reason). The SVN pass **cannot** avoid the
subprocess, so it needs an explicit timeout, no shell, no inherited stdin,
captured output — and must be one-shot-per-working-copy behind the version flag,
never per-operation.

---

## 6. Detection — when does the SVN pass run?

`ensure_git_hygiene` gates on `repo_path/".git"`. The SVN gate is
`repo_path/".svn"` — and since 1.7 `.svn` exists **only at the working-copy
root**, so a `.cognition/` nested deeper needs a walk up from `repo_path`.

- Both `.git` and `.svn` present: run **both** passes; they touch disjoint
  mechanisms. (Lab E6 verifies rather than assumes this.)
- Neither: run neither.
- `svn.exe` not on PATH: skip silently, note it in the announce. Never fail,
  never prompt — same never-raises posture as git-hygiene.

---

## 7. What the prime announce should say

Today: `vibe-cognition configured: journal union-merge (.gitattributes),
chromadb ignore (.cognition/.gitignore).`

SVN needs a **less reassuring** line, because the reassurance would be false.
Three things must be visible: (a) **we changed your working copy** — the pass
wrote properties without asking; (b) nothing reaches the team until someone
commits; (c) journal conflicts are still possible, and here is the fix.

> `vibe-cognition (svn): set svn:global-ignores on .cognition/ — your working copy
> has uncommitted property changes; commit them to share with the team. SVN has no
> union merge; on a journal conflict run <resolve command>.`

Constraints on the final wording: **"SVN has no union merge" must survive
editing** — a user who reads the git line and assumes parity is the failure mode
this document exists to prevent. And it should name the literal thing the user
sees in `svn status`: a property-only change shows `M` in the **second** column,
easy to scan past. Lab E4 captures the exact output to quote.

**Harness note:** the announce is plain Python in `prime.py`, not templated
prose, so it needs no render step. But if it names a command, that command's
invocation may differ per harness (cf. `harness.update_cta`) — check before
hardcoding a CTA string.

### 7a. The write-once flag is wrong for SVN — a design requirement, not a caveat

Lab E4 surfaces this and rev 3 left it buried in the lab. Promoting it:

git's writes are **committed files** — `.gitattributes` and `.cognition/.gitignore`
persist, so a write-once flag is safe. SVN's writes are **uncommitted property
modifications**, and `svn revert` erases them while the flag stays set. The
working copy is then silently unprotected, forever, with no re-announce: exactly
the class of silent-failure this WP exists to prevent.

**Requirement:** the SVN analogue of `check_hygiene_state` must **re-derive live
state from `svn propget` / `svn status`**, never trust a persisted flag. The flag
may still gate the *write* (so we do not re-propset every startup), but the
*announce* must reflect what the working copy actually has right now, and re-warn
whenever the properties are absent. This is a real behavioural difference from
`git_hygiene.py`, not a port of it.

Cost note: that makes the announce path shell out to `svn` on every session
start, where git's is a file read. Needs to be cheap, timeout-bounded, and
failure-silent (§5), or gated behind a cached check with a short TTL.

---

## 8. NEW IN REV 3 — `cognition_readme` is a second surface

`readme.py` carries a **"Team setup (git)"** section (≈L225–262) that teaches the
manual `.gitattributes` line, the shared-checkout warning, the auto-configuration
note, and the residual autocrlf-risk disclosure. Rev 1 and rev 2 both missed it.

An SVN team gets **none** of that today, and under the §3c recommendation this
section is where the EOL defense actually lives. Scope addition: a **"Team setup
(svn)"** counterpart covering what SVN can and cannot do, the `svn propset`
commands, the `svn add` precondition (§10 Q1), and the resolver.

`readme.py` is plain Python (not templated), but the skill and README tool tables
are drift-tested — see §9.

---

## 9. NEW IN REV 3 — multi-harness surface cost, and why the resolver is a CLI

The hygiene pass itself is **free** across harnesses: it lives in `storage.py`,
runs at server startup, and touches no harness-specific fact. No template tokens,
no render step, no parity row.

The resolver is the only piece with a choice, and the two options are not close:

| | MCP tool | console script |
|---|---|---|
| Tool-surface audit (HARD RULE) | required every release | n/a |
| `Args:`/`Returns:`/error docstring compliance | required | n/a |
| Skill tool table row (`test_doc_drift.py`) | enforced | n/a |
| README tool table row | required (manual check) | n/a |
| `parity.json` rows | both harnesses, `--check` gated | none (no `cli:` category) |
| Precedent | — | `migrate_mcp`, `backfill_identity`, 5 `[project.scripts]` entries |

**Recommendation: console script.** `python -m vibe_cognition.migrate_mcp` is the
exact precedent — a repair utility the user runs when something needs fixing, not
an agent-facing capability. The announce and the §8 readme section are where it
gets discovered. If it later proves it wants to be agent-callable, promoting a
working CLI to a tool is cheap; the reverse is not.

Other surfaces this WP touches: README's "Automatic Git Hygiene" section needs an
SVN counterpart, and the README `VIBE_*` config table (≈L623) needs a
`VIBE_COGNITION_NO_VCS_HYGIENE` row.

**The env-var consolidation is bigger than rev 3 first estimated** ("at least
three places" was wrong). Verified surface:

| where | what |
|---|---|
| `src/vibe_cognition/config.py:172` | typed pydantic field `vibe_cognition_no_git_hygiene` + description |
| `src/vibe_cognition/cognition/git_hygiene.py:326` | `_opt_out()` reads `os.environ` **directly** |
| `src/vibe_cognition/cognition/readme.py:248` | user-facing team-setup text |
| `README.md:298`, `README.md:623` | opt-out prose + `VIBE_*` config table |
| `tests/test_config.py:280-295` | binding tests |
| `tests/test_git_hygiene.py:416-449` | five truthiness cases |

(`README.md:298` is an existing occurrence to rename; the `VIBE_*` config table
near L623 is an **insertion point** — it has no hygiene row today.)

Two independent readers of the same variable — the pydantic field **and**
`_opt_out()`'s direct `os.environ.get` — is a drift hazard the consolidation must
resolve rather than duplicate. Backward compatibility wants
`AliasChoices`-style acceptance of the old name on the config field, and matching
logic in whatever `_opt_out()` becomes. Neither is hard; both are easy to
half-do, leaving one reader honouring the alias and the other not.

### 9a. CI cannot test this the way it tests the git pass

**The single largest unaddressed risk in the plan, and it needs a ruling.**

`git_hygiene.py` is pure stdlib file I/O, which is why `tests/test_git_hygiene.py`
runs 620 lines without a git binary. The SVN pass shells out to `svn.exe` for
every write (§2). CI runs `ubuntu-latest` and `windows-latest`; **neither installs
Subversion, and no CI file references `svn` at all.**

That leaves two unappealing options:

- **Mock the subprocess.** Cheap, cross-platform, and **too weak to catch the
  failures §5 exists to warn about** — a mocked `svn` cannot reproduce the BOM
  mangling or the Windows CRT glob expansion, which are precisely the bugs that
  shipped silently at FRG.
- **Install a real `svn` in CI.** `apt-get install subversion` on the ubuntu leg
  is easy; the Windows leg is more work, and Windows is where both §5 gotchas
  actually live — so the cheap half of this option tests the platform that does
  not have the bugs.

**RULED (Colton, 2026-09-11): neither. SVN tests run LOCAL ONLY, as part of
burn-down.** No CI changes at all; `svn` stays absent from both legs.

**This collides with a standing acceptance criterion and needs a carve-out.** The
burn-down standing criteria (`docs/260702-fable-burndown-plan.md`) include: *"No
new test may spawn real subprocesses/sockets/network (mock them)."* SVN tests
spawn a real `svn.exe` by definition. Reconciling the ruling with that rule means
the SVN suite must sit **outside the default `uv run pytest` gate**, not inside it:

- Mark them `@pytest.mark.svn` and **deselect by default** via
  `addopts = -m "not svn"` in `[tool.pytest.ini_options]` (which today sets only
  `timeout = 60`; the existing marker vocabulary is `asyncio`/`parametrize`/
  `skip`/`skipif`/`timeout`, so `svn` is a new registered marker).
- `skipif` on `shutil.which("svn")` so an accidental `-m svn` on a machine without
  SVN skips rather than errors.
- Run explicitly (`uv run pytest -m svn`) during burn-down and before any release
  touching this module.

That keeps CI green and the no-subprocess criterion intact, while giving the SVN
code a real-binary suite that a human actually runs. **Consequence to state
plainly in the release procedure:** a green CI run says *nothing* about the SVN
pass. The burn-down step is the only gate it has, and skipping it ships untested
code — same bucket as the standing install-mechanics constraint.

The Windows-specific BOM and CRT-glob behaviour stays covered by the local lab
(`wp-svn-hygiene-lab.md`) as a hands-on gate, since even the local suite runs on
whatever platform the operator is on.

### 9b. Standing release cost this WP still pays

Rev 3 detailed the tool-surface HARD RULE and omitted the rest of the procedure.
As a user-facing change this WP also owes: version bump across **three** manifests
(`pyproject.toml`, `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json` —
`tests/test_codex_plugin.py` fails if they disagree) plus `uv lock`; a
`CHANGELOG.md` section; `.claude-plugin/whats-new.json` bullets; and the standing
gates `uv run ruff check .`, `render_harness.py --check`,
`render_parity.py --check`, full pytest, and sonnet review. Choosing a CLI over an
MCP tool (§9) avoids the audit, not any of this.

---

## 10. Decisions and open questions

### Settled (Colton, 2026-09-01)

- **One consolidated opt-out.** `VIBE_COGNITION_NO_VCS_HYGIENE` covers both
  passes; `VIBE_COGNITION_NO_GIT_HYGIENE` kept as a deprecated alias, either
  truthy value suppressing both. Same truthiness rules as today.
- **Never commit on the user's behalf.** Unconditional, unaffected by §3c.
- **The resolver ships in THIS WP.** It is the half that delivers the stated goal.
- **Verification is the local lab** in `docs/wp-svn-hygiene-lab.md`. `svn` and
  `svnadmin` 1.14.5 are already on PATH via TortoiseSVN — no install, no server.

### Open — needs a ruling before implementation

**~~Q1. Must the pass `svn add` `.cognition/`?~~ RULED (Colton, 2026-09-11):
the pass adds the folder AND its contents.** Full-depth `svn add .cognition`, not
depth-empty. The journal and document store are scheduled for commit; the user
still commits them (never us, §10 settled). See §3e for what this forces.

**~~Q4. Is a thinner automated-test posture acceptable?~~ RULED (Colton,
2026-09-11): SVN tests run LOCAL ONLY, as part of burn-down.** No CI changes —
`svn` is not installed on either leg. See §9a for what that implies.

**~~Q2. Does §3b auto-write, or become disclosure only?~~ WITHDRAWN in rev 4.**
Rev 3 raised this on a false analogy to the git-side `-text` decision; §3c
explains why it does not transfer. **The 2026-09-01 ruling stands: auto-write,
never commit, announce loudly.** No ruling needed.

**Q3. Does `svn:auto-props` displace per-pattern, or shadow wholesale?** A
DISPLACEMENT result is consistent with two mechanisms: true per-pattern
nearest-ancestor-wins (what §2 and §3b assume), or any `svn:auto-props` on
`.cognition/` blocking *all* ancestor auto-props regardless of pattern. Under the
second, installing our policy silently disables every other root auto-prop inside
`.cognition/` — a side effect we are not entitled to cause. **Lab E2's `*.log`
discriminator separates them.**

**Q3 is independent of how the policy gets installed.** Rev 3 called it "moot if
Q2 lands on disclosure-only"; that was wrong for the same reason §3c was.
Wholesale-vs-per-pattern shadowing is a property of Subversion, not of who runs
`propset`. Had §3b become disclosure-only, the readme's copy-pasteable command
would carry the identical hazard — and in a worse place, as unvalidated prose
outside our release gates rather than code covered by them. E2 gates the
correctness of whatever ships, in either form.

---

## 11. The real fix, named honestly

`svn:needs-lock` on `journal.jsonl` is the SVN-native "two people cannot collide"
answer and it is **wrong here** — it makes the file read-only until locked, and
the plugin appends constantly from a local process, so every append would fail
with a permission error. Do not ship it; the reasoning lives in the graph
(discovery `c931484f51ce`), not an inline comment.

**Implementation note:** `git_hygiene.py` is heavily commented — a 30-line module
docstring plus a per-version changelog. Do **not** mirror that style. Per current
code-hygiene rules, rationale goes to vibe-cognition; the SVN module gets only the
bare minimum comments needed for readability.

The genuine structural fix is **per-identity journal sharding**:
`.cognition/people/*.jsonl` already demonstrates it — one writer per file means no
conflict exists to merge, in SVN *or* git (where `merge=union` degrades to
belt-and-braces). Substantial, well outside this WP, and worth its own backlog
item. It would also retire the shared-worktree journal protocol constraint that
currently makes a manager serialise flushes by hand.

---

## 12. Sources

- [Repository Dictated Configuration Part 1 — Inheritable Properties](https://subversion.apache.org/blog/2013-06-24-repository-dictated-configuration-part-1-inheritable-properties.html)
- [Part 2 — Autoprops](https://subversion.apache.org/blog/2013-06-25-repository-dictated-configuration-part-2-autoprops.html)
- [Part 3 — Global Ignores](https://subversion.apache.org/blog/2013-06-26-repository-dictated-configuration-part-3-global-ignores.html)
- [Apache SVN wiki — Inheritable-Ignores-AutoProps](https://cwiki.apache.org/confluence/display/SVN/Inheritable-Ignores-AutoProps)
- [Apache SVN wiki — Property Inheritance](https://cwiki.apache.org/confluence/display/SVN/InheritedProperties)
- [SVN Book — Properties](https://svnbook.red-bean.com/en/1.8/svn.advanced.props.html)
- [SVN Book — Using External Differencing and Merge Tools](https://svnbook.red-bean.com/en/1.7/svn.advanced.externaldifftools.html)
- [Apache Subversion 1.14 LTS release notes](https://subversion.apache.org/docs/release-notes/1.14)
- [TortoiseSVN — Resolving Conflicts](https://tortoisesvn.net/docs/release/TortoiseSVN_en/tsvn-dug-conflicts.html)
- Internal: `F:\FRGVersionControl\ServerAdmin\Deploy-Policy.ps1`, `ServerAdmin\CLIENT-SETUP.md`,
  `docs\svn-server\SVN_CONTINUATION_HANDOFF.md`
- Graph: discoveries `c931484f51ce`, `9797cc63f2d3`, `41091f6214ae`; decisions
  `df70048739c8`, `9f13a8099e03`; task `914e4a354031`
