# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.21.0] — 2026-07-15

**WP-TC7: prime-triggered new-user onboarding.**

### Added
- **Onboarding notice in the session-start prime digest** — when the current git identity's email resolves but has no matching person node, `generate_prime` prepends a `## New Here?` notice (pinned first, ahead of `## Active Constraints`) prompting the agent to gather the human's name/role/seniority/manager and call `cognition_register_person` with `from_agent=false`, or, if the human declines, append their casefolded email to the new per-machine, git-ignored `.cognition/onboard-declined` file instead of creating a placeholder person node.
- **`prime_onboard` config knob** (`PRIME_ONBOARD` env var, default `true`) — kill switch for the notice, wired through `Settings` and `PrimeConfig` alongside the existing `prime_*` trim knobs.
- `.cognition/.gitignore` now also lists `onboard-declined` (git-hygiene writer bumped to v3; existing installs pick up the new rule via the standard versioned re-run).

### Notes
- Mutually exclusive with the empty-graph `ONBOARDING_BLOCK`: `main()`'s empty-graph branch never calls `generate_prime`, so the two onboarding paths never both fire in the same session.
- The decline file is read-only to the server/prime process; nothing new writes it programmatically — an agent appends to it with an ordinary file write per the notice's own instructions, no new MCP tool.

**WP-TC8: stamped task assignment.**

### Added
- **`assigned_to_email` on `cognition_add_task` / `cognition_update_task`** — directs a task at an email, identity-matched (unlike the free-text, never-matched `owner`), so it surfaces under the assignee's "Your Open Tasks" at their next session start even without creating or claiming it. Assigning is not claiming — the assignee still transitions the task to `in_progress` themselves.
- **`metadata.assigned_to`** (a casefolded email string, absent when unassigned — never stored as `""`) and an append-only **`metadata.assignments`** audit trail (`{to, at, by}`, `by` server-resolved) on the task node. Blank/whitespace `assigned_to_email` at creation time seeds nothing; every EFFECTIVE (genuinely different-email) assign/reassign/unassign appends exactly one entry; a same-email resubmission is a no-op.
- `cognition_list_tasks` rows now carry `assigned_to` (`None` when absent, never coerced — same convention as `from_agent`).
- `generate_prime`'s "Your Open Tasks" personalization now also matches `metadata.assigned_to` (alongside the existing `created_by`/`claimed_by` match) — assignment never feeds the multi-user auto-detect signal, so it can't flip a solo graph to personalized on its own.

### Notes
- No-op guard modeled on `cognition_update_person`'s compare-before-append `reports_to_email` handling, not the adjacent `owner` block (which unconditionally marks the update as changed on any non-None value — the wrong template for an audited field).
- Anyone may assign anyone; the assignee need not be a registered person node yet (dangling is legal, mirroring `reports_to_email`). `assigned_to` is client-declared and unverifiable by the server, same trust class as `from_agent` — the server proves who made the assignment, never that it was accepted.
- Tool-surface self-sufficiency audit (workflow `67751ebc39bd`) re-run over both changed tools.

## [0.20.0] — 2026-07-15

**WP-TC11: dashboard redesign V1 (PM core).**

### Added
- **Dashboard nav-rail redesign** — the 3-column constellation-first layout is replaced by a left nav rail with three views: **Overview** (stat tiles, active constraints, needs-attention list, recent episodes, recent high-severity incidents), **Board** (kanban columns for tasks with a tree-view toggle for the epic/subtask hierarchy, done capped to the most recent 20, cancelled behind a toggle), and **Graph** (the original constellation, kept for curation debugging, demoted from front door to a lazy-loaded tab).
- **Shared detail drawer** replaces the old always-visible detail sidebar — opened from a Board card, a Graph node, a search result, or any Overview list row. Adds a provenance block with trust-class labeling (server-resolved `recorded_by`/`created_by`/`claimed_by` render as a solid person chip; a pre-P13n node with only a free-text `author` renders a visually distinct dashed "unverified" chip — never identical to a verified one), a task transition timeline, related nodes grouped by edge type (supersedes/contradicts rendered loud), and a conflict banner slot for contradicted-or-superseded nodes. A `from_agent` badge (WP-TC6) renders when the key is present on a node's metadata and nothing when it's absent.
- **`GET /api/tasks`** — every task shaped for the Board view (`status`, `priority`, `owner`, `parent_id`, `depth`, `created_by`, `claimed_by`, `timestamp`, `claimed_at`, `last_transition_at`, `transitions_count`), built independently of `cognition_list_tasks` (the dashboard-only `claimed_at`/transition-timestamp fields aren't in that tool's row shape).
- **`GET /api/overview`** — server-computed aggregate: task counts by status, done-this-week (from the `done` transition's timestamp, not node creation), HEAD-filtered active constraints (severity ≠ low, no incoming `supersedes` edge), needs-attention (stale claims — `claimed_at` older than 5 days — and blocked tasks), recent episodes (5), recent high-severity incidents (last 14 days), and HEAD-only workflow/document counts.
- **D-3 fold-in**: auto-poll now refreshes stats/overview/board every 30s regardless of the active view; the Graph tab's `cytoscape()` instance is constructed exactly once, on first activation, and subsequent refreshes update its elements in place — the pre-redesign code rebuilt a fresh instance every tick even when the Graph tab wasn't showing. A persistent "Search disabled — server started with `--no-embeddings`" banner replaces the old auto-hiding placeholder text for that state.

### Fixed
- Board/Overview/drawer person chips never silently upgrade a free-text `author` fallback to look like a server-resolved identity — closes the gap decision `6be2e867f91e` exists to prevent, now enforced at the list-chip level too, not just the drawer.

### Notes
- Read-only v1 (unchanged): no new mutating endpoints; node delete is the only write path, unchanged.
- Documents/Workflows dedicated views and an Activity feed are out of scope for this WP (V2, per the approved design doc) — `/api/documents` and `/api/document/{id}/download` are unchanged and still reachable directly.
- Threadpool spawn risk (`4163f54f2848`) assessed, not widened: the two new endpoints are sync handlers matching the existing `get_graph`/`get_stats`/`list_documents` pattern — only `search()` explicitly calls `run_in_threadpool`, unchanged by this WP.

## [0.19.0] — 2026-07-15

**WP-TC5 + WP-TC6: identity node layer (person nodes + agent-origin provenance).**

### Added
- **`person` node type** — models a HUMAN identity (name, role, seniority, `reports_to_email`); agent identity is never persisted here, it lives in teammate-comms. Updated **in place** (never supersession-versioned) with an append-only `metadata.profile_history` audit trail (`{changed: {field: {from, to}}, at, by}` per edit). Email is the identity key (casefolded) — one person node per email, enforced. New tools: `cognition_register_person` (omit `email` to self-register via the server-resolved git identity; pass one to register someone else, trust-based), `cognition_update_person` (anyone may update anyone — the audit trail is the control), `cognition_get_person`, `cognition_list_people`. `reports_to_email` may point at an unregistered email (legal, dangling — surfaced as `reports_to_registered: false`); self-reporting and cycles are rejected. Graph-inert in the deterministic matcher; searchable via `cognition_search(node_type="person")`.
- **`from_agent` provenance bool** — `cognition_record`, `cognition_add_task`, `cognition_store_document`, `cognition_register_person`, and `cognition_update_person` now accept `from_agent: bool = True`, stamped as `metadata.from_agent`. Default `true` ("via agent" — an undeclared write is honestly agent-originated); set `false` only when a human explicitly dictated/authored the content themselves. Surfaces in `cognition_search` results, `cognition_get_node`, and `cognition_list_tasks` rows; a pre-existing node has no `from_agent` key, which reads as unknown — never coerced to `true`/`false`.

### Fixed
- The startup embedding-sync reconciler (`_sync_cognition_embeddings`) now tolerates a node type it doesn't recognize (e.g. a newer client wrote a node type this server predates) — one bad node is logged and skipped instead of aborting the whole sync batch and silently starving every node behind it in iteration order (the exact defect this WP's peer review traced in an older version's sync).

### Upgrade note
If you share a graph with teammates, **upgrade promptly** once person nodes start landing. An older client's startup embedding sync has no per-node type tolerance: the first `person` node it encounters raises inside its sync loop, and every node behind it in iteration order silently never gets embedded — recurring on every restart until that client upgrades. Journal replay itself is unaffected (old and new clients both read `type` as a raw string); this only degrades local search coverage on a stale client.

## [0.18.0] — 2026-07-07

**WP-P13n-2: personalized session-start prime digest.**

### Added
- **Personalization** — when the graph shows more than one distinct stamped git identity (auto-detected; override via `PRIME_PERSONALIZE=auto|on|off`), the prime digest splits the task section into **Your Open Tasks** (tasks you created or currently claim — `created_by`/`claimed_by` email match, see the `claimed_by` contract in [0.17.0]) and **Team Critical** (other open critical/high tasks), and adds a new **Your Recent Activity** section (your own recent episodes, decisions, and discoveries). Active Constraints, Workflows, Documents, Patterns, Decisions, and Incidents stay global in every mode. Matching is email-only — never name/owner — and case-insensitive (`casefold`); stamps themselves remain stored verbatim. A solo graph, or an unresolvable identity, always gets the unchanged global digest.

### Fixed
- **`_format_constraints`** now HEAD-filters superseded constraints (a constraint with an incoming `SUPERSEDES` edge is excluded, mirroring `_format_workflows`' existing filter), so a revised constraint no longer duplicates alongside its replacement. Applies globally, regardless of personalization mode.

## [0.17.0] — 2026-07-07

**WP-P13n-1: server-stamped provenance.**

### Added
- **`recorded_by`** — every `cognition_record` write now stamps `metadata.recorded_by = {name, email}`, resolved server-side via `resolve_git_identity` (file-read only, never subprocess — v0.12.1 P0 contract). `author` is unchanged (still caller-supplied free text).
- **`claimed_by`** — `cognition_update_task` stamps `metadata.claimed_by = {name, email}` on an actual `status -> in_progress` transition. Re-claiming (e.g. `blocked -> in_progress` again) re-stamps to the new claimer; a same-status `in_progress` call — even combined with another field that does apply, like `owner=` — is a no-op transition and leaves `claimed_by` untouched (a takeover requires a real transition).

## [0.14.0] — 2026-07-02

**Fable-audit burndown (39 tasks): journal integrity, tool-surface honesty, docs, skills, install robustness, data integrity.**

### Changed
- **WP-1** Journal loss visibility (rehydrate/replacement detection), delete provenance (`removed_by`), byte-rewrite disclosure.
- **WP-2** Search honesty: node-type validation, home embedding-model drift guard.
- **WP-3** Embedding write-path integrity: collection-metadata stamping race closed, re-embed on journal replay.
- **WP-4** Reconciler/writer parity, chunk-completeness detection, a "syncing" `embedding_status` for teammates joining an existing graph.
- **WP-5** Merge-shaped replay defense (deferred retry + WARNING), deterministic-edge sweep fix, episode-dedup warning.
- **WP-6** Hardened `REPO_PATH` handling against an empty env value, dropped the unsafe `.env` resolution, CI version-match check.
- **WP-7** The compact hook now restores the prime data digest, not just static instructions; `SERVER_INSTRUCTIONS` gained the tasks-first/workflow-first gates the tools already mandated.
- **WP-8** Tool-surface docstring accuracy sweep (`cognition_get_neighbors`, `cognition_dashboard`, `cognition_load_project`) and edge-semantics tables ported into the edge tools.
- **WP-9** README accuracy pass (concurrency, cross-project tools, attribution), a new `docs/topology-guide.md`, `vibe-cognition-snapshot` console entry point.
- **WP-10** `/vibe-curate` skill drift fixed: the `source` field is per-edge (not a tool kwarg), `part_of` is not tool-enforced (skill-level rule, not "forbidden"), concurrency/embedding-warm-up notes added, cluster-analyzer's scan widened.
- **WP-11** `migrate_mcp`'s write path now handles locked/read-only files cleanly, server startup failures log diagnosably before re-raising, SessionStart hook timeout raised to 600s with a multi-cause failure message, new shell-level hook test harness.
- **WP-12** Task priority validation, cheap document-staleness surfacing in search + supersedes-offer on content changes, an orphaned-document-artifact reconciler, `.gitignore`-before-blob-write ordering.
- **WP-13** `vibe-cognition-prime`/`vibe-cognition-backfill` gained real argparse (`--help` no longer silently executes the full command) and backfill gained `--days`; `OllamaBackend` now applies the same nomic query/document prefixes the sentence-transformers backend does; the dashboard token no longer appears in INFO logs.
- **WP-14** `duplicate_of` retired from `CognitionEdgeType` — it was tool-rejected since inception (never reachable) and `supersedes` is now the reconciliation edge, including for the episode-duplicate case. Removed `CognitionStorage.redirect_edges` (zero production callers, would double edges if resumed). `supersedes` gained shape guardrails at edge creation: legal only same-node-type-to-same-node-type, or a fail/incident superseding a non-workflow node (the retraction pattern), plus cycle prevention.

## [0.13.0] — 2026-07-01

**Session-start prime trim + post-commit journal hook removal.**

### Changed
- **Trimmed the session-start `prime` injection** (~1,346 → ~634 tok on this
  repo's graph at release, a ~53% cut; scales with graph size). Added a
  `PrimeConfig` dataclass with 7 env-overridable
  knobs (`PRIME_CONSTRAINT_LIMIT`, `PRIME_TASK_CAP`, `PRIME_PATTERN_LIMIT`,
  `PRIME_DECISION_LIMIT`, `PRIME_INCIDENT_DAYS`, `PRIME_SUMMARY_MAXLEN`,
  `PRIME_INCIDENT_MIN_SEVERITY`), a hard-cut-safe summary truncator, and
  severity gating: incidents keep only `high`+`critical` within a 14-day
  window (was 30), constraints drop only `low` (`None`/`normal`/`high`/
  `critical` all kept). A `Settings()` build failure in the hook falls back to
  `PrimeConfig()` defaults — the same trimmed shape, never the old fat output.

### Removed
- **The post-commit journal hook** (`hooks/post-commit.py` / `.sh`, the
  `PostToolUse` wiring in `hooks/hooks.json`). It auto-appended an episode node
  to `.cognition/journal.jsonl` after every `git commit`, re-dirtying the tree
  right after a clean commit — redundant with deliberate `cognition_record`.
  `/vibe-backfill` (opt-in recovery) and the shared `journal_io` helper (now
  server-only) are unaffected.

## [0.10.0] — 2026-06-22

**Team git hygiene, readme tool, and Plan agent cross-project memory.**

### Added
- **`cognition_readme` tool** — returns the full orientation guide and getting-started
  text directly from the MCP server. Empty-graph sessions inject an onboarding block via
  the SessionStart hook, pointing users at `cognition_readme` and encouraging the first
  record. (#24)
- **Journal `merge=union` team guidance** — the `cognition_readme` guide now includes a
  `## Team setup (git)` section explaining the one-line `.gitattributes` entry that makes
  the append-only journal union-merge for teams using separate clones. Warning against
  `-text` (the C-3 scar) and the shared-checkout exception are documented. (#26)
- **Automatic git hygiene on startup** — on first use in a new project the server
  automatically adds `.cognition/journal.jsonl merge=union` to the repo-root
  `.gitattributes` and `chromadb/` to `.cognition/.gitignore`. One-time-ever per working
  copy (content-versioned sidecar flag); idempotent, locked, crash-proof. Opt out with
  `VIBE_COGNITION_NO_GIT_HYGIENE=1`. Re-arm by deleting
  `.cognition/.git-hygiene-managed`. The SessionStart hook announces what was configured.
  (#27)
- **Plan agent cross-project memory** — the `vibe-cognition:Plan` agent can now discover
  sibling projects via teammate-comms, resolve their paths on disk, attach their knowledge
  graphs read-only, and search them during planning. Hard cap of 2 foreign graphs per
  plan; unconditional unload before returning. (#28)

### Fixed
- **Plan agent broken tool names (S-2)** — all 5 cognition tool names in the Plan agent
  frontmatter were using the old `.mcp.json` prefix (`mcp__vibe-cognition__*`) which does
  not resolve for plugin-declared MCP servers. Corrected to
  `mcp__plugin_vibe-cognition_vibe-cognition__*`. (#28)

## [0.9.0] — 2026-06-21

**Cross-project cognition** — load another project's knowledge graph alongside your
own, read-only, and query it; no second agent needed. Plus an embedding-quality fix.

### Added
- **Cross-project read access.** `cognition_load_project` attaches another project's
  graph by path (read-only); `cognition_list_projects` / `cognition_unload_project`
  manage what's loaded. Your home project is always loaded and cannot be unloaded.
- **`project` arg on the read tools** — route a read (search, get_node, get_chain,
  get_history, get_neighbors, …) to a loaded project by tag/path; `"*"` fans aggregate
  queries (search, history, edgeless/uncurated) across all loaded projects. Results
  carry a `project` provenance tag. Single-node lookups require a specific project
  (node ids aren't project-namespaced).
- **Semantic search over a loaded project**, gated by an embedding-model guard — a
  project on a different embedding model/dimension has semantic search disabled
  (structural reads still work) rather than returning silently-wrong rankings.
- Writes are never cross-project — recording always targets your own project.

### Fixed
- **Embedding asymmetry (E-3).** Documents and nodes were embedded with the *query*
  prefix instead of the *document* prefix, discarding nomic-embed-text-v1.5's
  asymmetric-retrieval training (degraded ranking). All stored vectors now use the
  document prefix; queries keep the query prefix. **One-time re-embed** on the first
  server start after upgrading rebuilds the embedding collection (in the background;
  search is briefly degraded; no data loss — the journal is the source of truth).
  *(Ollama backend has no prefix distinction, so it is unaffected.)*

## [0.8.0] — 2026-06-13

First-class **document storage**: keep client docs, PDFs, specs, and transcripts as
durable project memory, with the knowledge inside them woven into the graph.

### Added
- **Store documents as first-class nodes.** Default **reference mode** records the
  file path + metadata + a content hash (the bytes stay where they live); opt-in
  **copy mode** (`store_copy`) saves the bytes into a content-addressed store, with
  `local_only` to keep a copy out of git.
- **Searchable document text.** You (the agent) extract the text; it's chunked into
  the local embedding store, so `cognition_search` finds documents and returns the
  matching excerpt.
- **The `/vibe-document` skill.** Stores a document, then records the facts inside it
  as descriptor nodes that cite the document's `doc:<hash>` in their references —
  which auto-links them to the document. This citing step is how a document connects
  to the rest of the graph.
- **New tools:** `cognition_store_document` and `cognition_get_document` (which reports
  freshness — `unchanged | modified | missing` — by re-hashing the referenced file).
- **Dashboard:** a Documents panel listing stored documents, with a token-gated,
  path-safe **download** (the copied blob, or the extracted text for reference-mode
  documents).
- Deleting a document reclaims its managed artifacts (extracted-text sidecar, copied
  blob, search vectors) but never touches the referenced original file.

### Fixed
- **Cross-process ghost search (N1):** search (in both the MCP tools and the
  dashboard) no longer returns hits for nodes that were deleted on another machine —
  a deletion replayed from the shared journal previously left the embedding behind. A
  startup sweep also reclaims orphaned vectors.

> **Privacy note:** a copy-mode blob committed to git survives in git history and on
> the remote even after you delete its node — deleting a document does not un-publish
> an already-committed copy.

## [0.7.4] — 2026-06-11

### Fixed
- The SessionStart hook now detects a half-installed dependency (a venv left
  mid-swap by an interrupted update) and shows a clear "close all sessions and
  start one" message, instead of failing with a cryptic MCP connection error.
- **Journal cross-process atomicity (C-1):** journal appends now go through a
  single shared, lock-protected atomic-append helper, so two processes (two
  sessions, or a session and the post-commit git hook) appending at the same
  time can no longer interleave and silently lose entries. The post-commit hook
  is routed through the same helper (closes H-2 — it used to fork the journal
  format), so both writers share one format and one lock.
- **Journal replacement detection (C-3):** a running server now detects when the
  journal file is replaced or divergently merged (e.g. by a `git pull`/merge of
  the committed journal — which preserves the first line, so a first-line check
  would miss it) by hashing the already-replayed prefix and re-hydrating instead
  of replaying from a now-meaningless byte offset. (Residual: a replacement that
  matches both byte size and nanosecond mtime evades the cheap skip-path —
  vanishingly unlikely.)
- Note on upgrading: until **all** running Claude Code sessions are on this
  version, an older session keeps appending with the previous unlocked,
  buffered writer — so the cross-process guarantee holds only once every session
  has upgraded. Restart sessions after updating.

## [0.7.3] — 2026-06-10

> **Known upgrade note (0.7.2 → 0.7.3):** this release re-sources torch from a
> CPU-only wheel index. If you update while other Claude Code sessions are open
> (which hold the old torch's files, mainly on Windows), the dependency swap can
> be interrupted and leave the shared venv half-installed — the MCP server then
> fails to load. It self-heals: close ALL Claude Code sessions and windows, then
> open ONE. (A later version adds an explicit message for this case.)

WP-1 — Tier 1 mechanical cleanup (from the 2026-06-10 audit).

### Added
- `LICENSE` (MIT) — was declared in both manifests but no file existed (H-6).
- `CHANGELOG.md` — this file.
- `.gitattributes` rule `merge=union` for `.cognition/journal.jsonl` — correct
  merge semantics for an append-only, globally-unique-ID JSONL log (defense in
  depth; resolves textual conflicts only, not C-3 replay order).
- Regression test for ChromaDB telemetry being disabled.

### Fixed
- **E-1:** Disable ChromaDB anonymized (PostHog) telemetry. Defense-in-depth:
  inert at our pinned chromadb 1.5.5 (no-op stub), but chromadb 0.5–0.6.x —
  permitted by our `>=0.5.0` floor — actively phoned home gated on this flag.

### Changed
- Unified authorship to "Colton Dyck" across `pyproject.toml` and
  `plugin.json` (was "BlckLvls" / "ColtonDyck") (H-6/d).
- Documented why `einops` is a runtime dependency (nomic's `trust_remote_code`
  model code imports it) (H-6).
- Ruff baseline cleanup: fixed 20 of 23 findings (UP017×8, F401×3, I001,
  SIM102×2, SIM105×2, E741×4). Deferred: 2× UP042 (StrEnum changes `str()`
  semantics) and 1× UP017 in `hooks/post-commit.py` (runs on system Python,
  kept 3.10-compatible) (§8.1).
- Corrected stale comments in `server.py` (REPO_PATH source), the
  `session-start.sh` header (it removes, not configures, per-project MCP), and
  the `post-commit.py` docstring (actual `hooks/hooks.json` wiring) (T-10, H-6, H-2).

### Removed
- Dead direct dependency `httpx` (zero imports; satisfied transitively) (H-6).
- Duplicated `[project.optional-dependencies].dev` block — `[dependency-groups].dev`
  is the one `uv sync` reads (H-6).
- Stale `__version__ = "0.1.0"` from `vibe_cognition/__init__.py` (read by
  nothing; real version lives in the manifests) (T-10).
- `.ruff_cache/` from version control (added to `.gitignore`) (H-6).

WP-2 — CI + slim install.

### Added
- GitHub Actions CI (`.github/workflows/ci.yml`): runs ruff, pyright, and pytest
  on every PR and push to `main`. pyright uses a baseline-count ratchet that
  fails on new type errors and tightens as the count drops.

### Changed
- **Smaller install:** torch now resolves from PyTorch's CPU wheel index,
  removing the multi-gigabyte CUDA stack (18 GPU-only packages) from installs —
  a large first-install size reduction for Linux users, who previously pulled
  the full nvidia/CUDA toolchain this CPU-inference tool never uses (audit B-4).
  - Technical note: torch is declared a direct dependency pinned exactly to
    `==2.11.0` (uv ignores index sources for transitive deps; the exact pin
    guarantees zero drift at adoption). A future `sentence-transformers` bump
    requiring newer torch will hard-conflict at re-lock by design — fail loud,
    decide deliberately, loosen only when forced.
- `.cognition/journal.jsonl` marked `-text` in `.gitattributes` so git stores
  it verbatim (byte-determinism for the journal's byte-offset replay; C-3 defense).

WP-3 — post-commit hook + skill correctness.

### Fixed
- **H-1:** The post-commit hook no longer runs on a bare `python` (which fails
  silently where python isn't on PATH — macOS default, many Windows installs).
  It now runs through `uv run` via a `hooks/post-commit.sh` wrapper; uv is a
  guaranteed plugin dependency.
- Commit messages with non-ASCII characters are no longer mangled in the
  cognition journal (e.g. "§" → "Â§"): the hook now decodes git output as UTF-8
  instead of the system locale codepage.
- **B-3 (Windows):** the hooks' `CLAUDE_PLUGIN_DATA` fallback no longer mis-strips
  a backslash path, which had placed the venv back inside the version-pinned
  cache dir (where a `/plugin update` could lock/wipe it). Fixed across all three
  bash hooks.
- **S-1:** The `vibe-curate` skill now references its subagent prompt files from
  the skill's own directory, so they resolve when the plugin is installed (they
  previously used repo-relative paths that only worked from a checkout).
