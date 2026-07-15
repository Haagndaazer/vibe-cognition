![Python Version](https://img.shields.io/badge/python-3.11--3.13-blue?style=flat&logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green?style=flat)
![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)
![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)
![MCP](https://img.shields.io/badge/MCP-Server-purple?style=flat)
# Vibe Cognition

A fully local [MCP](https://modelcontextprotocol.io/) server for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that captures project knowledge — decisions, failures, discoveries, patterns, and more — so future sessions have context on *why* the code is the way it is. After a one-time model download, all processing and storage happens on your machine — no API keys, no cloud services.

## Quick Start

1. Install [uv](https://github.com/astral-sh/uv) if you don't have it:

   **macOS / Linux:**
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
   **Windows (PowerShell):**
   ```powershell
   irm https://astral.sh/uv/install.ps1 | iex
   ```

2. Add the marketplace and install the plugin:
   ```bash
   claude plugin marketplace add https://github.com/Haagndaazer/colton-claude-plugins
   claude plugin install vibe-cognition@coltondyck
   ```

3. Restart Claude Code. First start is a two-stage wait: Python dependencies install via
   `uv` (30-60 seconds), then the embedding model (~250MB) downloads automatically from
   Hugging Face. Subsequent sessions start instantly — both are one-time costs.

That's it. The plugin handles dependency installation, MCP server registration, hooks, and skills automatically.

**How do I know it worked?** Ask Claude to run the `get_status` tool, or just start
working — a new project shows an onboarding message in your first session's context
(the SessionStart hook injects it automatically). `get_status`'s `embedding_status` field
moves `loading` → `ready` as the model finishes loading; cognition tools other than
search work immediately, before that.

## Table of Contents

- [Features](#features)
- [How It Works](#how-it-works)
- [What's Included](#whats-included)
- [Prerequisites](#prerequisites)
- [MCP Tools](#mcp-tools)
- [Dashboard](#dashboard)
- [Storage](#storage)
- [Cognition History Graph](#cognition-history-graph)
- [Working as a Team](#working-as-a-team)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Uninstall / Cleanup](#uninstall--cleanup)
- [Development](#development)

## Features

- **Project Knowledge Graph**: Capture decisions, failures, discoveries, assumptions, constraints, incidents, and patterns
- **Document Storage**: Store client docs, PDFs, and specs as first-class `document` nodes (reference or copy mode) with agent-extracted, searchable text; descriptor nodes that cite the returned `doc:<hash>` auto-link to the document (see the `/vibe-document` skill)
- **Semantic Search**: Find project history using natural language through local vector embeddings
- **Deterministic Edge Creation**: `part_of` edges are created instantly via reference matching (shared commit/issue/PR refs) — no LLM needed
- **Manual & Batch Edge Creation**: Create edges individually or in bulk via MCP tools, with provenance tracking
- **Curation Skill**: `/vibe-curate` skill with edge-analyzer and cluster-analyzer subagents for semantic edge creation and cluster identification
- **Local Dashboard**: Interactive graph viewer with semantic search and node-detail sidebar — launch in your browser from Claude or the CLI
- **Session Context Injection**: Start every Claude Code session with recent project context via hooks
- **Local-First**: All processing and storage happens on your machine — no API keys, no cloud services
- **Git-Committed Knowledge**: The cognition journal is designed to be committed to Git and shared with your team

## How It Works

Vibe Cognition uses:
- **ChromaDB** for local vector storage of cognition node embeddings
- **sentence-transformers** for generating embeddings locally
- **NetworkX** for building and querying the in-memory cognition graph
- **Ollama** (optional) as an alternative local embeddings backend

### Embedding Model

By default, Vibe Cognition uses [nomic-ai/nomic-embed-text-v1.5](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) from Hugging Face. The model (~250MB) is downloaded automatically on first server start — no manual setup required.

Alternatively, you can use **Ollama** as an embedding backend if you prefer to manage models separately.

## What's Included

The plugin bundles everything needed — no manual configuration required:

| Component | What It Does |
|-----------|-------------|
| **MCP Server** | 29 tools for recording, searching, querying, and visualizing the knowledge graph |
| `/vibe-cognition` skill | Teaches Claude when and how to capture decisions, failures, discoveries, patterns |
| `/vibe-curate` skill | Curates semantic edges and identifies clusters using edge-analyzer and cluster-analyzer subagents |
| `/vibe-backfill` skill | Backfills the cognition graph from git commit history (watermark-based — finds everything untracked since the last backfilled commit, however old) |
| `/vibe-dashboard` skill | Launches the local graph dashboard via the `cognition_dashboard` MCP tool |
| **SessionStart hook** | Injects recent project context (constraints, patterns, decisions, incidents) at session start |

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- [uv](https://github.com/astral-sh/uv) — Python package manager (handles Python installation and dependencies automatically)
- Internet access for first run (downloads Python dependencies and the embedding model, ~250MB)

### Resource Requirements

- **Disk**: ~2-4GB for Python dependencies (includes PyTorch), ~250MB for the embedding model (cached at `~/.cache/huggingface/`)
- **RAM**: ~1-2GB for the embedding model at runtime
- **GPU**: Not required. CPU is the default; GPU is used automatically when available

### First Run

The first time the MCP server starts in a new session, `uv` automatically installs Python dependencies. This takes 30-60 seconds on the first run. Subsequent sessions start instantly.

The embedding model (~250MB) also downloads on first use from Hugging Face. After that, it's cached locally at `~/.cache/huggingface/`.

## MCP Tools

### Cognition Tools

| Tool | Purpose |
|------|---------|
| `cognition_record` | Record a knowledge node (decision, fail, discovery, pattern, episode, etc.) |
| `cognition_add_task` | File a trackable task, server-attributed to the git user (open work + lifecycle) |
| `cognition_list_tasks` | List the backlog: open tasks, priority-sorted, grouped by parent |
| `cognition_update_task` | Update a task's status/owner/priority/parent/assignment in place (status-transition and assignment logged) |
| `cognition_register_person` | Register a HUMAN identity (never an agent) as a first-class person node |
| `cognition_update_person` | Edit a person's profile fields in place (audit-trailed via `profile_history`) |
| `cognition_get_person` | Get a person's full profile, including the `profile_history` audit trail |
| `cognition_list_people` | List every registered person — the team roster |
| `cognition_store_document` | Store a document (reference or copy mode) + extracted text as a first-class node |
| `cognition_get_document` | Retrieve a stored document: metadata + full text + freshness |
| `cognition_readme` | Get the full orientation guide + getting-started walkthrough (same content as the `/vibe-cognition` skill) |
| `cognition_get_node` | Read a single node's full narrative (incl. `detail`) by id |
| `cognition_update_node` | Edit a node's narrative (summary/detail/context/severity) in place; re-embeds on text change |
| `cognition_search` | Search project history by natural language |
| `cognition_get_chain` | Traverse causal reasoning chains (LED_TO edges) |
| `cognition_get_superseded_chain` | Walk a node's version history via SUPERSEDES (newest first) |
| `cognition_get_workflow` | Find a workflow procedure by name/topic; resolves to the current HEAD version |
| `cognition_get_incident_resolution` | Get an incident + its resolutions, follow-ons, and contradictions |
| `cognition_get_history` | Browse nodes by context area, type, or recency |
| `cognition_add_edge` | Create a single edge between two nodes |
| `cognition_add_edges_batch` | Create multiple edges in one call (max 500) |
| `cognition_get_edgeless_nodes` | Find nodes with no edges (need curation) |
| `cognition_get_uncurated_nodes` | Find nodes not yet reviewed by the curation skill |
| `cognition_mark_curated` | Mark a node as reviewed by the curation skill |
| `cognition_get_neighbors` | Get all connections to a node (all edge types) |
| `cognition_remove_edge` | Remove a specific edge between two nodes |
| `cognition_remove_node` | Delete a node and all its attached edges (destructive; also purges its embedding) |
| `cognition_reload` | Force-reload the graph from the on-disk journal (diagnostic; a running server DOES pick up teammates' new nodes automatically on its normal catch-up path — this tool is for confirming that, not the only way it happens) |

### Service Tools

| Tool | Purpose |
|------|---------|
| `get_status` | Graph statistics, embedding status (including a `syncing` state — see [Joining an Existing Graph](#joining-an-existing-graph)), edge-type breakdown |
| `cognition_dashboard` | Launch the local web dashboard (graph viewer + semantic search). Returns the URL and opens it in your browser. |

### Cross-Project Tools

Query another project's cognition graph from your current session (e.g. a monorepo
sibling, or a related repo you keep open side-by-side) without switching directories.
Attaching is READ-ONLY — it never writes to the foreign project's journal or embeddings.

| Tool | Purpose |
|------|---------|
| `cognition_load_project` | Attach a foreign project's `.cognition/` graph to this session by path; returns a short `tag` to reference it |
| `cognition_unload_project` | Detach a previously-loaded foreign project |
| `cognition_list_projects` | List all currently-loaded projects (home + foreign) and their status |

Once loaded, pass `project=<tag>` (or the path) to most read tools (`cognition_search`,
`cognition_get_node`, `cognition_get_history`, `cognition_get_neighbors`, …) to route
that call at the foreign project instead of your home one. Pass `project="*"` on
aggregate tools like `cognition_search` to search across every loaded project at once.
If the foreign project's embedding model or vector dimensions differ from yours,
semantic search on it is disabled (structural reads still work) — the load response's
`model_guard` field tells you which case you're in.

## Dashboard

The dashboard is a local, read-only web view of the cognition graph, organized like a project-management tool rather than a picture of a graph. A left nav rail switches between three views, and every view opens nodes in a shared detail drawer on the right:

- **Overview** (default) — stat tiles (open/in-progress/blocked tasks, done this week, documents, workflows), active constraints, a needs-attention list (stale claims, blocked tasks), recent episodes, and recent high-severity incidents (last 14 days).
- **Board** — a kanban view of tasks (Open / In Progress / Blocked / Done, done capped to the most recent 20 with cancelled behind a toggle), with a tree-view toggle for the epic/subtask hierarchy. Cards show priority, creator, claimant, and claim age.
- **Graph** — the original interactive force-directed constellation, kept for curation debugging (spotting edgeless clusters). It's lazy-loaded: nothing is fetched or constructed until you open the tab, and the 30-second auto-refresh updates it in place rather than rebuilding it.

The shared **detail drawer** (opened from a Board card, a Graph node, or any list row) shows the full node — summary, detail, references — plus a provenance block (who recorded/created/claimed it, with a visually distinct dashed "unverified" chip for older nodes that predate server-resolved identity and only have a free-text author), a task's transition timeline, related nodes grouped by edge type, and a conflict banner when the node is contradicted or superseded by a newer version. A global header keeps semantic search, the embedding-status banner, refresh, and stats — unchanged from before the redesign.

Documents and Workflows browsing (dedicated views, freshness/citation metadata) and an Activity feed are not yet in the dashboard — planned for a later pass; `/api/documents` and `/api/document/{id}/download` still work if called directly. The dashboard has no write path beyond node delete (it has no per-request viewer identity, so any other write would stamp misleading provenance).

It runs on `127.0.0.1` and is protected by a per-session token included in the URL. No data leaves your machine.

### Launch from Claude Code (recommended)

Two equivalent paths inside a Claude Code session:

- **Slash command** — type `/vibe-dashboard` and press enter.
- **Natural language** — ask Claude something like *"open the cognition dashboard"* or *"show me the graph"*.

Either route invokes the `cognition_dashboard` MCP tool, which boots a local HTTP server inside the already-running MCP process and opens the URL in your default browser. Calling it again returns the same URL — there's only ever one dashboard per MCP server.

### Launch from the CLI

The recommended route is the MCP tool / natural language above — it just works for
plugin users. The standalone CLI below requires a checkout of THIS repository (the
package is not a dependency of your project, and as a plugin it lives in plugin-data,
not your project venv). From a clone of this repo:

```bash
uv run --directory /path/to/vibe-cognition vibe-cognition-dashboard --repo-path /path/to/your/project
```

(`--repo-path` defaults to the current directory, so from your own project you would
still need the package available — hence `--directory` pointing at this repo's checkout.)

Useful flags:

| Flag | Effect |
|------|--------|
| `--port N` | Preferred port (default `7842`; falls back to `+1..+10`, then ephemeral) |
| `--no-browser` | Don't auto-open a browser (just print the URL) |
| `--no-embeddings` | Skip loading the embedding model — the graph view loads instantly but search returns 503. Useful when you only need to browse the structure. |

The CLI runs uvicorn in the foreground; press Ctrl-C to stop.

### What you can do

- **Switch views** with the nav rail — Overview, Board, Graph.
- **Click a task card, list row, or graph node** → opens the shared detail drawer with the full node, provenance, and (for tasks) the transition timeline.
- **Toggle Board** between kanban columns and a tree view of the epic/subtask hierarchy; a checkbox reveals cancelled tasks.
- **Pan / zoom** the Graph tab's canvas to explore connections; nodes are colored by type (decision, fail, discovery, pattern, episode, task, person, …).
- **Search** with natural language → matching nodes get a yellow border in the Graph tab if it's loaded, and results open directly in the detail drawer either way.
- **Delete a node** from the drawer (it's removed from `journal.jsonl` and the embedding index — irreversible).

## Storage

Vibe Cognition stores all data in a single `.cognition/` directory within your project:

```
your-project/
├── .cognition/
│   ├── journal.jsonl       # Cognition graph (Git-committed, team-shared)
│   └── chromadb/            # Cognition vector embeddings (gitignored, regenerable)
└── ... your code
```

`.cognition/` is the **only** thing the plugin writes into your project. The MCP server is declared by the plugin itself and resolves your project directory automatically (via `CLAUDE_PROJECT_DIR`), so there is no per-project `.mcp.json` to manage. Python dependencies live in the plugin's own data directory, not in your repo. (If you installed an earlier version that wrote a `vibe-cognition` entry into your project's `.mcp.json`, the plugin removes just that entry on next start — other servers are left untouched.)

- **`.cognition/journal.jsonl`** should be **committed to Git** — it's the shared project knowledge base
- **`.cognition/chromadb/`** should be in `.gitignore` — it's a regenerable cache (rebuilt automatically on next server startup if deleted)

> **Important:** Only gitignore `.cognition/chromadb/`, NOT the entire `.cognition/` directory. The journal file must be committed to Git for team sharing.

Add to your project's `.gitignore`:
```bash
echo '.cognition/chromadb/' >> .gitignore
```

### Automatic Git Hygiene

On first startup in a new project, vibe-cognition automatically configures two git hygiene rules for `.cognition/`:

1. **`.gitattributes`** — adds `.cognition/journal.jsonl merge=union` so concurrent journal appends from different branches/clones union-merge cleanly instead of conflicting. (`merge=union` is a built-in git merge driver; it only affects 3-way merge resolution and never rewrites the journal blob.)
2. **`.cognition/.gitignore`** — adds `chromadb/` so the regenerable vector cache is never accidentally committed.

Both writes are **idempotent** (existing files are appended, never clobbered) and happen exactly once per working copy, tracked by a local flag file `.cognition/.git-hygiene-managed`. The committed rules (`.gitattributes`, `.cognition/.gitignore`) travel to teammates via git; the flag is git-ignored so every fresh clone self-heals with one pass on first startup.

**Opt out:** set `VIBE_COGNITION_NO_GIT_HYGIENE=1` to suppress the entire pass (useful in single-shared-checkout repos that use the worktree-flush protocol instead of union-merge).

**Re-arm:** delete `.cognition/.git-hygiene-managed` to make the pass re-run and re-add any rule you removed.

## Cognition History Graph

The cognition graph captures project knowledge — decisions made, approaches that failed, non-obvious discoveries, constraints, incidents, and patterns — so future sessions have context on *why* the code is the way it is.

### How It Works

1. **Record nodes** during conversations via `cognition_record`
2. **Deterministic matching** instantly creates `part_of` edges (and `relates_to` for document→episode) when nodes share references (commit hashes, issue/PR numbers, `doc:` keys) — the only automatic edges
3. **Semantic edges** (led_to, resolved_by, supersedes) are the agent's job: after recording, run the `/vibe-curate` skill (or add them with `cognition_add_edge`)
4. **Query** with `cognition_search` (semantic) or `cognition_get_history` (by context/type)

### Node Types

| Type | Purpose |
|------|---------|
| `decision` | A choice between alternatives (and why) |
| `fail` | An approach that didn't work |
| `discovery` | A non-obvious finding |
| `assumption` | A premise being relied on |
| `constraint` | A hard limitation or scoping exclusion |
| `incident` | A production problem |
| `pattern` | A reusable lesson learned |
| `episode` | Full narrative of completed work (Linear task, feature, debugging session) |
| `workflow` | A step-by-step procedure stored as ONE unit; versioned by supersession (update = new node + `supersedes` edge) |
| `task` | Trackable open work, server-attributed to the git user; mutable status/priority + arbitrary-depth parent hierarchy (created with `cognition_add_task`) |
| `person` | A HUMAN identity — name, role, seniority, reports-to (never an agent; agent identity lives in teammate-comms); updated in place with an audit trail (created with `cognition_register_person`) |

### Edge Types

| Edge | Meaning | How Created |
|------|---------|-------------|
| `part_of` | Entity belongs to an episode, or a descriptor to a document | Deterministic (entity↔episode on any shared ref; entity→document on a shared `doc:` ref) |
| `led_to` | Causal chain — X led to Y | Semantic (via `/vibe-curate` skill or manual) |
| `resolved_by` | Problem X was fixed by Y | Semantic |
| `supersedes` | X replaces Y (THE reconciliation edge for duplicates — same node type on both ends, no cycles, enforced) | Semantic |
| `contradicts` | X conflicts with Y | Semantic |
| `relates_to` | Same topic, no causal link | Deterministic (document→episode on a shared `doc:` ref) OR semantic (`/vibe-curate` or manual) |

The graph uses a **MultiDiGraph** — multiple edge types between the same pair of nodes are supported (e.g., A can be both `part_of` B and `led_to` B). Each (from, to, edge_type) triple is unique.

### Curation

Edges are created through two mechanisms:

1. **Deterministic matching** (always on): `part_of` and (for document→episode) `relates_to` edges are created automatically when nodes share references. No setup needed. This is the *only* automatic edge creation.
2. **`/vibe-curate` skill** (launches a background curator): Triggering curation is the agent's responsibility — after recording any nodes, the agent runs the `/vibe-curate` skill to launch a background curate-orchestrator agent, which creates semantic edges (led_to, resolved_by, supersedes) and identifies clusters via Haiku subagents. The main agent never authors these edges itself.

## Working as a Team

The journal is designed to be shared — see the [topology guide](docs/topology-guide.md)
for choosing between **shared checkout** (multiple agents in one working directory,
needs a specific flush protocol) and **separate clones** (each teammate's own clone,
mostly automatic via `merge=union`), and the full protocol for whichever fits your setup.

### Joining an Existing Graph

When you attach to a project that already has cognition history (a fresh clone, or a
new teammate's first session), there's a brief window where the graph is usable but not
fully searchable yet:

1. **Structural tools work immediately** — `cognition_get_node`, `cognition_get_history`,
   `cognition_get_neighbors`, and friends read the on-disk journal directly; no waiting.
2. **Semantic search needs the embedding model AND a sync pass.** `get_status`'s
   `embedding_status` field tells you where things stand: `loading` (model still
   downloading/initializing — only on a machine's first-ever run), then `syncing` (model
   ready, but historical nodes recorded before this session's embedding index existed
   are still being embedded in the background — search may miss some older nodes during
   this window), then `ready` (fully caught up). `embedding_sync_progress` gives counts
   while `syncing`.
3. **How long this takes** scales with graph size, not something you need to plan
   around for typical projects — a sync pass processes existing nodes in the background
   without blocking any tool call; you can keep working the whole time.

### Attribution — who wrote this node

Provenance is verified for **tasks and deletions**, not for other node authorship:

- **Task creator** (`cognition_add_task`'s `created_by`) is resolved server-side from
  your git config — a client can't override it. This is unspoofable: the tool doesn't
  even accept a caller-supplied creator.
- **Who deleted a node** (`cognition_remove_node`) is likewise resolved server-side and
  stamped as `removed_by` on the journal tombstone — verified the same way task creation is.
- **Entity author** (the `author` argument on `cognition_record` — decisions, fails,
  discoveries, etc.) is arbitrary client-supplied text. It's advisory, not verified —
  in practice this is often an agent persona name rather than a human name, which is a
  legitimate use, not a bug. Don't treat the `author` field on a decision/discovery/
  pattern/etc. node as proof of who actually wrote it the way you can for a task's
  `created_by` or a deletion's `removed_by`.

### Team semantics — person nodes and `from_agent`

**Person nodes** (`cognition_register_person`) model your team's HUMANS — name, role,
seniority (`owner | senior | mid | junior`), and a direct `reports_to_email` — so the
graph knows *about* the people behind its provenance stamps, not just their emails.
Agent identity is never stored here; agents live in teammate-comms. A person node is
**updated in place** (never supersession-versioned) with an append-only
`metadata.profile_history` audit trail (`{changed: {field: {from, to}}, at, by}` per
edit) — so "who changed what, when" is always recoverable even though the current
profile can be edited by anyone.

**`from_agent`** is a provenance bool stamped on every write from `cognition_record`,
`cognition_add_task`, `cognition_store_document`, `cognition_register_person`, and
`cognition_update_person` — `true` (the default) means "recorded via an agent call, as
usual"; `false` is the deliberately marked case where a human explicitly dictated or
authored the content themselves. It surfaces wherever provenance already does:
`cognition_search` results, `cognition_get_node`, and `cognition_list_tasks` rows. A
node written before this feature shipped simply has no `from_agent` key — that surfaces
as `null`/absent ("unverified/legacy"), **never** coerced to `true` or `false`.

**Stamped task assignment.** `metadata.assigned_to` (set via `assigned_to_email` on
`cognition_add_task`/`cognition_update_task`) is who a task is directed AT — an
email-keyed, identity-matched target distinct from the free-text, never-matched
`owner`. An assigned task surfaces under the assignee's "Your Open Tasks" at their next
session start even if they neither created nor claimed it. Assigning is **not**
claiming: the assignee still accepts the work by transitioning it to `in_progress`
themselves, same as any other claim. Every EFFECTIVE (genuinely different-email)
assignment, reassignment, or unassignment (`assigned_to_email=""`) appends one entry to
the append-only `metadata.assignments` audit trail (`{to, at, by}`, `by`
server-resolved); resubmitting the same email is a no-op. The target need not be a
registered person node yet — dangling is legal, mirroring `reports_to_email`.

**Known trust-model limits** (documented, not "fixed" — this is a local trust domain,
not a security boundary): `reports_to_email` and `seniority` are trust-declared and
freely peer-editable — the audit trail is the control, not an ACL, and anyone sharing
the graph may update anyone's profile. `from_agent` is client-declared and unverifiable
by the server (like the `author` field on `cognition_record`). `assigned_to` is
similarly client-declared and unverifiable — the server proves who made the assignment
(`assignments[].by`), never that the assignee accepted or even saw it; there is no
notification beyond the assignee's next session-start prime. Person data is only as
fresh as the team keeps it — nothing enforces it, though the onboarding notice below
reduces how often it goes stale.

**New-user onboarding notice.** When a session's git identity resolves to an email
with no matching person node yet, the session-start prime digest opens with a `## New
Here?` notice prompting the agent to ask the human for their name, role, seniority,
and manager, then call `cognition_register_person` with `from_agent=false`. If the
human would rather skip it, the agent appends their casefolded email to
`.cognition/onboard-declined` (a per-machine, git-ignored file — never synced, never a
placeholder person node) instead of registering them. The notice disappears the
session after a matching person node lands, or immediately once declined. Set
`PRIME_ONBOARD=false` to disable the notice outright.

## Configuration

All configuration is optional. Vibe Cognition works out of the box with sensible defaults.

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `REPO_PATH` | Current directory | Repository path |
| `REPO_NAME` | Directory name | Repository name |
| `EMBEDDING_BACKEND` | `sentence-transformers` | Backend: `sentence-transformers` or `ollama` |
| `EMBEDDING_MODEL` | `nomic-ai/nomic-embed-text-v1.5` | Model for sentence-transformers |
| `EMBEDDING_DIMENSIONS` | `768` | Embedding vector dimensions |
| `EMBEDDING_REVISION` | (unset — Hub HEAD) | Pin the sentence-transformers model to a specific HuggingFace Hub revision (branch, tag, or commit SHA), instead of always pulling HEAD of the model's remote code. Recommended for production/reproducible setups. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL (if using Ollama for embeddings) |
| `OLLAMA_MODEL` | `nomic-embed-text` | Ollama embedding model |
| `LOG_LEVEL` | `INFO` | Logging level |

### Using Ollama for Embeddings (Optional)

If you prefer to use Ollama for embeddings instead of sentence-transformers:

1. Install and start [Ollama](https://ollama.com)
2. Pull an embedding model: `ollama pull nomic-embed-text`
3. Set `EMBEDDING_BACKEND=ollama` in your environment

This avoids the ~2GB sentence-transformers/PyTorch dependency.

## Troubleshooting

**"Embedding model is still loading"** — Search tools need the embedding model, which loads in the background on startup (2-30 seconds). Other cognition tools work immediately. Wait and try again.

**MCP server fails to connect** — Ensure `uv` is installed and on your PATH. Run `uv --version` to check. The plugin uses `uv` to manage Python dependencies and launch the server.

**Multiple sessions on the same project — is that supported?** Yes, running several
Claude Code sessions against one project (multiple agents, or you + a teammate on
separate clones) is a normal, supported mode — this is NOT single-instance software.
The real caveat: each session's MCP server hydrates its in-memory graph from
`journal.jsonl` once at its own startup, so a session doesn't automatically see nodes a
DIFFERENT session recorded after it started — it picks them up on its normal catch-up
path as it makes calls, and `get_status`'s `embedding_status` reports `syncing` (rather
than `ready`) while a just-started session's embedding index is still catching up on
historical nodes it hasn't embedded yet. If you're setting up multiple agents in ONE
shared working directory specifically (not just multiple independent sessions), see the
[topology guide](docs/topology-guide.md) — that setup has its own protocol.

**ChromaDB lock / database errors** — this is unrelated to running multiple sessions
(see above); check for a stale lock file under `.cognition/chromadb/` from a process
that didn't shut down cleanly, or antivirus/backup software holding a file open.

**Journal permanently shows as modified, or replay resets after merges (Windows / autocrlf)** — The journal is replayed by byte offset, so line-ending normalization must never rewrite its bytes; on `core.autocrlf` setups this holds only by convention. If `git status` permanently shows `.cognition/journal.jsonl` as modified, or logs show "re-hydrated from top" after merges/pulls, add `.cognition/*.jsonl merge=union -text` to your repo-root `.gitattributes` — EARLY in the graph's life. Do not retrofit `-text` onto a grown shared-checkout journal without a planned cut-over: the first commit after adding it re-normalizes the file once, which live sessions see as a replaced journal. (Auto-configuration writes only `merge=union`, never `-text`.)

**Semantic edges not appearing** — Curation is agent-driven: after recording nodes, run the `/vibe-curate` skill to launch the background curator, which creates semantic edges. Only `part_of` edges (from shared references) are automatic. Nodes are stored and searchable regardless.

**Model download failures** — The embedding model (~250MB) is downloaded from Hugging Face on first run. Check your internet connection and proxy settings. Corporate firewalls may block Hugging Face downloads.

**"A dependency update did not finish" / server won't start after a plugin update** — A
plugin update that changes a heavy dependency (e.g. swapping the PyTorch build) can fail
mid-install if another Claude Code session still has the old version's files open
(mainly a Windows file-locking issue). The plugin self-heals: **close ALL Claude Code
sessions and windows, then open ONE** — the next session-start hook detects the
half-installed state and finishes the repair automatically.

**Windows: several old `vibe_cognition.server` processes still running** — Each Claude
Code session spawns its own server process; on Windows these do not always get reaped
when their parent session ends, and orphans have been observed persisting for days,
each holding 0.5-1GB of RAM. This is a known, still-open limitation — there is no
automatic self-exit yet. Workaround: periodically check Task Manager (or
`Get-Process python -ErrorAction SilentlyContinue`) for accumulated `vibe_cognition`
processes with no corresponding open Claude Code window, and end them manually. Don't
end a process tied to a session you're still using.

**General** — `.cognition/chromadb/` is always safe to delete. It is fully regenerated on the next server startup.

## Uninstall / Cleanup

1. Uninstall the plugin:
   ```bash
   claude plugin uninstall vibe-cognition@coltondyck
   ```

2. Optionally remove the marketplace:
   ```bash
   claude plugin marketplace remove coltondyck
   ```

3. Delete the regenerable cache from your project:
   ```bash
   rm -rf .cognition/chromadb/
   ```

4. Optionally delete the cognition history (warning: this deletes shared project knowledge):
   ```bash
   rm -rf .cognition/
   ```

5. Remove the cached embedding model (shared across all projects):
   ```bash
   rm -rf ~/.cache/huggingface/hub/models--nomic-ai--nomic-embed-text-v1.5/
   ```

## Development

To contribute or run Vibe Cognition from source:

```bash
# Clone and install
git clone https://github.com/Haagndaazer/vibe-cognition.git
cd vibe-cognition
uv sync

# Register as a manual MCP server (for testing against a project)
cd /path/to/your-project
claude mcp add vibe-cognition \
  --env REPO_PATH="$PWD" \
  -- uv run --directory /path/to/vibe-cognition python -m vibe_cognition.server

# Run tests
uv run pytest

# Run linting
uv run ruff check .

# Run type checking
uv run pyright
```

### Standalone CLIs (from a checkout of this repo)

Like `vibe-cognition-dashboard` (see [Dashboard](#dashboard)), these run from a
clone of this repo via `uv run --directory /path/to/vibe-cognition ...` and are
NOT dependencies of your project:

- **`vibe-cognition-backfill`** — a plain report (no LLM extraction) of git
  commits not yet tracked as episode nodes, over a FIXED 30-day window. This
  is a narrower, scriptable alternative to the `/vibe-backfill` skill, not a
  substitute for it — the skill finds the watermark (the newest tracked
  episode's commit reference) and walks forward however far back that is; the
  CLI never looks further back than 30 days, so an untracked commit older
  than that is invisible to it. Prefer the skill for a graph that hasn't been
  backfilled recently.
- **`vibe-cognition-prime`** — prints the same session-start context digest
  the SessionStart hook injects automatically; useful for previewing it by hand.

## License

MIT
