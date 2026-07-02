#!/usr/bin/env bash
# SessionStart(compact) hook — re-inject standing practices + the prime digest.
#
# The server's SERVER_INSTRUCTIONS are surfaced via the MCP initialize handshake, but it
# is undocumented whether those survive a context compaction. This hook (matcher: compact
# in hooks.json) re-injects them as additionalContext after a compact so the rules stay
# in force. It ALSO regenerates the prime data digest (open tasks, constraints, patterns,
# decisions, incidents — WP-7, 530adc9e6f3f) via vibe_cognition.instructions.main(), so a
# compacted session gets the graph's actual backlog back, not just the static rules.
# Emits a SessionStart hookSpecificOutput JSON, or '{}' on any failure.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT}"
# Same plugin-data venv resolution as session-start.sh: the venv lives in CLAUDE_PLUGIN_DATA
# (persistent, outside the version-pinned cache), NOT in PLUGIN_ROOT — uv must be pointed
# at it explicitly. Fall back to the version-independent parent of the cache dir. %[/\\]*
# strips the last segment on EITHER separator — a plain %/* leaves a Windows backslash path
# untouched and the venv lands back inside the pinned cache dir (audit B-3).
PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-${PLUGIN_ROOT%[/\\]*}}"

if command -v cygpath &>/dev/null; then
    PLUGIN_ROOT_NATIVE=$(cygpath -m "$PLUGIN_ROOT")
    PLUGIN_DATA_NATIVE=$(cygpath -m "$PLUGIN_DATA")
else
    PLUGIN_ROOT_NATIVE="$PLUGIN_ROOT"
    PLUGIN_DATA_NATIVE="$PLUGIN_DATA"
fi

VENV_DIR="${PLUGIN_DATA_NATIVE}/.venv"

# --no-sync is safe: a compact only happens mid-session, after a startup SessionStart
# already synced the venv. Capture-then-print so a non-zero exit under set -e still
# yields valid JSON ('{}') instead of a torn/empty stdout.
OUT=$(UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
    uv run --no-sync --project "${PLUGIN_ROOT_NATIVE}" \
    python -m vibe_cognition.instructions 2>/dev/null) || OUT=""

if [ -n "$OUT" ]; then
    echo "$OUT"
else
    echo '{}'
fi
exit 0
