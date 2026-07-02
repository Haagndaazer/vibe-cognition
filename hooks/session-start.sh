#!/usr/bin/env bash
# SessionStart hook — installs deps, removes any stale per-project .mcp.json
# vibe-cognition entry (the server is plugin-declared now, not per-project),
# and injects context.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
# Persistent plugin-data dir — survives plugin updates, so the venv never lives
# inside the version-pinned cache dir (a running server would lock it on Windows
# during /plugin update). Fall back to the version-independent parent of the
# cache dir if CLAUDE_PLUGIN_DATA is not provided. %[/\\]* strips the last
# segment on EITHER separator — a plain %/* leaves a Windows backslash path
# untouched and the venv lands back inside the pinned cache dir (audit B-3).
PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-${PLUGIN_ROOT%[/\\]*}}"

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
# Hook timeout (hooks.json, this SessionStart entry): 600s. WP-11 (c3074f43cd49):
# a cold `uv sync` on a genuinely first install downloads ~2-4GB (mostly PyTorch)
# -- at a healthy 50Mbps that alone is ~8 minutes, so the previous 120s was
# insufficient even on normal home connections, not just slow ones. 600s targets
# that case with margin. Not a guarantee for every connection speed -- a truly
# degraded link could still exceed it -- but the hook self-heals: the version
# stamp below is only written on a VERIFIED-successful sync+import, so a timeout
# leaves it unwritten and the NEXT session-start just retries (uv's own package
# cache means a retry is rarely a full re-download).
if command -v sha256sum &>/dev/null; then
    HASH=$(cat "${PLUGIN_ROOT}/pyproject.toml" "${PLUGIN_ROOT}/uv.lock" 2>/dev/null | sha256sum | cut -d' ' -f1)
elif command -v shasum &>/dev/null; then
    HASH=$(cat "${PLUGIN_ROOT}/pyproject.toml" "${PLUGIN_ROOT}/uv.lock" 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
else
    HASH="no-hash-tool"
fi

if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$HASH" ]; then
    # Guard the sync: a dependency-swap update (e.g. 0.7.3's torch PyPI->CPU-index
    # move) can fail mid-uninstall when running servers hold the package files
    # (Windows DLL locks), leaving a half-installed package. Do NOT let that kill
    # the hook under `set -e` — the health probe below turns it into actionable
    # guidance instead of a cryptic MCP connection failure.
    UV_PROJECT_ENVIRONMENT="${VENV_DIR}" uv sync --project "${PLUGIN_ROOT}" --no-dev 2>/dev/null || true

    # Post-sync venv health probe. Runs ONLY here (install / upgrade / broken
    # retry) — the steady-state happy path matches the stamp and skips this whole
    # block, paying nothing. Imports the heavy native deps that actually brick
    # (torch was the 0.7.3 culprit; chromadb is the other native dep). This is a
    # targeted check for the half-installed-native-dep class, not the server's
    # full import graph. The `if` reads python's own exit code (no pipe — ledger
    # 17). The stamp is written ONLY for a verified-importable venv, so a broken
    # venv leaves it unwritten and re-warns on every start until a clean start
    # (all sessions closed) finishes the swap and self-heals.
    if UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
        uv run --no-sync --project "${PLUGIN_ROOT}" \
        python -c "import torch, chromadb" 2>/dev/null; then
        mkdir -p "${VENV_DIR}"
        echo "$HASH" > "$STAMP"
    else
        # WP-11 (c3074f43cd49): the probe fires identically for several distinct
        # root causes, not just the DLL-lock case it originally named -- an
        # interrupted download (network drop) or the 600s hook timeout killing a
        # slow sync leave the SAME half-installed signature. Enumerate the
        # plausible causes and give ONE generic recovery (delete the venv) that
        # works regardless of which one actually happened, alongside the
        # cheaper DLL-lock-specific fix. Unquoted heredoc so VENV_DIR expands.
        cat << EOF
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "vibe-cognition: a dependency update did not finish — a Python package is half-installed. The MCP server cannot load until this is repaired, and it self-heals on a clean retry. Possible causes: (1) the plugin updated while other Claude Code sessions were open and holding its files, mainly on Windows (FIX: close ALL Claude Code sessions and windows, then open ONE); (2) the download was interrupted (network drop, or the install exceeded the session-start hook's timeout on a slow connection); (3) the disk ran out of space mid-install. If closing all sessions and reopening one doesn't resolve it, delete ${VENV_DIR} and restart Claude Code — session-start will rebuild it from scratch."
  }
}
EOF
        exit 0
    fi
fi

# ── Step 3: Migrate away from the per-project MCP entry ──
# Earlier versions wrote a "vibe-cognition" entry into the project's .mcp.json.
# The server is now declared by the plugin itself (plugin.json), and a
# project-scope entry OUTRANKS the plugin definition — so remove any stale
# entry. The removal is surgical: only our entry is touched; every other MCP
# server and top-level key is preserved (see vibe_cognition.migrate_mcp).
# Capture a one-line note ONLY when a stale entry is actually removed (empty
# otherwise). prime (Step 4) surfaces it, so we never drop project-context
# injection on the migration session. Guarded for set -e: a failure -> "".
MCP_JSON="${PROJECT_DIR_NATIVE}/.mcp.json"
MIGRATE_NOTE=$(UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
    uv run --no-sync --project "${PLUGIN_ROOT}" \
    python -m vibe_cognition.migrate_mcp "$MCP_JSON" 2>/dev/null) || MIGRATE_NOTE=""

# ── Step 4: Inject project context (+ any migration note) via prime ──────
# prime self-guards: it emits output when there is a migration note OR a
# .cognition/ dir, and exits silently otherwise.
PRIME_OUTPUT=$(UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
    REPO_PATH="${PROJECT_DIR_NATIVE}" \
    VIBE_MIGRATION_NOTE="${MIGRATE_NOTE}" \
    uv run --no-sync --project "${PLUGIN_ROOT}" \
    python -m vibe_cognition.cognition.prime 2>/dev/null) || PRIME_OUTPUT=""

if [ -n "$PRIME_OUTPUT" ]; then
    echo "$PRIME_OUTPUT"
else
    echo '{}'
fi
exit 0
