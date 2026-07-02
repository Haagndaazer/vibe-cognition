# Fable Audit — Stage 2: Interconnectivity Audit — 2026-07-01

## Intended purpose (confirmed with human)

Vibe Cognition is a fully local MCP server plugin for Claude Code that gives a codebase persistent, structured memory — a git-committed knowledge graph (`.cognition/journal.jsonl`) of decisions, failures, discoveries, constraints, incidents, patterns, workflows, and open tasks — so future Claude Code sessions (and human teammates) understand why the code is the way it is, without re-litigating settled choices or repeating known failures. Primary users are developers using Claude Code daily, and multi-agent / multi-human team collaboration is a first-class use case, not an edge case. Local embeddings (no API keys) power semantic search; the browser dashboard is a secondary, nice-to-have surface — the graph, MCP tools, hooks, and skills are the primary surfaces. Curation is agent-driven via /vibe-curate by current design (it could be automated again in the future, but that is not the current design). Success for a new consumer: install from the marketplace, restart Claude Code, and within a session or two see Claude spontaneously recording history, retrieving relevant context at session start, and — after /vibe-curate — having a browsable, linked graph.

## Scope of this stage

Five Sonnet 5 auditors, each tracing one SEAM (split by boundary/data-flow rather than by system so nothing fell between scopes): (1) write-path data flow (record/task/document → journal → graph → embeddings → search, plus delete/update propagation), (2) process topology (N same-project servers + dashboard + hook subprocesses + CLIs over shared journal/ChromaDB/venv), (3) env/config contract (Claude Code env vars → plugin.json → hooks → config.py; release/version seam), (4) context-injection pipeline (prime, server instructions, readme, skills — overlap/ordering/compact), (5) git ↔ journal ↔ graph (merge=union vs replay, commit-ref deterministic edges, backfill dedup). Each brief listed the Stage 1 filings to avoid re-reporting. 13 raw findings synthesized below.

## Findings

### Two same-project servers can double-wipe each other's embeddings via the unguarded collection-recreate migration  [severity: high]  [type: bug]
- **What:** `ChromaDBStorage.__init__` never stamps `embed_scheme` on a freshly created collection — only `recreate_collection()` adds the stamp. Each server's background init thread checks a **process-local, startup-frozen** metadata snapshot and, if unstamped, calls `recreate_collection()` (delete-by-name + recreate). Because fresh collections are unstamped, this fires on **every new install**, not just legacy upgrades; two sessions opened on the same project within the 2–30s model-load window both decide "not stamped" and the second delete-recreates after the first has synced — silently wiping its vectors and invalidating its collection handle (swallowed by a broad `except` that just sets `embedding_error`).
- **Evidence:** `src/vibe_cognition/embeddings/storage.py:43-77`; `src/vibe_cognition/server.py:184-235` (esp. 219-225). Code-reading-derived; not reproduced live (read-only audit).
- **History:** `wp-emb-e4-discovery-chromadb-rust-lock` covers only low-level SQLite lock contention; `xp0-q3-concurrent-foreign-read` tested cross-project read-only attach and concluded "no retry wrapper needed" — same-project write-write was never tested. `git_hygiene.py:63-90` solves the structurally identical "one-time migration across concurrent processes" problem with a file lock + stale-lock cleanup — inconsistent discipline between two identical seams. Oversight.
- **Impact:** Silent embedding loss in the mundane case of opening two windows on one project. A live two-process repro would also satisfy part of T-1c (`c34c788b8d5b`).
- **Root cause / Fable's read:** The migration was written as if it were process-exclusive; stamping at creation time (making the trigger legacy-only) plus a file lock in the `recreate` call site closes both the every-install trigger and the race.

### Compact silently drops the entire prime digest; only the static rules come back  [severity: high]  [type: broken-assumption]
- **What:** `startup|resume|clear` injects the real data digest (constraints, open tasks, patterns, decisions, incidents) via prime; `compact` reinjects only the static `SERVER_INSTRUCTIONS` (the three standing practices). The matchers are mutually exclusive; nothing restores the backlog after compaction and nothing tells the agent its task/constraint view is stale. The skills explicitly bill session-start task injection as the reason no TODO file is needed ("the graph itself IS the backlog").
- **Evidence:** `hooks/hooks.json:5-24`; `src/vibe_cognition/instructions.py:50-63`; `src/vibe_cognition/cognition/prime.py:170-186,224-241`; `skills/vibe-cognition/SKILL.md:62-68`.
- **History:** Decision `7e9665028bb1` (+ discovery `082148c5da25`) scoped the reinject narrowly to "MCP instructions may not survive compaction"; no node ever weighed losing prime's data payload. Unexamined gap, not a trade-off.
- **Impact:** Long sessions — exactly where a persistent backlog matters — silently lose it mid-flight. The cheap fix is to also run a trimmed `generate_prime()` (or at least a "re-fetch via cognition_list_tasks" cue) in the compact hook.

### Merge-shaped journal replay silently drops edges/removals — no log, no retry, permanent  [severity: high]  [type: bug]
- **What:** `_replay_entry` processes lines strictly in file order; for `add_edge`/`remove_edge`/`remove_node`, if a referenced node isn't yet in the graph, the action is silently dropped (no log — malformed JSON at least logs) and the offset advances past it forever. `merge=union` — the officially supported separate-clones mechanism — can interleave divergent branches' blocks so an edge line precedes its endpoint's node line. No test constructs a merge-shaped journal (edge-before-node, duplicate add_node); existing tests cover whole-file replacement detection and live-API paths only.
- **Evidence:** `storage.py:1001-1029` (no else/log branch), `:900-919` (single forward pass); `tests/test_journal_concurrency.py` (no reordering tests).
- **History:** `383d9e3c65b6`, `4ed473ba9c75`: the maintainers' own topology is shared-checkout + manager-flush, which never exercises a real git merge — `merge=union` is maintained for a topology the team doesn't use. Blindspot. (F1 likelihood depends on git's actual union-merge ordering, not empirically reproduced; the finding is zero defense/detection if it occurs.)
- **Impact:** The officially supported separate-clones team can permanently and invisibly lose edges — including deterministic `part_of` links — after an ordinary git merge.

### The startup embedding reconciler is a second, drifted implementation of the embed contract  [severity: high]  [type: bug]
- **What:** `_sync_cognition_embeddings` reuses `_embed_document` for documents but reimplements the entity embed inline for everything else — omitting `status`/`owner` from task embed text/metadata (which `_embed_entity_node`, documented as "THE single shared embed path", includes) and routing **workflow** nodes through the generic branch (one untruncated vector, never `_embed_workflow`'s chunking). Nodes created while `embeddings_ready` is false (the 2–30s model-load window; `_add_task` deliberately defers to "startup sync catches it later") get their first and only vector from this drifted path; the sync never revisits present nodes. Tasks self-heal only on a later `update_task`; workflows have NO repair path (in-place edit refused by design — supersession only).
- **Evidence:** `server.py:108-124` vs `cognition_tools.py:58-98` (`_embed_entity_node`), `:101-136` (`_embed_workflow`); `cognition_tools.py:981` (deferred task embed), `:791-800` (workflow edit refusal).
- **History:** Pattern `c6f92230831c` records the deliberate single-shared-path intent; WP-Task-Node (`4ec20f6d21eb`, `21fa79e52827`) extended `_embed_entity_node` but missed the pre-existing duplicate in server.py. Oversight — the reconciler violated the exact invariant it exists to restore.
- **Impact:** Permanently degraded search for tasks/workflows created in a routine window. Distinct from replay-no-embed and E-8 (different mechanisms, both Stage-1-filed).

### `REPO_PATH=""` silently redirects the whole graph to the plugin's install directory  [severity: medium-high]  [type: broken-assumption]
- **What:** `plugin.json` sets `REPO_PATH=${CLAUDE_PROJECT_DIR}` unconditionally. If substitution ever yields an empty string, pydantic-settings treats present-but-empty as an explicit value (no `env_ignore_empty`), skipping the `CLAUDE_PROJECT_DIR`→cwd fallback factory entirely; `Path("")` = process cwd = the plugin root (via `uv run --directory`). Empirically verified against the live Settings class: no error, `.cognition/` lands in the plugin checkout. The same env var is read three ways across the codebase — `config.py`/`prime.py:204`/`backfill.py:81` are all vulnerable; only `dashboard/cli.py:89` (`.get(...) or cwd`) is accidentally guarded — proof the empty case was never a decision.
- **Evidence:** `config.py:12-30`; `plugin.json:21`; `prime.py:204`; `backfill.py:81`; `dashboard/cli.py:89`.
- **History:** `a906f12a6ef7`, `d0362d89d295` cover adjacent seams; nothing considers present-but-empty. Constraint `b8ec24fe9107` (install mechanics gate on a human machine) is the class of gate that should catch this but its recorded steps never exercise a missing/empty `CLAUDE_PROJECT_DIR`. Blindspot; trigger probability unconfirmed (depends on Claude Code's substitution behavior), mechanism proven.
- **Impact:** Silent total misdirection of the product's core artifact. Cheap hardening: `env_ignore_empty=True` + validate `repo_path` non-empty, and unify the three read patterns.

### No dedup contract for episodes citing the same commit — and the repair edge is forbidden by the curate skill  [severity: medium-high]  [type: gap]
- **What:** Nothing prevents two clones/agents from independently minting distinct episodes for the same `commit:<sha>` (`_record_node` does no `find_nodes_by_ref` lookup first — contrast documents' explicit sha-dedup). Both survive any merge forever (replay is purely additive). The one edge type built for the aftermath — `duplicate_of` — is refused by both edge tools AND explicitly disallowed by /vibe-curate's own instructions, so even a curator reviewing both duplicates side-by-side cannot mark them. `redirect_edges` exists but is unreachable from any workflow.
- **Evidence:** `cognition_tools.py:159-178`; `storage.py:122-142` (document dedup, the contrast), `:339-391`; `skills/vibe-curate/SKILL.md` Step 2; `backfill.py:14-21` (local-view advisory scan only).
- **History:** WP-ID (`bdc17a401bf0`) solved same-journal id collisions and explicitly flags cross-clone minting as residual — but for merge topologies this isn't a narrow race, it's routine whenever two clones act on the same untracked commit before syncing. Stage-1 task `4800d5d16adb` treats duplicate_of purely as dead-code cleanup — this finding upgrades its motivation. Oversight.
- **Impact:** Duplicate history that the system can neither prevent, detect, nor heal — "scrambled convergence" in the first-class team scenario.

### Deterministic-edge backfill skips any node with ANY edge — cross-clone linkage stays permanently partial  [severity: medium]  [type: blindspot]
- **What:** `_create_deterministic_edges_for_edgeless` (startup-only) skips nodes that have any edge at all, not nodes missing the deterministic edges their references warrant. A node linked locally to its own episode is never reconsidered for the `part_of` edge to a teammate's later-merged episode sharing the same `commit:` ref; `get_edgeless_nodes` can't surface it either (same coarse predicate).
- **Evidence:** `server.py:22-49` (esp. 34-44); `cognition_tools.py:2299-2309`.
- **History:** No node discusses the skip condition's coarseness. Blindspot in the cross-clone path the feature exists for.
- **Impact:** Teammates' knowledge about the same commit sits unlinked unless chance semantic curation finds it — the deterministic mechanism designed for this case doesn't cover it.

### Document chunk completeness is probed by chunk-0 only — a crash mid-chunking reads as "fully synced" forever  [severity: medium]  [type: gap]
- **What:** `_embed_document`/`_embed_workflow` are delete-then-write loops; the startup reconciler's completeness check is `"<id>#chunk-0" present`. A crash after chunk-0 but before chunk-N leaves the document permanently under-indexed with no signal anywhere.
- **Evidence:** `server.py:104-106`; `cognition_tools.py:500-534,101-136`.
- **History:** Graph silent — oversight; adjacent to but distinct from the replay/sync gaps.
- **Impact:** Searches matching later chunks silently never surface; nothing in get_document/search/dashboard indicates partial coverage.

### `.env` resolves against the shared plugin root, not the project — per-project overrides inert, plugin-root `.env` leaks globally  [severity: medium]  [type: blindspot]
- **What:** `Settings` uses `env_file=".env"` (relative → resolved against process cwd = `${CLAUDE_PLUGIN_ROOT}` for every project's server). A `.env` in the user's repo does nothing; a `.env` at the plugin root silently configures EVERY project sharing the install (embedding model, prime knobs, …).
- **Evidence:** `config.py:23-30`; `plugin.json:11-23`.
- **History:** Graph silent; the field default predates the shared-plugin architecture. Oversight.
- **Impact:** Hard-to-diagnose config drift; the "config env leakage" class. Fix: pin `env_file` to `repo_path/.env`, or drop it.

### No automated check that pyproject.toml and plugin.json versions match  [severity: medium]  [type: gap]
- **What:** The release procedure requires bumping both by hand; CI (`ruff`/`pyright`/`pytest`/`uv sync --locked`) never asserts they agree. `uv sync --locked` catches only pyproject-vs-uv.lock drift (discovery `a37a60664cfc` — the team already hit an adjacent version-drift class). Currently in sync by convention only.
- **Evidence:** `pyproject.toml:3`; `.claude-plugin/plugin.json:3`; `.github/workflows/ci.yml` (no check); no test references plugin.json.
- **History:** H-6 hardening closed several release-hygiene items but not this. Oversight adjacent to completed work.
- **Impact:** A forgotten bump ships a plugin whose displayed version misrepresents the code Loki pins.

### Document blob + sidecar are written before the owning node is journaled — crash orphans artifacts unreclaimably  [severity: low-medium]  [type: broken-assumption]
- **What:** In `_store_document`, `write_text_sidecar` (line 626) and blob materialization (633-642) precede node minting/journaling (682) — inverting the journal-first discipline (C-4) used for all graph mutations. A crash in between leaves artifacts referenced by nothing; delete-time reclaim only walks down from a node, and no reconciler scans for ownerless artifacts. If `store_copy=True` without `local_only`, the leaked file sits in the git-committed tree.
- **Evidence:** `cognition_tools.py:537-698`; `operations.py:81-111`; `documents.py:104-118` (write_blob atomic — retry-safe, but only if retried).
- **History:** Graph silent; distinct from Stage-1 `07fdfe725e7f` (ordering within `_materialize_blob`). Oversight.
- **Impact:** Quiet disk/git leak; asymmetry — reclaim-on-delete exists, reclaim-on-never-created doesn't.

### Onboarding is stated in three independently-maintained channels; the v0.13.0 trim never counted the always-on instructions tax  [severity: low]  [type: gap]
- **What:** `SERVER_INSTRUCTIONS` (every session, unconditional), prime's `ONBOARDING_BLOCK` (empty graph), and `cognition_readme`'s guide all restate the record→curate loop in separately-maintained prose — no contradiction yet, but the exact shape that produced the Stage-1 skill drift, concentrated at the highest-friction first session. Separately, the "~1346→~634 tok (~53%)" trim accounting (CHANGELOG, decision `ef95333d105c`) is scoped to `generate_prime()` alone and excludes the ~200-300-token `SERVER_INSTRUCTIONS` paid every session AND on every compact — a comparable fraction of the post-trim payload; the WP brief (`doc:55ffe81753ad`) never mentions instructions.py, out of scope by omission.
- **Evidence:** `instructions.py:19-43`; `prime.py:225-226`; `readme.py:7-146`; `server.py:323-326`; CHANGELOG.md:15-24.
- **History:** `b840621ce2d2` centralized the standing rules precisely to avoid restatement; WP-Readme (`bd745214cb69`) later added the third channel without a cross-check. Oversight.
- **Impact:** Drift-in-waiting + a first-session token tax; the trim narrative understates the true session-start floor.

### Verified-coherent seams (no finding)
- In-process delete cascade (edges → node vector → chunk vectors → sidecar/blob refcount reclaim) is coherent end-to-end.
- `update_node`/`update_task` re-embed via the shared path, including metadata-only changes (deliberate, `986687c1ed27`); `mark_curated` correctly skips re-embed; edge writes correctly never re-embed.
- Connect-time vs compact-reinject instructions text is one constant — no drift between those two paths; H-5 double-fire is fixed and verified.
- Journal append atomicity, git-hygiene file locking, venv-sync guarding, cross-project read-only attach — deliberately hardened, confirmed intact.
- Hatch force-include noted in the June audit has since been removed; packaging matches (only `packages = ["src/vibe_cognition"]`).

## Summary & Recommendations

**Theme 1 — The supported-but-unused topology is the least defended.** Merge-shaped replay drops (silent), cross-clone episode duplication (no dedup, repair edge forbidden), and the coarse edgeless-sweep all fail specifically in the separate-clones topology that `merge=union` officially supports — and the maintainers' own shared-checkout workflow never exercises it. Either invest in merge-shaped replay tests + dedup contracts, or honestly de-scope separate-clones support in the docs. This extends Stage 1's "multi-agent is first-class in intent, second-class in verification" with a sharper point: the gap is topology-specific.

**Theme 2 — Repair paths drift from the canonical paths they repair.** The startup reconciler reimplements the embed contract (dropping task status/owner and workflow chunking); its completeness probe checks one chunk. The codebase's own "single shared embed path" pattern node states the invariant; the reconciler violates it. Recommendation: reconcilers must CALL the canonical write path, never reimplement it — and a small "reconciler == writer" test would lock that.

**Theme 3 — Cross-process discipline is applied inconsistently to structurally identical problems.** git_hygiene's one-time setup takes a file lock; ChromaDB's one-time recreate doesn't (and fires on every fresh install due to the missing creation-time stamp). One codebase, two answers to the same question.

**Theme 4 — The context pipeline restores rules but not state.** Compact reinjects the constitution and silently drops the backlog; three channels restate onboarding while no channel owns it. Recommendation: make the compact hook re-run a trimmed prime (or inject a staleness cue), and pick one source of truth for the onboarding loop.

**Theme 5 — The host contract is held by convention, not validation.** Empty `REPO_PATH`, plugin-root `.env`, hand-synced versions: each is one unchecked assumption about the host environment away from silent misbehavior, and the install-mechanics human gate doesn't currently enumerate them.

## Potential tasks (checklist)

- [ ] Stamp `embed_scheme` at collection creation + file-lock the recreate migration (kills both the every-install trigger and the two-session wipe race); add the two-process repro test (partially satisfies T-1c) — priority: high
- [ ] Compact hook: re-run trimmed `generate_prime()` (or inject an explicit "backlog/constraints stale — re-fetch" cue) — priority: high
- [ ] Merge-shaped replay defense: log dropped add_edge/remove_* actions, add a deferred-retry pass for out-of-order lines, add merge-shaped journal tests (edge-before-node, duplicate add_node) — priority: high
- [ ] Route `_sync_cognition_embeddings` through `_embed_entity_node`/`_embed_workflow` (delete the inline reimplementation); add reconciler==writer parity test — priority: high
- [ ] Harden REPO_PATH: `env_ignore_empty=True`, validate repo_path non-empty + exists, unify the three env-read patterns (config/prime/backfill vs dashboard-cli) — priority: high
- [ ] Episode dedup contract for commit refs: ref-lookup before mint (warn/reuse), or merge-time duplicate detection; make `duplicate_of` reachable (implement merge or amend the curate-skill ban) — upgrades Stage-1 task 4800d5d16adb's motivation — priority: normal
- [ ] Deterministic-edge sweep: re-evaluate nodes against the full reference index (missing-deterministic-edges predicate), not has-any-edge — priority: normal
- [ ] Chunk completeness: record expected chunk count in node-level Chroma metadata and verify the full set in the reconciler — priority: normal
- [ ] Pin `Settings.env_file` to `repo_path/.env` or remove it; document config scoping — priority: normal
- [ ] CI step asserting pyproject.toml version == plugin.json version — priority: normal
- [ ] Journal-first ordering for document artifacts (or an orphan-artifact reconciler scanning .cognition/documents/ + sidecars for ownerless files) — priority: low
- [ ] Consolidate onboarding to one source of truth; include SERVER_INSTRUCTIONS in session-start token accounting — priority: low
