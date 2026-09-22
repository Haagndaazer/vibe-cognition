# Hermes as harness #3 — re-evaluation

Status: RESEARCH rev 2 (findings only — no implementation authorized, none
performed)
Peer review: sonnet adversarial pass 2026-09-21 — APPROVE-WITH-CHANGES; all
nine findings applied, including the two blocking ones. The review's central
catch: rev 1 claimed the Hermes `delegate_task` rejection made curation
*containment* permanently partial, which contradicted this document's own
verified finding that containment is server-side and harness-independent —
Codex has no per-agent tool scoping either and still reached `full` on all
four curation agents. The real Hermes deficiency is model tiering. The
headline was rebuilt on that distinction and the recommendation now rests on
the two reasons the review found independently sufficient.
Author: Colton Dyck (solo session)
Date: 2026-09-21
Baseline: `docs/hermes-parity-plan.md` rev 2 (2026-09-01), scoping episode
`d82978200b66` (2026-08-31), deferral decision `faa1ffbf0ff6` (2026-09-08).
Trigger: Codex shipped as harness #2; re-assess Hermes now that the
multi-harness infrastructure exists.

Evidence policy (ledger 23): every Hermes claim below is **[doc]** or
**[src-read]** from a live research pass, never a live call — nobody on this
team has Hermes installed. Documentation and source reads are confidence, not
truth. No Hermes claim here may harden into a work-package brief without a
live spike on a real install first. Claims about **our own tree** are
**[verified]** — read and re-checked in this session at HEAD `b7d62eb`.

## Headline

**Recommendation: do not adopt Hermes as harness #3 now. Adopt the
harness-agnostic hardening the Codex port skipped, and if a third harness is
wanted afterwards, Hermes is not the strongest candidate.**

Three independent reasons, in order of weight:

1. **"Harness #3 is cheaper than harness #2" was promised but not built.**
   The decision that set the Hermes direction (`c0c6c89a381c`) made cheapness
   the success criterion. The parts that make it cheap — the generalized
   adapter conformance kit (WP-H0c) and the capability contract doc (WP-H0b)
   — **do not exist in the tree** [verified]. What shipped instead was one
   Codex-specific prototype. The remaining work for a third harness is a new
   conformance suite, a new manifest test, a harness-specific render-test
   set, and ~50 hand-authored prose blocks. Whether that totals *more or
   less* than the Codex port is not measured here, only that the line items
   the plan intended to eliminate are still there.

2. **Parity on harness #2 is already unpaid.** Of 54 parity rows, 11 are
   `full` on Codex and 43 are `partial` [verified]. Releases 0.41.0 through
   0.43.0 shipped before the next Codex field gate ran, and when it ran it
   found two real defects (v0.43.1). A third harness multiplies a human gate
   we are already behind on.

3. **Hermes permanently forecloses the cheap curation shape, and its
   injection path has an open reliability hole.** Per-spawn model pinning for
   `delegate_task` was proposed twice (issues #9459, #35409), implemented
   once (PR #48644, closed unmerged 2026-07-14), and rejected by the
   maintainer with "We do not want this" **[src-read]**. Note carefully what
   this does and does not cost us — see "What Hermes actually costs" below.
   Separately, plugin and shell hook registration is reported broken under
   `hermes serve` and the desktop backend **[doc]**, which would make our
   context injection silently no-op for those users.

### What Hermes actually costs (corrected after review)

An earlier draft of this document claimed the `delegate_task` rejection made
curation *containment* permanently partial on Hermes. **That was wrong, and
the correction matters.** Containment is a server-side token and does not
depend on any harness capability. Codex already has no per-agent tool
scoping — its policy entry reads "curation-session token only (Codex roles
cannot restrict tools)" — and all four Codex curation agents are nonetheless
recorded `full` [verified]. A harness without tool scoping can reach full
containment parity, and Hermes would.

What Hermes genuinely forecloses is **model tiering**, and the mechanism is
specific. Our standing rule since fail `f09e770da046` is that every analyzer
spawn carries an *explicit* model argument, because an implicit pin was
observed not to hold. On Codex that rule is satisfiable: each `spawn_agent`
call takes a `model`, and the orchestrator template states that this explicit
argument is the only thing pinning the model there. On Hermes there is no
per-call model parameter at all — children inherit the parent, or a single
global `delegation.model` in the user's `config.yaml` **[src-read]**. So:

- The rule cannot be honored per spawn. The only lever is a user-level config
  file we do not control and cannot guarantee.
- Orchestrator and analyzers cannot run at different tiers, because one
  global setting covers both. The mid-orchestrator, small-analyzer split that
  makes high-volume fan-out affordable is unavailable.

That is a **permanent cost problem, not a safety problem**, and it is
narrower than the earlier draft claimed. It is still a real and unclearable
parity gap, but on its own it would not be sufficient grounds to reject
Hermes. Reasons 1 and 2 are what carry the recommendation.

## What changed on the Hermes side since 2026-08-31

The original five findings **all held up**. None was wrong; several got
sharper, and two were materially wrong in our favour.

| # | 2026-08-31 finding | Today | Effect on cost |
|---|---|---|---|
| 1 | stdio MCP via `config.yaml` or Agent Plugins v1; `${workspaceFolder}` is the project root | **Refined.** `${workspaceFolder}` exists only on the *native config* path. The portable-plugin path expands only `${PLUGIN_ROOT}`/`${PLUGIN_DATA}` and defaults cwd to the plugin root **[src-read]** | Same project-root problem Codex had; the Codex solution (register from a hook, not a manifest) transfers |
| 2 | MCP `instructions` not surfaced | **Unchanged**, now source-confirmed: only `capabilities.tools` is read **[src-read]** | Standing practices must ride skills, as planned |
| 3 | `session:start` observer-only; injection only via a native Python plugin's `pre_llm_call`, ~10K cap | **Better than thought.** The 10K cap is exact and *spills to a file* rather than truncating. And **shell-script hooks can also return `{"context": …}` on `pre_llm_call`** **[doc]** — no native Python plugin required | **Large cost reduction.** Our injection path is a shell script today |
| 4 | `session:compress` exists for post-compaction reinjection | **Unchanged**, source-confirmed with its payload **[src-read]** | D3 is workable |
| 5 | `delegate_task` has no per-spawn model or tool scoping | **Unchanged, and now known to be deliberate** — implemented and rejected upstream **[src-read]** | Tiering permanently unavailable; containment unaffected. See "What Hermes actually costs" |

Newly learned, not covered by the original pass:

- **Windows is now first-class** **[doc]**: native Windows 10/11, no WSL, a
  bundled portable Git Bash for command execution, and a checked-in CI linter
  for Windows footguns. This removes a risk the plan rated high (Hermes plan
  risk 6). One gap remains, a dashboard terminal pane needing a POSIX pty.
- **Hook registration is broken outside the plain CLI** **[doc]**: two open
  issues report that plugin hooks and config shell hooks never register under
  `hermes serve` / the desktop backend. Our entire context-injection story
  would silently no-op there, with no error surfaced. This is exactly the
  class of failure the install-mechanics constraint exists for.
- **MCP timeouts are survivable**: 60 s default connect timeout, per-server
  overridable, correctly enforced around `initialize` **[src-read]**. But the
  first agent build waits only ~1.5 s for tool discovery, so on a cold start
  our tools are absent on turn 1. That is the same shape as Codex's
  "send one message, then restart", which we already ship and document.
- **The hook JSON contract deliberately aliases Claude Code's shapes**
  (`{"decision":"block"…}`, `{"decision":"modify"…}`) **[doc]**, and the
  portable format injects `PLUGIN_ROOT`/`PLUGIN_DATA` reserved env vars —
  a direct functional analog of the Claude variables.

Adoption: reported as very large, but the specific figures the research pass
returned (star and contributor counts that would make it one of the largest
repositories on GitHub, and ~5,800 commits in a month) are implausible enough
that they should be checked by hand before anyone cites them in a decision.
The qualitative signal — a reviewed first-party plugin catalog with dozens of
real SHA-pinned entries, and third-party directory sites — is the part worth
trusting.

## What changed on our side: the real cost of harness #3 today

All findings in this section are [verified] against HEAD.

### What genuinely generalizes (the promise kept)

- **Curation containment is fully server-side.** `cognition_begin_curation`
  mints a token held in process state; the four edge-writing tools refuse
  without it and stamp every write with the session id; `edges_outside_curation`
  telemetry persists. None of it depends on a harness capability, and the
  token tests pass with `VIBE_HARNESS` unset. **A third harness inherits
  containment for free** — this was WP-H0a and it is done.
- **Dependency bootstrap is shared, not duplicated.** The Codex hook wrapper
  does only Codex-specific registration and then `exec`s into the same
  `hooks/session-start.sh` that Claude Code runs. The uv sync, the
  `import torch, chromadb` health probe, and the retry-next-session self-heal
  are written once. A third harness wrapper plausibly follows the same
  exec-and-delegate shape for near-zero incremental cost.
- **The harness policy table's load-bearing fields do generalize**:
  `skill_prefix`, `default_models`, `curation_available`, `manifest_path`,
  `marketplace_manifest_path`, and the update-CTA templates are all consumed
  generically by `instructions.py`, `cognition_tools.py`, `service_tools.py`
  and `update_check.py`.
- **The parity registry fails correctly on a missing column.** A row lacking
  the new harness key is a hard CI failure, not a silent pass.
- **The forbidden-literal lint works.** No stray harness literals were found
  outside the policy table and the harness blocks.

### What does not generalize (the promise not kept)

1. **The renderer is hardcoded to exactly two harnesses.** `targets()`,
   `reference_targets()` and `role_targets()` each pull `claude` and `codex`
   from the table *by name* and hardcode which skills, references and roles
   render for Codex, with literal output paths. Adding a harness to the
   policy table does nothing here. This is a code change, and the project's
   own checklist admits it.

2. **A third harness silently renders empty prose.** The renderer's block
   substitution returns the block body only when the harness name matches,
   and an empty string otherwise. There are **50 harness blocks across 7
   source files, 35 of them in `agents-src/curate-orchestrator.md`** — every
   one would render as empty content for a harness with no matching block,
   with **no error**. The render test only checks for leftover `{{` markers,
   not for suspiciously empty output. This is the single sharpest defect
   found: it turns the largest authoring task into a silent-correctness risk.
   It is also a *present-day* granularity gap, not only a future-harness one:
   the per-harness difference test compares whole rendered files, so a
   mistyped block that renders empty for **both** shipped harnesses would
   still pass as long as anything else in the same file differs.

3. **A third of the policy table is dead.** `spawn_background_hint`,
   `spawn_depth_note`, `plugin_root_var`, `plugin_data_var` and
   `update_source` are declared and populated for both harnesses but read by
   nothing. `is_codex()` has zero callers. Worse, `update_check.py` and
   `whats_new.py` read the literal strings `CLAUDE_PLUGIN_ROOT` and
   `CLAUDE_PLUGIN_DATA` from the environment instead of the fields that exist
   for exactly that purpose. Both call sites do `if not plugin_root or not
   plugin_data: return 0` — a silent, output-free early return [verified].
   This is **inert today**: the Codex hook wrapper sets the Claude-named
   variables explicitly, so nothing is broken on either shipped harness.
   It becomes live only for a harness that does not mimic the Claude names,
   and Hermes is exactly that — it injects `PLUGIN_ROOT`/`PLUGIN_DATA` and
   has no `CLAUDE_PLUGIN_ROOT` anywhere **[doc]**. On such a harness the
   update nudge and what's-new would quietly do nothing, with no crash and
   no log.

4. **There is no adapter conformance kit.** WP-H0c called for a
   harness-parameterized suite that launches our code the way each harness
   does. The word "conformance" appears nowhere in the tree outside plan
   prose. What exists is one hand-written Codex instance
   (`tests/test_codex_hooks_shell.py`, 262 lines) plus a Codex manifest test
   (163 lines). A third harness writes both from scratch. Of the 15 render
   tests, 7 are Codex-specific by name and a couple more exercise the Codex
   harness object internally [verified].

5. **`docs/harness-contract.md` does not exist.** The D1–D9 duty contract
   lives only inside the shelved Hermes plan. `docs/HARNESSES.md` is a
   shorter, informal successor.

6. **There is no prime size budget.** Prime has per-item and per-summary caps
   but no ceiling on the assembled digest, and no per-harness configuration.
   Hermes's 10 K injection limit would need this built. It is genuine feature
   work, and it benefits Claude Code too.

7. **The destructive-pattern lint does not scan `adapters/`.** A new adapter
   tree ships unscanned unless that is fixed.

### Itemized cost for any third harness

| Kind | Items |
|---|---|
| Data edit | Policy-table entry; one-line `HARNESS_NAMES` edit; a third column on all 54 parity rows |
| Code change to a two-harness assumption | Renderer target selection and per-harness subsets; the empty-block silent failure; the two hardcoded Claude env-var reads; the what's-new fast path keyed to `.claude-plugin/plugin.json`; two hardcoded test lists; the lint's scan directories |
| New hand-written files | Manifest, hooks declaration, wrapper hook scripts, Windows shims if needed, a conformance test file, a manifest test file, a harness-specific render-test set |
| New hand-written content | ~50 harness blocks carrying real spawn and orchestration semantics, 35 of them in one file, none of it derivable from the policy table |
| Human field gate | A full install-and-exercise pass on a real machine before any row goes `full` |

The first row is cheap and mechanically enforced. The last three are the
same line items harness #2 paid, and the infrastructure meant to eliminate
them was not built. No one has measured whether the total is larger or
smaller than the Codex port, and this document does not claim a number.
**What can be said precisely: harness #3 inherits containment and bootstrap
for free, and inherits nothing else.**

## Is Hermes even the right #3?

A parallel survey scored the field against the same D1–D9 duties. Two
findings reframe the question.

**Agent Plugins v1 is now a governed standard and Anthropic is not in it.**
The specification has a steering committee including Amazon, Cursor,
Microsoft, OpenAI, Vercel and Google, with VS Code, Cursor, Copilot, Codex
and Kiro as compatible clients **[doc]**. Our packaging already targets it
because of Hermes. That bet looks better than it did, but its value no longer
depends on Hermes.

**Two candidates appear to beat Hermes on the tiering axis — but the
evidence is asymmetric and that must not be glossed.** Google Gemini CLI is
reported to have per-subagent model pinning and a deny-by-default tool
allowlist, a session-start hook whose output is injected, and MCP
`instructions` support **[doc]**. opencode is reported to discover our
existing `.claude/skills/*/SKILL.md` layout unmodified and to offer per-agent
model plus a permission whitelist **[doc]**.

The asymmetry: Hermes's weakness rests on primary evidence — a closed pull
request, two closed issues, a direct maintainer quote. The competitors'
strengths rest on documentation summaries only. Nobody has done for Gemini
CLI or opencode the install-mechanics dig that found Hermes's broken hook
registration under `hermes serve`, and that is precisely the class of finding
that only appears when you look hard. Gemini's MCP discovery timeout and
opencode's thin distribution story are the known unknowns; the unknown
unknowns are unsampled. **Treat this section as a shortlist for spiking, not
as a ranking that has been earned.**

Our SKILL.md renders port essentially unchanged to every viable candidate.
The packaging and marketplace layer is where translation work lives.

## What I recommend

**Phase A, live defects on the currently shipped product.** These are worth
doing whether or not a third harness ever exists, and each one is a present
gap, not preparation:

- Make the renderer fail loudly, not silently, when a source has harness
  blocks and none matches the target. Add a per-block or size-based
  assertion, since the current whole-file comparison cannot see an
  all-harnesses-empty block.
- Add `adapters/` to the destructive-pattern lint's scan directories. The
  Codex hook scripts ship today and are unscanned today.
- Finish WP-P4, the only unfinished work package from the Codex plan. It was
  never filed as a task.

**Phase A-prime, preparatory and honestly labelled as such.** These cost
little and are inert until a third harness exists. They are worth doing only
if the answer to ruling 2 is "yes, eventually":

- Wire `plugin_root_var`/`plugin_data_var` into `update_check.py` and
  `whats_new.py`, or delete the fields. The bug they would cause is inert
  today; the dead fields are a trap for whoever ports next.
- Delete or use `is_codex()`, `spawn_background_hint`, `spawn_depth_note`
  and `update_source`.
- Generalize the Codex hook-shell test into the parameterized conformance
  kit WP-H0c specified, and write `docs/harness-contract.md`. The one
  defensible present-tense justification is regression safety between the
  two harnesses we already ship; if that does not convince, this belongs in
  Phase B.
- Give prime a digest size budget. This one does benefit Claude Code now,
  but no shipped harness currently truncates us, so the benefit is
  robustness rather than a fix.

**Phase B, only after a ruling.** If a third harness is wanted, spike the
*candidate*, not the plan: one live install, one measurement of the injection
path, one measurement of first-`initialize` behaviour against a cold
multi-gigabyte sync, one attempt at a subagent spawn with a pinned model. On
present evidence Gemini CLI and opencode are the better first spikes, because
Hermes is the one candidate *known* to foreclose model tiering — but their
advantages are documentation-level only, so the spike is what would settle
it, not more reading.

**Hermes specifically** stays shelved, and the deferral decision
`faa1ffbf0ff6` stands. If it is ever revisited, the two things to check first
are whether the maintainer's position on scoped delegation has moved, and
whether hook registration under `hermes serve` has been fixed.

## Rulings needed before any of this becomes work

1. Do we spend Phase A now as a maintainability pass, separately from any
   harness decision? My recommendation is yes, and it is mostly small.
2. Is a third harness still wanted at all, given that parity on harness #2 is
   43 of 54 rows `partial` and the human gate is the binding constraint?
3. If yes, do we keep Hermes as the target, or re-target the spike to Gemini
   CLI or opencode on the parity-per-cost argument above?

## Known-intentional, not defects

- Hermes remaining shelved is a standing decision, not an oversight.
- Codex containment being token-only is recorded honestly — but **in
  `harness.py`'s `containment` field, surfaced through `get_status`, not in
  PARITY.md**. The Codex plan intended it to appear in PARITY.md; the
  registry schema settled into per-item rows and the statement never landed
  there. Worth fixing, and worth noting that an earlier draft of this
  document repeated the stale claim without checking it.
- CI never installing a harness is a deliberate choice; the human field gate
  is its counterpart.
