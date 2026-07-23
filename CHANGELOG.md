# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.29.0]

### Added
- **Session-start update nudge (WP-Nudge-1)**: the SessionStart hook now checks once every 24 hours whether a newer released version of the plugin is available and, if so, surfaces a one-line nudge (with the exact `/plugin update` command) alongside the usual context injection. Nudge-only — nothing is installed or changed automatically, updating is always the user's call. Compares version strings (never SHAs — an installed `gitCommitSha` need not match the marketplace pin's SHA at the same released version) against the marketplace's release pin (never this repo's `main`, which can carry unreleased versions). Throttled via a local, machine-only timestamp file — at most one check per 24h, a hard ~8s wall-clock ceiling on the network phase, zero cost otherwise. Default on; disable with `VIBE_UPDATE_NUDGE=off` (also accepts `0`/`false`/`no`). New `src/vibe_cognition/update_check.py` (stdlib-only, mirrors `migrate_mcp.py`'s standalone style), new `vibe_update_nudge` config field, README "Update Notifications" section, two "no cloud services" claims narrowed to name this one exception.

## [0.28.0] — 2026-07-16

**team-cognition epic (cedf4a8457e9) — CLOSED.** Three trains, same-day: dashboard
polish, provenance smalls, and legacy identity backfill v1.

**Train A — Dashboard (Board epic-grouping, tab title, People view management gaps, cache headers).**

### Added
- **Board's Kanban columns are now grouped by epic** — each column shows a header
  per top-level epic (walking `part_of` ancestry to the topmost epic task) with a
  trailing "(no epic)" group for tasks with no epic ancestor, sorted by severity
  within each group. The separate Tree-view toggle and its `#board-tree` markup
  are fully removed — one board rendering, not two.
- **Dashboard tab title** now includes the project's folder name
  ("`{project} — Vibe Cognition Dashboard`"), so multiple open dashboard tabs
  (one per project) are distinguishable at a glance. HTML-escaped; served via a
  templated `HTMLResponse` rather than the prior static `FileResponse`.
- **People view gains an "Unregistered writers" section** (`/api/people/unregistered`)
  — every stamped email with no matching person node, the management-gap list a
  team lead needs to know who to onboard. Person nodes in the People view are
  drilldown-clickable (read-only) into a drawer showing `node_counts` by type,
  `last_active`, and `claimed_tasks`/`created_tasks` — `claimed_tasks` matches
  against ALL task nodes' `claimed_by` (not gated on the claimant being the
  node's own creator-stamped identity — a task created by one person and claimed
  by another now correctly shows under the claimant, not the creator).
- **`Cache-Control: no-cache` on every dashboard response** (index HTML + static
  assets, both 200 and 304), via a `_NoCacheStaticFiles` StaticFiles subclass —
  closes the stale-browser-cache bug where a plain reload after a plugin
  upgrade could render new HTML with cached pre-upgrade CSS/JS (the
  mangled-dashboard incident). Distinct from the still-open server-side
  stale-first-session race (fail fb24257ee2da, task 43d3c3dab10f) — this fix
  addresses only the browser asset cache, not that server-side class.

### Notes
- Gate parity: `uv run pytest` 1074 passed/1 skipped (was 1056/1, +18 new tests),
  `uv run ruff check .` clean, `uv run pyright` 39 errors (unchanged baseline).
- No new JS test harness introduced — new frontend logic is covered by the
  existing `TestFrontendStructure` string/structure-presence idiom (this repo
  has no JS test runner), matching established convention.
- A pre-existing circular import in `python -m vibe_cognition.dashboard.cli`
  (unrelated to this WP, confirmed via `git stash` on the unmodified checkout)
  was discovered and filed separately rather than fixed in-WP.

**Train B — Provenance smalls (store_document recorded_by, seniority-tier docstrings, search conflicted_with).**

### Added
- **`cognition_store_document` now stamps `metadata.recorded_by`** on newly
  created document nodes (server-resolved git identity, same verbatim shape as
  `_record_node`/`_add_task`/`_register_person`) — document nodes were the one
  write path WP-P13n-1 never reached. The dedup branch (re-storing an existing
  sha) is untouched; backfilling already-stored legacy documents is out of scope
  here (legacy-identity-backfill's job). Dashboard's `list_documents` surfaces
  the field (display-only, `None` on pre-fix documents).
- **`cognition_search`'s conflicted hits gain `conflicted_with`** —
  `[{id, summary, author, reason}]` naming each `CONTRADICTS` counterparty (both
  directions), via a new sibling function `cognition.queries.conflict_details`.
  `conflict_flags`'s pinned 2-tuple return is untouched — the new field is
  computed only for hits already flagged `conflicted=True`. `author` resolves
  from the counterparty's `recorded_by` stamp (name, else email) when present,
  else its free-text `author`; `reason` is the edge's own `reason` field, null
  if absent. A dangling edge target is silently skipped.

### Changed
- **`cognition_register_person`/`cognition_update_person` docstrings** now spell
  out the closed 4-tier seniority set and instruct the assistant to present the
  options and ask, clarifying that `role` stays free text.

### Notes
- Gate parity: `uv run pytest` 1081 passed/1 skipped (was 1074/1, +7 new tests),
  `uv run ruff check .` clean, `uv run pyright` 39 errors (unchanged baseline).
- Live-verified `conflicted_with` against this repo's own two real `contradicts`
  pairs (f09e770da046↔d79cd9a93a02, 3c16d91417cb↔03ae7b2cd063) in addition to
  unit tests.

**Train C — Legacy identity backfill v1 (task 962ab7b442d5, design doc rev 2, decision 833e9f67de4d).**

### Added
- **New standalone CLI, `python -m vibe_cognition.backfill_identity <project-path>
  [--map "Name=email"]... [--map-file <path>] [--apply] [--recompute-backfilled]`**
  — stamps pre-P13n-1 legacy nodes (free-text `author` only) with an inferred
  `recorded_by`/`created_by`, so an existing project's history benefits from the
  identity features already built for new writes (personalized prime, search
  identity weighting, exclude-people filters, activity attribution,
  unregistered-writers view). NO auto-stamping: registered-roster match and
  journal git-blame are suggestion generators only — the only path that writes
  is a human-confirmed map file (a dry run emits a skeleton pre-filled with
  suggestions; the owner edits it; `--apply --map-file` writes exactly that).
  Every stamp this tool writes carries `backfilled: true` +
  `backfill_source: "roster"|"git-history"|"manual"`, validated against that
  closed set and downgraded to `"manual"` whenever the confirmed email doesn't
  match the original suggestion — the marker never claims more provenance than
  actually happened. PERSON nodes, DOCUMENT nodes, and task
  claims/transitions reconstruction are permanently out of v1 scope. A
  journal-mtime recheck aborts `--apply` if the journal changed since the run
  started, rather than racing a live session's append.

### Notes
- Gate parity: `uv run pytest` 1115 passed/1 skipped (was 1081/1, +34 new
  tests), `uv run ruff check .` clean, `uv run pyright` 39 errors (unchanged
  baseline).
- Live-verified (dry-run only, no `--apply` against the production graph) on
  this repo's own 352 eligible legacy nodes: matched the design doc's own
  predicted findings, including the "Colton Dyck" email-drift case (this
  repo's blame history really does resolve that name to two distinct emails)
  and the agent-persona-authors-are-the-majority-unmapped-case observation.
- `.cognition/.gitignore`'s managed rule set (git_hygiene.py) gained
  `backfill-identity-map.skeleton.json` (version bumped to v5) — the CLI's
  dry-run scratch artifact must never ride into a journal-flush commit.

### Fixed
- `_run_git`'s subprocess call used `text=True`, which decodes via the
  platform-locale codec — cp1252 on Windows — and raised `UnicodeDecodeError`
  on a real non-cp1252 byte encountered live in this repo's own commit history.
  Now decodes UTF-8 explicitly, with replacement.

## [0.27.0] — 2026-07-15

**WP-doc-fix: README/skill/docstring consistency sweep (Gate B-final findings, doc-only, zero behavior changes).**

### Changed
- **Edge-authoring contradiction fixed.** README, both SKILL.md files, and the
  `cognition_add_edge`/`cognition_add_edges_batch`/`cognition_store_document`/
  `cognition_update_node`/`cognition_record`/`cognition_remove_node` docstrings
  now consistently state `/vibe-curate` is the ONLY path that writes semantic
  edges (including `supersedes`); the vibe-workflow skill's stray
  `cognition_add_edge(...)` example command and the Edge Types table's "or
  manual" wording are gone. `cognition_add_edge`/`cognition_add_edges_batch`/
  `cognition_mark_curated` docstrings now also state explicitly that this is a
  documented CONVENTION enforced by agent discipline, not a server-side ACL —
  none of the three tools has a caller-identity check — and point at
  `get_status`'s `edges_outside_curation`/`edge_sources` as the after-the-fact
  detection signal. `get_status`'s own docstring gained matching remediation
  guidance (inspect `edge_sources`, remove an accidental manual/batch edge with
  `cognition_remove_edge` or document it as an accepted baseline, and re-run
  `/vibe-curate` regardless since a low count doesn't mean nothing's missing).
- **Personalized session-start prime documented in all three canonical sites**
  (README.md, `skills/vibe-cognition/SKILL.md`, `cognition/readme.py`) — the
  widened `auto` heuristic, the identity header format, mutual exclusion with
  the New Here banner, and the full pinned section order, none of which had
  been written down outside WP-OnboardPayoff's own diff until now.
- **`cognition_get_history`/`cognition_get_neighbors` docstrings** gained a
  disclosure that results are NOT HEAD-filtered and carry no conflict/
  supersession marker, contrasting with `cognition_search`'s `conflicted`/
  `superseded_by` fields, so a caller doesn't assume parity across tools.
- **`readme.py`'s "Since You Were Gone" section** gained the same
  constraints-are-HEAD-filtered / decisions-and-incidents-are-not disclosure
  README.md already carried, closing the last of the three-site gap.
- **`skills/vibe-cognition/SKILL.md`'s `claim_warning` bullet** now enumerates
  the three `kind` values (`claim_collision` | `takeover_note_required` |
  `reopen`), matching README's existing documentation.
- **`SERVER_INSTRUCTIONS`** (`instructions.py`) gained a trailing unnumbered
  paragraph naming what session-start prime injects (open tasks, constraints,
  patterns, decisions, incidents, plus the personalized sections including the
  identity header) and that `get_status` exposes WP-TC15 observability keys.
  Token-estimate comment re-measured: ~545 tokens (was ~433 pre-doc-fix).

### Notes
- Zero behavior changes — no code paths touched, only docstrings/prose. Full
  gate parity: `uv run pytest` 1056 passed/1 skipped (unchanged baseline),
  `uv run ruff check .` clean, bare `uv run pyright` 39 errors (unchanged
  baseline). `test_tool_wrappers.py`'s pinned edge-type/`duplicate_of`/
  `retire` substring assertions verified to still pass — all edits to those
  three docstrings were additive only.
- No version bump (batches into 0.27.0 with the rest of the Gate D train).

**WP-OnboardPayoff: person-node-aware auto-personalize + prime identity header (Gate D S5 fix).**

### Added
- **`prime_personalize="auto"` now also personalizes on >1 registered person**,
  not just >1 distinct stamped writer email. Closes the Gate D finding that a
  team's first-onboarded member (every graph node so far written by ONE person,
  but several people now registered) got zero personalized sections — the
  experience was indistinguishable from unregistered except the New Here banner
  disappearing. New `_registered_person_emails(storage)` helper returns a SET of
  emails (never a node count — a replay-duplicate person node for one email must
  not flip a solo graph to "multi-user"). All three existing solo byte-identity
  pin tests pass UNMODIFIED.
- **One-line identity header** ("You are registered as {name} — {role}
  ({seniority}), reporting to {manager}.") opens the personalized block,
  immediately before Your Tasks, whenever `current_email` resolves to a
  registered person node. Degrades field-by-field when role/seniority/manager
  are blank; an unresolvable manager email falls back to showing the raw email.
  Mutually exclusive with the New Here banner by construction (the header needs
  a matching person node, the banner needs the absence of one) — no new config
  knob. `_RoleContext` gains `my_manager_name` (resolved from the same single
  person-node scan `_derive_role` already does — no second scan).

### Notes
- Passive display change only, in prime.py — no acknowledgment/interceptor
  mechanics (a permanently rejected feature class), no tool-return changes.
- Known edge case (disclosed, not "fixed"): an UNREGISTERED user priming a
  single-writer graph with >=2 registered persons now sees the personalized-
  but-sparse layout (banner + mostly self-gated-empty sections) — the same
  shape any unregistered user of a multi-*writer* graph already gets today, not
  a new kind of experience, and it nudges toward registering.
- `config.py`'s `prime_personalize` Field description updated to document the
  OR condition. No version bump (batches into 0.27.0 with the other Gate D
  fixes). README/skill harmonization deliberately deferred to the doc-fix WP
  (6e54452b9735), which goes last.

**WP-SearchFlags: `cognition_search` results gain `conflicted`/`superseded_by` (Gate D S2/S4 fix).**

### Added
- **`conflicted: bool`** and **`superseded_by: str | null`**, ALWAYS present on every
  `cognition_search` result. `conflicted` is true iff the hit has >=1 incoming OR
  outgoing `CONTRADICTS` edge (contradicts is one-way/arbitrary-direction — pattern
  `6ed494680fb3` — so membership is bidirectional). `superseded_by` is the id of the
  newest node with an incoming `SUPERSEDES` edge onto the hit, or null; only the
  superseded (older) side gets a non-null value, the resolving/newer node stays null.
  Branch case (multiple incoming supersedes edges): tie-break picks the superseder
  with the greatest NODE timestamp (authorship time), never the edge's own mint time.
  Closes the Gate D finding that a superseded hit could outrank its correction with no
  marker, and that two contradicting decisions rendered side-by-side with no signal —
  while the dashboard's rows already flagged the same nodes (inconsistent read
  surface, now unified).
- **No ranking change** (pinned decision): flags never affect `weighted_score` or
  result order — search stays visibility-preserving (TC9 doctrine); a superseded or
  conflicted hit ranks exactly where its raw similarity places it. Regression-tested.
- **Single implementation**: membership logic relocated to
  `cognition.queries.conflict_flags(storage, node_id) -> (conflicted, superseded_by)`,
  shared by `cognition_search`'s per-result flags and the dashboard's `_is_conflicted`
  wrapper (which composes `conflicted OR superseded` to keep its existing single-⚠
  semantics — dashboard tests pass UNMODIFIED after the relocation).

### Notes
- `cognition_get_workflow`'s internal top-1 match search shares the same formatter, so
  these flags are computed for that throwaway result too and discarded (3 wasted edge
  lookups per call) — accepted, not worth a special-case bypass.
- Return-shape change to an MCP tool — ran the tool-surface self-sufficiency audit on
  `cognition_search`'s docstring; it now also explicitly states results are NOT
  HEAD-filtered (a superseded hit ranks normally; the flag is the marker).
- No version bump on this branch (batches into 0.27.0 with the other Gate D fixes).

**WP-list-tasks-claim: `cognition_list_tasks` rows gain `claimed_by`/`claimed_at`.**

### Added
- **`claimed_by`** (server-resolved identity dict, or `None`) and **`claimed_at`** (ISO
  timestamp of the latest `->in_progress` transition, via the shared `_task_claimed_at`
  helper — same implementation the dashboard and prime's manager rollup already use) on
  every `cognition_list_tasks` row. Gate B-final finding (task `8c7bab562c37`): without
  this, an agent following claim etiquette had no read-only way to see who holds a task
  before attempting `cognition_update_task(status="in_progress")` — discovery only
  happened by collision. `claimed_by` persists after a task closes (claim history, not
  a liveness flag — use `status` for that).

### Notes
- Return-shape change to an MCP tool — ran the recurring tool-surface self-sufficiency
  audit on the updated `cognition_list_tasks` docstring (name/args/return-shape read
  standalone, no assumed context). No version bump on this branch (batches into 0.27.0
  later).

## [0.26.0] — 2026-07-15

**WP-DashV3: dashboard team-cognition wiring — People view, seniority chips, list-level conflict flags (phase V3 of 3, final).**

### Added
- **`GET /api/people` + People view** — one `PERSON`-type scan, sorted by name. `reports_to_registered` is derived from a casefolded-email set built ONCE from that same scan (O(1) membership per row) rather than mirroring `cognition_list_people`'s per-row `_find_person_by_email` rescan (O(N²) — a separate pre-existing nit, out of scope here). Frontend: a card grid (name, role · seniority, email, reports-to — a dangling manager email renders as an "unregistered" chip rather than crashing) with a "view activity" shortcut that switches to Activity pre-filtered to that person's name (reuses the existing client-side author filter, no new endpoint).
- **Seniority chips** on every resolved (server-stamped) identity render — Board cards, Activity rows, and the drawer's provenance block — via a client-side roster join: `app.js` fetches `/api/people` ONCE lazily (first render that needs it), caches it in a module-level map keyed by casefolded email, and never refetches it on view switches or the 30s poll (unlike `boardTasksCache`/`activityCache`, which legitimately refetch per activation). Chip style is SOLID, never dashed — the mockup's dashed `.chip.seniority` collides with the reserved `.chip.person.unverified` signal, so copying it verbatim would make a fully-verified identity read as unverified. Trust boundary: an unverified free-text author NEVER gets a seniority chip, even if its name happens to match a registered person — the lookup is only reachable from the resolved-identity branch of `identityChipHTML`.
- **`conflicted: bool`** on `/api/tasks` and `/api/activity` rows (`_is_conflicted` in `api.py`), rendered as a ⚠ indicator on Board cards and Activity rows. Direction semantics (peer-review BLOCKING catch): `contradicts` edges are stored ONE-WAY with ARBITRARY direction (no reciprocal edge is ever minted), so membership is bidirectional — incoming OR outgoing contradicts, checked separately since an incoming-only implementation flags only one side of every pair. `supersedes` stays incoming-only: a node with an outgoing supersedes edge is the newer version and is not itself in conflict. Overview's constraint/attention lists are deliberately unchanged (they're already HEAD-filtered; adding the flag there would invite confusion) — `_entity_row` takes `storage` as an opt-in parameter so only `/api/activity`'s calls gain the field.
- **Drawer conflict-banner direction repair** (same WP, same class of fix as the row-level flag): the V1 `conflictBannerHTML` had the identical incoming-only gap — a node on the outgoing side of a contradicts edge showed no banner at all. Now scans `node.successors` too, with distinct wording ("contradicts [id]" for outgoing vs. the existing "contradicted by [id]" for incoming) so the direction stays legible. Supersedes banner handling is unchanged (incoming-only is correct there).

### Notes
- No MCP tool changed (dashboard-only) — no tool-surface audit required. No prime/storage changes at all this WP — `/api/people` scans person nodes via the existing `get_nodes_by_type`. Search-overlay seniority-weight labeling (design doc §5 item 4) is explicitly deferred — not in scope for this phase.
- This closes the three-phase dashboard redesign (V1 Overview/Board/Graph, V2 Workflows/Documents/Activity, V3 this WP) and task 30fabf12c81b. No version bump on this branch — 0.26.0 candidate; bump timing decided at merge.

## [0.25.0] — 2026-07-15

**WP-DashV2: dashboard Workflows library, Documents table, Activity feed (phase V2 of 3).**

### Added
- **`GET /api/workflows`** — HEAD-only workflow cards (a node with no incoming `SUPERSEDES` edge), each with its version chain inline via `get_superseded_chain` (newest → oldest, one graph walk per HEAD, no N+1 round-trips). Superseded (non-HEAD) versions never appear as top-level cards, only inside their successor's chain.
- **`GET /api/documents`** gains two fields per row: `freshness` (`unchanged`/`modified`/`missing` for reference-mode docs with a source path, via a full re-hash — `null`/"n/a" for copy-mode docs and path-less reference docs, since neither has a source to check) and `cited_by` (count of entity nodes citing the document via a `part_of` edge, `storage.get_predecessors(doc_id, PART_OF)` — derived at read time, never a stored counter).
- **`GET /api/activity`** — chronological feed across episode/decision/fail/discovery/incident/constraint/pattern/assumption nodes (tasks, documents, workflows, and people are covered by their own views), newest-first, `limit` query param (default 100, capped 500). Rows reuse `_entity_row` verbatim, so `recorded_by` and `author` stay separate fields (the existing trust-class chip rendering needs both, not a pre-collapsed string).
- Three new nav-rail views (Workflows, Documents, Activity) with matching frontend renderers; all fetch on tab activation only — none join the existing 30-second poll (explicit acceptance of the pre-existing threadpool-spawn risk, condition: no new polling).
- `cognition/documents.py` gains `freshness_by_rehash()`, relocated from `cognition_tools._get_document`'s inline re-hash logic (single-implementation doctrine — the dashboard needed the same check outside the MCP tool layer). `_get_document`'s behavior is unchanged; existing document tests pass unmodified.

### Notes
- Freshness is a full SHA-256 re-hash of the referenced file (cost scales with file size), not the cheap stat-check (`cheap_staleness_signal`) the design doc's prose suggested — that helper structurally cannot detect a same-size content edit, which would make the "modified" badge state a lie. Disclosed deviation from the design doc, flagged for async review; no caching added (a stale freshness badge defeats its own purpose).
- No MCP tool changed (dashboard-only) — no tool-surface audit required. No version bump on this branch — V2 is a 0.25.0 candidate; bump timing decided at merge.

## [0.24.0] — 2026-07-15

**WP-TC15: curation-containment observability (`get_status` surfaces edge writes made outside curation runs).**

### Added
- **`get_status`'s `cognition_graph` gains `edge_sources` (dict) and `edges_outside_curation` (int).** Derived at read time in `get_statistics`'s existing edge-iteration pass (`storage.py`, `for _, _, edge_data in self._graph.edges(data=True)`) — no new persistence, no write-path changes, one pass. `edge_sources` is a full histogram of every edge `source` value seen; `edges_outside_curation` sums the subset NOT in the curation-legitimate set (`deterministic`, `task-parent`, `curate-skill`, `curate-conflict`, `curate-cluster`, `curator`) — i.e. `manual`, `batch`, or any unrecognized value, conservative by construction (a future legitimate producer must be exempted deliberately in code). A replayed journal entry with no `source` field at all falls back to the existing legacy default (`"curator"`, `storage.py`'s `_catch_up`) and is therefore exempt, so no pre-existing graph shows a false baseline. Both keys are always present, even on a zero-edge graph (`{}` and `0`).
- **`agents/curate-orchestrator.md` Step 4** now sets `source="curate-cluster"` on cluster `part_of` edges (mirroring Step 2/3's own source wording) — without this, cluster edges defaulted to `"batch"` and conflated legitimate curation output with the exact bucket this WP counts as violations.
- `get_status`'s docstring documents both new keys and the full exempt/counted source taxonomy (tool-surface audit: `get_status`'s output shape changed).

### Notes
- `get_statistics`'s return type widened `dict[str, int]` → `dict[str, int | dict[str, int]]` (peer-review HIGH — the un-widened annotation fails the pyright CI ratchet once a nested dict value is returned). The internal `stats` dict stays narrowly `dict[str, int]` throughout the function body (avoids rippling the union into every pre-existing `stats[key] += 1` line); the histogram is merged in as a sibling structure only at the return boundary.
- Known day-one baseline on the real graph: 85 (`edges_outside_curation`) — 84 historical `"batch"`-sourced Step-2-style semantic edges predating consistent `curate-skill` tagging, plus 1 `"manual"` edge. Disclosed, not remediated — no journal rewriting, ever; `edge_sources` makes the baseline legible.
- Trust model (documented, not enforced): `source` is caller-declared: a deliberately spoofed source defeats the counter. Ruled out of scope — the threat model is accidental misuse in a local trust domain, not adversarial. Token-handshake/auth designs for curation runs are permanently rejected.
- No dashboard change, no journal format change, no new persistence, `mark_curated` untouched. The containment constraint itself stays docs-first — this is a smoke detector, not a lock.
- Version bump: standalone 0.24.0 bump commit follows after the SECOND of TC14/TC15 merges (supersedes TC14's "batch's last WP carries the bump" note) — gated and merged like any WP, removing the double-/missed-bump race.

**WP-TC14: "Since You Were Gone" digest (per-email last-seen marker + prime section).**

### Added
- **`.cognition/last-seen.json`** (new, git-ignored, machine-local, git hygiene version 3→4): casefolded email → aware-UTC ISO timestamp of that email's last session-start. Read-modify-write preserves other emails' entries — a manager and subordinate sharing a machine must not stomp each other's marker. Written ONLY by `prime.py`'s own `main()` (the CLI/hook path), AFTER prime output is produced — `generate_prime()` stays pure read-only (the invariant is directly tested: a bare `generate_prime()` call never creates/updates the marker). Guarded by `git_hygiene`'s established lock (60s stale detection) plus a short bounded retry (3 attempts, 20ms apart) so two teammates' concurrent session-starts on a shared machine realistically both land; retries exhausted still degrades to "skip the stamp entirely" — a missed stamp just falls back to the lookback window next session, never blocking or failing the SessionStart hook. Atomic write (temp file + `os.replace`), whole body wrapped in `suppress(OSError)`. A future-dated marker (clock skew) self-heals via the next unconditional restamp.
- **`## Since You Were Gone`** (personalized-only): decision/constraint/incident nodes with `timestamp` STRICTLY greater than a per-email cutoff (the marker when present, else `now - prime_digest_fallback_days` — new knob, default 7, a capped lookback, never a full-history dump). Comparison is pure LEXICOGRAPHIC ISO string compare, same mechanism as `_format_incidents` but STRICT `>` (a deliberate divergence — the marker is a high-water mark, exactly-once semantics) — no datetime parsing anywhere in this section, so the TC16-F2 naive-timestamp crash class is structurally absent. Excludes nodes whose stamped identity matches my own email ("your own writes are not news to you"); UNSTAMPED nodes are INCLUDED (an awareness view reports content, not people — deliberate divergence from the rollup's attribution doctrine). Constraints are HEAD-filtered (mirrors `## Active Constraints`); decisions/incidents are not (mirrors their own global sections — same "mirror each type's existing semantics" precedent as TC16's manager-decisions section). Newest-first interleave across all three types, capped at `prime_digest_cap` (new knob, default 5) with the standard overflow line. A node can legitimately also appear in `## Your Manager's Recent Decisions` and the global `## Recent Decisions` — deliberate, same overlap ruling as TC16. Placed immediately after `## Your Manager's Recent Decisions`, before `## Your Recent Activity`. Gates on personalize + a resolvable email only — NOT on manager/subordinate role, so a role-less user on a multi-user graph still gets it.
- Two new knobs (`prime_digest_cap=5`, `prime_digest_fallback_days=7`) in both `PrimeConfig` and `Settings`, covered by the existing defaults-equivalence test. No separate on/off knob — self-gates on `prime_personalize` (TC16 no-dead-knob philosophy).

### Notes
- Peer-review HIGH (applied): the marker read-modify-write is genuinely racy on a shared machine without the lock — an unlocked RMW can lose the OTHER email's fresh entry. Tested with real `threading.Thread` + `threading.Barrier` (not a sequential call-then-call check, which would pass trivially even unlocked) plus a deterministic `threading.Event`-based proof that the lock primitive itself blocks a concurrent acquire attempt.
- Known limits (documented): per-machine semantics (the same email on two machines sees independent digests); ephemeral sandboxes re-fallback every session; marker granularity is session-start, not "actually read"; last-writer-wins on concurrent SAME-email sessions (benign — both stamps are "now" within seconds); unstamped-node inclusion means a graph with systematically broken git identity shows more "news"; lexicographic compare assumes uniform `+00:00` offsets (true of every write path at this pin) — a hand-edited journal entry with a non-UTC offset could sort wrongly near the cutoff, shared with `_format_incidents`, disclosed not fixed.
- No MCP tool changed — no tool-surface audit required. No dashboard changes, not even imports.
- Version bump held — batches with TC15 as the 0.24.0 candidate; the batch's LAST WP carries the bump.

**WP-TC16: role-aware prime sections (manager rollup / subordinate view).**

### Added
- **`## Your Team`** (manager role, personalized-only): one `TASK` scan bucketed by claimant email among a manager's direct reports (`reports_to_email` casefolds to the session email). In-progress rows show claimant + claim age (`- <summary> (<report name>, claimed <age>)`); blocked rows show `- <summary> (<report name>, blocked)`. A claim is **stale** iff its age is *strictly greater than* `prime_stale_claim_days` (new knob, default 7) — exactly-7-days-old is NOT stale, and a null/legacy `claimed_at` (no recorded `in_progress` transition) is NEVER stale. Capped at `prime_rollup_cap` (new knob, default 5) total rows, stale first, then blocked, then in-progress, recency-desc within each group, with an overflow line. Unclaimed/unstamped tasks and tasks claimed by non-reports never appear. Placed immediately after `## Team Critical`.
- **`## Your Manager's Recent Decisions`** (subordinate role, personalized-only): decision nodes whose stamped identity matches the session's manager email, newest first, capped at `prime_manager_decision_limit` (new knob, default 3). Deliberately **no** HEAD/supersession filter — mirrors the global `## Recent Decisions` section's own unfiltered model exactly, so a superseded decision can legitimately appear in both (a known, documented overlap; deduping would silently change the global section's own semantics). A dangling manager email (no registered person node) still works — matched by the stamped email string. Placed immediately after `## Your Team`.
- **Role derivation**: ONE `get_nodes_by_type(PERSON)` scan per prime run resolves MANAGER (has direct reports), SUBORDINATE (has a `reports_to_email`), both (middle manager — both sections), or neither (no new sections). A user with no person node, no reports either direction, or `prime_personalize=off` sees prime output byte-identical to before this feature — the strongest-pinned regression fixture.
- Three new knobs (`prime_stale_claim_days=7`, `prime_rollup_cap=5`, `prime_manager_decision_limit=3`) in both `PrimeConfig` and `Settings`, covered by the existing defaults-equivalence test.

### Changed
- **`_task_claimed_at` relocated** from `tools/cognition_tools.py` to a new light, stdlib-only shared module `cognition/task_meta.py` — `prime.py` cannot import `tools/cognition_tools` (it drags in chroma/embeddings, violating prime's light-import constraint), so the single-implementation claim-age computation needed a shared home outside both. `cognition_tools.py` re-exports the name (`from ..cognition.task_meta import _task_claimed_at`) so the old import path — including `tests/test_task.py`'s direct import — keeps working unchanged. `dashboard/api.py`'s import line updated to the new path (mechanical, zero dashboard behavior change; existing dashboard tests pass unmodified).

### Fixed
- **Mixed-case `claimed_by` email silently dropped from `## Your Team`** (gate finding, Vince) — `claimed_by.email` is a verbatim git-config provenance stamp, never casefolded at write time (unlike person emails, which ARE casefolded), so a report whose git config carries a mixed-case email vanished from their manager's rollup on a raw-vs-casefolded comparison. Now casefolded at read time, matching the existing `_task_matches_email`/`_node_email` precedent.
- **Naive (no-tzinfo) `claimed_at` crashed `generate_prime`** (gate finding, Vince, same class as TC9's `98dcca4` seniority-crash fixup) — a replayed/hand-edited journal entry with a naive ISO `at` timestamp parses fine via `datetime.fromisoformat` (no exception), but the subsequent aware-minus-naive subtraction raised an uncaught `TypeError` (not `ValueError`) in both `_humanize_claim_age` and `## Your Team`'s stale check — crashing the SessionStart hook for every user of that graph. Now: a shared `_parse_iso_datetime` helper normalizes a naive parse to UTC-aware before any subtraction, at both sites — write-side validation is not protection against replay.

### Notes
- Terminology: the manager/subordinate classification is a REPORTING relationship derived from `reports_to_email` — distinct from the pre-existing free-text `person.role` job title; never conflated.
- Ruling: graph owns HUMAN roles only — agent roles stay in teammate-comms (ruling `6be2e867f91e`).
- Known limits (documented): single-manager assumption (`reports_to_email` is one string; matrixed orgs out of scope); stale detection requires a transitions-recorded claim (pre-TC4-era claims with no `in_progress` entry never go stale); an unregistered claimant is invisible to the rollup even if their `reports_to_email` points at the manager.
- No MCP tool changed — no tool-surface audit required.
- Version bump held — batches with TC14/TC15 as the 0.24.0 candidate.

## [0.23.0] — 2026-07-15

**WP-TC4: claim-collision + reopen warnings on `cognition_update_task`.**

### Added
- **`claim_warning` on `cognition_update_task` success responses** — never blocks, with one enforced exception. `blocked → in_progress` over someone else's LIVE claim (`metadata.claimed_by` present, status `in_progress`/`blocked` — `open`/`done`/`cancelled` is a released claim) requires `note=`; without one, the call is rejected BEFORE mutation with an error naming the current claimant and claim age (kind `claim_collision` once a note is supplied). Every other collision-adjacent shape never blocks: a same-status `status="in_progress"` poke against a foreign live claimant — combo or bare — succeeds with `kind: "takeover_note_required"` and no restamp (a bare poke, which previously hit the generic "No updatable fields" error, now gets an explicit answer instead); the same shape WITH `note=` is a single-call takeover (`kind: "claim_collision"`) that restamps `claimed_by` and logs the seizure. Reopening someone else's `done`/`cancelled` task (`kind: "reopen"`) never requires a note — the ruling scopes the note requirement to takeover only — and is suppressed when the closing transition can't be attributed (no matching transitions entry, e.g. legacy data). `claim_warning` shape: `{kind, claimant: {name, email}, claimed_at, assigned_to (only when set), message}`; the key is absent for self-actions, released claims, and unverifiable identities (either side's git-config email blank — same never-wipe-on-unverified doctrine as `exclude_people`).

### Changed
- `_task_claimed_at` (last-wins scan for the latest `in_progress` transition) moved from `dashboard/api.py` into `tools/cognition_tools.py` — a single shared implementation now backs both the dashboard and `_update_task`'s claim-collision detection; the dashboard imports it back. Mechanical, zero dashboard behavior change.

### Notes
- Interpretation ruling (Colton, task detail): "claiming a task already claimed_by someone else returns a warning naming the current claimant and claim age — NEVER blocks; takeover (re-claim over a live claim) requires a transition note; reopening someone else's done/cancelled task gets the same warning shape." This WP resolves the "never blocks" vs. "requires a note" tension by enforcing the note ONLY for the two unambiguous transition-based takeover shapes (2a, and the seizure half of 2b) — every ambiguous or non-takeover shape warns-and-proceeds instead. Flagged for Colton's async review; if overruled, 2a's rejection relaxes to warning-plus-proceed as a one-flip patch (the warning plumbing is unchanged).
- Tool-surface self-sufficiency audit re-run over `cognition_update_task`.

**WP-TC9: seniority + agent-origin weighting on `cognition_search`.**

### Added
- **Penalty-only ranking weight on every `cognition_search` hit** — `weighted_score = score * weight.multiplier`, `multiplier` always in `(0, 1.0]` (never a boost). `score` (raw similarity) is never mutated, and weighting can only push a hit lower relative to its peers — it never hides a hit from `results` or wipes it. Every hit carries `weight: {multiplier, seniority, from_agent, basis}`, present even when neutral (`multiplier == 1.0`) — never silent. `basis` is one of `exempt:<node_type>` (constraint/incident, always pinned at 1.0), `agent` (`from_agent` stamped `true` — the multiplier is strictly below every human seniority tier, so human input always outweighs agent input), `human:<seniority>` (stamped + a matching registered person node — `owner`/`senior` 1.0, `mid` 0.95, `junior` 0.9), `human:unregistered` (stamped, no matching person node), or `unverified` (no identity stamp at all). Constraint/incident hits are never outranked by this mechanism — the exempt multiplier is pinned, not merely favored.
- **Multi-project (`project="*"`) resort now uses `weighted_score`, not raw `score`**, for the post-fan-out merge — without this the mechanism would silently no-op for aggregate search while single-project search still weighted correctly. Each fan-out entry builds and applies its own person registry (per-entry, not shared/merged across projects).
- **`cognition_get_workflow` deliberately inherits weighting** via its shared internal top-1 match search — this can change WHICH workflow a name/topic lookup resolves to, not just the order of a candidate list. Documented explicitly, not a silent side effect.

### Changed
- Person-email→seniority lookup is memoized once per top-level `cognition_search` call (one `get_nodes_by_type(PERSON)` scan), reused across every adaptive-widening round of that call — not rebuilt per round.

### Fixed
- **C1 cross-version tolerance for seniority reads** (gate finding, Vince) — `_hit_weight`'s `_SENIORITY_MULTIPLIERS[seniority]` and `_person_seniority_map`'s direct `["metadata"]["person"]["seniority"]` access both crashed `cognition_search` with `KeyError` on a person node carrying an out-of-vocabulary seniority tier (a newer plugin version, a hand-edited journal, a cross-version team) or one predating the `seniority` field entirely. Now: an unknown tier gets the neutral multiplier (never a penalty for vocabulary this build doesn't know) while the raw tier string still surfaces verbatim in `weight.seniority`/`basis`; a person node missing `seniority` falls through to the existing `human:unregistered` path instead of raising. The strict-dominance invariant (agent 0.85 below every seniority multiplier) is unaffected.

### Notes
- Scope: `cognition_search` only. `cognition_get_history`, `cognition_list_tasks`, session-start priming, and the dashboard's own search path are unaffected.
- Ruling (Colton): weighting never wipes or hides lower-seniority/agent findings; weights are always visible, never silent; constraints/incidents are never outranked by seniority; human input always outweighs agent input.
- Tool-surface self-sufficiency audit re-run over `cognition_search` (return shape gained `weight`/`weighted_score`).

## [0.22.0] — 2026-07-15

**WP-TC1: curation conflict lens.**

### Added
- **`agents/curate-conflict-analyzer.md`** — a new, dedicated propose-only Haiku subagent that hunts deliberately for `contradicts`/`supersedes` edges on stance-bearing nodes (`decision`/`constraint`/`pattern`/`assumption`). The general edge-analyzer treats `contradicts` as "genuinely rare" and never actively looks for it, so the graph had almost none; this pass exists specifically to fill that gap with a hardened precision bar (a false `contradicts` edge is a trust cost the analyzer is designed to avoid). Every proposal carries a `reason` plus verbatim quoted stances from both nodes, and `contradicts` is only proposed when both endpoints are current/HEAD; same-lineage evolution (especially same `recorded_by`) prefers `supersedes` instead. Output uses `"source": "curate-conflict"`, distinct provenance from the general edge-analyzer's `"curate-skill"`.
- **`curate-orchestrator.md` — new Step 3: Conflict Pass**, inserted between edge curation and cluster identification (cluster identification renumbered to Step 4). Spawns `curate-conflict-analyzer` with an explicit `model: "haiku"` override (same fail-f09e770da046 precedent as the existing analyzer spawns). The stance-bearing candidate list is captured at Step 1, immediately after the initial `cognition_get_uncurated_nodes` fetch — never re-fetched at the conflict pass's own insertion point, since Step 2's per-batch `cognition_mark_curated` calls would make a later fetch return empty (a silent zero-edge failure mode). A whole-run suspect cap discards the entire pass's proposals if, once at least 15 candidates have been examined, more than 20% came back `contradicts` — guarding against a systematic false-positive run. The mandated final-report line now includes a `conflict pass: X proposed / Y committed / Z discarded` clause.

### Verified
- **Precision eval**: the shipped analyzer definition (verbatim body) was exercised in-session via the Agent tool against a 36-pair labeled fixture (`tests/fixtures/conflict_lens_labeled_pairs.json`, authored independently by the gate) — 10 `contradicts`, 6 `supersedes`, 20 hard negatives. Result at the haiku pin: `contradicts` precision **1.0** (4/4, zero false positives) against a ≥0.9 bar; recall 0.4 (reported, not gated — the analyzer is deliberately biased toward `supersedes` when uncertain, which costs recall, not precision). No escalation to sonnet was needed. `scripts/eval_conflict_lens.py` validates the fixture's shape and scores a results file; it has no LLM/API access by design, so the actual per-pair judgment is agent-driven, not scriptable.
- **Real-graph smoke test** (`scripts/smoke_conflict_pass.py`): ran the conflict-pass scaffolding — Step 1 candidate-capture timing, and `source="curate-conflict"` edge-commit mechanics — against an isolated temp copy of the production `.cognition` journal (never the live graph). Confirmed the timing invariant (a candidate list captured before simulated Step 2 differs from a naive post-Step-2 re-fetch) and that edges commit correctly. The live production graph currently has 0 stance-bearing uncurated candidates, so a real run today reports `0 proposed / 0 committed / 0 discarded`.

### Notes
- Zero `src/` diff — this WP is agent-definition files, tests, docs, and scripts only; the curation pipeline's containment (orchestrator is the sole edge-writer, analyzers stay read-only/propose-only) and the `/vibe-curate` skill launcher (unchanged, stays generic) are untouched.

**WP-TC10: per-person search/task exclude filter + "returned N of M" completeness.**

### Added
- **`exclude_people` on `cognition_search` and `cognition_list_tasks`** — an optional comma-separated (casefolded) email list that drops hits/tasks authored by those identities. Matched on the same server-resolved stamp personalization uses (`recorded_by.email` for entities, `created_by.email` for tasks) — never the free-text `author`/`owner` field, and never an unstamped pre-P13n node (unverifiable is not the same as "matches"). **User-invoked only**: the docstring instructs the agent to add it ONLY when a human explicitly asks to filter people out for that call, never on its own initiative; there is no persistent/env-var muting, by design. `cognition_search`'s filter exempts constraint/incident hits (same never-wipe doctrine those node types get elsewhere) — an exempted hit passes through and does not count toward `excluded_count`. A filtered call discloses `excluded_count` (distinct nodes dropped) + `excluded_for` (the casefolded emails matched against), present iff the param was passed AND something was actually excluded — never a silent zero.
- **`total_found` / `exhaustive` on `cognition_search` and `cognition_get_history`** (always present, unconditional) — `total_found` is the distinct match count discovered (post-exclusion where applicable) before the response's `limit`-slice; `exhaustive` is `true` when that count is exact (the search space was fully seen) and `false` when it's a floor (stopped at `limit` or an internal cap — more matches may exist unseen). `count` (the length of `results`) can be less than `total_found` for reasons independent of any exclusion filter, simply because more matched than `limit` allowed through — this was previously invisible to callers. `cognition_get_history` is always exhaustive (a full structural scan); `cognition_search`'s adaptive vector search can genuinely stop early. Multi-project (`project="*"`/a tag set): `total_found` sums across entries, `exhaustive` AND-reduces, and `exclude_people`'s `excluded_count`/`excluded_for` likewise combine across entries.

### Changed
- `adaptive_vector_search`'s `dedupe` callback contract: now returns `(list, excluded_count)` instead of a bare list, and the list is no longer capped to `limit` internally — the caller (`adaptive_vector_search`) owns both the limit-slice and the total_found/exhaustive accounting. The dashboard's own search `_dedupe` closure was updated to conform (plumbing only; the dashboard JSON response is unchanged — it never exposes `total_found`/`exhaustive`/`excluded_count`).
- `storage.get_recent_nodes` gained an additive, keyword-only `with_total: bool = False` parameter — every pre-existing caller (prime.py's patterns/decisions sections, the dashboard's recent-episodes tile) is unaffected by default; `cognition_get_history`'s recency branch passes `with_total=True` to get the exact pre-slice count.

### Notes
- `_format_search_results` now unconditionally fetches the full node (`storage.get_node`) per hit (previously only for `document`-type hits, to compute staleness) — needed so the exclusion filter can read the server-resolved identity stamp; a cheap in-memory lookup, no new I/O.
- Tool-surface self-sufficiency audit re-run over `cognition_search`, `cognition_get_history`, and `cognition_list_tasks` (the three tools this WP touched).

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
