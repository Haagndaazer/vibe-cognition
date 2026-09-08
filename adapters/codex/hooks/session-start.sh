#!/usr/bin/env bash
# Codex SessionStart wrapper: registers the MCP server with Codex (user-level,
# so the server inherits the project cwd), then hands off to the shared hook.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT is not set (Codex sets it for plugin hooks)}"
PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:?CLAUDE_PLUGIN_DATA is not set (Codex sets it for plugin hooks)}"
mkdir -p "$PLUGIN_DATA"

export VIBE_HARNESS=codex
export VIBE_UPDATE_NUDGE=off

if command -v cygpath >/dev/null 2>&1; then
    ROOT_NATIVE=$(cygpath -m "$PLUGIN_ROOT")
    DATA_NATIVE=$(cygpath -m "$PLUGIN_DATA")
else
    ROOT_NATIVE="$PLUGIN_ROOT"
    DATA_NATIVE="$PLUGIN_DATA"
fi
VENV_DIR="${DATA_NATIVE}/.venv"

SERVER_NAME="vibe-cognition"
DESIRED="v1|uv run --no-sync --project ${ROOT_NATIVE} python -m vibe_cognition.server|UV_PROJECT_ENVIRONMENT=${VENV_DIR}|VIBE_DATA_DIR=${DATA_NATIVE}|VIBE_HARNESS=codex"
STAMP="${PLUGIN_DATA}/codex-mcp.stamp"
LOCK="${PLUGIN_DATA}/codex-mcp.lock"
MANUAL_CMD="codex mcp add ${SERVER_NAME} --env UV_PROJECT_ENVIRONMENT=${VENV_DIR} --env VIBE_DATA_DIR=${DATA_NATIVE} --env VIBE_HARNESS=codex -- uv run --no-sync --project \"${ROOT_NATIVE}\" python -m vibe_cognition.server"

_bc() { echo "[vibe-cognition codex hook] pid=$$ $1 t=$(date +%s)" >&2; }

_stamp_matches() { [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$DESIRED" ]; }

_register() {
    if ! command -v codex >/dev/null 2>&1; then
        NOTE="vibe-cognition: the 'codex' CLI was not found on PATH from the session-start hook, so its MCP server is not registered yet. Run this once, then restart Codex: ${MANUAL_CMD}"
        return 0
    fi
    _bc "register_start"
    if codex mcp add "$SERVER_NAME" \
        --env "UV_PROJECT_ENVIRONMENT=${VENV_DIR}" \
        --env "VIBE_DATA_DIR=${DATA_NATIVE}" \
        --env "VIBE_HARNESS=codex" \
        -- uv run --no-sync --project "$ROOT_NATIVE" python -m vibe_cognition.server >/dev/null 2>&1; then
        printf '%s' "$DESIRED" > "$STAMP"
        NOTE="vibe-cognition registered its MCP server with Codex (user-level entry '${SERVER_NAME}', bound to whatever project Codex is opened in). INSTRUCTION: tell the user to restart Codex once to activate it; tools appear from the next session on."
        _bc "register_done_ok"
    else
        NOTE="vibe-cognition: 'codex mcp add' failed while registering the MCP server. INSTRUCTION: tell the user to run this once and restart Codex: ${MANUAL_CMD}"
        _bc "register_done_fail"
    fi
}

NOTE=""
if ! _stamp_matches; then
    acquired=false
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        if mkdir "$LOCK" 2>/dev/null; then acquired=true; break; fi
        if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +2 2>/dev/null)" ]; then rmdir "$LOCK" 2>/dev/null || true; continue; fi
        sleep 0.5
    done
    if [ "$acquired" = true ]; then
        trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
        if ! _stamp_matches; then
            _register
        fi
        rmdir "$LOCK" 2>/dev/null || true
        trap - EXIT
    else
        _bc "register_skipped_lock_busy"
    fi
else
    _bc "register_skipped_stamp_match"
fi

export VIBE_HARNESS_NOTE="$NOTE"
exec bash "${PLUGIN_ROOT}/hooks/session-start.sh"
