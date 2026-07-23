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

# WP-A 1b (decision 9022f7de94e9): timing breadcrumbs around this hook's three
# `uv run` calls, so hook-vs-server venv overlap is diagnosable (distinguishes
# H1 venv-lock contention from H5 baseline cold-start tax). stderr only --
# Claude Code already captures the hook's stderr, and stdout must stay
# reserved for the hook's JSON output. Tagged by this hook process's OWN pid
# ($$) -- inherently per-process (stderr, not a shared file), so concurrent
# hook invocations never collide. Seconds resolution (portable `date +%s`,
# not GNU-only `%N`) -- this is diagnosing multi-second contention, not
# sub-second timing.
_bc() { echo "[vibe-cognition hook] pid=$$ $1 t=$(date +%s)" >&2; }

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
    _bc "probe_start"
    if UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
        uv run --no-sync --project "${PLUGIN_ROOT}" \
        python -c "import torch, chromadb" 2>/dev/null; then
        _bc "probe_done_ok"
        mkdir -p "${VENV_DIR}"
        echo "$HASH" > "$STAMP"
    else
        _bc "probe_done_fail"
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
_bc "migrate_mcp_start"
MIGRATE_NOTE=$(UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
    uv run --no-sync --project "${PLUGIN_ROOT}" \
    python -m vibe_cognition.migrate_mcp "$MCP_JSON" 2>/dev/null) || MIGRATE_NOTE=""
_bc "migrate_mcp_done"

# ── Step 3b: Check for a newer released version, nudge-only ──────────────
# Nudge-only: no auto-update, no cloud service touched beyond a couple of
# read-only GETs the check module itself makes. Kill switch VIBE_UPDATE_NUDGE
# (off/0/false/no, any case) skips WITHOUT spawning uv/python at all --
# checked here, bash-side, before the throttle gate below would otherwise
# cost a process spawn. Lowercased ONCE via `tr` so the case list doesn't
# have to enumerate every casing (oFf, Off, OFF, ...) individually. Guarded
# for set -e: a failure -> "".
UPDATE_NOTE=""
NUDGE_SETTING=$(printf '%s' "${VIBE_UPDATE_NUDGE:-}" | tr '[:upper:]' '[:lower:]')
NUDGE_OFF=false
case "$NUDGE_SETTING" in
    off|0|false|no) NUDGE_OFF=true ;;
esac

STAMP2="${PLUGIN_DATA_NATIVE}/update-check.json"
# `find` PATH hazard: on Windows, if System32 precedes Git's usr/bin on the
# ambient hook PATH, plain `find` resolves to the Windows string-search tool
# (a completely different program), and the throttle gate below silently
# degrades to "always stale" (network call every session). Prefer Git
# Bash's own find explicitly; fall back to PATH lookup off Windows.
FIND_BIN="/usr/bin/find"
if [ ! -x "$FIND_BIN" ]; then
    FIND_BIN="find"
fi
# 24h throttle keyed off the stamp file's MTIME, not its JSON payload --
# parsing checked_at in bash needs jq/python and reintroduces the process
# spawn this gate exists to avoid. `find -mtime -1` is identical GNU/BSD
# (unlike `stat -c` vs `stat -f`), so this stays portable off Windows too.
# IMPORTANT: `find ... -mtime -1` exits 0 whether or not anything matched --
# the predicate filters OUTPUT, it does not set the exit code. Gate on the
# output being non-empty, and keep this as an explicit if/fi (not a bare
# `[ ]` test at top level) -- the routine "stale -> proceed" case is NOT an
# error and must never trip `set -e`.
STAMP_FRESH=false
if [ -f "$STAMP2" ]; then
    if [ -n "$("$FIND_BIN" "$STAMP2" -mtime -1 2>/dev/null)" ]; then
        STAMP_FRESH=true
    fi
fi

if [ "$NUDGE_OFF" = false ] && [ "$STAMP_FRESH" = false ]; then
    _bc "update_check_start"
    UPDATE_NOTE=$(UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
        CLAUDE_PLUGIN_ROOT="${PLUGIN_ROOT_NATIVE}" \
        CLAUDE_PLUGIN_DATA="${PLUGIN_DATA_NATIVE}" \
        uv run --no-sync --project "${PLUGIN_ROOT}" \
        python -m vibe_cognition.update_check 2>/dev/null) || UPDATE_NOTE=""
    _bc "update_check_done"
fi

# ── Step 4: Inject project context (+ any migration/update note) via prime ─
# prime ALWAYS emits something -- a migration note and/or update note when
# present, the full project-context digest when .cognition/ has nodes, or
# the onboarding block when the graph is empty/absent. It never exits silent.
_bc "prime_start"
PRIME_OUTPUT=$(UV_PROJECT_ENVIRONMENT="${VENV_DIR}" \
    REPO_PATH="${PROJECT_DIR_NATIVE}" \
    VIBE_MIGRATION_NOTE="${MIGRATE_NOTE}" \
    VIBE_UPDATE_NOTE="${UPDATE_NOTE}" \
    uv run --no-sync --project "${PLUGIN_ROOT}" \
    python -m vibe_cognition.cognition.prime 2>/dev/null) || PRIME_OUTPUT=""
_bc "prime_done"

if [ -n "$PRIME_OUTPUT" ]; then
    echo "$PRIME_OUTPUT"
else
    echo '{}'
fi
exit 0
