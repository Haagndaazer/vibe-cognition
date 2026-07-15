# Dashboard Redesign — From Constellation Viewer to Project-Cognition Dashboard

**Date:** 2026-07-14
**Epic:** `cedf4a8457e9` (team-cognition)
**Status:** Design / scoping ONLY — no implementation. Vorpid implements from this once approved.
**Companion:** `docs/dashboard-redesign-mockup.html` — static wireframe with fake data, no backend.
**Produced by:** Vince (manager). Code survey by an Explore subagent (Haiku); design reviewed by a second-opinion subagent (Sonnet) before delivery.

## 1. Ask

Colton: extend the dashboard to be meaningful for the task, workflow, and document node types added recently. Redesign it to feel like a **project management dashboard** (sprint-software feel: documentation, recent episodes, workflows, task views). The constellation viewer doesn't necessarily need to survive.

History check: the graph records dashboard bug/UX history but **no decision mandating a graph-first UI** — demoting the constellation contradicts nothing on record.

## 2. Current state (surveyed)

- **Stack:** Starlette + uvicorn (daemon thread, port 7842, loopback-only, 32-byte token auth), vanilla JS SPA (~870 lines frontend, ~770 backend), Cytoscape.js 3.30.2 vendored (~600 KB).
- **Layout today:** 3 columns — episodes+documents sidebar, force-directed graph canvas (the primary navigation), node detail pane. Header: embedding-status banner, semantic search, stats.
- **8 API endpoints:** graph, node get/delete, search, documents list/download, stats, static.
- **The problem in one sentence:** the UI is organized around *the graph as a picture*, while the graph's newest value (tasks, workflows, documents, provenance) is organized data the picture can't show — and the API doesn't even expose it:

| Graph has | Dashboard API exposes? |
|---|---|
| Task status / priority / owner / parent_id | ❌ (tasks render as generic dots) |
| Task transitions audit log, created_by, claimed_by | ⚠ in `/api/node/{id}` payload (metadata passes through), never rendered; absent from list payloads |
| recorded_by provenance on all nodes | ⚠ same — flows through node detail, stripped from `/api/graph`, unrendered |
| Workflow supersession chains (HEAD resolution) | ❌ |
| Document freshness (unchanged/modified/missing) | ❌ |
| Node detail, neighbors, semantic search | ✅ |

## 3. Design goals

1. **PM-dashboard feel:** the default view answers "what's the state of this project's work?" not "what does the graph look like?"
2. **First-class views** for tasks, workflows, documents, episodes — each node type gets the presentation its structure deserves (board, library, table, feed) instead of a colored dot.
3. **Team-cognition-ready:** every surface gets provenance chips now, and designed slots for what the epic will ship later (person nodes, seniority, agent-origin, conflict warnings) — slots designed today, features wired later.
4. **Read-only v1** (see §7).
5. **Keep what works:** embedding search, token auth model, vendored-assets rule (no CDN), dark theme, vanilla JS (no framework migration — the views below are lists/columns/tables, well within vanilla JS at this scale).

## 4. Proposed information architecture

Single-page app with a left **nav rail** replacing the current 3-column layout. Global header keeps: semantic search (opens results overlay), embedding-status banner, refresh. Every view opens nodes in a shared **detail drawer** (right side).

### 4.1 Overview (default view)
- **Stat tiles:** open tasks / in progress / blocked / done-this-week / documents / workflows (embedding status + node counts live in the header pills, not tiles — as mocked).
- **Active Constraints** strip (HEAD-filtered, severity-tagged) — mirrors the session-start prime's role: the things everyone must not violate, always visible.
- **Recent Episodes** feed (newest 5, one-line, → Activity).
- **Recent Incidents** (high-severity, last 14 days, mirroring prime defaults).
- **Attention flags:** stale in-progress tasks (claimed > N days), blocked tasks — small list, not a chart.

### 4.2 Board (tasks)
- **Kanban columns:** Open → In Progress → Blocked → Done (done capped to recent N; cancelled behind a toggle).
- **Cards:** summary, priority chip, epic/parent breadcrumb, creator chip, claimant chip + claim age.
- **Tree toggle:** epic hierarchy view (indented, like `cognition_list_tasks` depth) as the alternative to columns.
- **Detail drawer for a task adds:** full detail, **transition timeline** ({status, at, by, note} rendered as a vertical history), references, related nodes.

### 4.3 Workflows
- Library of **HEAD versions** (card: summary, last-updated, author). Detail: full procedure body + **version chain** rendered as history ("v3 supersedes v2 supersedes v1", clickable).

### 4.4 Documents
- Table: filename, mode (copy/reference), size, stored date, **freshness badge** (unchanged / modified / missing — needs API, cheap stat-check variant), download, count of descriptor nodes citing the doc_ref.
- Detail: indexed-text preview + linked descriptor nodes (the graph-integration health of the doc at a glance — an edgeless document is a known smell).

### 4.5 Activity
- Chronological feed of episodes, decisions, discoveries, incidents, fails — type filter chips + author filter. This inherits the constellation's real job (telling the project's story over time) in a form that scales past 500 nodes.

### 4.6 Graph (constellation, demoted)
- **Recommendation: keep as a lazy-loaded tab in v1, primary in nothing.** Rationale: it's already built and vendored (zero new cost), still useful for curation debugging (spotting edgeless clusters); its *navigation* job is replaced by the detail drawer's related-nodes list. Revisit deletion after v2 if usage is zero — deleting now buys nothing but forecloses cheaply-kept utility.

### 4.7 Detail drawer (shared, replaces constellation as navigation)
- Everything the current detail pane shows **plus:** provenance block (recorded_by / created_by / claimed_by / author, clearly labeling the trust class of each), related nodes **grouped by edge type** (supersedes/contradicts visually loud; relates_to quiet), and task/workflow/document extras per above.
- **Trust-class labeling applies to list-level chips too, not just the drawer.** Every person chip anywhere in the UI is backed by **server-resolved identity** (`recorded_by`, or `created_by`/`claimed_by` for tasks); when a node predates WP-P13n and has no `recorded_by`, the chip falls back to free-text `author` and MUST be visually distinct (e.g. dashed outline + "unverified") — a free-text name rendered identically to a verified one is exactly the ambiguity decision `6be2e867f91e` exists to kill. (Review finding: the mockup currently renders both identically — treat the mockup as layout, this paragraph as the spec.)
- **Conflict banner slot:** when the node has a `contradicts` edge or is a non-HEAD supersedes member, a banner names the conflicting/superseding node — this is the dashboard face of the epic's conflict-surfacing pipeline (tasks `888a21f729dd`).

## 5. Team-cognition slots (designed now, wired later)

| Epic feature (when it ships) | Dashboard slot |
|---|---|
| Person nodes + seniority (`1c29cff92e20`) | Author chips become person chips w/ seniority badge; a **People** view (per-person activity, reports-to) becomes v3 |
| Agent-origin bool (decision `6be2e867f91e`) | Small "via agent" badge on cards/drawer next to the human chip |
| Conflict pipeline (`888a21f729dd`) | §4.7 conflict banner + Board card warning icon |
| Seniority weighting (`f746c5f9361e`) | Search overlay shows the visible weight labeling (weighted-never-wiped rendering lives here too) |

## 6. API work the redesign needs (backend gap list)

1. `/api/tasks` — mirrors `cognition_list_tasks` + metadata: status, priority, owner, parent_id, depth, created_by, claimed_by, timestamps.
2. `/api/node/{id}` — **no backend change needed**: `metadata` (transitions, recorded_by, created_by, claimed_by, parent_id) already passes through unfiltered; the gap is purely that the frontend never renders it. (`/api/graph` does strip fields — that stays; list views use the new endpoints instead.)
3. `/api/workflows` — HEAD workflows + supersession chains.
4. `/api/documents` — add freshness field (cheap stat-only check, like search's `staleness`) **and a descriptor-citation count per doc_ref** (§4.4's "cited by N" / inert-document flag; storage already maintains a reference index, so this is an aggregation, not a new index).
5. `/api/activity` — recent nodes by type + timestamp (or derive client-side from `/api/graph`, which already carries type/summary/timestamp — implementer's call).
6. Unchanged: search, stats, delete, download, auth model.

## 7. Non-goals & open questions

- **Read-only v1 — deliberate.** No task-status writes from the dashboard. Reason beyond scope: MCP writes stamp server-resolved git identity; a dashboard click has no *viewer* identity, so a write would stamp whoever's machine runs the server — misleading provenance, exactly what WP-P13n fixed. Revisit only with a real answer to "who clicked?". (Node delete already exists and stays, stamped `removed_by="dashboard"`.)
- **No multi-user serving / remote access** — loopback + token stays as-is.
- **No framework migration** — vanilla JS remains; the redesign is views + endpoints, not a rewrite.
- **Open:** should Done column default to last-7-days or last-N? Should Activity include tasks' transitions as feed items? Both implementer-decidable.

## 8. Risks & sequencing

- **Threadpool spawn risk (`4163f54f2848`):** the dashboard runs `run_in_threadpool` on its own uvicorn loop; new endpoints + polling add load to that path. Resolve or explicitly accept it in the same WP that adds endpoints — do not let the redesign silently widen a known-risk surface.
- **Token hygiene (`ebe050e78923`): already shipped (done)** — listed as a don't-regress guard, not open work: the redesign WPs touch the same files, so the fix's invariants (no URL+token INFO logging) must hold through them.
- **D-3 UX remainder (`d7071a4377ee`): folded into this redesign.** Its remaining scope (auto-poll polish + `--no-embeddings` banner) is subsumed by the rebuilt views; close D-3 into the redesign task when the redesign WP is cut.

## 9. Phasing (scoping opinion)

- **V1 — the PM core:** nav rail + Overview + Board + upgraded detail drawer; API items 1–2; constellation demoted to tab. This alone delivers "sprint-software feel".
- **V2 — the libraries:** Workflows + Documents views + Activity feed; API items 3–5.
- **V3 — team-cognition wiring:** People view, conflict banners, seniority/agent-origin chips — sequenced behind the epic's person-node and conflict-pipeline tasks, slots already in place from v1/v2.
