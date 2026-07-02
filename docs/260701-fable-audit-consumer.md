# Fable Audit — Stage 3: Consumer Audit — 2026-07-01

## Intended purpose (confirmed with human)

Vibe Cognition is a fully local MCP server plugin for Claude Code that gives a codebase persistent, structured memory — a git-committed knowledge graph (`.cognition/journal.jsonl`) of decisions, failures, discoveries, constraints, incidents, patterns, workflows, and open tasks — so future Claude Code sessions (and human teammates) understand why the code is the way it is, without re-litigating settled choices or repeating known failures. Primary users are developers using Claude Code daily, and multi-agent / multi-human team collaboration is a first-class use case, not an edge case. Local embeddings (no API keys) power semantic search; the browser dashboard is a secondary, nice-to-have surface — the graph, MCP tools, hooks, and skills are the primary surfaces. Curation is agent-driven via /vibe-curate by current design (it could be automated again in the future, but that is not the current design). Success for a new consumer: install from the marketplace, restart Claude Code, and within a session or two see Claude spontaneously recording history, retrieving relevant context at session start, and — after /vibe-curate — having a browsable, linked graph.

## Scope of this stage

Four Sonnet 5 auditors adopted newcomer perspectives: (1) a literal top-to-bottom README walkthrough on a fresh machine, logging every stuck point and verifying claims against code; (2) an agent newcomer for whom the 29 tool docstrings + server instructions ARE the documentation; (3) team adoption arcs (teammate #2 joins an existing graph; topology choice; concurrent agents; attribution trust; cross-project); (4) skills-as-commands, CLI --help, error-message UX, and whether the curated-graph payoff is reachable for a human. Stage 1/2 filings were excluded from re-reporting. 19 raw findings synthesized (two cross-agent duplicates merged).

## Findings

### A joining teammate is told "ready" while their search is silently incomplete  [severity: high]  [type: blindspot]
- **What:** When teammate #2 first opens a repo with an existing journal, their empty local Chroma needs a full backfill — but `embedding_ready.set()` fires at `server.py:206`, BEFORE the sync loop starts (:215). `get_status` reports `embedding_status: "ready"` within seconds while hundreds of historical nodes are still un-embedded (one un-batched generate per node); there is no third "syncing" status anywhere, and README's only related guidance ("model still loading… wait") describes model load, not backfill. Compounding: README's `cognition_reload` row claims "the store auto-converges with concurrent sessions," overstating reality against the graph's own high-severity drift discoveries; and no README section walks teammate #2 through what to do or expect (or how long).
- **Evidence:** `server.py:184-236`; `service_tools.py:21-82` (ready/loading/error only); README.md:134, :190-225, Troubleshooting; `readme.py` onboarding fires only for EMPTY graphs — never for "you joined an existing one."
- **History:** `4b99fa9f44d5` (drift structural), `be019b3eea3c` (E-8 perf — this is the distinct VISIBILITY gap), `55b6740e42f8`, `7330e0252c8a`; `bd745214cb69` shipped empty-graph onboarding only. Oversight.
- **Impact:** The teammate-join moment — the single most important first impression for the first-class team use case — looks broken ("search misses things") while every status surface says fine.
- **Root cause / Fable's read:** "Ready" conflates model-loaded with corpus-synced. One status value and one README subsection fix the trust problem even before the E-8 perf fix lands.

### The maintainers' shared-checkout protocol is not shipped — consumers get the topology the maintainers don't use, and none of the rituals for the one they do  [severity: high]  [type: gap]
- **What:** README mentions "shared-checkout" once, in an undefined parenthetical; `cognition_readme`'s team-setup section documents only separate-clones/`merge=union`. The actual shared-checkout discipline this project survives by — nobody commits the journal on WP branches, manager flushes via temp git worktree at checkpoints, hard destructive-op ban near the journal — lives only in private graph constraints and the maintainer's personal `~/.claude/team-rules` files. No shipped artifact tells a consumer team the two topologies exist, how to choose, or what shared-checkout requires.
- **Evidence:** README.md:223; `readme.py` COGNITION_GUIDE:83-103.
- **History:** Decision `4ed473ba9c75` (topology-first), constraint `1f39e60c6d83`, incidents `5d63a548783d` and `59416463f1e3` — the protocol exists precisely because of real data loss. Deliberate decision to teach only merge=union (`9f13a8099e03`); never shipping the shared-checkout half is an oversight.
- **Impact:** Mirrors Stage 2's finding that the SUPPORTED topology is untested: here, the TESTED topology is unsupported (undocumented). Between them, neither topology is both exercised and shipped. A consumer team choosing shared-checkout rediscovers the recorded incidents the hard way.

### README accuracy: a stale concurrency claim, 4 missing tools, an invisible feature, and troubleshooting that skips the real failure classes  [severity: high]  [type: bug + gap]
- **What:** Four related defects in the primary human-facing doc: (a) Troubleshooting claims "Only one Vibe Cognition instance can run per project at a time" — unchanged since the initial commit and contradicted by the proven Rust-lock/multi-client discoveries AND the maintainers' own daily multi-agent mode; the real caveat (per-process startup hydration divergence) goes unstated. (b) The tool tables list 25 of the advertised 29 tools — missing `cognition_readme`, `cognition_load_project`, `cognition_unload_project`, `cognition_list_projects`. (c) The entire cross-project capability (shipped v0.9.0) has zero README presence. (d) Troubleshooting omits the venv self-heal class the hook actively detects ("close ALL sessions…", incident-backed) and the Windows orphan-process pileup; the Quick Start hides the uv-sync wait and never gives a "how do I know it worked" moment; `EMBEDDING_REVISION` (a security knob) is absent from the config table.
- **Evidence:** README.md:305 (stale line; `git log -S` → initial commit only), :106-141 (tables), :299-311 (troubleshooting), :278-287 (config); tool count verified by `@mcp.tool()` decorators (26+1+1+1=29); `hooks/session-start.sh:60-85`.
- **History:** `wp-emb-e4-discovery-chromadb-rust-lock`, `xp0-q1/q3` (concurrency reality); `9b3e250d4591`, `35c4e21968f9`, `7c43f70b3e37` (XP shipped, docstrings-only — no README task ever filed); `3432e00e483d`, `361d6c2b638b` (venv incident + probe); `a54b0191e362` (orphans, open). Oversights — the S-3 doc-drift sweep (`f70c9ad1c55d`) never covered these.
- **Impact:** The stale concurrency line actively misleads teams into treating their normal mode as an error; the missing feature/tools mean consumers can't discover shipped multi-agent capability from the document they're pointed at.

### Edge semantics exist only inside a /vibe-curate sub-skill — the direct add_edge path the tools themselves recommend gets a bare enum  [severity: high]  [type: gap]
- **What:** `cognition_add_edge`/`add_edges_batch` docstrings list edge types with zero when-to-use/direction semantics. `cognition_record`'s docstring explicitly invites the direct path ("or add edges manually with cognition_add_edge"), but the actual semantics table (meaning, direction, disambiguation, task-specific rules) exists only in `skills/vibe-curate/edge-analyzer.md` — loaded exclusively by the curate flow's internal subagent.
- **Evidence:** `cognition_tools.py:2216-2268`, `:1459`; `skills/vibe-curate/edge-analyzer.md:20-36`.
- **History:** `f70c9ad1c55d` checked list-drift (consistent), never absence-of-semantics. Oversight.
- **Impact:** Hand-linking agents guess between led_to/relates_to/resolved_by from names alone — the exact ambiguity the (unreachable) table exists to prevent, producing miscurated edges that survive forever.

### `cognition_record` over-promises deterministic linking and hides two node-type boundaries  [severity: high]  [type: unclear-instruction]
- **What:** (a) The docstring says references make "nodes link to their episode via deterministic part_of matching" — worded generally, but the truth table (`storage.py:662-708`) fires only entity↔episode (any shared ref) and entity/episode↔document (doc: ref); entity↔entity NEVER auto-links, so the common mid-episode cluster (several decisions/discoveries citing the same issue before the closing episode exists) sits fully edgeless until curation, contrary to a reasonable reading. (b) The NODE TYPES enumeration omits `document` entirely (an agent stuffing a spec into a discovery node loses sidecar search/freshness/dedup) and reveals `task` only reactively via the rejection error.
- **Evidence:** `cognition_tools.py:1461-1470,1489-1494,1522-1529`; `storage.py:662-708` (esp. :676).
- **History:** `29e0fc908992` designed the task rejection (deliberate); the docstring's over-general linking claim and the document omission are graph-silent oversights. Distinct from Stage-2's sweep-predicate finding (`7c1899fe59ed`).
- **Impact:** The tool named first by the server instructions ("RECORD AS YOU WORK") misleads on both what gets linked and where documents go.

### The curated-graph payoff is effectively agent-only — a stats tally, not a browsable linked graph  [severity: medium]  [type: broken-assumption]
- **What:** `/vibe-curate`'s user-facing report is scoped to counts (uncurated before/after, edges by type, cluster count) — it never shows WHICH nodes now connect or why. The only surface rendering the actual graph for a human is the dashboard (secondary by design, separately invoked). A human who never opens it receives "12 edges created, 3 clusters" — the confirmed success criterion's "browsable, linked graph" is unreachable for them.
- **Evidence:** `skills/vibe-curate/SKILL.md:81-89`; `dashboard/api.py:86-123`; `skills/vibe-dashboard/SKILL.md:1-41`.
- **History:** Graph silent on payoff-reachability-without-dashboard — all prior dashboard nodes are mechanics. Oversight against the success bar.
- **Impact:** Direct hit on the confirmed definition of success. Cheap mitigation: have the curate report narrate the actual new links ("X now relates_to Y because…") so the payoff lands in-chat.

### Mandatory auto-curate fans out subagents with zero cost warning  [severity: medium]  [type: blindspot]
- **What:** `/vibe-cognition` makes curation mandatory "without being asked" after any recording turn; `/vibe-curate` then launches one Haiku edge-analyzer per 5-10-node batch (uncapped batch count) plus a cluster pass. The human is never told before/during — only a retrospective report after tokens are spent. Related: the standalone cadence guidance ("high number of uncurated nodes," "several related nodes") is never quantified.
- **Evidence:** `skills/vibe-cognition/SKILL.md:191-208`; `skills/vibe-curate/SKILL.md:13-18,31-49`.
- **History:** `e983273d722e` addressed model-choice cost (Haiku pin), never disclosure; `e095652e90eb` made curation mandatory. Oversight.
- **Impact:** To a new user this reads as Claude silently doing unexplained extra work — a transparency gap at the exact moment trust is forming.

### The always-pushed instructions omit two practices their own tools declare mandatory  [severity: medium]  [type: gap]
- **What:** `SERVER_INSTRUCTIONS` (the only unconditionally-pushed contract, framed as "three standing practices") names search/get_history/record/get_chain — never `cognition_list_tasks` ("**Before picking up work, check open tasks first**", its own docstring) or `cognition_get_workflow` ("**Before starting any multi-step task**, search for an existing workflow"). An agent trusting the pushed contract as complete never learns these gates exist.
- **Evidence:** `instructions.py:19-43`; `cognition_tools.py:1547-1563,2042-2068`.
- **History:** Distinct from Stage-2's redundancy finding (`9aca47c5803d` — restated content; this is MISSING content). Oversight.
- **Impact:** Task/workflow check-first behavior — load-bearing for the backlog and workflow features — depends on out-of-band discovery.

### `cognition_search` keys node type as `node_type`; every other read tool keys it `type`  [severity: medium]  [type: gap]
- **What:** `_format_search_results` returns `node_type`; get_history/get_node/get_neighbors/get_edgeless/get_uncurated all return the raw `type` field. No docstring flags it. Code or subagent templates branching on either key silently misclassify or drop nodes across the two tool families.
- **Evidence:** `cognition_tools.py:383-392` vs `:754-763`; `storage.py:456-464`.
- **History:** Graph silent. Oversight.
- **Impact:** Silent — dicts don't error on a missing key when displayed; only downstream logic breaks.

### Entity `author` is unauthenticated agent text while task authorship is git-verified — and neither is explained  [severity: medium]  [type: broken-assumption]
- **What:** Tasks resolve their creator server-side from git config (post-incident hardening; client cannot override). `cognition_record`'s `author` is an arbitrary client string with only advisory docstring guidance; this project's own graph shows agent personas as entity authors. README contains zero mentions of author/attribution — a human reading a shared multi-human graph has no way to know which authorship is verified and which is asserted.
- **Evidence:** `cognition_tools.py:145,171` (unvalidated) vs `:960` (server-resolved); README grep → no matches.
- **History:** `d1192f7e7bf8` explicitly designed task attribution "for trust" in multi-human graphs; entities were never given the same treatment or a documented caveat. Half-deliberate, half-oversight.
- **Impact:** The field meant to answer "who decided this, can I trust it" has inconsistent trust levels across node types, undocumented either way.

### `vibe-cognition-prime` / `-backfill` silently swallow `--help` and execute instead  [severity: medium]  [type: bug]
- **What:** Neither CLI parses argv at all; `--help` runs the full command (verified live: prime printed the full session context; backfill printed a 104-commit report). No usage text exists; the flag is silently no-op'd rather than erroring. `dashboard/cli.py`'s argparse, by contrast, is accurate.
- **Evidence:** `prime.py:189-253`; `backfill.py:75-125`; contrast `dashboard/cli.py`.
- **History:** The June audit filed "CLIs documented nowhere" and H-6(b) `--days`; the silent `--help` swallow is a distinct functional bug, graph-silent. Oversight.
- **Impact:** The natural first move of anyone investigating these commands produces a wall of output with no hint the flag was ignored.

### Walkthrough trace (ordered stuck-points for a literal newcomer)
1. Install step gives no success signal (nothing says what a working install looks like).
2. "Restart Claude Code" hides the two-stage wait (uv sync 30-60s + ~250MB model) — explained 60 lines later with no forward reference; the first session appears to hang.
3. No "how do I know it's working" moment anywhere — the success criterion is never operationalized.
4. Tool tables under-count (25 of 29); cross-project feature invisible.
5. Troubleshooting misses the incident-backed failure classes; the one concurrency claim present is stale-false.
6. Teammate #2 has no join sequence, no timing expectations, and a status surface that says "ready" while search is incomplete.

## Summary & Recommendations

**The product's actual strengths are undersold, and its documented claims drift false in exactly the trust-forming moments.** Stage 3's through-line is not missing features — it's that the consumer surfaces (README, docstrings, status values, skill reports) fail to carry what the code and the maintainers' practice already know. Grouped:

**Theme 1 — First impressions are unmanaged.** No install success signal, a hidden first-session wait, "ready" while syncing, and a stats-only curation payoff. Each is a moment where the newcomer decides whether the tool works; each currently defaults to silence or overstatement. Recommendation: an "expectations pass" — one Quick Start paragraph (what you'll see, how long, how to verify), a third `embedding_status` value, and a curate report that shows the links.

**Theme 2 — The maintainers' knowledge is not the consumers' knowledge.** The shared-checkout protocol, the real concurrency caveat, the incident-backed troubleshooting entries, the cross-project feature — all exist in the graph, the code, or private rules files, and none in the shipped docs. The knowledge-transfer product is not transferring its own knowledge. Recommendation: a "ship the graph's lessons" doc pass sourced directly from the constraints/incidents this audit cited.

**Theme 3 — The agent-facing contract has authority gaps.** The pushed instructions omit two mandatory practices; edge semantics are locked inside a subskill; `cognition_record` over-promises linking and hides node-type boundaries; key names differ across tool families. An agent doing exactly what the surface says produces a worse graph than the design intends. (Continuous with Stage 1 Theme 3 — the docstring/skill layer IS the API.)

**Theme 4 — Trust metadata is inconsistent.** Verified task authorship next to spoofable entity authorship, actor-less deletes (Stage 1), and no documentation of any of it. For a multi-human record of "why," provenance is the product; it should be uniformly strong or at least honestly labeled.

## Potential tasks (checklist)

- [ ] Teammate-join visibility: add a third "syncing" embedding status (set ready only after backfill, or expose progress), surface it in get_status, and add a README "joining an existing graph" walkthrough with timing expectations; fix the "auto-converges" overstatement — priority: high
- [ ] Ship the topology guide: user-facing doc for shared-checkout vs separate-clones, including the worktree-flush protocol, journal-commit rules, and destructive-op ban (source from constraints 1f39e60c6d83 / 4ed473ba9c75 and incidents 5d63a548783d / 59416463f1e3) — priority: high
- [ ] README accuracy pass: replace the stale "only one instance" claim with the real hydration caveat; add the 4 missing tools + a cross-project section; add EMBEDDING_REVISION; add troubleshooting entries for the venv self-heal and orphan-process classes; add install-success signals and first-run expectations to Quick Start — priority: high
- [ ] Add edge-semantics (when-to-use/direction table) to cognition_add_edge and add_edges_batch docstrings, sourced from edge-analyzer.md — priority: high
- [ ] Fix cognition_record docstring: scope the deterministic-linking claim to its actual truth table; add document (→ store_document) and task (→ add_task) to the NODE TYPES section — priority: high
- [ ] Curate payoff + transparency: report the actual new links (not just counts); add a one-line cost/scope preflight note before subagent fan-out; quantify cadence guidance — priority: normal
- [ ] SERVER_INSTRUCTIONS: add the tasks-check-first and workflow-check-first practices (or explicitly defer to cognition_readme as the complete contract) — priority: normal
- [ ] Unify the type/node_type result key across search and the other read tools (or document the difference in both docstrings) — priority: normal
- [ ] Attribution honesty: document the entity-vs-task authorship trust asymmetry (README + docstrings), or server-resolve entity authorship like tasks — priority: normal
- [ ] Add argparse + accurate --help to vibe-cognition-prime and vibe-cognition-backfill (fold in H-6(b) --days) — priority: low
