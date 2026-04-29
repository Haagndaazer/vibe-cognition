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
   claude plugin marketplace add https://github.com/Haagndaazer/vibe-cognition
   claude plugin install vibe-cognition@coltondyck
   ```

3. Restart Claude Code. The embedding model (~250MB) downloads automatically on first start.

That's it. The plugin handles dependency installation, MCP server registration, hooks, and skills automatically.

## Table of Contents

- [Features](#features)
- [How It Works](#how-it-works)
- [What's Included](#whats-included)
- [Prerequisites](#prerequisites)
- [MCP Tools](#mcp-tools)
- [Dashboard](#dashboard)
- [Storage](#storage)
- [Cognition History Graph](#cognition-history-graph)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Uninstall / Cleanup](#uninstall--cleanup)
- [Development](#development)

## Features

- **Project Knowledge Graph**: Capture decisions, failures, discoveries, assumptions, constraints, incidents, and patterns
- **Semantic Search**: Find project history using natural language through local vector embeddings
- **Deterministic Edge Creation**: `part_of` edges are created instantly via reference matching (shared commit/issue/PR refs) — no LLM needed
- **Manual & Batch Edge Creation**: Create edges individually or in bulk via MCP tools, with provenance tracking
- **Curation Skill**: `/vibe-curate` skill with edge-analyzer and cluster-analyzer subagents for semantic edge creation and cluster identification
- **Local Dashboard**: Interactive graph viewer with semantic search and node-detail sidebar — launch in your browser from Claude or the CLI
- **Session Context Injection**: Start every Claude Code session with recent project context via hooks
- **Auto-Capture**: Automatically create episode nodes from git commits via hooks
- **Local-First**: All processing and storage happens on your machine — no API keys, no cloud services
- **Git-Committed Knowledge**: The cognition journal is designed to be committed to Git and shared with your team

## How It Works

Vibe Cognition uses:
- **ChromaDB** for local vector storage of cognition node embeddings
- **sentence-transformers** for generating embeddings locally
- **NetworkX** for building and querying the in-memory cognition graph
- **Ollama** (optional) for automatic edge creation between related nodes via a local LLM

### Embedding Model

By default, Vibe Cognition uses [nomic-ai/nomic-embed-text-v1.5](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) from Hugging Face. The model (~250MB) is downloaded automatically on first server start — no manual setup required.

Alternatively, you can use **Ollama** as an embedding backend if you prefer to manage models separately.

## What's Included

The plugin bundles everything needed — no manual configuration required:

| Component | What It Does |
|-----------|-------------|
| **MCP Server** | 13 tools for recording, searching, querying, and visualizing the knowledge graph |
| `/vibe-cognition` skill | Teaches Claude when and how to capture decisions, failures, discoveries, patterns |
| `/vibe-curate` skill | Curates semantic edges and identifies clusters using edge-analyzer and cluster-analyzer subagents |
| `/vibe-backfill` skill | Backfills the cognition graph from git commit history |
| `/vibe-dashboard` skill | Launches the local graph dashboard via the `cognition_dashboard` MCP tool |
| **SessionStart hook** | Injects recent project context (constraints, patterns, decisions, incidents) at session start |
| **PostToolUse hook** | Auto-creates episode nodes from git commits |

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- [uv](https://github.com/astral-sh/uv) — Python package manager (handles Python installation and dependencies automatically)
- Internet access for first run (downloads Python dependencies and the embedding model, ~250MB)

### Resource Requirements

- **Disk**: ~2-4GB for Python dependencies (includes PyTorch), ~250MB for the embedding model (cached at `~/.cache/huggingface/`)
- **RAM**: ~1-2GB for the embedding model at runtime
- **Disk (if using curator)**: additional ~5.5GB for the Ollama model
- **GPU**: Not required. CPU is the default; GPU is used automatically when available

### First Run

The first time the MCP server starts in a new session, `uv` automatically installs Python dependencies. This takes 30-60 seconds on the first run. Subsequent sessions start instantly.

The embedding model (~250MB) also downloads on first use from Hugging Face. After that, it's cached locally at `~/.cache/huggingface/`.

## MCP Tools

### Cognition Tools

| Tool | Purpose |
|------|---------|
| `cognition_record` | Record a knowledge node (decision, fail, discovery, pattern, episode, etc.) |
| `cognition_search` | Search project history by natural language |
| `cognition_get_chain` | Traverse causal reasoning chains (LED_TO edges) |
| `cognition_get_history` | Browse nodes by context area, type, or recency |
| `cognition_add_edge` | Create a single edge between two nodes |
| `cognition_add_edges_batch` | Create multiple edges in one call (max 500) |
| `cognition_get_edgeless_nodes` | Find nodes with no edges (need curation) |
| `cognition_get_uncurated_nodes` | Find nodes not yet reviewed by the curation skill |
| `cognition_mark_curated` | Mark a node as reviewed by the curation skill |
| `cognition_get_neighbors` | Get all connections to a node (all edge types) |
| `cognition_remove_edge` | Remove a specific edge between two nodes |

### Service Tools

| Tool | Purpose |
|------|---------|
| `get_status` | Graph statistics, embedding status, curator info, edge-type breakdown |
| `cognition_dashboard` | Launch the local web dashboard (graph viewer + semantic search). Returns the URL and opens it in your browser. |

## Dashboard

The dashboard is a local web viewer for the cognition graph: an interactive force-directed layout of every node and edge, an episode timeline (newest first) on the left, embedding-powered search that highlights matches and their direct neighbors in the graph, and a node-detail sidebar with delete capability.

It runs on `127.0.0.1` and is protected by a per-session token included in the URL. No data leaves your machine.

### Launch from Claude Code (recommended)

Two equivalent paths inside a Claude Code session:

- **Slash command** — type `/vibe-dashboard` and press enter.
- **Natural language** — ask Claude something like *"open the cognition dashboard"* or *"show me the graph"*.

Either route invokes the `cognition_dashboard` MCP tool, which boots a local HTTP server inside the already-running MCP process and opens the URL in your default browser. Calling it again returns the same URL — there's only ever one dashboard per MCP server.

### Launch from the CLI

For browsing without an active Claude Code session, run from your project directory:

```bash
uv run vibe-cognition-dashboard
```

Or point at any project from anywhere:

```bash
uv run vibe-cognition-dashboard --repo-path /path/to/your/project
```

Useful flags:

| Flag | Effect |
|------|--------|
| `--port N` | Preferred port (default `7842`; falls back to `+1..+10`, then ephemeral) |
| `--no-browser` | Don't auto-open a browser (just print the URL) |
| `--no-embeddings` | Skip loading the embedding model — the graph view loads instantly but search returns 503. Useful when you only need to browse the structure. |

The CLI runs uvicorn in the foreground; press Ctrl-C to stop.

### What you can do

- **Pan / zoom** the canvas to explore connections; nodes are colored by type (decision, fail, discovery, pattern, episode, …).
- **Click a node** → its label and its direct neighbors' labels appear; the node-detail sidebar shows the full summary, detail, references, and incoming/outgoing edges.
- **Click an episode** in the left panel → focuses that episode and its connected entities in the canvas.
- **Search** with natural language → matching nodes get a yellow border, and the canvas dims everything outside their immediate neighborhood so you can see *where* the matches live.
- **Delete a node** from the sidebar (it's removed from `journal.jsonl` and the embedding index — irreversible).

## Storage

Vibe Cognition stores all data in a single `.cognition/` directory within your project:

```
your-project/
├── .cognition/
│   ├── journal.jsonl       # Cognition graph (Git-committed, team-shared)
│   └── chromadb/            # Cognition vector embeddings (gitignored, regenerable)
└── ... your code
```

- **`.cognition/journal.jsonl`** should be **committed to Git** — it's the shared project knowledge base
- **`.cognition/chromadb/`** should be in `.gitignore` — it's a regenerable cache (rebuilt automatically on next server startup if deleted)

> **Important:** Only gitignore `.cognition/chromadb/`, NOT the entire `.cognition/` directory. The journal file must be committed to Git for team sharing.

Add to your project's `.gitignore`:
```bash
echo '.cognition/chromadb/' >> .gitignore
```

## Cognition History Graph

The cognition graph captures project knowledge — decisions made, approaches that failed, non-obvious discoveries, constraints, incidents, and patterns — so future sessions have context on *why* the code is the way it is.

### How It Works

1. **Record nodes** during conversations via `cognition_record` (or automatically via the post-commit hook)
2. **Deterministic matching** instantly creates `part_of` edges when nodes share references (commit hashes, issue/PR numbers)
3. **Semantic edges** (led_to, resolved_by, supersedes) are created via the `/vibe-curate` skill, manual `cognition_add_edge` calls, or the opt-in background curator
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

### Edge Types

| Edge | Meaning | How Created |
|------|---------|-------------|
| `part_of` | Entity belongs to an episode | Deterministic (automatic via shared references) |
| `led_to` | Causal chain — X led to Y | Semantic (via `/vibe-curate` skill or manual) |
| `resolved_by` | Problem X was fixed by Y | Semantic |
| `supersedes` | X replaces Y | Semantic |
| `contradicts` | X conflicts with Y | Semantic |
| `relates_to` | Same topic, no causal link | Semantic (use sparingly) |
| `duplicate_of` | X is semantically identical to Y | Curator only (triggers merge) |

The graph uses a **MultiDiGraph** — multiple edge types between the same pair of nodes are supported (e.g., A can be both `part_of` B and `led_to` B). Each (from, to, edge_type) triple is unique.

### Curation

Edges are created through three mechanisms:

1. **Deterministic matching** (always on): `part_of` edges are created automatically when nodes share references. No setup needed.
2. **`/vibe-curate` skill** (recommended): Bundled with the plugin. Provides semantic edge creation (led_to, resolved_by, supersedes) and cluster identification via subagents.
3. **Background curator** (optional, disabled by default): Uses a local Ollama LLM to automatically create semantic edges in the background.

**For the background curator:**
1. Install [Ollama](https://ollama.com)
2. The curator model (`qwen3:8b`) is pulled automatically on first server start
3. Requires ~5.5GB VRAM (or runs on CPU, slower)

## Configuration

All configuration is optional. Vibe Cognition works out of the box with sensible defaults.

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `REPO_PATH` | Current directory | Repository path |
| `REPO_NAME` | Directory name | Repository name |
| `EMBEDDING_BACKEND` | `sentence-transformers` | Backend: `sentence-transformers` or `ollama` |
| `EMBEDDING_MODEL` | `nomic-ai/nomic-embed-text-v1.5` | Model for sentence-transformers |
| `EMBEDDING_DIMENSIONS` | `768` | Embedding vector dimensions |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL (if using Ollama) |
| `OLLAMA_MODEL` | `nomic-embed-text` | Ollama embedding model |
| `CURATOR_ENABLED` | `false` | Enable automatic background edge curation via local LLM |
| `CURATOR_MODEL` | `qwen3:8b` | Ollama model for cognition graph curation |
| `CURATOR_MAX_CANDIDATES` | `8` | Max candidate nodes to evaluate per curation |
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

**ChromaDB lock / database errors** — Only one Vibe Cognition instance can run per project at a time. Check for duplicate MCP server instances or other processes using `.cognition/chromadb/`.

**Curator not creating edges** — Verify Ollama is running (`ollama list`). Without Ollama, the curator logs a warning and does not create edges. Nodes are still stored and searchable.

**Model download failures** — The embedding model (~250MB) is downloaded from Hugging Face on first run. Check your internet connection and proxy settings. Corporate firewalls may block Hugging Face downloads.

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

## License

MIT
