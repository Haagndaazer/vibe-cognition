#!/usr/bin/env bash
# SessionStart hook — installs deps (if needed) and injects project context.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"

# Write project dir marker for MCP server to read at startup
echo "$PROJECT_DIR" > "${PLUGIN_ROOT}/.active-project"
VENV_DIR="${PLUGIN_ROOT}/.venv"
STAMP="${VENV_DIR}/.uv-sync-stamp"

# ── Step 1: Check for uv ─────────────────────────
if ! command -v uv &>/dev/null; then
    cat << 'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "WARNING: vibe-cognition plugin requires 'uv' (Python package manager) but it was not found on PATH. Install it: https://docs.astral.sh/uv/getting-started/installation/"
  }
}
EOF
    exit 0
fi

# ── Step 2: Conditional dependency install ────────
if command -v sha256sum &>/dev/null; then
    HASH=$(cat "${PLUGIN_ROOT}/pyproject.toml" "${PLUGIN_ROOT}/uv.lock" 2>/dev/null | sha256sum | cut -d' ' -f1)
elif command -v shasum &>/dev/null; then
    HASH=$(cat "${PLUGIN_ROOT}/pyproject.toml" "${PLUGIN_ROOT}/uv.lock" 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
else
    HASH="no-hash-tool"
fi

if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$HASH" ]; then
    UV_PROJECT_ENVIRONMENT="${VENV_DIR}" uv sync --project "${PLUGIN_ROOT}" --no-dev 2>/dev/null
    mkdir -p "${VENV_DIR}"
    echo "$HASH" > "$STAMP"
fi

# ── Step 3: Inject project context via prime ──────
COGNITION_DIR="${PROJECT_DIR}/.cognition"

if [ -d "$COGNITION_DIR" ]; then
    PRIME_OUTPUT=$(UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
        REPO_PATH="${PROJECT_DIR}" \
        uv run --no-sync --project "${PLUGIN_ROOT}" \
        python -m vibe_cognition.cognition.prime 2>/dev/null) || PRIME_OUTPUT=""

    if [ -n "$PRIME_OUTPUT" ]; then
        echo "$PRIME_OUTPUT"
        exit 0
    fi
fi

# No cognition dir or prime failed — output empty
echo '{}'
exit 0
