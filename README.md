![Python Version](https://img.shields.io/badge/python-3.11--3.13-blue?style=flat&logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green?style=flat)
![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)
![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)
![MCP](https://img.shields.io/badge/MCP-Server-purple?style=flat)
# Vibe Cognition

A fully local [MCP](https://modelcontextprotocol.io/) server for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that captures project knowledge — decisions, failures, discoveries, patterns, and more — so future sessions have context on *why* the code is the way it is. After a one-time model download, all processing and storage happens on your machine — no API keys, no cloud services.

## Quick Start

```bash
git clone https://github.com/Haagndaazer/vibe-cognition.git
cd vibe-cognition
uv sync
```

Then from your project directory:

```bash
cd /path/to/your-project
claude mcp add vibe-cognition \
  --env REPO_PATH="$PWD" \
  -- uv run --directory /path/to/vibe-cognition python -m vibe_cognition.server
```

Restart Claude Code. The embedding model (~250MB) downloads automatically on first start.

> **Don't have uv?** See [Installing uv](#installing-uv). **On Windows?** See [Platform Notes](#platform-notes).

## Table of Contents

- [Features](#features)
- [How It Works](#how-it-works)
- [Installation](#installation)
- [Usage with Claude Code](#usage-with-claude-code)
- [MCP Tools](#mcp-tools)
- [Storage](#storage)
- [Cognition History Graph](#cognition-history-graph)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Uninstall / Cleanup](#uninstall--cleanup)
- [Development](#development)

## Features

- **Project Knowledge Graph**: Capture decisions, failures, discoveries, assumptions, constraints, incidents, and patterns
- **Semantic Search**: Find project history using natural language through local vector embeddings
- **Automatic Edge Creation**: A local LLM curator automatically links related knowledge nodes
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

## Installation

### Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — Vibe Cognition is an MCP server designed for Claude Code
- Python 3.11-3.13 (`python --version` to check — or let `uv` manage it for you)
- [uv](https://github.com/astral-sh/uv) package manager
- Internet access for first run (downloads the embedding model, ~250MB)

#### Installing uv

**macOS / Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell):**
```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

> After installing, restart your terminal so `uv` is available on your PATH.

#### Resource Requirements

- **Disk**: ~2-4GB for Python dependencies (includes PyTorch), ~250MB for the embedding model (cached at `~/.cache/huggingface/`)
- **RAM**: ~1-2GB for the embedding model at runtime
- **Disk (if using curator)**: additional ~5.5GB for the Ollama model
- **GPU**: Not required. CPU is the default; GPU is used automatically when available

#### Platform Notes

Shell examples in this README use bash syntax (macOS, Linux, Git Bash on Windows). Key things to know:

- `uv sync`, `uv run`, and most commands work identically in PowerShell
- Claude Code on Windows uses its own bundled bash for hooks and the Bash tool, so hook commands in `.claude/settings.json` should use **forward-slash paths** (e.g., `C:/Users/me/vibe-cognition`)
- `$PWD` works in both bash and PowerShell. If you use cmd.exe, substitute `%CD%` or the full path

### Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/Haagndaazer/vibe-cognition.git
   cd vibe-cognition
   ```

2. Install dependencies:
   ```bash
   uv sync
   ```

3. **(Optional) Pre-download the embedding model:**
   ```bash
   uv run python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('nomic-ai/nomic-embed-text-v1.5', trust_remote_code=True)"
   ```
   This downloads the model (~250MB) ahead of time so the server starts instantly. **You can skip this step** — the model downloads automatically on first server start, but the initial startup will take 30+ seconds while it downloads.

   > `trust_remote_code=True` is required by the nomic model's custom architecture. The code comes from the [nomic-ai HuggingFace repository](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5). Review it there if you want to audit before first run.

4. **Verify the installation:**
   ```bash
   uv run python -c "from vibe_cognition.server import mcp; print(f'OK: {mcp.name}')"
   ```
   You should see `OK: Vibe Cognition`. If you get import errors, check that `uv sync` completed successfully.

5. **(Optional) Curator setup** — The cognition curator uses [Ollama](https://ollama.com) to automatically link knowledge nodes. It is **enabled by default**, but **Ollama is not required** — without it, the server works normally; nodes are stored and searchable, they just won't have automatic edges between them.

   If you have Ollama installed, the curator model (`qwen3:8b`, ~5.5GB) is pulled automatically on first server start. To explicitly disable:
   ```bash
   # Add --env CURATOR_ENABLED=false when registering the MCP server (see next section)
   ```

That's it! No API keys or external service configuration needed.

## Usage with Claude Code

Navigate to your project directory and add Vibe Cognition as an MCP server:

**bash (macOS / Linux / Git Bash):**
```bash
cd /path/to/your-project

claude mcp add vibe-cognition \
  --env REPO_PATH="$PWD" \
  -- uv run --directory /path/to/vibe-cognition python -m vibe_cognition.server
```

**PowerShell (Windows):**
```powershell
cd C:\path\to\your-project

claude mcp add vibe-cognition `
  --env REPO_PATH="$PWD" `
  -- uv run --directory C:/Users/me/vibe-cognition python -m vibe_cognition.server
```

Replace `/path/to/vibe-cognition` (or `C:/Users/me/vibe-cognition`) with the absolute path to your Vibe Cognition clone. The `--` separates `claude mcp add` options from the server command.

`$PWD` expands to your current directory at the time you run this command, so make sure you run it from your project's root directory.

> **Note:** After adding the MCP server, exit your current Claude Code session and start a new one for changes to take effect. The embedding model loads in the background on startup (2-30 seconds). Cognition tools that require embeddings (`cognition_search`) may return "still loading" briefly. Other tools (`cognition_record`, `cognition_get_chain`, `cognition_get_history`, `get_status`) are available immediately.

## MCP Tools

### Cognition Tools

- `cognition_record` - Record a knowledge node (decision, fail, discovery, pattern, episode, etc.)
- `cognition_search` - Search PROJECT HISTORY (decisions, failures, patterns) by natural language
- `cognition_get_chain` - Traverse causal reasoning chains between nodes
- `cognition_get_history` - Browse cognition nodes by context area, type, or recency

### Service Tools

- `get_status` - Get cognition graph statistics, embedding status, and curator info

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

1. **Record nodes** during conversations via `cognition_record` (or automatically via hooks)
2. **Curator LLM** (Qwen3 8B via Ollama) automatically creates edges between related nodes in the background
3. **Query** with `cognition_search` (semantic) or `cognition_get_history` (by context/type)

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

### Edge Types (created automatically by curator)

| Edge | Meaning |
|------|---------|
| `led_to` | Causal chain — X led to Y |
| `supersedes` | X replaces Y |
| `contradicts` | X conflicts with Y |
| `relates_to` | Same topic, no causal link |
| `resolved_by` | Problem X was fixed by Y |
| `part_of` | Entity belongs to an episode |
| `duplicate_of` | X is semantically identical to Y |

### Setup: Curator (Optional but Recommended)

The curator is **enabled by default** (`CURATOR_ENABLED=true`). It uses a local Ollama LLM to automatically create meaningful edges between cognition nodes. Without it, nodes are stored but not connected.

1. Install [Ollama](https://ollama.com)
2. The curator model (`qwen3:8b`) is pulled automatically on first server start
3. Requires ~5.5GB VRAM (or runs on CPU, slower)

**No Ollama?** The server works fine without it. Nodes are stored, searchable via `cognition_search`, and browsable via `cognition_get_history`. The only thing missing is automatic edge creation between nodes.

To disable the curator explicitly, add `--env CURATOR_ENABLED=false` when registering the MCP server.

### Setup: Auto-Capture Hooks (Optional)

#### Prime — Inject project context at session start

The `vibe-cognition-prime` command outputs recent constraints, patterns, decisions, and incidents. Configure it as a Claude Code hook so every session starts with project context:

Add to your project's `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "uv run --directory /path/to/vibe-cognition vibe-cognition-prime"
      }]
    }],
    "PreCompact": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "uv run --directory /path/to/vibe-cognition vibe-cognition-prime"
      }]
    }]
  }
}
```

> When run as a Claude Code hook from your project directory, `REPO_PATH` is not needed — the hook defaults to the current working directory.

#### Post-Commit — Auto-create episodes from git commits

The post-commit hook creates episode nodes automatically when commits happen during Claude Code sessions:

Add to your project's `.claude/settings.json` (merge with existing hooks):

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "Bash",
      "hooks": [{
        "type": "command",
        "command": "python3 /path/to/vibe-cognition/agents/hooks/post-commit.py"
      }]
    }]
  }
}
```

> This script uses only Python stdlib, so it does not require `uv run`. Use `python3` on macOS/Linux. On Windows, use `python` with forward-slash paths (e.g., `python C:/Users/me/vibe-cognition/agents/hooks/post-commit.py`).

#### Backfill — Find commits missing episodes

The `vibe-cognition-backfill` command finds recent git commits without corresponding episode nodes and outputs instructions for creating them:

**bash:**
```bash
cd /path/to/your-project
REPO_PATH="$PWD" uv run --directory /path/to/vibe-cognition vibe-cognition-backfill
```

**PowerShell:**
```powershell
cd C:\path\to\your-project
$env:REPO_PATH = "$PWD"; uv run --directory C:/path/to/vibe-cognition vibe-cognition-backfill
```

Also available as the `/vibe-backfill` skill in Claude Code if you copy `agents/vibe-backfill` to your project's `.claude/skills/` directory.

### Setup: Skill File (Optional)

Copy the `agents/vibe-cognition` directory from the Vibe Cognition repo to your project's `.claude/skills/` directory. Create `.claude/skills/` first if it doesn't exist.

This teaches the LLM to use concise entity summaries (<250 chars), create episodes for completed work, and always include references for curator linking.

## Configuration

All configuration is optional. Vibe Cognition works out of the box with sensible defaults.

| Environment Variable | Required | Default | Description |
|---------------------|----------|---------|-------------|
| `REPO_PATH` | No | Current directory | Repository path |
| `REPO_NAME` | No | Directory name | Repository name |
| `EMBEDDING_BACKEND` | No | `sentence-transformers` | Backend: `sentence-transformers` or `ollama` |
| `EMBEDDING_MODEL` | No | `nomic-ai/nomic-embed-text-v1.5` | Model for sentence-transformers |
| `EMBEDDING_DIMENSIONS` | No | `768` | Embedding vector dimensions |
| `OLLAMA_BASE_URL` | No | `http://localhost:11434` | Ollama server URL (if using Ollama) |
| `OLLAMA_MODEL` | No | `nomic-embed-text` | Ollama embedding model |
| `CURATOR_ENABLED` | No | `true` | Enable automatic cognition edge curation via local LLM |
| `CURATOR_MODEL` | No | `qwen3:8b` | Ollama model for cognition graph curation |
| `CURATOR_MAX_CANDIDATES` | No | `8` | Max candidate nodes to evaluate per curation |
| `LOG_LEVEL` | No | `INFO` | Logging level |

### Using a `.env` File

Instead of passing `--env` flags, you can create a `.env` file **in the vibe-cognition clone directory** (not in your project):

```env
REPO_PATH=C:/Users/me/my-project
CURATOR_ENABLED=false
```

> **Note:** A `.env` file sets `REPO_PATH` to a single project. If you use Vibe Cognition with multiple projects, use `--env REPO_PATH=...` per-project instead.

> **Windows users**: Always use forward slashes in `.env` file paths (e.g., `C:/Users/me/project`). Backslashes are interpreted as escape sequences by python-dotenv (`\t` = tab, `\n` = newline, `\v` = vertical tab), which will silently corrupt your paths.

### Using Ollama for Embeddings (Optional)

If you prefer to use Ollama for embeddings:

1. Install and start [Ollama](https://ollama.com)
2. Pull an embedding model: `ollama pull nomic-embed-text`
3. Configure Vibe Cognition:
   ```bash
   claude mcp add vibe-cognition \
     --env REPO_PATH="$PWD" \
     --env EMBEDDING_BACKEND="ollama" \
     -- uv run --directory /path/to/vibe-cognition python -m vibe_cognition.server
   ```

## Troubleshooting

**"Embedding model is still loading"** — Search tools need the embedding model, which loads in the background on startup (2-30 seconds). Other cognition tools work immediately. Wait and try again.

**ChromaDB lock / database errors** — Only one Vibe Cognition instance can run per project at a time. Check for duplicate MCP server instances or other processes using `.cognition/chromadb/`.

**Curator not creating edges** — Verify Ollama is running (`ollama list`). Without Ollama, the curator logs a warning and does not create edges. Nodes are still stored and searchable.

**Model download failures** — The embedding model (~250MB) is downloaded from Hugging Face on first run. Check your internet connection and proxy settings. Corporate firewalls may block Hugging Face downloads.

**General** — `.cognition/chromadb/` is always safe to delete. It is fully regenerated on the next server startup.

## Uninstall / Cleanup

To remove Vibe Cognition:

1. Remove the MCP server:
   ```bash
   claude mcp remove vibe-cognition
   ```

2. Delete the regenerable cache from your project:
   ```bash
   rm -rf .cognition/chromadb/
   ```

3. Optionally delete the cognition history (warning: this deletes shared project knowledge):
   ```bash
   rm -rf .cognition/
   ```

4. Remove any hooks you added to `.claude/settings.json` (SessionStart, PreCompact, PostToolUse entries for vibe-cognition)

5. Remove the cached embedding model (shared across all projects):
   ```bash
   rm -rf ~/.cache/huggingface/hub/models--nomic-ai--nomic-embed-text-v1.5/
   ```

## Development

```bash
# Run tests
uv run pytest

# Run linting
uv run ruff check .

# Run type checking
uv run pyright
```

## License

MIT
