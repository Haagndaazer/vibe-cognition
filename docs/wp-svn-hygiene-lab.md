# WP-SVN-Hygiene — validation lab

**Status:** rev 2.2 — **Build 1 RUN 2026-09-11. See RESULTS at the end of this file.**
Headline: E0/E0b/E3/E1/E5 all passed or confirmed as predicted; **E2 returned
COMBINATION and killed design §3b** — the whole E2b fallback ladder is exhausted.
E4 and E6 not yet run; neither blocks the design change E2 forces.
**Purpose:** settle the unknowns in `docs/wp-svn-hygiene-plan.md` against a real
SVN client before any implementation work starts.
**Gating:** E2 gates design §3b. E0 gates whether the pass can work on a fresh
install at all. The rest confirm decisions already made but unverified.

> **rev 2 changes:** added E0 (unversioned `.cognition/`) and E6 (detection), which
> the first draft missed entirely; hardened E2's controls and added the
> shadowing-vs-per-pattern discriminator; moved E4 to its own lab build after the
> review showed the mandated run order would have made it observe nothing;
> replaced POSIX hexdump commands with PowerShell; added §10 scope-of-validity.

---

## 0. Prerequisites — already satisfied on Colton's machine

Verified 2026-09-01:

| tool | path | version |
|---|---|---|
| `svn.exe` | `C:\Program Files\TortoiseSVN\bin\svn.exe` (on PATH) | 1.14.5 |
| `svnadmin.exe` | `C:\Program Files\TortoiseSVN\bin\svnadmin.exe` (on PATH) | 1.14.5 |

**Nothing to install.** No server, no auth, no network — everything is `file:///`.
A second client version (1.15.x) is out of scope; 1.8+ inheritance semantics are
what we depend on and 1.14 is what teams actually run. See §10 for exactly how far
these results generalize — the honest answer is "less far than they will look."

---

## 1. Lab layout

```
E:\SvnLab\                     <- OUTSIDE both repos. Never inside vibe-cognition.
├── repo\                      svnadmin create target; URL file:///E:/SvnLab/repo
├── svnconfig\                 isolated --config-dir
├── wc-alice\                  Client A working copy
├── wc-bob\                    Client B working copy
├── wc-bob-fresh\              Client B cold-clone target (E2 only)
└── props\                     BOM-free property value files fed to `propset -F`
```

**`E:\SvnLab` must not live under `E:\E Drive Projects\vibe-cognition`** — git would
see it, and a `.cognition/` inside it would be picked up by the real hygiene pass
and pollute the experiment.

**Every `svn` invocation passes `--config-dir E:\SvnLab\svnconfig`.** Not optional.
The operator's real `%APPDATA%\Subversion\config` may already carry `[auto-props]`
entries and an `enable-auto-props` setting; without isolation, E2 cannot
distinguish "inherited property won" from "the operator's local config did it."

**BOM discipline:** every property value goes through a file in `props\` written
UTF-8 **no-BOM**, applied with `propset -F`. Never inline a value. E5 deliberately
violates this to reproduce the FRG failure.

**Hex-check helper (PowerShell 5.1).** The POSIX `head -c 16 | od -An -tx1` idiom
is unavailable in the lab's primary shell. Use:

```powershell
svn --config-dir E:\SvnLab\svnconfig propget svn:auto-props <target> > E:\SvnLab\props\_check.tmp
Format-Hex -Path E:\SvnLab\props\_check.tmp -Count 16
```

A leading `EF BB BF` is the BOM. (Git Bash is also available if the operator
prefers the POSIX form.)

### Lab builds

Two independent builds, because E4 cannot share a repo with E1/E2 (see E4):

- **Build 1** — E0, E2, E3, E1, E5, E6. Run in that order.
- **Build 2** — E4 only, from a clean `svnadmin create`.

`Remove-Item -Recurse -Force E:\SvnLab` between builds. Nothing may depend on a
prior experiment's *uncommitted* state; where an experiment depends on a prior
one's *committed* state, this document now says so explicitly.

---

## 2. Shared setup (Build 1)

Roles: **Client A = `wc-alice`** (policy author, commits first), **Client B =
`wc-bob`** (teammate who checks out fresh and later collides). Inherited
properties only reach a client through the repository, so B must always be
freshly checked out or updated.

1. `svnadmin create E:\SvnLab\repo`
2. Check out A at `file:///E:/SvnLab/repo` → `wc-alice`.
3. **Do not create `.cognition/` yet** — E0 needs it unversioned. In A, create and
   commit only the E2 controls and a root marker file.
4. Check out B → `wc-bob`.

Record the sha256 and byte length of every file created under `.cognition/` before
any commit; several experiments compare bytes after a round trip.

---

## 3. E0 — can the pass even run on a fresh install? (NEW, rev 2)

**Validates:** an assumption the design plan makes silently and never states.
**This is the most common real-world path** — first run of the hygiene pass in a
working copy where `.cognition/` has just been created by the plugin and has never
been added to SVN.

Design §3 says "set these three properties on the `.cognition/` directory" with no
mention of an `svn add` precondition. But `svn propset` fails on a target that is
not under version control. If that is confirmed, the pass has a much bigger
decision to make than the design plan currently admits — see the consequence box.

**Client A:**
1. Create `.cognition\journal.jsonl` (5 CRLF lines, unique `id` each),
   `.cognition\documents\text\<64-hex>.txt`, `.cognition\people\colton.jsonl`.
   **Do not add, do not commit.**
2. `svn propset svn:global-ignores -F props\global-ignores.txt .cognition`
   → record the exact error and exit code.
3. `svn add --depth empty .cognition` then retry the propset.
4. If step 3 works, determine the minimum: does `--depth empty` suffice for the
   property to be set and later committed, or must the children be added too?
   Does the property survive `svn commit` of a depth-empty add?
5. Repeat 3–4 with `svn add .cognition` (full depth) and compare.

**Observations:** exact error text from step 2; whether `--depth empty` is
sufficient; whether a depth-empty add commits the directory without its contents.

### E0b — does `svn add` honour an UNCOMMITTED ignore property? (rev 2.2)

Added after Colton ruled (2026-09-11) that the pass adds `.cognition/` **and its
contents**. Plan §3e derives a required order: add the dir depth-empty → propset
the ignores → full-depth add, so the add is filtered by our own policy. **That
whole sequence rests on an untested assumption.**

6. From the depth-empty state, `svn propset svn:global-ignores -F ... .cognition`
   — **do not commit.**
7. Create the junk set beside real content: `.cognition\a.lock`,
   `.cognition\last-seen.json`, `.cognition\onboard-declined`, plus
   `.cognition\journal.jsonl` and a `documents\text\<sha>.txt`.
8. `svn add .cognition` (full depth).
9. `svn status` → **which files got scheduled?**

| result | consequence |
|---|---|
| junk skipped, real content added | plan §3e's order works as written |
| junk added too | the property is not consulted for an uncommitted dir → the pass must enumerate files and apply the ignore list **in Python**, not delegate to `svn add` |

The second outcome is not fatal — the pass already owns the ignore list as data —
but it is a materially different implementation. **This must be answered before
anyone writes the add path.** Shipping the wrong assumption sweeps machine-local
files into version control: the WP-TC14 gate-F1 failure class, repeated in a
second VCS.

> **Consequence if propset requires a versioned target (expected):** the pass can
> no longer be described as "sets some properties." It would have to **add a
> directory to version control on the user's behalf** — a materially larger action
> than writing a property, and one that starts scheduling *content* (the journal,
> the document store) for commit. That is a new scope question for Colton, not a
> detail: see plan §8 open question 6.

**PASS:** we know exactly what the pass must do before its first propset, and what
it is committing the user to.

---

## 4. E2 — auto-props displacement (THE GATING EXPERIMENT)

**Validates:** design §3b. If this fails, §3b does not ship.

> **rev 2.1 note (plan rev 4).** E2 gates §3b **regardless of whether the policy
> is auto-written by the pass or copy-pasted by a user from the readme.**
> Wholesale-vs-per-pattern shadowing is a property of Subversion, not of who runs
> `propset` — if anything, a documented manual command is the *worse* place for an
> unverified hazard, since it sits outside our release gates. Plan rev 3 said E2
> would become "documentation research" under a disclosure-only ruling; that was
> wrong and is withdrawn along with the ruling question itself.

**The question:** does an explicit `svn:mime-type=text/plain` rule for `*.txt` on
`.cognition/` *displace* an inherited root rule `*.txt = svn:eol-style=native`, or
do the two *combine* because they set different property names?

### Setup — Client A

1. `props\root-autoprops.txt` (simulating a team's root policy) — note the
   **fourth line**, which `.cognition`'s policy deliberately does *not* set:
   ```
   *.md = svn:eol-style=native
   *.txt = svn:eol-style=native
   *.jsonl = svn:eol-style=native
   *.log = svn:eol-style=native
   ```
   → `svn propset svn:auto-props -F props\root-autoprops.txt .`
2. `props\cog-autoprops.txt` (ours):
   ```
   *.md = svn:mime-type=text/plain
   *.txt = svn:mime-type=text/plain
   *.jsonl = svn:mime-type=text/plain
   ```
   → `svn propset svn:auto-props -F props\cog-autoprops.txt .cognition`
3. **BOM-check BOTH files** using the §1 helper — `.` *and* `.cognition`.
   The first draft checked only the root, which was the wrong one: a BOM in
   `cog-autoprops.txt` silently kills its **first line only** (`*.md`), producing
   a split result — COMBINATION on `.md`, DISPLACEMENT on `.txt`/`.jsonl` — that
   is indistinguishable from a real SVN finding.
4. Commit.
5. **Independently confirm the commit landed.** From a fresh checkout, assert
   `svn propget svn:auto-props .cognition` byte-equals `cog-autoprops.txt`.
   Without this, a silently-failed propset at step 2 is indistinguishable from
   "inheritance doesn't reach `.cognition/`" — the outcome table's third row —
   and the experiment would be explaining its own setup bug as an SVN behaviour.

### Controls — three, not one

At the **repo root**, add `control.txt`, `control.md`, `control.jsonl` and
`svn proplist -v` each. **All three must show `svn:eol-style=native`.** If any
does not, the corresponding root rule never fired and every subject observation
for that extension is void — stop and debug the setup. (The first draft
controlled only `.txt` while drawing conclusions about `.md` and `.jsonl` against
an unverified baseline.)

### Subjects — Client B, cold clone

Check out to **`wc-bob-fresh`** (a new directory — checking out over the existing
`wc-bob` behaves as an update, not a cold clone, which defeats the point). Then
`svn add` and `svn proplist -v`:

- `.cognition\documents\text\<newsha>.txt`
- `.cognition\notes.md`
- `.cognition\extra.jsonl`
- `.cognition\x.log` ← **the discriminator, see below**

### Outcomes

| observed on `.txt` / `.md` / `.jsonl` subjects | verdict | consequence |
|---|---|---|
| `svn:mime-type` only | **DISPLACEMENT** | §3b viable — now read the discriminator |
| both `svn:mime-type` and `svn:eol-style=native` | **COMBINATION** | §3b as written fails → run E2b |
| `svn:eol-style` only | inheritance not reaching `.cognition/` | only trustworthy if step 5 passed; else it is a setup bug |

### The discriminator — `x.log`

A DISPLACEMENT result is consistent with **two different mechanisms**, and the
first draft could not tell them apart because its policies had 100% pattern
overlap:

- **(a) per-pattern nearest-ancestor-wins** — the claimed mechanism, and what
  §3b actually depends on. `x.log` **inherits** `svn:eol-style=native` from root,
  because `.cognition`'s policy says nothing about `*.log`.
- **(b) whole-property shadowing** — any `svn:auto-props` on `.cognition` blocks
  *all* ancestor auto-props regardless of pattern. `x.log` gets **nothing**.

This is not academic. Under (b), installing our policy would **silently disable
every one of the team's other root auto-props inside `.cognition/`** — a side
effect the design plan does not mention and would not be entitled to cause. If
(b) holds, §3b must either replicate the inherited rules it displaces or be
abandoned.

### Secondary observation, free with this run

The isolated config dir has no `[auto-props]` and no `enable-auto-props`. If props
still land, that confirms inherited auto-props fire independently of client
config — the claim `F:\FRGVersionControl\ServerAdmin\CLIENT-SETUP.md` makes, and
the reason the pass can promise a teammate anything at all.

### E2b — fallbacks, run only on COMBINATION

Stop at the first that works.

- **E2b-i — same-property override.** Set our rule to `*.txt = svn:eol-style=`
  (empty value). Does svn accept it, and does it neutralise the inherited one?
- **E2b-ii — split the policy by what each path needs.** Most promising.
  `documents/**` carries no `merge=union` in git because its contents are
  content-addressed and must never merge, so `svn:mime-type=application/octet-stream`
  costs nothing there and guarantees verbatim storage. **Only** for
  `documents/**` — applying it to `journal.jsonl` makes every collision a
  whole-file binary conflict and destroys the §4 resolver (E3 demonstrates this).
- **E2b-iii — extension change.** Sidecars named with an extension no root policy
  targets (e.g. `.cogblob`). Real design change and migration cost, but the only
  option immune to a root policy we have never seen.

---

## 5. E3 — journal conflict behaviour and the resolver recipe

**Validates:** design §4. **Depends on E2's committed state** (the `.cognition`
auto-props are live by now) — that is intentional and matches the real deployment.

**Do not assume the conflict.** If SVN's diff3 merges two pure EOF appends
cleanly, §4 is unnecessary and the plan's headline softens considerably. That
would be a good outcome and must not be assumed away.

**Sequence:**
1. A and B at the same revision, `journal.jsonl` committed.
2. **A** appends 3 lines (unique ids), commits.
3. **B** — *without updating* — appends 2 different lines (unique ids).
4. **B** runs `svn update`.

**Observations:** does it conflict at all, or auto-merge (record verbatim); if
conflicted, the **exact** artifact filenames (expected `journal.jsonl.mine`,
`journal.jsonl.rN`, `journal.jsonl.rM` — capture the real names, the resolver has
to find them); text vs tree conflict; markers present in the working file;
`svn status` letter.

**Then validate the resolver recipe by hand**, exactly as §4 describes: union the
line sets of `.mine` and `.rNEW`, dedupe by node `id`, preserve order, write,
`svn resolve --accept working`, commit. **Then: does A see all 5 lines after
updating, no loss, no duplicates?** That end-to-end check is E3's real deliverable.

**Mime-type interaction (links E2 and E3):** re-run 1–4 with
`svn:mime-type=application/octet-stream` on `journal.jsonl`. Expected: whole-file
binary conflict, different artifacts. This is what turns E2b-ii's "documents only,
never the journal" boundary into an evidenced rule rather than a hunch.

**Also run for `.cognition\people\colton.jsonl`** — same append-only shape, already
union-merged in git as of hygiene v6, so the resolver must cover it.

---

## 6. E1 — `svn:global-ignores` on a subdirectory

**Validates:** design §3a, plus a claim made from memory in the design plan
(`chromadb/` with a trailing slash fails to match where bare `chromadb` succeeds)
that must not ship unverified.

**Depends on E2's committed state.** Note that `.cognition\keepme.txt` below will
carry E2's `svn:mime-type` property when added — that is expected cross-experiment
carry-over from the committed policy, not a bug.

**Client A:** write `props\global-ignores.txt` (BOM-free): `chromadb`,
`.git-hygiene-managed`, `*.lock`, `.last-rehydrate.json`, `onboard-declined`,
`last-seen.json*`, `backfill-identity-map.skeleton.json`. Propset on `.cognition`,
commit.

**Client B:** `svn update`, then create unversioned junk:
- `.cognition\chromadb\chroma.sqlite3` — directory case
- `.cognition\a.lock`
- `.cognition\last-seen.json` **and** `.cognition\last-seen.json.tmp` — glob case
- `.cognition\documents\nested.lock` — **recursion case**, the whole reason this is
  `global-ignores` and not `ignore`
- `.cognition\ChromaDB\x.db` — **case-sensitivity case** (rev 2). SVN's matching is
  fnmatch-based and case-sensitive on every platform, while Windows filesystems are
  case-insensitive. If this escapes the ignore, the shipped glob list needs
  case variants or a documented caveat.
- `.cognition\keepme.txt` — **negative control**, must still appear as `?`

**Observations:** `svn status` hides the junk and shows `keepme.txt` as `?`;
`svn status --no-ignore` shows the junk as `I`; `svn add .cognition --force` adds
only `keepme.txt`.

**Trailing-slash sub-test:** repeat with `chromadb/` and re-observe.

| result | meaning |
|---|---|
| bare matches, trailing-slash does not | design §3a correct as written |
| both match | the plan's warning is wrong — soften it |
| neither matches | directory ignoring needs a different glob; blocks §3a |

**Already-versioned sub-test (rev 2):** commit `.cognition\tracked.lock` *first*,
then confirm the `*.lock` ignore does not hide it from `svn status`, `svn update`,
or `svn commit`. Design §3a states this as settled fact and never tested it. Low
risk, one command.

**PASS:** recursion works and the negative control still shows. Recursion is the
load-bearing claim; the slash and case questions each change a sentence or a glob.

---

## 7. E4 — uncommitted props, and what the announce must say

**Validates:** the "auto-write but never commit, announce loudly" decision (§8).

> **RUNS ON ITS OWN LAB BUILD (Build 2), before E1/E2 have touched anything.**
> Peer review caught that in the original E2→E3→E1→E4→E5 order, `.cognition`
> already carries *committed* `svn:auto-props` and `svn:global-ignores` by the time
> E4 runs — so propsetting the same values produces no delta and step 2's `M`
> never appears, while `svn revert` in step 4 restores the committed values rather
> than removing them. E4 would have observed nothing and looked like a pass.

1. Fresh `svnadmin create`, checkout, `.cognition/` versioned, **no hygiene props
   committed**.
2. Propset both properties. **Do not commit.**
3. `svn status .cognition` → record exact output. Expect `M` in the **second**
   column (property modification), easy to scan past. The announce must name the
   literal thing the user will see.
4. `svn update` → confirm uncommitted prop mods survive.
5. `svn revert .cognition` → confirm the props are **gone**.
6. Reason through the pass: with the version flag already written, would it
   restore them? Almost certainly not — which is a real design bug.

**This experiment is expected to produce a plan change, not a confirmation.**
Candidate fix to weigh: fold the prop check into the read-only
`check_hygiene_state` path so the announce re-warns every session while the props
are missing, instead of trusting the write-once flag.

---

## 8. E5 — reproduce the BOM trap (confirmatory)

**Validates:** a finding borrowed from `F:\FRGVersionControl`, never reproduced by
us. Cheap; converts inherited knowledge into our own evidence.

1. Write `props\bom-ignores.txt` **with** a UTF-8 BOM, first line `chromadb`.
2. Propset it on `.cognition`.
3. Read it back with the §1 `Format-Hex` helper → expect the value to begin
   `c3 af c2 bb c2 bf` (the BOM re-encoded through CP1252).
4. Create `.cognition\chromadb\x.db` → expect it **not** ignored, with no error.

**PASS:** the first rule is silently dead. Confirms "always write no-BOM" is
load-bearing, not cargo-cult.

---

## 9. E6 — detection and coexistence (NEW, rev 2)

**Validates:** design §2, which is entirely assertion today. Cheap, no new build.

1. **Walk-up `.svn` detection.** Confirm `.svn` exists only at the WC root (1.7+),
   then place `.cognition/` two directories deep and verify the walk-up from
   `repo_path` finds the root. The design's detection logic depends on this.
2. **Mixed git+svn.** `git init` inside `wc-alice`, so `.git` and `.svn` coexist.
   Confirm both passes' mechanisms are independent: `svn status`/`svn add` are
   unbothered by `.git/`, and writing `.gitattributes` does not disturb SVN. The
   design claims they "cannot conflict"; verify rather than assert.
3. **Teammate who never updates.** After the hygiene props are committed by A, a B
   that never runs `svn update` gets **zero** protection — no ignores, no
   mime-type override — silently. Demonstrate it. This is the practical meaning of
   design §1 point 2 and belongs in the README's team-setup text.

---

## 10. Scope of validity — what these results will NOT prove

The results must be reported with this caveat attached, or "true on one Windows
box with TortoiseSVN 1.14.5 against a scratch `file://` repo" will silently be
read as "true for SVN."

- **Single client build.** Auto-props override resolution is exactly the kind of
  edge-case mechanism that has had version-specific quirks. A team on 1.9/1.10 is
  not covered by "1.14.5 said DISPLACEMENT."
- **Single OS.** No Linux or macOS client. Case sensitivity (E1) and OS-level
  EOL/encoding interactions are unverified for the mixed-platform teams this
  feature exists to serve — and cross-platform is precisely where an
  `svn:eol-style=native` rule does its damage.
- **`file://` only — no server, no hooks, no authz.** A real server can run
  pre-commit hooks that strip or rewrite properties, and path-based authz can deny
  a user write access to `.cognition/`. Neither is reachable here, so "the pass can
  promise a teammate anything" is proven only for a permission-free, hook-free
  environment.
- **Fresh single-history repo.** No test against a repo carrying years of
  inconsistent prior property-setting by other tools — the actual condition of most
  real team SVN repos.

---

## 11. Deliverable

A results section appended to this file: per experiment, the verdict, verbatim
evidence (commands + output), and the specific edit each result forces in
`wp-svn-hygiene-plan.md` — plus the §10 caveat. Then the plan is updated and
implementation can start.

**Order:** Build 1 — E0 (gates whether the pass can work at all), E2 (gates a
design), E3 (gates the resolver), E1, E5, E6. Build 2 — E4.

If E0 shows the pass must add files to version control, **stop and get a ruling
from Colton before continuing** — that is a scope change, not a detail. If E2
returns COMBINATION and all of E2b fails, stop and re-plan §3b.

**Explicitly not in scope:** any write to the real vibe-cognition repo; any
network or server SVN; any 1.15 client; implementing the hygiene module.

---

# RESULTS — Build 1, run 2026-09-11 (svn/svnadmin 1.14.5, Windows 11, file:///)

Lab at `E:\SvnLab\build1`. Isolated `--config-dir` verified clean (0 auto-props
lines) before any experiment. All property values written UTF-8 no-BOM except
where E5 deliberately violates it.

## E0 — unversioned `.cognition/` — **CONFIRMED, as predicted**

`svn propset` on an unversioned directory fails:

```
'...\.cognition' is not under version control
svn: E155010: The node '...\.cognition' was not found.
```

`svn add --depth empty .cognition` succeeds and registers the directory ALONE —
`svn status` then shows `A .cognition` with contents still `?`. `propset` on that
uncommitted, depth-empty-added directory **succeeds**, and all 7 ignore lines read
back intact.

**Plan Q1's premise is real.** The pass cannot set any property without first
adding the directory.

## E0b — does `svn add` honour an UNCOMMITTED ignore property? — **PASS**

**Yes, decisively.** With `svn:global-ignores` set but not committed, on a
directory only *scheduled* for addition:

- `svn status --no-ignore` marked every junk file `I`: `.git-hygiene-managed`,
  `.last-rehydrate.json`, `a.lock`, `chromadb`, `last-seen.json`,
  `last-seen.json.tmp`, `onboard-declined`.
- `svn add --force .cognition` scheduled **only** real content — `journal.jsonl`,
  `documents/text/<sha>.txt`, `people/colton.jsonl`.
- Recursion confirmed: `.cognition\documents\nested.lock` showed `I` inside an
  unversioned-then-added subdirectory.

**Plan §3e's ordering works as written. The Python-side ignore-filtering fallback
is NOT needed.**

One correction to §3e: step 4 must be **`svn add --force .cognition`**, not
`svn add .cognition`. Plain `add` refuses an already-versioned directory
(`W150002` / `E200009: Could not add all targets because some targets are already
versioned`). `--force` is what makes it recurse and pick up unversioned children.

## E2 — auto-props displacement — **COMBINATION. §3b IS DEAD.**

Controls all three fired (`control.txt`, `control.md`, `control.jsonl` each got
`svn:eol-style=native`), and the committed `.cognition` policy was asserted
byte-identical to its source file, so the experiment is valid.

Every subject in a **cold clone** came back carrying **both** properties:

| subject | result |
|---|---|
| `documents/text/<sha>.txt` | `svn:eol-style=native` **+** `svn:mime-type=text/plain` |
| `notes.md` | `svn:eol-style=native` **+** `svn:mime-type=text/plain` |
| `extra.jsonl` | `svn:eol-style=native` **+** `svn:mime-type=text/plain` |
| `x.log` (discriminator) | `svn:eol-style=native` only |

**Two findings:**

1. **Different property names for the same pattern COMBINE.** A `svn:mime-type`
   rule does not displace an inherited `svn:eol-style` rule. §3b's entire
   mechanism does not exist.
2. **Q3 answered, and the answer is good:** `x.log` inherited the root rule
   normally, so our policy does **not** wholesale-shadow ancestor auto-props. The
   blast-radius claim in §2 is safe — we never silently disable a team's other
   rules.

### E2b fallback ladder — all three exhausted

- **E2b-i (empty value)** — **FAILS DANGEROUSLY.** `*.md = svn:eol-style=` does not
  neutralize; it makes `svn add` refuse the file outright:
  `svn: E135001: Unrecognized line ending style '' for '...probe1.md'`. The file
  could not be added at all. Strictly worse than doing nothing.
- **E2b-ii (binary mime-type)** — **FAILS.** Because the properties combine, SVN
  tries to apply both and errors:
  `svn: E200009: Can't set 'svn:eol-style': file '...probe2.txt' has binary mime
  type property`. `svn add` fails. Also strictly worse than doing nothing.
- **E2b-iii (same property, valid value)** — **WORKS**, and is the only mechanism
  that does. `*.jsonl = svn:eol-style=CRLF` on `.cognition/` beat the root's
  `native`; the subject came back with `svn:eol-style CRLF` alone. So
  nearest-ancestor-wins is real, but **only per property NAME**, not across
  different properties matching the same pattern.

**Conclusion:** an inherited `svn:eol-style` can be *overridden* but never
*removed*, and every valid value (`native`/`CRLF`/`LF`/`CR`) translates line
endings. There is no value meaning "do not translate". **§3b cannot be built.**

## E3 — conflict behaviour and the resolver — **PASS, end to end**

The conflict is real, not assumed. Alice appended 3 lines and committed (r7); Bob
appended 2 different lines without updating, then updated:

```
C    .cognition\journal.jsonl
Summary of conflicts:  Text conflicts: 1
```

Artifacts: `journal.jsonl.mine`, `journal.jsonl.r6`, `journal.jsonl.r7` — a **text**
conflict, markers in the working file. Note the "theirs" file is named by actual
revision number, so **the resolver must discover it (highest `.rN`), not assume a
fixed name.**

The §4 recipe then worked exactly as designed: union `.mine` + `.r7`, dedupe by
node `id`, write, `svn resolve --accept working`, commit (r8). After Alice
updated: **10 lines, 10 unique ids, 0 conflict markers**, both sides' work intact
(`a0001-a0005, bob1, bob2, alice1, alice2, alice3`).

## E1 — ignore semantics — **PASS, plan's claim confirmed**

- **Trailing slash matters.** `chromadb/` **fails to match** (directory shows `?`);
  bare `chromadb` ignores it. The plan's from-memory claim was correct.
- **Case sensitivity: inconclusive on Windows, and moot there.** `ChromaDB\` and
  `chromadb\` are the same directory on a case-insensitive filesystem, so the
  divergence cannot be produced locally. **Still a live risk for Linux/macOS
  teammates** and unverifiable in this lab — carry it as a documented caveat.

## E5 — BOM trap — **REPRODUCED**

With a UTF-8 BOM on the value file, `chromadb` was **not** ignored (showed `?`)
— while `svn propget` displays the first line as a clean `[chromadb]`. The
corruption is invisible to the obvious check. FRG's finding is now ours.

## Not run

E4 (needs its own clean build), E6 (detection/coexistence). Neither blocks the
design change E2 forces.
