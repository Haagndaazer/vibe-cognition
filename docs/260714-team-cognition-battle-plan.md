# Team-Cognition Epic — Battle Plan

**Date:** 2026-07-14
**Epic:** `cedf4a8457e9`
**Status:** PROPOSED — awaiting Colton's approval. No WP starts before it.
**Basis:** `docs/260714-team-cognition-survey.md` (incl. §9 rulings), `docs/260714-dashboard-redesign.md`. Peer-reviewed (Sonnet subagent); 13 review changes incorporated.
**Roles:** Vince plans/briefs/gates/releases; Vorpid implements; Loki pins; Colton rules and hands-on-checks.

## 1. Goal restated

Multi-tiered teams — humans of differing seniority plus their agents — share one graph while maintaining hierarchy and cohesion: conflicts surface at read and write time, claims and assignments are visible and verifiable, human input outweighs agent input, seniority shapes the story without ever erasing anyone, and the tool surface stays self-explanatory to an LLM.

## 2. Work breakdown

Two parallel tracks. Track A (graph semantics) is the critical path; Track B (dashboard) runs alongside because V1/V2 need nothing from Track A.

### Track A — graph semantics

**Wave 1 — identity foundation (FIRST, per Colton's ruling `0027f40e392d`: identity is the core the whole epic stands on):**
| WP | Content | Notes |
|---|---|---|
| TC5 | Person node type: name/role/seniority/reports-to; in-place update with audit trail; email-keyed; new MCP tools (create/update/get) | **Owns the README "team semantics" section** (acceptance line: README documents person nodes, weighting model, known limits — per the P13n documented-limits mandate). TC5 ∥ TC6 — no interdependency, parallelizable |
| TC6 | Agent-origin bool on all node writes; displayed wherever provenance shows | Client-declared; **default RULED: true ("via agent") when undeclared** — human-origin is the deliberate assertion, so weighting can't be accidentally inflated |
| TC7 | New-user onboarding: detect unknown email → introduce-yourself flow → person node + reports-to | Depends TC5. **Trigger RULED: from the prime** — prime detects the missing person node (journal-scan, light-import) and injects a notice directing the agent to run the conversation. Brief sub-question: solo-project decline/snooze path |
| TC8 | Stamped assignment: `assigned_to`/`assigned_by` + audit; prime "Your Open Tasks" matches `assigned_to` | Composes with TC4 collision warnings (TC4 ships Wave 2; TC8's warning surface follows it if needed) |

**Wave 2 — conflict pipeline:**
| WP | Content | Notes |
|---|---|---|
| TC1 | Curation conflict lens: contradiction/supersession-hunting pass in the curate pipeline | Agent-file-only change. Acceptance: precision bar measured on a labeled sample BEFORE ship — false `contradicts` edges poison a trusted signal |
| TC2 | Read-time conflict annotation on `cognition_search` + prime (deterministic edge lookups; prime part stays light-import) | Ships AFTER TC1 has produced real edges, else it trains false confidence |
| TC3 | Write-time `possible_conflict_with` on decision/constraint/pattern record | Mirrors `possible_duplicate_of`; propose-only; threshold shared with duplicate detection |
| TC4 | Claim-collision + reopen warnings on `cognition_update_task` | Never blocks; names claimant + claim age; takeover requires a note |

**Wave 3 — retrieval semantics:**
| WP | Content | Notes |
|---|---|---|
| TC9 | Seniority + origin weighting in retrieval: visible weight labels, never silent, never wipes, constraints/incidents exempt | Depends TC5 + TC6 (both Wave 1 — TC9 can start as soon as Wave 1 gates, in parallel with Wave 2). Acceptance MUST include explicit visible fallback for pre-TC6/pre-TC5 nodes (no bool, no person node) — mirror the dashboard's "unverified" treatment, don't improvise |
| TC16 | Role-aware prime sections: manager rollup (in-progress by claimant, stale, blocked); subordinate view (own claims + manager's recent decisions) | Depends TC5 (reports-to). Role-ownership question already resolved: graph = humans only. Journal-scan only, light-import |

**Floating smalls (slot into gaps at Vorpid's pace, no wave dependency):**
| WP | Content |
|---|---|
| TC10 | Per-author filter param (default none, user-invoked-only per docstring, excluded-count disclosure) — fully ruled, zero dependencies; bundles M4 "returned N of M" IF Colton approves M4 |
| TC14 | "Since you were gone" digest (per-email local marker, capped first-run lookback) |
| TC15 | Curation-containment observability in `get_status` (edge writes outside curation runs) |

### Track B — dashboard (task `30fabf12c81b`, design already done)

| WP | Content | Timing |
|---|---|---|
| TC11 | Dashboard V1: nav rail, Overview, Board, detail drawer, `/api/tasks`; threadpool risk `4163f54f2848` resolved-or-accepted here | Parallel with Waves 1–2 — needs nothing from Track A |
| TC12 | Dashboard V2: Workflows, Documents (freshness + cited-by), Activity | After V1 |
| TC13 | Dashboard V3: person chips, seniority badges, agent-origin badge, conflict banners | Gated behind TC5/TC6/TC9 — the only dashboard phase that needs Track A |

## 3. Audit gates

Gate A is standing practice; Gates B, C, D are what this epic adds (Colton's explicit requirement: consumer auditing, stability auditing, tool-surface coherence).

- **Gate A (per-WP, existing):** Vince gates at pinned commit in an isolated worktree — `uv run python -m pytest` + `uv run ruff check .`, import-provenance check. Unchanged.
- **Gate B — tool-surface coherence:** the standing self-sufficiency workflow (`67751ebc39bd`) re-runs at every WP that adds/changes a tool **including return-shape changes** (TC2/3/4/5/6/7/8/9/10/15 qualify; TC1 and dashboard endpoints don't — no MCP surface). PLUS one final holistic pass at epic end: a fresh-eyes LLM subagent, given ONLY the pushed server instructions and tool docstrings (no repo docs, no CLAUDE.md), must correctly explain how the plugin is used on a team. Failures become doc-fix WPs. Includes SERVER_INSTRUCTIONS / SKILL.md / README drift check.
- **Gate C — stability audit** (two instances): **C1 after Wave 1 (identity)** — journal compatibility (old journals load with new metadata; additive-only; cross-version readers ignore unknown keys), re-embed cost of person/metadata writes, onboarding-path behavior, multi-process convergence unaffected, chromadb flake watch. **C2 after Wave 3** — search latency with weighting, multi-user prime size/latency (weighting doesn't exist until TC9, so C1 can't test it). *Authorship split:* any needed measurement harness is an implementer WP Vorpid builds under a Vince brief (manager writes no code); Vince gates the results. *Remediation clause:* a Gate C failure blocks the next wave — it becomes a fix WP (or an explicit Colton-approved acceptance) before new feature WPs ship.
- **Gate D — dual-persona consumer audit (epic close):** subagents simulate both personas (human team lead; LLM agent) executing the epic's core scenarios **against an isolated throwaway test graph — never the production graph** (a scripted multi-user conflict/claim exercise on the live shared journal is exactly the corruption class topology-guide.md documents). Scenarios: (1) junior/agent tries to relitigate a settled decision → warned; (2) conflicting decision recorded → surfaced at write AND read; (3) claim collision → visible; (4) the seniority story arc (junior's better find, senior's later revision) → retrievable as narrative with visible weights; (5) fresh user onboarded → person node + personalized prime. Pass/fail per scenario; failures become tasks; **the epic does not close until all scenarios pass.**

## 4. Release & batching policy

~15 releasable units would mean ~15 Loki pings. Policy: **batch adjacent small WPs into one version bump** where they gate together cleanly (e.g. TC1+TC2 once TC2 is ready; TC14+TC15; TC3+TC4) — target roughly one release per wave-chunk, each following the standard procedure (bump both version files → gate → push → Loki pin). Install-mechanics hands-on check: **expected N/A for this epic** (no new deps or plugin.json launch changes anticipated) — if any WP surprises us there, it triggers the human-gated release checklist explicitly rather than by boilerplate.

Journal discipline throughout: shared-checkout protocol per `docs/topology-guide.md` — nobody commits the journal on a WP branch; Vince flushes via temp worktree onto main and pushes before any merge.

## 5. Needs Colton's ruling (before or at first brief)

1. ~~M4~~ **RULED: APPROVED** into TC10 — "returned N of M" on every search; "just good communication."
2. ~~M3~~ **RULED: REJECTED** — constraint acknowledgment dropped permanently ("really messy and complicated"); do not re-propose.
3. ~~TC7 onboarding trigger~~ **RULED:** from the prime (decision `8f3612ad9583`).
4. ~~TC6 default~~ **RULED:** default true, "via agent" (decision `8f3612ad9583`).
5. Battle-plan approval overall + go-ahead to file TC-numbered tasks for the not-yet-filed items (TC1–4/10/14/15/16 and the Gate WPs). *(Dashboard redesign direction already approved on the mockup.)*

## 6. Sequencing rationale (one paragraph)

**Identity foundation first — ruled by Colton (decision `0027f40e392d`): the identity node layer is the core the whole epic stands on** — person nodes gate weighting, onboarding, role-aware prime, stamped-assignment matching, and dashboard V3. Conflict pipeline second (no schema deps, high leverage, and its read-time warnings get richer once provenance/identity are in place to name who recorded the conflicting node). Retrieval semantics third because weighting without identity is unbuildable — though TC9 may start as soon as Wave 1 gates. Dashboard V1/V2 run in parallel from day one because they consume only what already exists, while V3 waits for the data it renders. Floating smalls fill implementer gaps without blocking anything.
