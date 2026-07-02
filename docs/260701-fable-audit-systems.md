# Fable Audit — Stage 1: Per-System Audit — 2026-07-01

## Intended purpose (confirmed with human)

Vibe Cognition is a fully local MCP server plugin for Claude Code that gives a codebase persistent, structured memory — a git-committed knowledge graph (`.cognition/journal.jsonl`) of decisions, failures, discoveries, constraints, incidents, patterns, workflows, and open tasks — so future Claude Code sessions (and human teammates) understand why the code is the way it is, without re-litigating settled choices or repeating known failures. Primary users are developers using Claude Code daily, and multi-agent / multi-human team collaboration is a first-class use case, not an edge case. Local embeddings (no API keys) power semantic search; the browser dashboard is a secondary, nice-to-have surface — the graph, MCP tools, hooks, and skills are the primary surfaces. Curation is agent-driven via /vibe-curate by current design (it could be automated again in the future, but that is not the current design). Success for a new consumer: install from the marketplace, restart Claude Code, and within a session or two see Claude spontaneously recording history, retrieving relevant context at session start, and — after /vibe-curate — having a browsable, linked graph.

## Scope of this stage

Seven Sonnet 5 auditors ran in parallel, each auditing one system in isolation: (1) core graph & persistence (models/storage/journal_io/operations/queries), (2) node-type subsystems (documents/chunking/tasks/workflows), (3) embeddings & semantic search, (4) MCP server & tool surface, (5) session lifecycle & install/upgrade (hooks/prime/migrate_mcp/git identity+hygiene/backfill/packaging), (6) skills layer (all six SKILL.md files as LLM-facing specs), (7) dashboard (weighted secondary per confirmed intent — corruption/security/staleness only). Each finding was researched against the cognition graph to separate deliberate decisions from oversights. 39 raw findings were synthesized into the entries below; already-tracked open tasks are noted as such and not re-filed.

## Findings

### Journal rehydrate-reset is completely silent — recorded memory can vanish with no signal  [severity: critical]  [type: blindspot]
- **What:** `CognitionStorage._catch_up()` detects a shrunk/replaced/divergently-merged journal and calls `_rehydrate_reset()`, discarding all in-memory state and rebuilding from disk. The only signal is `logger.info("Journal changed under our replay offset; re-hydrating from top")` — no MCP tool response, `get_status` field, dashboard banner, or session-start message ever indicates that nodes recorded since the last flush may be gone.
- **Evidence:** `src/vibe_cognition/cognition/storage.py:829-930` (`_catch_up`), `:896-898` (info-only log), `:822-827` (`_rehydrate_reset`); `tests/test_cognition.py:433-448` proves nodes present a moment ago report `nodes: 0` with no error.
- **History (vibe-cognition):** Not hypothetical — incident `5d63a548783d` (shared-worktree branch-switch clobbered 2 live journal nodes, gone from file AND memory). Mitigation was process-level only: constraint `1f39e60c6d83` ("hard destructive-op ban near the journal"). No code-level detection/alert was ever filed. Oversight (the incident response stopped at process rules).
- **Impact:** Silently losing institutional memory is the exact failure this product exists to prevent. Today the system's only defense is every human/agent remembering a rule; the system itself neither detects nor reports the loss.
- **Root cause / Fable's read:** The incident post-mortem produced a constraint, not a detector. The rehydrate path already knows the pre/post node counts — surfacing the delta (log at WARNING, expose in `get_status`, flag in the next prime) is cheap and converts an invisible loss into a recoverable one (the nodes may still exist in git history or a teammate's session).

### Byte-rewrite defense (`-text` gitattribute) is withheld from every consumer team  [severity: high]  [type: gap]
- **What:** `git_hygiene.py` auto-config and `readme.py` team-setup teach ONLY `merge=union` — never `-text`. The C-3 byte-offset replay defense survives checkout/merge/pull in the dev repo only because "autocrlf=true smudge coincides with Python's CRLF writes" (the code's own words) — a coincidence not guaranteed for other teams' git configs. The maintainers added `-text` to their own repo via a dedicated cut-over ritual; consumers get neither the rule nor a disclosure of the residual risk.
- **Evidence:** `src/vibe_cognition/cognition/git_hygiene.py:8-16,39`; `src/vibe_cognition/cognition/readme.py:83-103`; this repo's `.gitattributes:8-24`.
- **History:** Decision `9f13a8099e03` deliberately teaches only `merge=union` ("never -text; set early, not retrofit onto a grown shared-checkout journal"). Nodes `54304ecf567c`, `90ee3c1b968c` (both high severity) confirm the underlying risk is real. Withholding `-text` is deliberate; never disclosing the residual risk to users is an oversight.
- **Impact:** Multi-agent/multi-human teams (first-class) following the docs verbatim get a materially weaker safety net than the maintainers use on themselves, for a failure class internally rated high.

### Home project's ChromaDB collection has no model/dimension drift guard — search silently returns empty  [severity: high]  [type: gap]
- **What:** The `model_guard` (dim/model-mismatch detection) is wired only into the foreign-project attach path (`cognition_load_project`, XP1). The home collection is never checked. If stored vs. configured model/dims diverge (env edit, future default-model bump), Chroma raises, `ChromaDBStorage.vector_search` catches broadly and returns `[]`, and `cognition_search` reports `{"results": [], "count": 0}` — indistinguishable from "no history." `get_status` performs no drift comparison either.
- **Evidence:** `src/vibe_cognition/embeddings/storage.py:181-190` (broad except → `[]`), `:30-36` (stamp docstring scoped "for XP1"); `src/vibe_cognition/tools/cognition_tools.py:426-451`, `:1296-1356` (guard only in load_project); `src/vibe_cognition/tools/service_tools.py:20-92`. Companion blindspot: zero tests cover home-collection drift, Ollama backend, or the embedding config fields (`tests/test_config.py`, `tests/test_embeddings_storage.py`); guard tests live exclusively in `tests/test_xp1_registry.py` via `load_project`.
- **History:** Discovery `xp0-q4-dim-model-mismatch` built the stamp/guard explicitly as an XP1 prerequisite; no node considers applying it to home. Scope gap / oversight (high confidence).
- **Impact:** A future embedding-model change would silently blank every existing user's semantic search, with no actionable error and no documented recovery.

### `cognition_search` never validates `node_type` — a typo returns a successful-looking empty result  [severity: high]  [type: bug]
- **What:** `cognition_get_history`/`get_edgeless_nodes`/`get_uncurated_nodes` all validate `node_type` via the shared `_parse_node_type` and return a clear error on bad values. `cognition_search` — the highest-traffic tool — passes it straight into a Chroma `where` equality filter; `node_type="descision"` matches nothing and returns `{"results": [], "count": 0}`.
- **Evidence:** `src/vibe_cognition/tools/cognition_tools.py:426-451` and `:1934-1973` (no `_parse_node_type`) vs `:2177-2179` (get_history validates); `src/vibe_cognition/embeddings/storage.py:179`.
- **History:** Episode `49ef4310ba17` (WP-T commit 3, T-6) created `_parse_node_type` to kill exactly this "silent wrong answer" class, but scope was deliberately held ("unify the parser + the clearly-wrong silent answers only") and `cognition_search` wasn't in the enumerated set. Scoped-out-then-forgotten.
- **Impact:** Directly undermines "CHECK HISTORY FIRST" — the agent concludes no history exists and re-litigates or re-fails.

### Journal replay never re-embeds — teammates' nodes are searchable-invisible until restart  [severity: high]  [type: gap]
- **What:** `_replay_entry`'s `add_node` branch mutates only the graph and reference index; no replay branch touches embeddings. A node journaled by another process becomes visible to `get_node`/traversal on next catch-up but is absent from `cognition_search` until server restart.
- **Evidence:** `src/vibe_cognition/cognition/storage.py:976-1041` (no embedding call in any branch).
- **History:** Discovery `4b99fa9f44d5` (high): "node↔embedding drift is structural… observed live as 37 nodes vs 35 embeddings." The v0.13.0 decision `ef95333d105c` removed one of two named drift sources (post-commit hook) and its own edge reason concedes "broader replay/reload drift persists — not fully resolved." Known, partially addressed, no open task covers the replay half.
- **Impact:** In the first-class multi-agent scenario, a teammate's fresh decision is retrievable by `get_history` but invisible to semantic search — a half-converged state that quietly defeats "check history first."

### Getting-started onboarding example is broken — wrong parameter name, missing required args  [severity: high]  [type: bug]
- **What:** `COGNITION_GETTING_STARTED` (served by `cognition_readme`, the canonical first action on an empty graph) shows `cognition_record(type="decision", summary=…, detail=…)`. The real signature requires `node_type` (not `type`) plus required `context` and `author`. The very first recorded write by a literal-following newcomer fails.
- **Evidence:** `src/vibe_cognition/cognition/readme.py:120-121` vs signature at `src/vibe_cognition/tools/cognition_tools.py:1439-1448`. All examples in `skills/vibe-cognition/SKILL.md:214-247` are correct — this is an isolated miss.
- **History:** `readme.py` created in WP-Readme (`bd745214cb69`); the S-3 doc-drift cleanup (`f70c9ad1c55d`) never enumerated this example. Oversight (high confidence).
- **Impact:** Fails the confirmed success criterion at its most visible moment — the new consumer's first write.

### Orphan server: still unimplemented at v0.13.0, and the plan's harm register misses the live delete-listener angle  [severity: high]  [type: gap — TRACKED (task `a54b0191e362`), one new angle]
- **What:** No parent-death detection exists anywhere in `server.py` (no signal handler, atexit, PPID poll, or stdin-EOF watchdog); v0.13.0 shipped without WP-Server-Lifecycle. NEW angle: an orphan that had launched the dashboard keeps a live, token-gated, DELETE-capable HTTP listener on 127.0.0.1 for the orphan's lifetime (observed up to 3 days). The plan doc's harm register frames harm purely as RAM/handles/update-corruption — never as a lingering mutation surface.
- **Evidence:** `src/vibe_cognition/server.py:238-338`; `src/vibe_cognition/dashboard/server.py:169-224` (daemon thread, no liveness tie-in); `docs/wp-server-lifecycle-plan.md:31-38` and §1.
- **History:** Deliberate, tracked, still-open (task `a54b0191e362`, plan "DRAFT, NOT dispatched"); the delete-listener angle is an oversight within the plan.
- **Impact:** Unbounded process/RAM pileup on Windows plus a persistent destructive endpoint the user believes is gone.

### Dashboard's full-capability token is written into MCP tool results and INFO logs  [severity: medium]  [type: gap]
- **What:** `cognition_dashboard` returns the URL with the embedded 32-byte token verbatim (persisted in the conversation transcript for its lifetime), and `start_dashboard`/`run_dashboard_blocking` also `logger.info` the full URL. That token gates read, document download, AND node delete.
- **Evidence:** `src/vibe_cognition/tools/dashboard_tool.py:20-42`; `src/vibe_cognition/dashboard/server.py:109-114,153-198`; `src/vibe_cognition/dashboard/middleware.py`.
- **History:** Dashboard hardening history is extensive (IPv6/host-header/DNS-rebinding, mime, path traversal — task `134076abbcbd`, episodes `b34a0853f8af`/`d8471cdbd962`/`adbfb550baa2`) but network-focused; out-of-band leakage via transcript/logs was never modeled. Oversight.
- **Impact:** The real exposure surface is "anyone who can read the transcript/log," which none of the network-layer defenses mitigate.

### Node deletion journals no actor — the dashboard is where the provenance hole bites  [severity: medium]  [type: gap]
- **What:** The `remove_node` journal tombstone is `{"action": "remove_node", "data": {"id": …}}` with no author, unlike `add_node`. The dashboard's `DELETE /api/node/{id}` sits behind only a browser `confirm()` — a human click deletes a node with zero record of who or why. Related docstring gaps: `cognition_remove_node` omits the `unlinked_artifacts` key it actually returns for documents, and never warns that deleting a parent task silently detaches children (whose `metadata.parent_id` keeps reporting the dead id — deliberate storage behavior per F10, `tests/test_task.py:579-594`, but uncommunicated at the tool surface).
- **Evidence:** `src/vibe_cognition/cognition/storage.py:270-287`; `src/vibe_cognition/dashboard/static/app.js:226-245`; `src/vibe_cognition/tools/cognition_tools.py:2561-2586`; `src/vibe_cognition/cognition/operations.py:113-123`. Companion blindspot: `DELETE /api/node` is the only gated endpoint with no auth-rejection test (`tests/test_dashboard.py:146-171` vs `:227-238`).
- **History:** Deletion design is deliberate (`6c8337d42c79`, `c9ea55e945e5`); actor attribution never discussed. Oversight.
- **Impact:** The one mutation the graph cannot explain afterward, in a system whose entire value is attributable history.

### Server startup failures die as a raw traceback — no diagnosable error, no partial functionality  [severity: medium]  [type: blindspot]
- **What:** In `lifespan()`, only `Settings()` construction is wrapped (log-and-reraise); `CognitionStorage(...)` and `ChromaDBStorage(...)` — the failure-prone steps (corrupted sqlite, `.cognition/` permissions) — have zero handling. An exception before `yield` propagates uncaught through FastMCP; Claude sees no cognition tools and no actionable reason. (Confirmed by reading fastmcp internals, not reproduced at runtime.)
- **Evidence:** `src/vibe_cognition/server.py:244-267,332-338`; `.venv/Lib/site-packages/fastmcp/server/mixins/lifespan.py:137-148`.
- **History:** Graph silent — oversight (medium confidence), plausible given the project's documented Windows IO/process wedge history (`79add0bd5705`, `b076d80a7b41`, `d0362d89d295`).
- **Impact:** "What does Claude see if startup dies halfway?" — today, nothing actionable.

### `get_status` docstring promises keys that don't exist  [severity: medium]  [type: unclear-instruction]
- **What:** Docstring documents `embedding_model: str` and `embedding_ready: bool`; the implementation returns neither — it returns `embedding_status` (string) and an undocumented `curation` key. Docstrings ARE the API for LLM consumers here.
- **Evidence:** `src/vibe_cognition/tools/service_tools.py:21-36` vs `:74-92`.
- **History:** The project has a standing RECURRING workflow for exactly this (`67751ebc39bd`, tool-surface self-sufficiency audit) last scoped to newly-shipped tools only; `get_status` drifted after its fields changed. Oversight (high confidence).
- **Impact:** Drift in the primary self-diagnostic tool undermines trust in every other documented contract.

### `migrate_mcp` write path is unguarded — a locked `.mcp.json` crashes the migration invisibly  [severity: medium]  [type: bug]
- **What:** `remove_server_entry()` guards the read (`FileNotFoundError`/`JSONDecodeError`/`OSError`/`UnicodeDecodeError`) but neither write branch guards `_atomic_write()`; a `PermissionError`/`OSError` (editor/AV lock, read-only ACL — realistic on Windows) propagates to an unhandled `SystemExit` traceback. The hook wraps the call in `2>/dev/null || MIGRATE_NOTE=""`, so the failure is invisible and the stale entry (which outranks the plugin-declared server per decision `a906f12a6ef7`) survives with no diagnostic anywhere.
- **Evidence:** `src/vibe_cognition/migrate_mcp.py:67-76` vs `:95-100,110-114,123-129`; `hooks/session-start.sh:98-100`.
- **History:** migrate_mcp has substantial hardening history (incident `20ca506fb3ab`, episodes `c470e5451cf1`/`ad89abd9cfee`, 20+ tests) — none touches write-time OSError. Oversight (high confidence on path; medium on frequency).
- **Impact:** Compounds with the silent-hooks gap (H-3): a class of users stuck on the stale-server config with no trail.

### First-install robustness: 120s hook timeout vs cold `uv sync`, and a single-cause failure message  [severity: medium]  [type: gap]
- **What:** The SessionStart hook timeout is 120s while a first install must download torch/chromadb/sentence-transformers — plausibly longer on slow connections, and a timeout kill mid-sync lands the user in the half-installed state. Separately, the health-probe failure message unconditionally diagnoses the DLL-lock class ("close ALL Claude Code sessions…") though the probe fires identically for interrupted downloads, disk-full, or hook-timeout kills — cases the advice won't fix. Companion blindspot: both shell scripts have zero automated test coverage despite a shipped regression in exactly this class (B-3 `${var%/*}` backslash bug, discovery `41e24b74219d`).
- **Evidence:** `hooks/hooks.json:6-13`; `hooks/session-start.sh:59,70-85,98-109`; no test references either script.
- **History:** DLL-lock class thoroughly tracked (incident `3432e00e483d`, pattern `72b0bac54647`); H-4 tracks a different first-install race; timeout sizing and message framing are graph-silent. Oversight (medium confidence).
- **Impact:** Threatens the exact "install → first session works" success bar, with recovery guidance that can be wrong.

### The graph itself overstates completion: backfill skill/CLI drift was closed by a coarse `resolved_by` edge  [severity: medium]  [type: broken-assumption]
- **What:** Two disagreeing backfill mechanisms still exist: the documented `/vibe-backfill` skill (watermark-based) and the undocumented `vibe-cognition-backfill` CLI (hardcoded 30-day window, no `--days`, mentioned nowhere in README — nor is `vibe-cognition-prime`). The June audit flagged this (AUDIT-2026-06-10 §S-3); the S-3 task node (`f70c9ad1c55d`) carries a `resolved_by` edge to the v0.12.2 episode, but the shipped scope (dispatch brief `aace1420ce90`) covered only S-3(b) and S-3(e) — this sub-item silently fell off the backlog while the graph says "resolved."
- **Evidence:** `skills/vibe-backfill/SKILL.md:22-36` vs `src/vibe_cognition/cognition/backfill.py:81-94`; `pyproject.toml:43-44`; README (no CLI mentions); `docs/AUDIT-2026-06-10.md:226`.
- **History:** Graph-accuracy oversight: a multi-item task closed by one coarse edge. (Meta-lesson for the graph's own methodology.)
- **Impact:** Beyond the object-level drift, this is a trust defect in the knowledge graph itself — the product's core promise.

### Task `priority` is docstring-constrained but unvalidated freeform text  [severity: medium]  [type: bug]
- **What:** `cognition_add_task`/`cognition_update_task` document `critical | high | normal | low` but write any string straight into `severity` — unlike `status`, validated one function away. A typo'd priority (`"urgent"`, `"P0"`) silently sorts into the "normal" band and evades `cognition_list_tasks(priority="high")`.
- **Evidence:** `src/vibe_cognition/tools/cognition_tools.py:901-918`, `:1214-1216`, docstring `:1569`; fallback `SEVERITY_ORDER.get(…, 2)` in `prime.py:16,90,107,142` and `cognition_tools.py:1031`.
- **History:** "Locked clarifier 6" covered only the status vocabulary. Graph silent on priority — oversight (high confidence); no test exercises invalid priority.
- **Impact:** Backlog-as-graph filtering silently loses tasks for multi-agent teams.

### Document drift is pull-only, never surfaced, and re-storing a changed file never links versions  [severity: medium]  [type: gap]
- **What:** Only `cognition_get_document` re-hashes and reports `unchanged|modified|missing`; nothing else (search, startup sync, prime) ever checks. Re-storing a changed file mints a new unlinked document node with no `supersedes` edge (manual-only by docstring design). Stale embedded content keeps serving through `cognition_search` indefinitely. Note: `tests/test_doc_drift.py` is misleadingly named — it guards docs-vs-code drift, not document-blob drift.
- **Evidence:** `src/vibe_cognition/tools/cognition_tools.py:729-742,594-624,2229-2234`; `server.py:87-134`; `skills/vibe-document/SKILL.md:72`.
- **History:** Passive pull-detection is deliberate (decision `e752ff313ad7` §8d, "staleness handled by re-hash on access"); proactive surfacing and version-linking were never decided either way — oversight/never-revisited (medium confidence).
- **Impact:** A changed source document silently poisons search results until someone happens to pull that exact document.

### /vibe-curate skill drift: phantom `source` kwarg, overstated `part_of` "forbidden" claim, no concurrency or warm-up guidance  [severity: medium]  [type: unclear-instruction]
- **What:** Four related spec defects in the curation skill family: (a) SKILL.md instructs committing edges "with `source: \"curate-skill\"`" but the batch tool has no such kwarg — `source` is a per-edge JSON key (default `"batch"`), and the edge-analyzer's output schema omits it, so provenance silently degrades to `"batch"`; (b) SKILL.md claims agent `part_of` is "already forbidden" and edge-analyzer says "created automatically" — nothing in code rejects an agent-submitted `part_of` (only `duplicate_of` is blocked), and the same skill itself creates `part_of` edges in step 3.4; risky because these prompts run on Haiku (pinned by rule `e983273d722e`), the models most likely to take "forbidden" at face value; (c) no coordination step for two agents running /vibe-curate concurrently (stateless `get_uncurated_nodes`, no claim mechanism — wasted duplicate LLM work, though the `(from,to,type)` dedup prevents duplicate edges); (d) the embedding warm-up failure mode (`status: "loading_embeddings"`) is documented only in the dashboard skill, though /vibe-workflow's "always search first" mandate fires exactly at session start.
- **Evidence:** `skills/vibe-curate/SKILL.md:45,56-63,76-77,22-49`; `skills/vibe-curate/edge-analyzer.md:40,61-67`; `src/vibe_cognition/tools/cognition_tools.py:305,2239-2268,2340-2432`; `src/vibe_cognition/tools/utils.py:19-32`.
- **History:** All graph-silent — oversights. Multi-agent READ convergence was solved deliberately (`55b6740e42f8` → `7330e0252c8a`, v0.7.0) but the curate-skill race angle was never examined.
- **Impact:** The curation layer is the designated path to a linked graph; its spec drifting from the tools it drives degrades provenance and wastes tokens, silently.

### cluster-analyzer only ever sees the 50 most-recent nodes  [severity: medium]  [type: blindspot]
- **What:** The clustering subagent's sole entry point is `cognition_get_history(limit=50)` — no pagination, no context sweep, no coupling to the uncurated/edgeless worklists the edge pass uses (`get_uncurated_nodes(limit=500)`). Past 50 nodes, older graph regions are permanently outside the clustering window.
- **Evidence:** `skills/vibe-curate/cluster-analyzer.md:7` vs `skills/vibe-curate/SKILL.md:26`.
- **History:** Graph silent — oversight.
- **Impact:** Clustering — half of /vibe-curate's stated deliverable — stops working precisely on the mature, long-lived projects the tool serves best (this project's own graph is at 281 nodes).

### `snapshot_journal` (the torn-tail-safe copy built for manager flushes) has zero production callers  [severity: medium]  [type: blindspot]
- **What:** `journal_io.snapshot_journal` exists specifically so the manager flush "never captures a torn mid-append tail" (its docstring, CR-4) — but nothing in `src/`, no console script, hook, or skill calls it; the real worktree-flush protocol is a manual git-worktree copy.
- **Evidence:** `src/vibe_cognition/cognition/journal_io.py:146-169`; only caller is `tests/test_journal_concurrency.py:209-218`.
- **History:** Worktree-flush is the load-bearing safety mechanism (constraint `1f39e60c6d83`, decision `4ed473ba9c75`); no node shows `snapshot_journal` adopted into it. Oversight (medium confidence).
- **Impact:** The two recorded incidents in this exact area (`5d63a548783d`, `59416463f1e3`) are precisely where the unused primitive would matter.

### `cognition_store_document`'s `references` param silently lands in `context` — untaught by the skill that teaches references-vs-context  [severity: medium]  [type: gap]
- **What:** By deliberate design a document node's own `references` are restricted to its `doc:` key, so the tool's `references` arg redirects to `context` — the exact opposite of `cognition_record`, and unmentioned by `/vibe-document`, whose WRONG-vs-RIGHT section drills "refs go in references."
- **Evidence:** `src/vibe_cognition/tools/cognition_tools.py:1717-1718`; `skills/vibe-document/SKILL.md:52-60`.
- **History:** Storage-layer restriction deliberate (`e752ff313ad7`, `f8ef403cbe69`); omitting the asymmetry from the skill is an oversight.
- **Impact:** Agents primed by the skill's own lesson will mis-tag documents with no error or warning.

### Ollama backend silently drops the query/document prefix scheme  [severity: medium-low]  [type: blindspot]
- **What:** The SentenceTransformers backend prepends `search_query:` / `search_document:` (load-bearing for nomic retrieval quality); `OllamaBackend.encode` accepts `is_query` but ignores it ("ignored for Ollama") — with the same nomic model family as default, whose model card recommends the same prefixes.
- **Evidence:** `src/vibe_cognition/embeddings/generator.py:37-38,71-72` vs `:100-118`.
- **History:** Graph silent (the E-3 prefix work never touched Ollama) — likely oversight (medium-high confidence).
- **Impact:** The documented alternative backend delivers systematically worse search than the default, unwarned. Also: OllamaBackend has zero tests.

### `local_only` blob is written to disk before its `.gitignore` entry  [severity: low]  [type: blindspot]
- **What:** In `_materialize_blob`, `write_blob(...)` (line 480) precedes `add_gitignore_entry(...)` (line 494) — a brief window where a privacy-intended blob sits unignored; a coincident `git add`/commit could publish it.
- **Evidence:** `src/vibe_cognition/tools/cognition_tools.py:454-497`; `src/vibe_cognition/cognition/documents.py:104-137,167-178`.
- **History:** Graph silent; adjacent races were reasoned about carefully (`already_committed` proxy) — likely oversight, low-medium confidence.
- **Impact:** Narrow window, but it touches the explicit privacy guarantee; the fix is a two-line reorder.

### Workflow supersession has no structural guardrails; `redirect_edges`/`duplicate_of` are shelved half-features  [severity: low]  [type: gap]
- **What:** (a) Nothing restricts `supersedes` to workflow→workflow, checks HEAD-ness, or prevents cycles; `get_workflow_head` is cycle-safe but returns an implementation-defined node on a cycle — `cognition_get_workflow` can quietly serve a stale/wrong procedure. (b) `CognitionStorage.redirect_edges` has no production caller and doesn't remove old edges (relying on an unimplemented follow-up); both edge tools refuse `duplicate_of` ("requires merge logic… not supported here") — a modeled-then-paused merge feature.
- **Evidence:** `cognition_tools.py:231-274,243-246,314-315`; `queries.py:114-150`; `storage.py:339-391`; `models.py:47`; `tests/test_workflow.py:194-211`.
- **History:** Cycle-safety was consciously tested (accepted-by-extension permissiveness, moderate confidence); redirect_edges/duplicate_of graph-silent — shelved incomplete feature.
- **Impact:** Low probability, but failure lands exactly on the workflow feature's core guarantee; the dead code will double edges if ever resumed as-is.

### Known, deliberately-deferred items re-confirmed (no new task filed)
- **E-8** one-node-per-loop startup embedding sync — tracked (`be019b3eea3c`); cost falls on teammates pulling large journal deltas.
- **T-1c** cross-process shared-ChromaDB convergence test — tracked (`c34c788b8d5b`); all existing sync tests are single-process.
- **H-3** stderr breadcrumbs (`33719f0d26bb`) and **H-4** first-install race / plugin.json venv path (`afed9ed48066`, human-gated, constraint `b8ec24fe9107`) — both deliberate deferrals; H-3's absence now also masks the migrate_mcp write-path crash above.
- **backfill.py subprocess hardening** (`8cd9c158ed65`) — same wedge class as v0.12.1, correctly scoped as CLI-only-today; risk escalates sharply if ever called from the detached server.
- **Node-id mint TOCTOU** — self-documented accepted residual (`storage.py:178-183`, WP-ID episode `bdc17a401bf0`); worth a conscious re-acceptance since multi-agent teams maximally exercise it.
- **Task-delete orphaning storage behavior** — deliberate and tested (F10); only the tool-surface caveat is filed above.

### Confirmed-good (audited, no defect)
- v0.12.1 git-identity wedge: fully fixed (pure file reads, never shells out, never raises) and exemplarily linked in the graph (incident → discovery → decision → episode → pattern).
- Dashboard: localhost-only bind everywhere, no CORS (default-deny blocks cross-site DELETE preflight), per-request journal replay via `storage.snapshot()`/`_synced()` (never stale vs concurrent sessions), search parity with `cognition_search` (shared `adaptive_vector_search` + ghost filter — prior gap `c7d948583b4e` confirmed fixed and regression-tested), path-traversal/symlink/mime defenses tested, embedding generation lock prevents in-process races.

## Summary & Recommendations

**Theme 1 — The system does not defend or even announce its own memory loss (the audit's gravest throughline).** The rehydrate-reset silence (critical), the consumer-facing `-text` omission, the unused `snapshot_journal`, and the actor-less delete tombstone are one family: every defense against journal/graph loss is procedural (constraints, rituals, human discipline) rather than built into the code, and when loss happens the system says nothing. Recommendation: a "loss-visibility" work package — detect and surface rehydrate resets (delta counts in `get_status` + next prime), attribute deletes, wire `snapshot_journal` into the flush procedure, and either teach `-text` to consumers or document the residual risk honestly.

**Theme 2 — Search failures are indistinguishable from "no history."** Home-collection drift guard missing, unvalidated `node_type`, replay-without-embedding, warm-up gating undocumented in the skills that mandate search-first. Each independently returns a plausible empty result; together they mean "CHECK HISTORY FIRST" can silently no-op. Recommendation: an "honest empty" principle — every path that can return zero results for an infrastructural (not informational) reason must say so in the response.

**Theme 3 — The docstring/skill layer IS the API, and it has drifted in exactly the places newcomers and weak models touch first.** The broken getting-started example, `get_status` key drift, the curate skill's phantom kwarg and overstated "forbidden," the `store_document` references asymmetry. The project already owns the right tool — the recurring tool-surface self-sufficiency audit (`67751ebc39bd`) — it just hasn't been re-run repo-wide since the surfaces changed. Recommendation: re-run it across all tools AND skills, and add drift regression tests where cheap.

**Theme 4 — The graph's own record-keeping has a failure mode: coarse closure.** A multi-item task closed by one `resolved_by` edge silently dropped a real sub-item (backfill drift). Recommendation: when closing multi-item tasks, enumerate shipped vs. dropped sub-items in the closing episode, or split tasks before closing.

**Theme 5 — Multi-agent is first-class in intent but second-class in verification.** Cross-process convergence untested (T-1c), curate concurrency unexamined, TOCTOU accepted, replay-embedding drift. None is individually severe; the pattern is that single-process paths get tests and multi-process paths get reasoning.

## Potential tasks (checklist)

- [ ] Surface journal rehydrate-reset events: WARNING log with node-count delta, `get_status` field, flag in next prime — priority: critical
- [ ] Fix getting-started `cognition_record` example in readme.py (node_type/context/author) — priority: high
- [ ] Add home-collection model/dim drift guard + surface in get_status + drift regression test — priority: high
- [ ] Validate `node_type` in cognition_search via `_parse_node_type` (both home and multi-project paths) — priority: high
- [ ] Journal byte-rewrite defense for consumers: teach/offer `-text` in git_hygiene+readme, or document the residual autocrlf risk — priority: high
- [ ] Re-embed on journal replay (or periodic reconcile) — close the structural node↔embedding drift — priority: high
- [ ] Amend WP-Server-Lifecycle harm register: orphaned dashboard = lingering DELETE-capable listener — priority: normal
- [ ] Dashboard token hygiene: stop logging full URL at INFO; document transcript exposure; consider short-lived tokens — priority: normal
- [ ] Delete provenance: author in remove_node tombstone; docstring gaps (unlinked_artifacts, task-child detachment); dashboard delete auth-rejection test — priority: normal
- [ ] Graceful lifespan startup failure: catch storage/Chroma init errors, log diagnosably, degrade or exit with clear message — priority: normal
- [ ] Re-run tool-surface self-sufficiency audit repo-wide (get_status keys, store_document references asymmetry, etc.) + drift tests — priority: normal
- [ ] migrate_mcp: guard the write path (`_atomic_write`) like the read path — priority: normal
- [ ] First-install robustness: size hook timeout against cold `uv sync`; make health-probe message multi-cause — priority: normal
- [ ] Shell-script test coverage for session-start.sh / reinject-instructions.sh (bash-level regression harness) — priority: normal
- [ ] Validate task `priority` against the documented vocabulary (mirror status validation) — priority: normal
- [ ] Document staleness: surface `modified|missing` beyond get_document (search/status), auto-offer `supersedes` on re-store of a changed file — priority: normal
- [ ] Fix /vibe-curate skill drift: per-edge `source` wiring, honest `part_of` framing (or enforce in code), concurrency note, warm-up note in embedding-dependent skills — priority: normal
- [ ] Widen cluster-analyzer scan beyond `get_history(limit=50)` — priority: normal
- [ ] Wire `snapshot_journal` into the manager-flush procedure — priority: normal
- [ ] Re-open backfill skill/CLI drift (S-3 sub-item closed by coarse resolved_by): document or retire the CLI; reconcile watermark vs 30-day logic — priority: normal
- [ ] Ollama backend: apply nomic query/document prefixes (or record the deliberate decision not to) + first OllamaBackend tests — priority: low
- [ ] Reorder `_materialize_blob`: add `.gitignore` entry before writing local_only blob — priority: low
- [ ] Decide fate of `redirect_edges`/`duplicate_of` (implement merge or remove dead code); optional workflow-supersedes guardrails — priority: low
