# Vibe Cognition — Backlog

**Maintained by:** vince (planner/manager). **Source of truth** for what's shipped, what's
in flight, and what's queued. Derived from `docs/AUDIT-2026-06-10.md` (the ~70-finding audit)
and `docs/DESIGN-document-storage.md` (the v0.8.0 feature spine).

**Convention:** the proposed WP groupings below are a *triage inventory*, not briefs. Each WP
gets a peer-reviewed execution plan (a `docs/wp-*-plan.md`) before it's assigned to Vorpid, and
each ships through the standard gate (SHA-pinned merge, fix+proof same commit, voiding clause,
journal-flush-via-worktree). Last updated 2026-06-13 (post v0.7.4 pin).

---

## In flight

| WP | Scope | State |
|----|-------|-------|
| **WP-D1** | Document storage: reference-mode + opt-in blob store, sidecar, DOCUMENT type + matcher pair rules, store/get tools, dedup, deletion incl. chunk purge + **ghost-search fix (N1)**, extension sanitization, gitattributes, tests | **Assigned to Vorpid** (queued; brief = DESIGN doc §1–§9). Branch `fix/wp-d1-document-store` off main. Acceptance = §9 seam-gate findings. |

## Committed feature spine → v0.8.0

| WP | Scope |
|----|-------|
| WP-D2 | Chunked ChromaDB embeddings + search excerpts, teammate re-sync chunking, `get_status` node-vs-chunk count split, multi-call text append if needed |
| WP-D3 | `/vibe-document` skill (store → descriptor entities → curate), README/SKILL docs (the `doc:<hash>` citation guidance is load-bearing, not optional — see DESIGN S4/N3) |
| WP-D4 | Dashboard: document list + token-gated download — **folds in the open dashboard audit findings D-1…D-5** (see below) |

Version **0.8.0** cuts when D1–D4 land.

---

## Audit remainder — proposed WP groupings (not yet briefed)

Priorities: **P1** ship-soon / high leverage · **P2** real correctness, lower urgency · **P3** polish/dead-code.

### WP-T · tool-layer correctness + pyright ratchet — **P1**
Tight cluster, all in `tools/`, composes cleanly, and **lands a big CI win**: T-9 alone removes 22 of
the 31 pyright baseline errors, dropping the ratchet toward strict.
- **T-9** (MED, improvement): one `lifespan_context` accessor in `tools/utils.py` (raise if `request_context` is None) routed through ~22 sites — kills 22 pyright errors and dedupes the pattern. *Lower `.github/pyright-baseline.txt` in the same PR.*
- **T-2** (MED, bug): `total_uncurated` silently caps at 500 (`storage.py:382` hard-cap vs the tool asking for 999999) — reported backlog can never exceed 500.
- **T-3** (MED, bug): `add_edges_batch` crashes mid-batch on a non-dict element *after* earlier edges are journaled (partial commit). Needs `isinstance(e, dict)` guard.
- **T-6** (MED, inconsistency): error-contract split — error-dicts vs raised exceptions vs silent wrong answers across tools; `node_type` validated three ways. One shared `_parse_node_type` helper fixes most.
- **C-5** (LOW, bug): tool-level TOCTOU — `add_edge` return ignored, reports `{"created": True}` even when storage declined.

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

### WP-Dash · dashboard (fold into WP-D4) — **P2**
- **D-1** (MED, bug): `start_dashboard` never verifies the server started — bind failure dies silently in the daemon thread, tool reports a dead URL all session. Check `server.started`/`thread.is_alive()`.
- **D-2** (MED, bug): `port=0` returns the literal `0` instead of `getsockname()[1]` on the preferred-port path.
- **D-3** (MED, gap): UI fetched once at load — goes stale immediately during an active session (no refresh/poll); `--no-embeddings` leaves "Loading embedding model…" forever; deleted episode lingers in sidebar.
- **D-4** (MED, improvement): CDN cytoscape/fcose with no SRI — CDN compromise runs arbitrary JS in a page that can DELETE nodes; also breaks offline. Vendor into `static/` (already wheel-included).
- **D-5** (LOW): malformed-body 500s vs JSON 400s, host-check rejects `[::1]`, token compare not `secrets.compare_digest`, duplicate keys in payloads, etc.

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
