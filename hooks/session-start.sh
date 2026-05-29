#!/usr/bin/env bash
# SessionStart hook — installs deps, auto-configures per-project MCP, injects context.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
# Persistent plugin-data dir — survives plugin updates, so the venv never lives
# inside the version-pinned cache dir (a running server would lock it on Windows
# during /plugin update). Fall back to the version-independent parent of the
# cache dir if CLAUDE_PLUGIN_DATA is not provided.
PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-${PLUGIN_ROOT%/*}}"

# Normalize paths to forward-slash native format for uv / Python
if command -v cygpath &>/dev/null; then
    PLUGIN_ROOT_NATIVE=$(cygpath -m "$PLUGIN_ROOT")
    PROJECT_DIR_NATIVE=$(cygpath -m "$PROJECT_DIR")
    PLUGIN_DATA_NATIVE=$(cygpath -m "$PLUGIN_DATA")
else
    PLUGIN_ROOT_NATIVE="$PLUGIN_ROOT"
    PROJECT_DIR_NATIVE="$PROJECT_DIR"
    PLUGIN_DATA_NATIVE="$PLUGIN_DATA"
fi

VENV_DIR="${PLUGIN_DATA_NATIVE}/.venv"
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

# ── Step 3: Migrate away from the per-project MCP entry ──
# Earlier versions wrote a "vibe-cognition" entry into the project's .mcp.json.
# The server is now declared by the plugin itself (plugin.json), and a
# project-scope entry OUTRANKS the plugin definition — so remove any stale
# entry. The removal is surgical: only our entry is touched; every other MCP
# server and top-level key is preserved (see vibe_cognition.migrate_mcp).
MCP_JSON="${PROJECT_DIR_NATIVE}/.mcp.json"
UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
    uv run --no-sync --project "${PLUGIN_ROOT}" \
    python -m vibe_cognition.migrate_mcp "$MCP_JSON" >/dev/null 2>&1 || true

# ── Step 4: Inject project context via prime ──────
COGNITION_DIR="${PROJECT_DIR}/.cognition"

if [ -d "$COGNITION_DIR" ]; then
    PRIME_OUTPUT=$(UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
        REPO_PATH="${PROJECT_DIR_NATIVE}" \
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
