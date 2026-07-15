# Team-Cognition Survey — Multi-Tier Team Use of the Shared Graph

**Date:** 2026-07-14
**Epic:** `cedf4a8457e9` (team-cognition)
**Status:** Survey / brainstorm / scoping ONLY — nothing here is implemented or approved for implementation.
**Produced by:** Vince (manager). Code-surface catalog by an Explore subagent (Haiku); validity reviews by two general-purpose subagents (Sonnet) — one adopting a human-team-lead persona, one adopting the LLM-plugin-consumer persona. Plan peer-reviewed before execution.

## 1. Question

Colton's ask: how can a **team** — especially a multi-tiered hierarchy (humans of different seniority + manager agents + subordinate agents) — use vibe-cognition while **maintaining hierarchy and cohesion**, with everyone contributing to and reading from one shared graph?

Three just-filed backlog tasks seeded this epic:

| Task | Summary |
|---|---|
| `888a21f729dd` | Conflicting-decision warning on cognition_search results |
| `f746c5f9361e` | Weight human seniority in ranking/trust of nodes |
| `5222c93ca8f5` | Option to filter out nodes from certain people |

## 2. Current surface — what exists today

### 2.1 Identity & provenance (the trust model)

Shipped in WP-P13n (v0.17.0/v0.18.0). Two trust classes, deliberately asymmetric:

**Server-resolved (authoritative, unforgeable by callers):**
- `metadata.recorded_by` — stamped on every node write (`cognition_tools.py:248`), from `resolve_git_identity()` (git config file reads only, no subprocess, never raises).
- `metadata.created_by` — stamped on task creation (`cognition_tools.py:1213`); no client parameter exists.
- `metadata.claimed_by` — stamped on task claim (status→`in_progress`; re-claim re-stamps).
- `metadata.transitions[].by` — every task status change appends `{status, at, by, note?}` (append-only audit trail).

**Client-supplied (attributive, display-only, never matched):**
- `author` (entities/documents) — free text, "who dictated the record".
- `owner` (tasks) — free text, "who's on it".

**Email is the ONLY identity match key** (casefolded). Known, documented limit: `recorded_by` means "recorded on a machine with your git identity" — an agent recording on Colton's machine stamps Colton; a manager recording a teammate's relayed fact stamps the manager. Open task `73f750d8d528` (attribution honesty) tracks this.

### 2.2 Personalization (read side)

The session-start prime auto-detects multi-user graphs (>1 distinct non-empty stamped email) and renders per-user sections (Your Open Tasks, Team Critical, Your Recent Activity) matched strictly by email. Constraints, workflows, incidents, documents, patterns, decisions stay **global by design**. The prime is journal-read-only and light-import (no torch/chromadb) — a hard constraint from the Wedge/Sidecar program.

### 2.3 Tasks

Parent hierarchy (cycle-guarded), 5-state machine, claiming, transition audit log, free-text owner filter on `cognition_list_tasks`. No assignment mechanism beyond free-text `owner`; no claim-collision handling (re-claim silently re-stamps).

### 2.4 Edges & conflict machinery

Six edge types. Deterministic `part_of`/`relates_to` from shared references at record time; **all semantic edges come only from the background curate-orchestrator** (documented contract, not code-enforced — no tool checks caller identity). SUPERSEDES has cycle + shape validation. `contradicts` has **no special handling anywhere** and no producer that actively hunts for contradictions.

> Graph as of this survey: 817 edges — **1** `contradicts`, 13 `supersedes`. Conflict data is essentially absent.

One relevant write-time precedent: recording an episode citing a reference another episode already cites returns `possible_duplicate_of` — propose-only, never blocks.

### 2.5 Team distribution layer (already solved)

`docs/topology-guide.md` covers how a team shares the journal: shared-checkout protocol, separate clones with `merge=union`, out-of-order replay defense, episode duplicate detection. **Distribution exists; team semantics do not.**

### 2.6 Explicitly absent (per code survey)

No roles, no permissions, no seniority, no approval/draft states, no conflict detection or warnings, no per-user read preferences (all config is global env vars; no per-user files anywhere in `.cognition/`), no assignment with provenance, no change notifications, no claim-collision handling, no enforcement of curation containment.

## 3. Grounding constraints from recorded history

Prior decisions this scoping must respect (from the WP-P13n brief, `doc:aa047b0b0e0a`, and graph nodes):

1. **Client-passed identity: REJECTED** (reintroduces the trust gap server-resolution closed).
2. **Per-user config file mapping agent names → humans: REJECTED** (manual bookkeeping that rots).
3. **Per-user filtering of constraints/incidents: REJECTED** (actively harmful — they bind everyone).
4. **Embeddings/heavy imports in the prime path: REJECTED** (hook timeout; Wedge/Sidecar). Semantic work belongs in the MCP server, where embeddings already live.
5. **Curation containment** (constraint `f6ab87cb77a8`): only the curate-orchestrator writes semantic edges.
6. Local trust domain: every writer runs on a machine with some human's git identity. No malicious adversary in scope; **accidental misuse is in scope** (agent overreach, stale claims, silent muting).

## 4. Suggestions with dual-POV validity assessment

Each suggestion was independently assessed from two personas: **[H]** a human team lead running a mixed human/agent team, and **[L]** the LLM agent consuming the plugin (context budget, tool-contract self-sufficiency, warning fatigue, misuse affordances).

### S1 — Team roster / person registry
Durable mapping email → {display name, kind: human|agent, seniority or per-area authority, role}. Foundation for S2/S11.

- **[H] valid-with-changes.** Needed — "who is Vince, is he senior, is he even human" is answered manually today. But a committed `.cognition/team` file is the same shape as the already-rejected rot-prone mapping file. A first-class **`person` node type** is the right form: searchable, supersession-versioned (a promotion is a superseding node with a `recorded_by` trail), curated like everything else.
- **[L] valid-with-changes.** Same conclusion, different reason: a config file fails the tool-surface self-sufficiency audit (nothing tells the agent the file exists); person nodes reach the agent through the same channels as every other fact. Warns that a **stale tier is worse than no tier — it fabricates authority**; who maintains tiers is an open design question.
- **Synthesis: adopt the person-node shape, explicitly reject the file shape.** Seniority must be a signal, never an ACL.

### S2 — Seniority weighting *(task `f746c5f9361e`)*
Apply roster seniority where provenance flows: conflict tie-breaks, search-result annotation, prime selection.

- **[H] valid-with-changes.** Annotation ("decided by <senior human>") yes; **silent score re-ranking is a hard no** — two people asking the same question would get different answers with no trace why. Seniority ≠ correctness: a senior's stale decision must not outrank a junior's better-reasoned recent one. Advisory label only.
- **[L] valid-with-changes, kill the silent-reranking branch.** Staleness/politics in seniority data would steer the agent wrong invisibly. Must be hard-blocked from ever touching constraints/incidents — org chart does not outrank a constraint.
- **Synthesis: annotation-only survives; silent re-ranking is killed by both POVs.** Scope the task accordingly.

### S3 — `recorded_via` second stamp *(relates to task `73f750d8d528`)*
Optional env/client-supplied agent name alongside `recorded_by`, explicitly display-only (same trust class as `author`): distinguishes "Vince recorded this on Colton's machine" from "Colton recorded this".

- **[H] valid.** Cheap; directly closes a felt attribution gap. Must render consistently everywhere `recorded_by` appears or people treat it as verified by association.
- **[L] valid.** Solves the conflation without touching the trust-bearing field. The two stamps will sit side by side in output — the trust asymmetry must be impossible to miss (naming like `recorded_via_unverified`, or an explicit docstring note).
- **Synthesis: valid from both POVs; the whole design burden is honest labeling.**

### S4 — Read-time conflict annotation *(task `888a21f729dd`)*
Search results (and prime lines) carry a warning when a hit has a `contradicts` edge or is a non-HEAD member of a supersedes chain — naming the conflicting node and its `recorded_by`. Deterministic graph lookups only.

- **[H] valid.** Exactly the "trust what the graph tells you" feature: the conflict appears at the moment of acting on the info. Useless without S6 — on day one it would surface almost nothing and read as broken.
- **[L] valid — high priority.** The suggestion that most changes agent behavior: today a superseded/contradicted node arrives from search with zero signal and gets acted on as current. **Shipping S4 without S6 trains false confidence** ("no conflicts found" ≠ "conflicts not yet detected").
- **Synthesis: valid, but strictly paired with S6.**

### S5 — Write-time conflict candidate detection *(task `888a21f729dd`)*
On recording a decision (maybe constraint/pattern), semantic-search same-type nodes and return `possible_conflict_with: [ids]` — mirrors `possible_duplicate_of`; propose-only, never blocks; runs in the MCP server, never in prime.

- **[H] valid.** Catches the clash when it's cheapest to resolve. Noise risk: over-eager thresholds get trained out like ignored linters — quiet by default, loud above confidence.
- **[L] valid.** Reuses a contract shape the agent already knows — valuable in itself for surface consistency. Tune against the existing duplicate-detection threshold rather than inventing a new one.
- **Synthesis: valid both POVs; precision tuning is the only real risk.**

### S6 — Curation conflict lens *(task `888a21f729dd`)*
Extend the curate-orchestrator pipeline with an explicit contradiction-hunting pass so `contradicts`/`supersedes` edges actually get produced.

- **[H] valid — the structural prerequisite.** 817 edges / 1 contradicts means the pipeline does not do this today. Everything downstream (S4, S2's annotation value) is inert without it. Background job, so tuning pain is acceptable and correctable.
- **[L] valid — necessary infrastructure.** Zero prime cost. Caution: **a bad heuristic here poisons a trusted signal** — false `contradicts` edges between differently-worded-but-compatible decisions would be trusted because they're curator-authored. Needs a real precision bar, not just "more than 1 edge".
- **Synthesis: highest-leverage item in the set; precision bar is the acceptance criterion.**

### S7 — Per-call author filters on read tools *(task `5222c93ca8f5`)*
`authors`/`exclude_authors` email params on `cognition_search` + `cognition_list_tasks`; constraints/incidents never filtered out.

- **[H] valid.** "Filtering with your eyes open" — explicit, per-call, nothing persisted, nothing to rot. Genuinely different from the rejected persistent filtering. Social-rot caveat: a copy-pasted `exclude_authors` habit becomes de facto permanent filtering; docs callout needed.
- **[L] questionable.** ⚠️ **The one real disagreement in the set.** From the agent's seat this is "a tool shaped like an excuse": nothing stops an agent under completion pressure from excluding the one reviewer whose past decision is inconvenient, then honestly reporting "search found no conflicts". Conditions to ship: (a) every filtered call returns a non-zero **"N results excluded by filter"** count, so an exhaustive-search claim can never be made silently; (b) consider restricting eligible targets to **agent-kind identities** (post-S1) — de-duping bot noise, not muting humans.
- **Synthesis: keep, but adopt the LLM POV's disclosure requirement as non-negotiable; the agent-kind-only restriction is a design decision for Colton.**

### S8 — Persistent per-user mute preference (env var)
`COGNITION_MUTE_AUTHORS=email,email` applying S7 defaults per machine.

- **[H] kill.** Durable + invisible muting stacks the two worst failure modes; corrosive to cohesion (manager silently mutes a junior for six months; decisions get made in ignorance). The rejected config-file mapping "wearing an env-var disguise".
- **[L] kill.** The agent has no way to detect the filter is active; results silently diverge across machines and sessions. Even per-call disclosure doesn't rescue it, because a persistent default's whole purpose is to disappear from view — that IS the failure mode.
- **Synthesis: KILLED — unanimous.** S7 covers the legitimate use case. Recorded here so it isn't re-proposed later.

### S9 — Soft enforcement/observability for reserved curation tools
Curation containment is docs-only today. Options: launch-token handshake, or minimal `get_status` surfacing of "edge writes outside a curation run".

- **[H] valid-with-changes.** Minimal observability version only. The token handshake solves an adversarial threat model that's explicitly out of scope, and the token is itself a config artifact that rots.
- **[L] valid.** The minimal version directly targets the in-scope accidental case (an agent calling `add_edge` when it shouldn't). Ship minimal; escalate only if it proves insufficient.
- **Synthesis: minimal observability version approved by both; token handshake dropped.**

### S10 — Claim-collision warning + takeover etiquette
Claiming a task already claimed by someone else returns a warning naming claimant + claim age (never blocks); takeover requires a transition note.

- **[H] valid.** "Two agents grabbing the same task is not hypothetical, it's Tuesday." Go further: the done→reopen case should not be optional — silently reopening someone's closed work implies their "done" was wrong and is at least as disruptive.
- **[L] valid — high priority.** Gives exactly the information needed to decide back-off / message via teammate-comms / take over with a note. Propose-only pattern, consistent with the rest of the system. Ship the reopen case together.
- **Synthesis: valid both POVs; promote done→reopen warning from optional to included.**

### S11 — Role-aware prime sections
With S1: manager prime gets a team rollup (in_progress by claimant, stale claims, blocked); subordinate prime gets own claims + manager's recent decisions. Journal-scan only.

- **[H] valid-with-changes.** "What I want every morning" — but only after S1 lands in the person-node form, and with a "best effort, not authoritative" framing (matrixed orgs break the single-manager assumption; document as a known limit).
- **[L] valid-with-changes.** Deeper issue: **role source-of-truth conflict.** Agents already resolve their role from session designation → teammate-comms roster → cognition graph, in that order. If person nodes also carry "role", two sources can disagree. Fix direction: cognition should *accept* role as an input resolved upstream, not own/duplicate it — or the roster must be explicitly subordinate to teammate-comms for agent roles.
- **Synthesis: valid but strictly sequenced after S1, with the role-ownership question resolved first. Human seniority (S1/S2) and agent role (teammate-comms) may deserve different owners.**

### S12 — "Since you were gone" digest
Machine-local, uncommitted last-seen marker → prime section listing teammates' new decisions/constraints/incidents since your last session.

- **[H] valid.** A catch-up mechanism that *fights* silent-hiding rather than contributing to it. Needs a sane first-run default (bounded lookback or labeled first-run message — not a full-graph dump, not a silent no-op).
- **[L] valid-with-changes.** Delta-only framing may actually *reduce* prime cost. Must be keyed **per identity, not per machine** (manager + subordinate sharing a machine would stomp each other's marker — reuse the existing email key), and must handle ephemeral sandboxes/fresh worktrees (no local state) with a capped lookback.
- **Synthesis: valid; per-email keying and first-run fallback are the two acceptance criteria.**

## 5. Gaps the reviewers found that the suggestion set missed

- **M1 — Severity-gated push for new constraints/incidents** *(human POV)*: S12 is pull-based; a binding constraint recorded while a teammate is away for two weeks reaches them only when/if they look. A lightweight push (e.g. Slack) for constraint/incident nodes only, distinct from the digest.
- **M2 — Explicit assignment with provenance** *(human POV)*: there is server-stamped `claimed_by` (self-claim) but nothing for a manager *assigning* work — `owner` is free text. A server-stamped `assigned_by`/`assigned_to` would let a subordinate verify an assignment actually came from the manager. Directly serves hierarchy maintenance.
- **M3 — Constraint/incident acknowledgment** *(human POV)*: no way to confirm the team actually *registered* a new binding constraint (vs it merely existing in the graph). A lightweight ack tied to constraint/incident nodes closes the loop on "juniors/agents not relitigating settled decisions".
- **M4 — Result-completeness disclosure on search** *(LLM POV)*: more basic than any author filtering — plain top-k truncation already means "no conflicts found" could mean "more matches past the cutoff". A "returned N of M matches" contract on `cognition_search` should sit alongside S4/S5. Also the natural home for S7's "N excluded by filter" disclosure.

## 6. Cross-reference: suggestions ↔ tracked tasks

| Suggestion | Tracking |
|---|---|
| S4, S5, S6 | `888a21f729dd` (conflict warning) — scope now spans read-time + write-time + curation lens |
| S1, S2 | `f746c5f9361e` (seniority) — narrowed: annotation-only, person-node roster |
| S7 | `5222c93ca8f5` (per-author filter) — narrowed: per-call only, disclosure required; persistent variant (S8) killed |
| S3 | existing task `73f750d8d528` (attribution honesty) — recommend re-parenting under epic `cedf4a8457e9` (Colton's call) |
| S9, S10, S11, S12, M1–M4 | **not yet tracked** — file as new tasks under the epic if approved |

## 7. Recommended v1 slice (scoping opinion, not a plan)

Both POVs' priority lists overlap almost perfectly. The v1 slice that follows from them:

1. **S6 + S4 — the conflict pipeline** (curation lens producing `contradicts`/`supersedes`, read-time annotation surfacing them). Unanimous #1; S4 alone is hollow, S6 alone is invisible.
2. **S10 — claim-collision + reopen warnings.** No dependencies, deterministic, solves a today-pain of multi-agent teams.
3. **S3 — `recorded_via` stamp.** Cheap, closes a named known limit, and starts producing the agent-vs-human data that S7's agent-kind filtering and S1's person nodes would later want.
4. **S5 — `possible_conflict_with`** as a fast-follow once S6's precision threshold is tuned (shared threshold).

**Foundation track (behind v1):** S1 person nodes → then S2 annotation and S11 role-aware prime (after resolving role ownership vs teammate-comms). **Independent nice-to-have:** S12. **Contract hygiene:** M4 alongside S4/S5. **Killed:** S8, S2's silent re-ranking, S9's token handshake.

## 8. Open questions for Colton

> **Answered — see §9.** Q2 (role ownership) and Q3 (seniority model direction) are resolved by Colton's rulings below; Q1, Q4, Q5 remain open.

1. S7: should author-exclusion be restricted to agent-kind identities (LLM POV's position), or allowed for any author with mandatory disclosure (human POV's position)?
2. S1/S11: who owns "role" — teammate-comms (agents) with person nodes carrying only human seniority, or the graph for both? The LLM review flags a real two-sources-of-truth hazard.
3. Seniority model in S1: global tier vs per-area authority (the original task's open question — per-area maps better onto the existing `authority` concept in teammate-comms profiles).
4. Re-parent `73f750d8d528` (attribution honesty) under the team-cognition epic?
5. M1–M4: file as tasks under the epic now, or hold until v1 slice is approved?

## 9. Addendum — Colton's rulings (2026-07-14, same day)

Colton reviewed the survey and ruled on several items; the review verdicts above are preserved as history, these rulings govern. Overall: the remaining suggestions are approved in spirit ("otherwise I think your suggestions are great"). Recorded as decisions `6be2e867f91e`, `3c16d91417cb`, `b8a30a61e712`.

### 9.1 Agent identity never enters the graph (revises S3)

The `recorded_via` agent-name stamp is **rejected**. Instead: every node keeps the human identity stamp (`recorded_by`, as today) plus a **boolean** — did this record come via an agent, or directly from the human? That bool is a weighting input: **human-origin input always outweighs agent-origin input** — weighting, never erasure.

- Resolves the S11/L-review "two sources of truth" hazard outright: the cognition graph models *humans only*; agent identity and roles live exclusively in teammate-comms.
- Design notes for the WP brief: the bool is client-declared (server can't verify origin; trust-based, explicitly accepted — same trust class as `author`); default-when-undeclared and declaration mechanism are open.

### 9.2 Person nodes + seniority weighting in retrieval (revises S1/S2, answers Q2 & Q3)

Human identity nodes are approved: name, role, seniority, **reports-to (direct manager)** — patterned after teammate-comms profiles but fully separate from that plugin. Two deliberate deviations from the survey's synthesis:

1. **Updated in place** to reflect a person's current role — *not* supersession-versioned (rejects §4-S1's supersession shape for people).
2. **Seniority IS weighed in retrieval** — not annotation-only (overrides the unanimous review position). Colton's rationale: hierarchy is itself part of the project's cognitive story. A junior may genuinely find a better solution than the senior's earlier one; when the senior later revises it again, the seniority context is what makes that revision legible — *why* it changed. Weighting + conflict edges paint a fuller picture **without stomping anything**. Better context is the whole goal.

Hard requirements carried from the reviews into this ruling: weighting never wipes or hides lower-seniority findings ("weighted, never wiped"); weights are **visible in results, never silently applied** (this is the answer to the L-review's silent-reranking objection — visibility, not abandonment); constraints/incidents are never outranked by seniority.

### 9.3 New-user onboarding (net-new)

When a git identity unknown to the person registry first touches vibe-cognition, auto-detect it and prompt the user — via Claude Code — to introduce themselves: name, role, seniority, and who they directly report to. Trust-based (no verification). The introduction creates their person node + reports-to link. Mechanism constraint: the prime can't prompt interactively (light-import) — likely a prime notice directing the agent to run the onboarding conversation, or a dedicated tool call.

### 9.4 Updated tracking

| Item | Tracking |
|---|---|
| Person nodes (9.2) | new task `1c29cff92e20` (high, under epic) |
| Onboarding (9.3) | new task `62d8264bdb1d` (under epic) |
| Seniority weighting (9.2) | `f746c5f9361e` detail updated to approved direction |
| Agent-origin bool (9.1) | folded into epic direction; relates to `73f750d8d528` |

### 9.5 Round-2 rulings (Colton, 2026-07-14, recorded as decision `4a8078a25ceb`)

- **Q1 answered — per-author filter (S7/`5222c93ca8f5`):** optional parameter, **default none**; the LLM never adds it on its own initiative — only when the user explicitly asks. No restriction on targets; excluded-count disclosure stands; constraints/incidents never filterable.
- **Q4 answered — attribution-honesty task `73f750d8d528`:** stays OUT of the epic (the agent-origin bool covers its forward-looking half anyway).
- **Q5 answered in part — reviewer gaps:** M1 push notifications **rejected permanently** ("won't ever be a thing"); M2 stamped assignment **approved** → task `fc2c8b522ed8`. M3 (constraint acks) and M4 (search completeness disclosure) remain unruled — carried into the battle plan.
- Remaining suggestions approved in spirit ("otherwise I think your suggestions are great").
- **Dashboard addition:** the epic also now includes the dashboard redesign (task `30fabf12c81b`, design in `docs/260714-dashboard-redesign.md` + mockup) — PM-style views, constellation demoted, read-only v1.
