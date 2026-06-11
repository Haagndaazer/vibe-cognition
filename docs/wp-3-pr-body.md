Three audit findings on the post-commit hook + the vibe-curate skill, all distribution-surface. One commit per finding.

## (a) H-1 — post-commit hook ran on bare `python`
`hooks.json` invoked the PostToolUse hook with bare `python`, which fails silently on every Bash call where python isn't on PATH (macOS default; many Windows installs only have `py`). Now routed through `uv run --no-sync` via a new `hooks/post-commit.sh` wrapper that mirrors `session-start.sh`'s venv/env resolution (PLUGIN_DATA fallback, cygpath, `REPO_PATH`, stdin passthrough, valid-JSON-on-failure). uv is a guaranteed plugin dependency; python is not.

**Per-invocation overhead** (this hook fires on every Bash call; measured on the Windows dev box, 5-run avg):
- new `uv run --no-sync`: **~184 ms**
- old bare `python`: **~221 ms** (the Windows `python` launcher is slow)
- direct venv interpreter (`$VENV_DIR/.../python`): ~112 ms

So the new path is **faster than the status quo** here while fixing correctness. The direct-interpreter route would be ~70 ms faster still but diverges from the other hooks' `uv run` idiom and needs an OS branch — flagging it as an option; implemented `uv run` per the brief.

## (b) UTF-8 decode — the "Â§" mangle
`_get_latest_commit` / `_get_changed_files` read git output with `subprocess(..., text=True)` and no `encoding`, decoding git's UTF-8 bytes with the locale codepage (Windows cp1252) → "§" becomes "Â§". Added `encoding="utf-8"` to both. New `tests/test_post_commit_hook.py` (temp git repo, non-ASCII commit, codepoint-exact round-trip assertion) — verified **red-before / green-after** locally on Windows; on a UTF-8 CI runner it passes both before and after, so CI isn't gated on the red-before (documented in the test).

**Flagged for Vince (journal edits are yours):** the existing mangled node `0a23fe8200e4` ("Â§8.1: ruff baseline cleanup") in the journal — not hand-edited.

## (c) S-1 — vibe-curate skill paths
`SKILL.md` pointed subagents at `skills/vibe-curate/edge-analyzer.md` / `cluster-analyzer.md` — repo-relative, broken when installed (cwd = user project). Now referenced from the skill's own directory, where they ship beside SKILL.md.

## Acceptance
- Baselines: ruff clean, pyright 31 (≤31; the new test's one pyright error was fixed by reordering a None-narrowing assert), pytest 130 → **131**.
- CI green on this PR (live workflow gates).
- (a) and (c) are install-mechanics and can't be self-verified — **gated on human release test** (constraint b8ec24fe9107).
