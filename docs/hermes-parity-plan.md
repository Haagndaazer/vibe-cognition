# Hermes Full-Parity Plan — multi-harness infrastructure first

Status: DRAFT rev 2 (scoping/planning only — no implementation authorized yet)
Peer review: sonnet adversarial pass 2026-09-01 — APPROVE-WITH-CHANGES; all 8
findings applied in this revision (containment honesty, H1c prerequisites, CI
gate mechanism, D5 spike, injection-budget split, contract coverage,
simulation-fidelity flags, widened estimates).
Author: Colton Dyck (session: Vince, solo)
Date: 2026-09-01
Supersedes: initial Hermes scoping report (graph ref `hermes-scoping-2026-08-31`)

## Goal

Ship **full-parity** vibe-cognition support on the Hermes Agent harness (Nous
Research), built so that **harness #3 is cheaper than harness #2**. Multi-harness
support is the product feature; Hermes is its first proof. Four institutional
pillars, per Colton's direction: good infrastructure, regression testing,
burndowns, and workflows mandating parity before new releases.

Verified scoping baseline (peer-reviewed 2026-08-31, graph nodes
`df71fe10c4a9`, `f652a6df279f`, `40005f6e39eb`, episode `d82978200b66`):

- **Near-free:** stdio MCP server (Hermes `config.yaml` / portable Agent
  Plugins v1 `mcp.json`, `${workspaceFolder}` = project root), agentskills.io
  `SKILL.md` skills (slash-invoked), dashboard.
- **Adapter-needed:** prime injection (Hermes `session:start` hooks are
  observer-only; injection only via `pre_llm_call` into the user message, 10K
  cap), compact reinjection, dependency bootstrap, server instructions (Hermes
  does not surface MCP `InitializeResult.instructions`).
- **Doesn't transfer:** per-agent tool whitelists + model pins (`delegate_task`
  has neither; one global `delegation.model`) — curation containment and
  Sonnet/Haiku tiering must be redesigned.

## Architecture principle

**Policy lives in the server; adapters stay thin.** Anything enforced or
generated server-side (containment, prime content, graph rules) is written once
and works on every harness. An adapter supplies only what a harness genuinely
owns: registration, injection transport, bootstrap, curation runner, update
channel.

## The Harness Capability Contract

A short, versioned doc (`docs/harness-contract.md`, written in WP-H0b) that
enumerates the integration duties every adapter must implement or explicitly
waive. Draft duty list:

| # | Duty | Claude Code today | Hermes target |
|---|------|-------------------|---------------|
| D1 | Register MCP server with project root + data dir | `plugin.json` `mcpServers` + `${CLAUDE_PROJECT_DIR}`/`${CLAUDE_PLUGIN_DATA}` | portable `mcp.json` / `config.yaml` + `${workspaceFolder}`; data dir via env |
| D2 | Session-start context injection (prime digest) | SessionStart hook `additionalContext` | native Python plugin, `pre_llm_call` first-turn injection |
| D3 | Post-compaction reinjection (rules + prime) | SessionStart `compact` matcher hook | same plugin, `session:compress` → reinject on next `pre_llm_call` |
| D4 | Standing-practices delivery (server instructions) | MCP initialize `instructions` | injected via D2/D3 payload (Hermes ignores MCP instructions) |
| D5 | Dependency bootstrap (uv sync, ~2–4GB cold) + venv health probe/self-heal | SessionStart hook script (incl. torch/chromadb import probe) | decided by the WP-H1a **spike** (see below); candidates: decoupled install/setup step, or self-bootstrap with lazy torch import |
| D6 | Curation runner (background, contained) | Agent tool + `agents/*.md` tool whitelists + model pins | `delegate_task` + **server-side containment** (WP-H0a) |
| D7 | Skills registration | plugin `skills/` | same dirs (agentskills.io); harness-specific text via adapter overlay |
| D8 | Update/distribution + whats-new | marketplace pin + `update_check` | `hermes plugins install owner/repo` / catalog; adapter update-check |
| D9 | Harness-migration hygiene | `migrate_mcp` (stale `.mcp.json` cleanup) | **n/a with reason** (Claude-Code-specific legacy); row exists so the parity gate covers it explicitly |

The Claude Code injection channel today also carries three side notes
(migration, update-nudge, whats-new — see `hooks/session-start.sh`). Each gets
its own parity row: several are likely `n/a` on Hermes (Claude-specific
problems), but that must be an explicit ruling, never a silent drop.

Each duty gets: an injection/behavior spec (payload shape, size budget per
harness — D2's Hermes budget is 10K chars), a conformance test, and a row in
the parity matrix.

## Phase 0 — Portability foundations (harness-agnostic; ships value even if Hermes slips)

**WP-H0a — Server-side curation containment (honest claim: structural
friction + tamper-evident audit, NOT an enforced lock).** Peer-review finding
1 (2026-09-01): MCP provides no caller identity — every client speaks to the
same stdio server over the same undifferentiated tool-call interface, so a
main session could call `cognition_begin_curation` itself and write edges with
a valid token. What the token DOES buy, on every harness: every edge write is
tied to a curation session id (bypass becomes visible in telemetry, extending
`edges_outside_curation` into per-write provenance), and edge-write calls
without a token fail with an actionable redirect to `/vibe-curate`. True
enforcement requires a capability the main session structurally lacks — on
Claude Code that is the subagent tool whitelist (which stays as the real
boundary there); **Hermes ships with weaker containment than Claude Code**,
stated plainly in PARITY.md, until Hermes grows per-agent tool scoping.
Acceptance: token-less edge writes rejected with actionable error; per-write
curation-session provenance recorded; existing Claude pipeline green;
`edges_outside_curation` retained.

**WP-H0b — Repo restructure + Harness Capability Contract.** Adopt Agent
Plugins v1 portable core (`plugin.json` + `skills/` + `mcp.json`) with
`adapters/claude-code/` (current hooks/, agents/, hooks.json move here;
release packaging keeps them where Claude Code expects) and `adapters/hermes/`
(empty scaffold + contract doc). Core code reads harness-neutral env
(`REPO_PATH`, `VIBE_DATA_DIR` — already true). Acceptance: Claude Code build
byte-for-byte equivalent behavior; contract doc reviewed.

**WP-H0c — Parity matrix + conformance kit + CI gate.**
- `parity.yaml`: features × harnesses, status ∈ {full, partial, waived, n/a}
  with mandatory notes for partial/waived; generates `PARITY.md`.
- **Adapter conformance suite**: pytest, parameterized per adapter, exercising
  each contract duty against a *simulated* harness environment (launch the
  server exactly as that harness would — env, cwd, transport; validate D2/D3
  payload shape and size budget; verify D6 token flow). Deliberately does NOT
  require installing the real harness in CI (Hermes churn isolation); real-
  harness verification is a human field gate per the standing install-mechanics
  constraint. **Simulation-fidelity-critical duties, verified FIRST on the
  human gate:** D3 (does `session:compress` fire when/how assumed) and D5
  (real MCP connect-timeout behavior) — the two places a simulated harness can
  trivially diverge from the real one.
- **CI parity gate — completeness model, not touch-detection** (peer-review
  finding 3: detecting "features touched by a release" from diffs is
  undecidable — behavior changes via shared helpers with no diff to the
  feature's own file). Instead: a registry is *generated* from the server's
  tool definitions + `skills/*/SKILL.md` + hook entries, and CI fails whenever
  any registry item lacks a `parity.yaml` row for every supported harness
  (implemented or waived-with-reason). A static list-diff lint (registry keys
  vs. `parity.yaml` keys) forces new tools/skills/hooks to add their rows in
  the same PR. No silent Claude-only features.

**WP-H0d — Skill de-Claude-ing.** Factor harness-specific invocation text
(Agent tool, `subagent_type`, model names, `/plugin update` CTAs) out of shared
`SKILL.md` bodies into per-adapter overlay sections/files. Conformance check:
lint that greps shared skill text for harness-specific tokens.

## Phase 1 — Hermes adapter (full parity)

**WP-H1a — Packaging, registration, bootstrap (D1, D5, D8 skeleton).**
Portable `mcp.json` + `config.yaml` recipe + (stretch) Nous catalog manifest
PR. **D5 opens with a go/no-go SPIKE, not a design assumption** (peer-review
finding 4): measure Hermes's actual MCP `initialize` timeout — stdio clients
typically give up in seconds, while a cold 2–4GB torch download needs 8+
minutes, so first-connect self-bootstrap may just look like a broken server
and trigger install-retry loops. Candidates, chosen by spike result: (a) a
decoupled synchronous setup step (`hermes plugins install` hook / documented
one-time command) with the MCP connect path never paying the download; (b)
self-bootstrap with torch lazily imported only when embedding features are
invoked (server answers initialize fast, degrades gracefully until deps
arrive). Whatever ships also carries the venv health probe/self-heal semantics
from `session-start.sh` (D5 row). Pin a tested Hermes version range.

**WP-H1b — Injection plugin (D2, D3) + D4 rides skills, not injection.**
Native Hermes Python plugin (`plugin.yaml` + `register(ctx)`): `pre_llm_call`
injects on first turn and after a `session:compress` event. Peer-review
finding 5: the 10K cap is shared by everything we'd inject, so **D4's
largely-static standing practices move OUT of the per-turn injection path into
a skill** (loaded once, near-free), reserving the 10K budget for the dynamic
prime digest alone (prime gains a size-budget parameter server-side, which
benefits Claude too). The Claude-side side notes (migration/update/whats-new)
get explicit per-row rulings under D8/D9 — several are likely n/a on Hermes.
Decision (recorded): plugin over MemoryProvider — the single-active-provider
rule would collide with users' Mem0/Honcho/etc.; revisit if Nous relaxes it.

**WP-H1c — Curation on Hermes (D6).** `/vibe-curate` Hermes variant drives
`delegate_task`; containment via WP-H0a token; orchestrator/analyzer prompts
get a Hermes overlay (spawn-discipline text rewritten — no `name`/model
params exist there). Model tiering: not reproducible (global
`delegation.model`) — ship as **waived** in `parity.yaml` with the cost note,
revisit when per-agent profiles land upstream (Hermes FRs #9459/#35409).

**WP-H1d — Update/whats-new for Hermes (D8).** Adapter update-check against
GitHub releases; whats-new keyed off installed version as today.

**Gate H1 — human field verification** on a real Hermes install (Windows +
one POSIX), per the install-mechanics constraint: releases gate on a human's
machine; checklist includes the empty-project-root case.

## Phase 2 — Institutionalization (parity as standing process)

- **Release workflow update** (supersession of the "vibe-cognition plugin
  release procedure" workflow node, done at first Hermes release): version
  bump requires CI parity gate green + `parity.yaml` diff reviewed + harness
  burndowns clean or explicitly waived.
- **Burndowns as cognition epics**: one epic per harness ("EPIC: Hermes
  parity"), child tasks per contract duty; standing rule — any new
  feature/tool files parity child-tasks for every supported harness at design
  time, not after.
- **`docs/HARNESSES.md`**: the "add a harness" checklist derived from the
  contract + conformance suite — the artifact that makes harness #3 cheaper.
- **Recurring audit**: extend the existing tool-surface self-sufficiency audit
  workflow to include "parity.yaml row present for every harness".

## Sequencing & effort

H0a → H0b → (H0c ∥ H0d) → H1a(spike first) → (H1b ∥ H1c) → H1d → Gate H1 →
Phase 2. Real-edge check (corrected by peer-review finding 2): **H1c has three
hard prerequisites — H0a (token flow), H0c schema (the tiering waiver must
have a `parity.yaml` to land in), and H0d (the overlay mechanism its rewritten
spawn-discipline text ships through).** Only H0c's conformance-suite build
(the slow part) may overlap Phase 1; its schema lands early. Rough effort,
widened per finding 8 for a first-ever native Hermes plugin against a young,
fast-moving harness: Phase 0 ≈ 2 wk, Phase 1 ≈ 2–3 wk, Phase 2 small and
partly concurrent. Estimates carry an explicit churn caveat (risk 1).

## Risks / open questions

1. **Hermes churn** (young project): version-pin the adapter; conformance suite
   simulates the harness so CI never installs it; human gate catches drift.
2. **Tiering waiver**: Haiku-analyzer economics don't exist on Hermes today —
   waiver policy must be honest in PARITY.md (cost, not capability).
3. **10K injection cap**: prime must degrade gracefully; size-budget work is
   core, benefits Claude too.
4. **Conformance-vs-reality gap**: a simulated harness can drift from the real
   one — mitigated by the per-release human field gate and pinned versions.
5. **D5 outcome may change first-session behavior on Claude too** (lazy torch
   import / self-heal semantics) — needs its own careful WP brief either way
   (interacts with the open stale-first-session task); the WP-H1a spike is the
   gate before any design commitment.
7. **Containment asymmetry**: until Hermes grows per-agent tool scoping,
   Hermes users get friction+audit containment while Claude users get the
   subagent whitelist — PARITY.md must state this plainly (see WP-H0a).
6. **Windows**: Hermes-on-Windows behavior unverified; our venv-lock scars may
   recur in new forms.
