#!/usr/bin/env bash
# SessionStart hook — installs deps, auto-configures per-project MCP, injects context.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
VENV_DIR="${PLUGIN_ROOT}/.venv"
STAMP="${VENV_DIR}/.uv-sync-stamp"

# Normalize paths to forward-slash format for JSON / Python
if command -v cygpath &>/dev/null; then
    PLUGIN_ROOT_NATIVE=$(cygpath -m "$PLUGIN_ROOT")
    PROJECT_DIR_NATIVE=$(cygpath -m "$PROJECT_DIR")
else
    PLUGIN_ROOT_NATIVE="$PLUGIN_ROOT"
    PROJECT_DIR_NATIVE="$PROJECT_DIR"
fi

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

# ── Step 3: Auto-configure per-project MCP server ─
# Write/update .mcp.json so the MCP server runs from the project directory.
# Done AFTER uv sync so --no-sync is safe on next startup.
MCP_JSON="${PROJECT_DIR}/.mcp.json"
MCP_UPDATED=$(UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
    uv run --no-sync --project "${PLUGIN_ROOT}" python -c "
import json, sys, os

mcp_path = sys.argv[1]
plugin_root = sys.argv[2]
project_dir = sys.argv[3]
venv_dir = sys.argv[4]

# Read existing .mcp.json or start fresh
try:
    with open(mcp_path) as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}
except json.JSONDecodeError:
    print('skip', end='')
    sys.exit(0)

if 'mcpServers' not in data:
    data['mcpServers'] = {}

# Build the expected entry
expected = {
    'command': 'uv',
    'args': ['run', '--directory', plugin_root, 'python', '-m', 'vibe_cognition.server'],
    'env': {
        'REPO_PATH': project_dir,
    },
}

current = data['mcpServers'].get('vibe-cognition')
if current == expected:
    print('ok', end='')
    sys.exit(0)

# Write updated config
data['mcpServers']['vibe-cognition'] = expected
tmp = mcp_path + '.tmp'
with open(tmp, 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')
os.replace(tmp, mcp_path)
print('updated', end='')
" "$MCP_JSON" "$PLUGIN_ROOT_NATIVE" "$PROJECT_DIR_NATIVE" "${PLUGIN_ROOT_NATIVE}/.venv" 2>/dev/null) || MCP_UPDATED=""

# If MCP config was just written/updated, tell user to restart
if [ "$MCP_UPDATED" = "updated" ]; then
    cat << 'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "# Vibe Cognition\n\nvibe-cognition MCP server has been configured for this project. **Please restart Claude Code so the MCP server can connect.** Subsequent sessions will start automatically."
  }
}
EOF
    exit 0
fi

# ── Step 4: Inject project context via prime ──────
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
