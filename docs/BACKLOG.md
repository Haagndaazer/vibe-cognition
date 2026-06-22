# Vibe Cognition — Backlog

**Maintained by:** vince (planner/manager). **Source of truth** for what's shipped, what's
in flight, and what's queued. Derived from `docs/AUDIT-2026-06-10.md` (the ~70-finding audit)
and `docs/DESIGN-document-storage.md` (the v0.8.0 feature spine).

**Convention:** the proposed WP groupings below are a *triage inventory*, not briefs. Each WP
gets a peer-reviewed execution plan (a `docs/wp-*-plan.md`) before it's assigned to Vorpid, and
each ships through the standard gate (SHA-pinned merge, fix+proof same commit, voiding clause,
journal-flush-via-worktree). Last updated 2026-06-21 (**v0.9.0 LIVE** — cross-project cognition XP0→XP2 + E-3; WP-Dash-tail + WP-Emb-non-E-3 shipped. **WP-Readme + WP-Lint merged to main, release HELD** (no bump yet — Colton holds the v0.10.0 cut). Remaining = P3 doc/skill + hooks-tail + the WP-Test coverage hole).

---

## In flight

- **WP-Readme-GitAttr** (Vorpid, dispatched 2026-06-22): teach users to set `.cognition/journal.jsonl merge=union` in repo-root `.gitattributes` so the append-only journal union-merges for teams (separate-clones topology). Content-only in `readme.py` (new "Team setup (git)" section in COGNITION_GUIDE + pointer in GETTING_STARTED + one ONBOARDING_BLOCK clause) + `test_readme.py`. Honors the C-3/duplication scar: ONLY `merge=union`, never `-text`; set early, never retrofit a grown journal in a shared checkout. Plan `docs/wp-readme-gitattr-plan.md` (sonnet peer-reviewed). Folds into the held v0.10.0.

- **WP-Git-Hygiene-Auto** (Vorpid, IN IMPL on `fix/wp-git-hygiene-auto`; spec FINAL, 2 sonnet rounds / 6 blockers): server auto-configures git hygiene for `.cognition/` on startup, **one-time-ever** via a content-versioned, git-ignored `.cognition/.git-hygiene-managed` flag. Two writes: `.cognition/journal.jsonl merge=union` → git-root `.gitattributes` (never `-text`), and `chromadb/` + the flag → `.cognition/.gitignore` (scoped, not root). Flag written only after both writes resolve (partial-failure → retry). Default-on, `VIBE_COGNITION_NO_GIT_HYGIENE` opt-out; prime.py announces (read-only); README.md + readme.py notes + `rm flag` re-arm. New stdlib `cognition/git_hygiene.py`. Plan `docs/wp-gitattr-auto-plan.md`.
- **WP-Plan-XP-Discovery** (Vorpid, QUEUED; spec FINAL, sonnet + claude-code-guide): teach the `vibe-cognition:Plan` agent (`agents/plan.md`) cross-project memory (load_project + `project=`) + teammate-comms sibling discovery (label→path via filesystem). **Also CLOSES S-2** — rewrites the broken `mcp__vibe-cognition__*` prefix to the correct plugin form `mcp__plugin_vibe-cognition_vibe-cognition__*` (the agent's cognition tools don't resolve today). Plan `docs/wp-plan-xp-discovery-plan.md`.

**WP-Readme-GitAttr** (PR #26 → `c5af8a9`, merged main `7e95776`, verified in worktree) + **WP-Readme + WP-Lint** are merged to main awaiting the release cut (Colton holds v0.10.0). Next candidates from the P3 tail below.

### Merged to main, RELEASE HELD (Colton's v0.10.0 cut)
- **WP-Readme** (PR #24 → `9b14d88`): `cognition_readme` tool ({guide, getting_started}, modeled on vibe-memory's `memory_readme`) + empty-graph onboarding via `prime.py` (fires when `.cognition/` absent or nodes==0; instructs the LLM to alert the user + call `cognition_readme`; no `session-start.sh` change). Canonical ASCII/stdlib `readme.py` constant. Plan: `docs/wp-readme-plan.md`.
- **WP-Lint** (PR #25 → `0be175a`): healed main after the v0.9.0 release left CI silently red — regen `uv.lock` for 0.9.0 (release bumped version without `uv lock`) + cleared 39 pre-existing ruff violations the lockfile drift had been masking at the sync step. Discovery `bfba9f13e0b1`.

### P3 tail remaining (near the bottom of the barrel)
- **WP-Doc/Skill** (S-2 plan-agent frontmatter, S-3 README/SKILL drift, H-5), **WP-Hooks-tail** (H-2 PowerShell commit matcher, H-3 stderr breadcrumbs, H-4 install race [human-gated], H-6 remainder), **WP-Test** (T-1 the MCP-tool-layer coverage hole — P1-infra). See the audit-remainder groupings below.
- **E-8** (deferred): slow one-node-per-loop startup sync / dead `generate_batch` (last WP-Emb sliver).
- **CI/process guards** (from the WP-Lint finding): add a `uv lock` step to the release runbook after the version bump; adopt a standing pre-gate `uv run ruff check .` (not bare `ruff`) alongside `uv run pyright`. Pairs with discovery `bfba9f13e0b1`. **Pin the pyright version** (PYRIGHT_PYTHON_FORCE_VERSION or pin in deps) so local == CI — surfaced in WP-Readme-GitAttr where local pyright 1.1.408 flagged a uvicorn `install_signal_handlers` stub diagnostic that CI's pinned pyright doesn't, briefly reading as a "pre-existing server.py error."

**Document-storage feature COMPLETE (D1a → D4) — stored, searchable, deletable, documented, dashboard:**
- **WP-D1a** (PR #8 → `870ff09`): DOCUMENT type + reference mode + sidecar (+deletion) + store/get + dedup + pair-level graph-inert matcher guard + sync-path embed guard.
- **WP-D1b** (PR #9 → `0faf302`): matcher 6-pair truth table + the ONE shared `documents_with_sha` predicate + copy mode (blob, ext whitelist, size/git policy, S3) + per-blob-path refcounted deletion + chunk-purge wiring + **N1 ghost-search fix** (MCP). Manual-edge guard → scope note.
- **WP-D2** (PR #10 → `dd11cd2`): documents **searchable** — chunked embeddings + **adaptive** over-query/dedupe + `matched_excerpt`; re-sync/backfill; `get_status` node/chunk split; **dashboard N1 SAFETY filter** (shared `search_hit_is_live`).
- **WP-D3** (PR #11 → `9afc538`): `/vibe-document` skill (S4/N3 link-by-`doc_ref`, WRONG-vs-RIGHT contrast) + surface fixes (all 17 tools, edge-type accuracy, `relates_to` 3-provenance) + doc-drift GUARD test.
- **WP-D4** (PR #12 → `6939c99`): dashboard document list + **token-gated path-safe download** (traversal-hardened: ../-/absolute/symlink/null all rejected via `is_relative_to` on the resolved path; reference→sidecar never the original; mime+filename header-injection clamped) + D-6 nav (dedupe-to-node) + D-1 liveness + **D-4 vendored cytoscape/fcose (no-CDN/SRI, offline)** + D-5 security (compare_digest, 400, clamp).

Seam principle held all five PRs: each creates nothing it can't delete. Six gate holds across the run, all resolved.

## v0.9.0 — ✅ RELEASED & PINNED LIVE (cross-project cognition + E-3)
Code SHA `0c2c52f`; **Loki pinned the marketplace** (commit `c563ff2`, byte-exact HEAD match, clean ff). Colton installed + verified on his machine (re-embed self-heals in bg via the E-3 marker-gated rebuild). Shipped:
- **WP-Dash-tail** (PR #18 → `2c33f54`): adaptive over-query unify (`adaptive_vector_search`), `--no-embeddings` disabled state, IPv6 `[::1]` host-check, D-3/D-5 tidies.
- **WP-Emb non-E-3** (PR #19 → `cd81769`): E-6 dead-code prune (pyright 7→0 in storage.py), E-7 `revision=` pin + `datetime.now(UTC)`. **E-4 DROPPED** — chromadb 1.5.5 Rust backend swallows SQLite locking internally (discovery `wp-emb-e4-discovery-chromadb-rust-lock`).
- **WP-XP0 spike** (`d1576fe`): GO on Option A. 5 discoveries (`xp0-q1`…`q5`): coexistence, `close()` handle release, foreign-read safety, dim/model guard, journal catch-up cost.
- **WP-XP1** (PR #20 → `05b0797`): registry plumbing — model/dim collection stamp, `close()` handle release, `open_existing()`, `LoadedProjects`, load/unload/list tools.
- **WP-XP2** (PR #21 → `8a0a799`): cross-project READ routing — `project=` on search + read tools, `project_notes` provenance, write-isolation proof.
- **WP-E-3** (PR #22 → `e679857`): doc embedding prefix QUERY→DOCUMENT (4 sites) + marker-gated one-time rebuild (self-heal + model_guard re-stamp).
- **WP-XP-Docs** (PR #23 → `9384756`): LLM-self-sufficient docstrings for all XP tools (v0.9.0 gate).

## v0.8.0 — ✅ RELEASED & PINNED LIVE
WP-R3 (PR #13 → `8f3079f`) cut the version bump + CHANGELOG; Colton cleared both human checks (vendored-libs render ✓, owed v0.7.4 non-ASCII journal test ✓) and gave the go; **Loki pinned the marketplace to `6c2ce12`** (real ls-remote HEAD; marketplace commit `09e6ab0`; teammate-comms `53827f8` untouched). Rollback if needed = re-pin v0.7.4 `20519b9`. Document storage is live to users. (H-6 also resolved: `.cognition/journal.jsonl` committed, `.cognition/chromadb/` gitignored.)

## Post-v0.8.0 audit-remainder — shipped (both P1s done)
- **WP-T** (PR #14 → `cc9cd73`): tool-layer correctness + pyright ratchet — T-9 (lifespan accessor, baseline **29→8**), T-2 (honest uncurated count), T-3 (batch partial-commit guard), T-6 (unified node_type/direction error contract), C-5 (surface add_edge False). First tests for the previously-untested MCP tool layer.
- **WP-ID** (PR #15 → `d585a22`): GLOBAL node-id-collision data-loss fix (P1) — mint-on-collision in `add_node`'s locked block (replay-safe by construction, documented), unified out the D1a document salt-retry, post-commit hook commit-hash discriminator, embedding-id rebind closes the orphan-vector. TOCTOU **shrunk** (backlog #2 residual documented, not eliminated). Closes the last P1 audit item.
- **WP-Cap** (PR #16 → `9876b43`, P2): `cognition_get_node`, persisted edge `reason` (replayed), `cognition_update_node` with re-embed-on-any-whitelisted-change (closes search-staleness for both the match vector AND the displayed context/severity metadata), exposed `get_superseded_chain`/`get_incident_resolution`. Held once on the context/severity metadata re-embed gap; fixed.
- **WP-Core-tail** (PR #17 → `3961f32`, P3): C-4 journal-FIRST-then-mutate on all 8 write paths (a failing append leaves no phantom in-memory write; all 8 sites guarded by fails-before append-failure tests asserting the raw graph), C-6 (documented why appends don't advance the offset — would corrupt the C-3 prefix-hash; log reworded), C-7 (path-based cycle detection — diamonds no longer mislabeled). Composes with WP-ID's add_node mint. Held once to complete the regression-guard set.

## Tracked follow-ups (from the document-storage run)
- **Dashboard search over-query** (recall, LOW): the dashboard `search()` uses a FIXED `limit*5` over-query while the MCP `_search_cognition` is ADAPTIVE (the D2 B3 fix). Same starve class, far more remote at the dashboard default limit=20 (~100-chunk single-doc domination needed), recall-only, secondary surface. Unify the over-query logic (ledger 11) or document as accepted residual.
- **mime/agent-controlled-header hygiene** (discovery `9dc4a49ac093`): resolved in D4 for the download mime; the general rule (clamp every agent-controlled value that reaches an HTTP header/path/query sink at your own boundary, never rely on the parser) is filed as a lesson.

---

## New feature ideas (parking lot)

- **Doc-serving tools for the LLM/user — esp. gated on empty-graph detection** (Colton, 2026-06-21) — **✅ SHIPPED as WP-Readme** (PR #24, merged to main, release held): vibe-memory pioneered a pattern of MCP tools that exist specifically to SERVE DOCUMENTATION to the LLM (and through it, the user), most valuable when the server can DETECT the project has no memories/graph stored yet and proactively surface "here's what this is / how to begin recording." Adapt for vibe-cognition: an onboarding/explainer surface (a dedicated tool, or a `get_status` / `prime` path) that, on an empty or near-empty graph, serves start-here docs to the agent so it knows to begin capturing cognition rather than running blind. **Reference point: Reginald + the vibe-memory project** — that's where the pattern lives; ask Reginald / look at vibe-memory for the implementation. _(Meta: once the cross-project XP feature lands, this context becomes retrievable directly from vibe-memory's own graph without going through Reginald.)_

---

## Standing audits (recurring)

- **Tool-surface self-sufficiency audit** (Colton, 2026-06-21): every MCP tool and its description must let an LLM read it and know HOW to use it correctly **without any extra rules or prompting from the user** — the tool name, args, description, and return shape are the SOLE contract. **Re-run this audit every time a tool is added or changed** — it is not one-and-done; a new tool can quietly assume a convention the user would otherwise have to supply. Each pass: confirm every tool reads clearly in ISOLATION (no implicit user-supplied context required), args are unambiguous, return shapes are documented, and `"*"`/`project`/error semantics are stated. **Immediate scope:** the XP tools just shipped (`cognition_load_project` / `_unload_project` / `_list_projects`, and the new `project` arg on the read tools) — sweep those first. Pairs with the SKILL.md tool-table drift item (S-3).

---

## Audit remainder — proposed WP groupings (not yet briefed)

Priorities: **P1** ship-soon / high leverage · **P2** real correctness, lower urgency · **P3** polish/dead-code.

### WP-T · tool-layer correctness + pyright ratchet — ✅ SHIPPED (PR #14 → `cc9cd73`)
All five landed; pyright baseline **29 → 8**; the previously-untested MCP tool layer got its first tests.
- **T-9** ✅ `get_lifespan` accessor routed through all ~20 raw sites (baseline 29→9).
- **T-2** ✅ `storage.count_uncurated_nodes` (uncapped, mirrors the get-filter) → honest `total_uncurated`; free B2 test fix → baseline 8.
- **T-3** ✅ `isinstance(e, dict)` guard in the batch core — no more partial-commit crash.
- **T-6** ✅ one `_parse_node_type` + `_validate_direction`, unified `{"error": str}` shape (get_neighbors direction now an explicit error — intentional contract change).
- **C-5** ✅ `_add_edge_core`/batch surface `add_edge`'s False return.

### WP-ID · global node-id collision (data loss) — **P1** (surfaced by WP-D1a)
`generate_node_id` hashes `type:summary:timestamp`. Under a coarse clock (Windows ~15 ms),
two nodes of the **same type + same summary** recorded in one tick hash to the **same id**, and
`add_node` **silently overwrites** the first — silent data loss. WP-D1a fixed only the document
path (a local salt-retry in `_store_document`); `_record_node` (every decision/discovery/episode)
and the post-commit hook's hand-rolled id still collide. Discovery `e434566c8440`; defer decision
`0bd725b83bd0`. **Fix direction:** hoist a uniqueness loop (salt-until-`has_node`-free) **into
`add_node`'s locked block** (or a shared `storage.mint_unique_id`) so all writers benefit and the
cross-process `has_node`→`add_node` **TOCTOU** shrinks in the same change. Needs cross-writer
composition review — the post-commit hook reimplements id-gen (H-2 residue). Until then there's an
**interim asymmetry** (documents retry-on-collision, all other nodes overwrite) — tracked here so
it isn't mistaken for intent.

### WP-Cap · capability gaps — **P2** (synergy with the document track)
- **T-5** (MED, gap): no `cognition_get_node` (full `detail` unreadable after a search hit) and `update_node` is implemented+tested but unexposed. *Caveat: nothing re-embeds after `update_node` — must ship a re-embed path or search serves a stale summary forever (pairs with E-2). The document track's `get_document` is the graph's first get-by-id surface (audit G1) — coordinate so `get_node` isn't built twice.*
- **T-4** (MED, gap): curation `reason` is requested by the edge-analyzer then discarded — no field on `CognitionEdge`, dropped by the batch tool. Also the skill's top-level `source` tag doesn't reach the per-edge object (curated edges mislabel as `"batch"`). Persist the reason; fix the source plumbing.
- **T-11** (LOW, dead-code): `get_superseded_chain` / `get_incident_resolution` exported, tested, called by nothing — and the remove-node tool *recommends* `supersedes` chains no tool can traverse. Surface (synergy with WP-D1 versioning) or prune. (`get_incident_resolution` also has identical if/else branches.)

### WP-Emb · embeddings correctness — **P2**
- **E-3** (MED, bug): documents embedded with the **query** prefix (`generate_query_embedding`) instead of the document prefix — discards nomic's asymmetric-retrieval training, degraded ranking. **Fix requires a one-time collection re-embed (vector spaces incompatible) — coordinate, it invalidates all existing vectors.**
- **E-4** (MED, gap/inferred): two sessions open the same ChromaDB dir; PersistentClient isn't multi-process-safe (sqlite-lock errors feed E-2.3, stale per-process caches).
- **E-6** (MED, dead-code): code-search heritage — `bulk_upsert`, `delete_by_file/_by_repo`, `get_by_id`, `get_content_hashes`, `vector_search`'s never-written `repo`/`file_path_prefix` params, default `collection_name="code_embeddings"`. Prune, or put `bulk_upsert`+`generate_batch` to work in E-8.
- **E-5/E-7/E-8** (LOW–MED): backend/model-mismatch undetectable (no model/dims recorded in the collection); `datetime.utcnow()` deprecated; **`trust_remote_code=True` with no pinned `revision=`** (unpinned remote HF code at model load — worth pinning); error-shape mix in `vector_search`/`delete_embedding`; one-node-per-loop startup sync while `generate_batch`/`bulk_upsert` sit dead.

### WP-Core-tail · remaining core bugs — **P2/P3**
- **C-4** (MED, bug): mutate-then-journal with no rollback — if the append raises (disk full, AV lock), the in-memory graph keeps a phantom write nothing else will ever see. Journal-before-mutate ordering.
- **C-7** (LOW, bug): `get_reasoning_chain` marks diamonds (A→B→D, A→C→D) as cycles — `visited` is global to the traversal and never popped. Only linear chains are tested.
- **C-6** (LOW, noise): self-replay logs "caught up: +1 entries" on every process's own appends (`_append_journal` doesn't advance `_offset`). Benign; comment or advance offset on self-writes.

### WP-Dash · dashboard — **mostly SHIPPED in WP-D4**
- **D-1** ✅ DONE (WP-D4): `start_dashboard` polls `server.started`, returns failed (not a dead URL) if it never comes up.
- **D-2** ✅ already-fixed pre-D4 (ledger 23 re-verify: `_find_free_port` returns the bound port, not 0).
- **D-4** ✅ DONE (WP-D4): cytoscape/cose-base/fcose/layout-base vendored into `static/vendor/` — no CDN/SRI gap, works offline. (Human render-check owed at the v0.8.0 cut.)
- **D-6** ✅ DONE: SAFETY filter (WP-D2) + NAVIGATION dedupe-to-node + hydrate (WP-D4). (discovery `c7d948583b4e` resolved.)
- **D-5 (partial)** ✅ DONE in WP-D4: `secrets.compare_digest`, malformed-body→400, limit clamp. **OPEN (LOW):** `[::1]` IPv6 host-check, duplicate `type`/`edge_type` keys, unused `context`/`severity` in graph payload, hardcoded-port dedup, ExitStack-close-on-join-timeout.
- **D-3 (partial):** D-3a Refresh button ✅ DONE (WP-D4); D-3d deleted-episode-in-sidebar ✅ already-fixed (ledger 23). **OPEN (MED/LOW):** auto-poll, `--no-embeddings` "disabled" banner, search-wiring-attaches-only-on-init-success (a coherent UI-state cluster — own WP if pursued).

### WP-Doc/Skill — **P3**
- **S-2** (MED, bug): `agents/plan.md` frontmatter grants `Write, Edit` while the body says "READ-ONLY"; its MCP tool names (`mcp__vibe-cognition__*`) may not match the plugin namespace (`mcp__plugin_vibe-cognition_vibe-cognition__*`) — the Plan agent may get none of its cognition tools. *Verify namespacing in a live session before fixing.*
- **S-3** remainder (LOW): README standalone-dashboard instructions don't work for plugin users; SKILL.md tool table lists 10 of 15; edge-type list drifts (3 vs 4 vs 5); vibe-backfill says "consider" curate vs the MANDATORY rule; `instructions.py` claims prime is stdlib-only (imports pydantic/networkx) and references a non-existent PreCompact hook. (CHANGELOG ✓ done in WP-1.)
- **H-5** (LOW): both SessionStart entries fire on compact — re-running migrate_mcp post-compact is waste. Scope the first matcher or comment the intent.

### WP-Hooks-tail — **P3** (+ one human-gated, one decision)
- **H-2 remainder** (gap): the journal *format* fork is closed (WP-4 shared helper), but trigger heuristics stay loose — matcher is `"Bash"` only (**misses PowerShell commits — relevant on this machine**), fires on any command *containing* `git commit`, doesn't check command success. Plus the hook still has zero tests.
- **H-3 remainder** (gap): the fatal-failure case is fixed (`session-start.sh:59` now `… || true`), but sync/prime/reinject still discard stderr to `/dev/null` — no diagnostic breadcrumb when something fails. A log under `.cognition/` or plugin-data would make these diagnosable.
- **H-4** (gap, inferred — **human-machine gate**): first-install race (server `uv run` self-sync vs hook `uv sync` on the same multi-GB torch venv); `plugin.json` hard-codes the venv path with no fallback. Per constraint `b8ec24fe9107`, verify on a human machine at a release.
- **H-6 remainder** (LOW): `backfill.py` unused `json` import + unexposed `days` param + git failures swallowed; `migrate_mcp` would skip a UTF-8-BOM `.mcp.json` (`utf-8-sig` would migrate it); redundant hatch `force-include`; stale `session-start.sh` header comment.

### WP-Test · the coverage hole — **P1-infra**
- **T-1** (MED, gap): the entire 15-tool MCP layer has zero tests — every contract bug in WP-T/WP-Cap lives in that untested layer. Also untested: `prime.py` (its stdout *is* the hook payload), `post-commit.py` (journal format contract unpinned), `config.py` (the whole plugin-launch story), `instructions.py`, `server.py` lifespan. **Cross-process append test** (current `TestJournalCatchUp` is single-process) would shrink the human-machine gate. *Natural companion to WP-T — write the tool suite alongside the tool fixes.*

---

## Decisions needed from Colton

1. **H-6 — `.cognition/` commit-vs-ignore (chromadb).** The intent (commit the journal, gitignore
   `chromadb/`) is half-implemented: `.gitignore` has **no `.cognition/chromadb/` line** and
   `.cognition/` itself sits untracked, while `config.py:98/103` docstrings disagree (one says
   "Git-committed", one says "gitignored"). Decide the policy, then align `.gitignore` +
   docstrings + README in one pass. (Low effort; just needs the call.)

## Owed / tracking

- **v0.7.4 post-pin machine test** (Colton, AM): non-ASCII commit message → clean journal, on a
  real Windows machine (install-mechanics gate `b8ec24fe9107`). Rollback if it fails = re-pin to
  v0.7.3 SHA `d6c2f45`. Pin is **live** at `20519b9`.

---

## Shipped ledger (audit finding → release)

| Finding(s) | Shipped in |
|---|---|
| E-1 telemetry; H-6 LICENSE/CHANGELOG/httpx/dev-dup/einops-doc/authorship/`__version__`/`.ruff_cache`; ruff 20/23; T-10 stale comments | v0.7.3 (WP-1) |
| CI (ruff+pyright-ratchet+pytest matrix); B-4 CPU torch; C-3 `-text`/`merge=union` defense | v0.7.3 (WP-2) |
| H-1 hook interpreter; non-ASCII commit UTF-8 decode; B-3 Windows venv fallback; S-1 skill paths | v0.7.3 (WP-3) |
| C-1 cross-process atomicity; C-2 short-write; C-3 replacement detection; H-2 journal-format fork | v0.7.4 (WP-4) |
| WP-5 upgrade-brick detection (ledger 19) | v0.7.4 (WP-5) |
| H-3 fatal-failure guard (`|| true`) | v0.7.4-era hooks |
| WP-D1a document storage (reference mode, sidecar, DOCUMENT type, store/get, dedup, graph-inert + sync-path guards, sidecar deletion) | main `870ff09` (PR #8) — ships in v0.8.0 |
| WP-D1b document storage (matcher pair rules, shared identity predicate, copy mode + size/git policy, per-blob-path refcounted deletion, N1 ghost-search fix) | main `0faf302` (PR #9, pinned `3f6cdf5`) — ships in v0.8.0 |
| WP-D2 chunked document search (chunk embeddings + is_chunk count-split, adaptive over-query + dedupe + matched_excerpt, re-sync/backfill, dashboard N1 safety filter) | main `dd11cd2` (PR #10, pinned `63f2246`) — ships in v0.8.0 |
| WP-D3 /vibe-document skill (S4/N3 link-by-doc_ref workflow) + doc-surface fixes (all 17 tools, edge-type accuracy, relates_to 3-provenance) + doc-drift guard test | main `9afc538` (PR #11, pinned `c3b1bbf`) — ships in v0.8.0 |
| WP-D4 dashboard document list + token-gated path-safe download (traversal+header-injection hardened) + D-6 nav + D-1 liveness + D-4 vendored libs + D-5 security | main `6939c99` (PR #12, pinned `7f07dea`) — in v0.8.0 |
| WP-R3 v0.8.0 release commit (4-file version bump + CHANGELOG) | main `8f3079f` (PR #13) — **v0.8.0 PINNED LIVE at `6c2ce12`, marketplace `09e6ab0`** |
| WP-T tool-layer correctness + pyright ratchet (T-9/T-2/T-3/T-6/C-5; pyright 29→8; first tool tests) | main `cc9cd73` (PR #14, pinned `7e090ae`) |
| WP-ID global node-id-collision data-loss fix (mint-in-add_node, replay-safe, hook discriminator, embedding-id rebind) | main `d585a22` (PR #15, pinned `1494a9c`) — last P1 closed |
| WP-Cap capability gaps (get_node, edge reason persist, update_node+re-embed, expose superseded/incident queries) | main `9876b43` (PR #16, pinned `9d8a8f8`) — last P2 |
| WP-Core-tail core robustness (C-4 journal-first all write paths, C-6 offset-noise documented, C-7 path-based cycle detection) | main `3961f32` (PR #17, pinned `c4b4aa1`) — P3 |
| WP-Dash-tail (adaptive over-query unify, --no-embeddings disabled, IPv6 host-check, D-3/D-5 tidies) | main `2c33f54` (PR #18) — v0.9.0 |
| WP-Emb non-E-3 (E-6 prune + pyright 7→0, E-7 revision-pin + datetime.now(UTC); E-4 dropped — Rust backend swallows locks) | main `cd81769` (PR #19) — v0.9.0 |
| WP-XP0 spike (coexistence, handle release, foreign-read safety, dim/model guard, catch-up cost — GO Option A) | main `d1576fe` (+ docs `cbebd47`) |
| WP-XP1 cross-project registry (model/dim stamp, close() handle release, open_existing, LoadedProjects, load/unload/list) | main `05b0797` (PR #20) — v0.9.0 |
| WP-XP2 cross-project read routing (project= on search + read tools, project_notes provenance, write-isolation) | main `8a0a799` (PR #21) — v0.9.0 |
| WP-E-3 doc embedding prefix QUERY→DOCUMENT + marker-gated one-time re-embed (self-heal + model_guard re-stamp) | main `e679857` (PR #22) — v0.9.0 |
| WP-XP-Docs LLM-self-sufficient docstrings for all XP tools (v0.9.0 gate) | main `9384756` (PR #23) — v0.9.0 |
| **v0.9.0 release** (cross-project cognition XP0→XP2 + E-3) | code `0c2c52f` — **PINNED LIVE, marketplace `c563ff2`**; installed + verified on Colton's machine |
| WP-Lint (heal main: regen uv.lock for 0.9.0 + clear 39 masked ruff violations) | main `0be175a` (PR #25) — release held |
| WP-Readme (cognition_readme tool + empty-graph onboarding via prime.py) | main `9b14d88` (PR #24) — release held |
