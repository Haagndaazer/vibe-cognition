#!/usr/bin/env bash
# PostToolUse hook wrapper — runs post-commit.py through uv so it does NOT depend
# on a bare `python` being on PATH (audit H-1: the plugin guarantees uv, not
# python — macOS has no `python` by default, many Windows installs only `py`).
# Mirrors session-start.sh's venv + env resolution so it works from any project
# the plugin is installed in.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
# The venv lives in CLAUDE_PLUGIN_DATA (persistent, outside the version-pinned
# cache dir), NOT in PLUGIN_ROOT — point uv at it explicitly. Fall back to the
# version-independent parent of the cache dir if CLAUDE_PLUGIN_DATA is unset.
# %[/\\]* strips the last segment on EITHER separator: a plain %/* leaves a
# Windows backslash path untouched, landing the venv back INSIDE the pinned
# cache dir (audit B-3) — and this hook would silently recreate it on every
# Bash call after a /plugin update wipes it.
PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-${PLUGIN_ROOT%[/\\]*}}"

# Normalize to forward-slash native paths for uv / Python.
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

# The hook's stdin JSON is inherited through the command substitution and reaches
# post-commit.py. Capture-then-print so a non-zero exit under `set -e` (e.g. uv
# absent) still yields valid JSON ('{}') rather than torn/empty stdout. REPO_PATH
# tells the hook which project's journal to append to (matches session-start.sh).
# --no-sync: the venv is synced at SessionStart; post-commit.py is stdlib-only,
# so it runs even against a bare venv.
OUT=$(UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
    REPO_PATH="${PROJECT_DIR_NATIVE}" \
    PYTHONUTF8=1 \
    uv run --no-sync --project "${PLUGIN_ROOT_NATIVE}" \
    python "${PLUGIN_ROOT_NATIVE}/hooks/post-commit.py" 2>/dev/null) || OUT=""

if [ -n "$OUT" ]; then
    printf '%s' "$OUT"
else
    printf '%s' '{}'
fi
exit 0
