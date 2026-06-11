# WP-3 Execution Plan — fix/wp-3-hooks (post-commit hook + skill correctness)

Branch: `fix/wp-3-hooks` off main @ `ba1bd77`. Three audit findings, all distribution-surface. One commit per finding, cite IDs.

## (a) H-1 — post-commit hook invoked with bare `python`
**Problem:** `hooks/hooks.json` PostToolUse runs `python "${CLAUDE_PLUGIN_ROOT}/hooks/post-commit.py"`. Plugin only guarantees `uv`; macOS/many-Windows lack `python` on PATH → silent failure on EVERY Bash call.
**Fix:** add a bash wrapper `hooks/post-commit.sh` mirroring `reinject-instructions.sh` venv resolution:
- `PLUGIN_ROOT`, `PLUGIN_DATA=${CLAUDE_PLUGIN_DATA:-${PLUGIN_ROOT%/*}}`, cygpath `-m` normalization, `VENV_DIR=${PLUGIN_DATA_NATIVE}/.venv`.
- `OUT=$(UV_PROJECT_ENVIRONMENT="$VENV_DIR" uv run --no-sync --project "$PLUGIN_ROOT_NATIVE" python "$PLUGIN_ROOT_NATIVE/hooks/post-commit.py" 2>/dev/null) || OUT=""`; print `$OUT` else `{}`. (Capture-then-print → valid JSON even on failure under `set -euo pipefail`.)
- stdin (hook JSON) is inherited by the command substitution → reaches post-commit.py; env (REPO_PATH/CLAUDE_PROJECT_DIR) inherited.
- Change `hooks.json` command → `bash "${CLAUDE_PLUGIN_ROOT}/hooks/post-commit.sh"`. (`*.sh` already forced LF in `.gitattributes`.)
- Rationale: `uv` is guaranteed present (session-start warns if absent); `python` is not. Graceful no-op if uv somehow missing (never breaks the Bash call).
**MEASURE (Vince):** per-invocation overhead delta — `bash post-commit.sh` (uv run, non-commit early-return) vs old bare `python post-commit.py`. Report ms delta; the hook fires on every Bash call.
**Install-mechanics → report ends "gated on human release test".**

## (b) post-commit.py UTF-8 decode (the "Â§" mangle)
**Problem:** `_get_latest_commit` (`:30-33`) and `_get_changed_files` (`:52-55`) use `subprocess.run(..., text=True)` with no `encoding` → decodes git's utf-8 bytes with the locale codepage (Windows cp1252) → "§" becomes "Â§".
**Fix:** add `encoding="utf-8"` to both `subprocess.run` calls. (git log output is utf-8 by default.)
**Regression test (fails-before/passes-after):** new minimal harness `tests/test_post_commit_hook.py` — load `hooks/post-commit.py` via importlib, create a temp git repo, commit a non-ASCII summary ("§ …"), call `_get_latest_commit`, assert the message round-trips exactly. Fails-before on Windows (locale ≠ utf-8 → mangled), passes-after. Verify the fail-before locally (revert encoding, run, confirm red) since the dev machine is Windows. Test count increases (baseline rule).
**Flag, don't fix:** the existing mangled node `0a23fe8200e4` ("Â§8.1: ruff baseline cleanup") in the journal — report it for Vince's hand (journal edits are his).

## (c) S-1 — vibe-curate skill repo-relative paths
**Problem:** `skills/vibe-curate/SKILL.md:36,56` say "see `skills/vibe-curate/edge-analyzer.md`" / `cluster-analyzer.md` — repo-relative; installed cwd = user project, so they don't resolve. (Both files exist beside SKILL.md; only these 2 refs.)
**Fix:** reference them by the skill's own directory — the "Base directory for this skill: <abs path>" injected when the skill loads. Reword to "see `edge-analyzer.md` in this skill's directory (the base directory shown when this skill loads)" and same for cluster-analyzer.md.
**Install-mechanics → report ends "gated on human release test".**

## Commits (one per finding, cite IDs)
1. `H-1: route post-commit hook through uv (no bare python)` — post-commit.sh + hooks.json.
2. `post-commit.py: decode git output as utf-8 (+ regression test)` — encoding fix + test.
3. `S-1: vibe-curate skill paths resolve from the skill dir, not repo-relative` — SKILL.md.
4. CHANGELOG Unreleased entries.

## Acceptance / constraints
- Baselines hold: ruff clean, pyright ≤31, pytest count never decreases (+1 from the new test).
- CI green on the PR (live workflow gates).
- (a)+(c) install-mechanics → report ends "gated on human release test" (b8ec24fe9107).
- No new deps; no version bump (v0.7.3 is Vince's release after merge); no CLAUDE.md; no marketplace.json.
- Journal not committed on branch; branch-switch = ping Vince. utf-8 + pathlib on file IO.
