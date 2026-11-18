---
description: Launch the Vibe Cognition dashboard — a local web viewer for the project's cognition graph with embedding-powered search. Use when the user asks to open/show/launch the dashboard, the cognition graph viewer, or the project graph.
---

# Vibe Dashboard — Open the Cognition Graph Viewer

## What This Does

Launches the local web dashboard for the project's cognition graph: an interactive force-directed canvas of every node and edge, an episode timeline (newest first), embedding-powered search that highlights matches and their direct neighbors in the canvas, and a node-detail sidebar with a delete action.

The dashboard runs on `127.0.0.1` with a per-session token. No data leaves the machine.

## Steps

1. Call the `cognition_dashboard` MCP tool. Defaults are correct for nearly all cases:
   - `port` defaults to `7842` (falls back to `+1..+10`, then to an OS-assigned ephemeral port if all are busy)
   - `open_browser` defaults to `true` (opens the URL in the user's default browser)

2. Read the result and tell the user:
   - The URL (always include it — even when the browser opens automatically, the user may want to copy it).
   - Whether `status` is `running` (just started) or `already_running` (reusing the existing server — there is only ever one dashboard per MCP server).
   - Search readiness:
     - `embedding_ready: true` → semantic search is online.
     - `embedding_ready: false` with no `embedding_error` → the model is still loading; search will become available within ~30 seconds. The graph view itself works immediately.
     - `embedding_error` set → surface the error verbatim.

## When to Use

Trigger this skill when the user asks anything along the lines of:
- "Open the dashboard" / "show me the dashboard"
- "Open the cognition graph" / "visualize the graph"
- "Launch the graph viewer" / "I want to browse the graph"

Do **not** trigger it for queries that should be answered directly (e.g. "what decisions have we made about X?" — use `cognition_search` instead).

## Notes

- The dashboard is read-only except for node deletion. All other graph mutation goes through the regular `cognition_*` MCP tools.
- The URL is bound to `127.0.0.1` and includes a token; it cannot be shared across machines or sessions.
- The dashboard process dies with the MCP server (i.e. when Claude Code closes).
- If the user wants to launch the dashboard outside a Claude Code session, point them at the standalone CLI: `uv run vibe-cognition-dashboard --repo-path .` from the project root (requires the package on their PATH or via `uv tool install`).
