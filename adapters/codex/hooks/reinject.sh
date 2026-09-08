#!/usr/bin/env bash
# Codex compact-reinjection wrapper: same standing practices as Claude Code,
# with the Codex harness flag so the text matches what Codex can run.
set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT is not set (Codex sets it for plugin hooks)}"
export VIBE_HARNESS=codex
exec bash "${PLUGIN_ROOT}/hooks/reinject-instructions.sh"
