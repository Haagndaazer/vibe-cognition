#!/usr/bin/env bash
# Self-healing MCP server wrapper — ensures deps are installed before starting.
# Handles the case where SessionStart hook was skipped or failed.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT}"
VENV_DIR="${PLUGIN_ROOT}/.venv"
STAMP="${VENV_DIR}/.uv-sync-stamp"

# ── Check for uv ─────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo "Error: vibe-cognition requires 'uv' but it was not found." >&2
    echo "Install it: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi

# ── Conditional dependency install ────────────────
# Compute hash of dependency manifests to detect changes
if command -v sha256sum &>/dev/null; then
    HASH=$(cat "${PLUGIN_ROOT}/pyproject.toml" "${PLUGIN_ROOT}/uv.lock" 2>/dev/null | sha256sum | cut -d' ' -f1)
elif command -v shasum &>/dev/null; then
    HASH=$(cat "${PLUGIN_ROOT}/pyproject.toml" "${PLUGIN_ROOT}/uv.lock" 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
else
    HASH="no-hash-tool"
fi

if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$HASH" ]; then
    # Install or update dependencies
    UV_PROJECT_ENVIRONMENT="${VENV_DIR}" uv sync --project "${PLUGIN_ROOT}" --no-dev 2>/dev/null
    mkdir -p "${VENV_DIR}"
    echo "$HASH" > "$STAMP"
fi

# ── Start the MCP server ─────────────────────────
exec env UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
    REPO_PATH="${REPO_PATH:-${CLAUDE_PROJECT_DIR:-$(pwd)}}" \
    uv run --no-sync --project "${PLUGIN_ROOT}" python -m vibe_cognition.server
