# WP-1 Execution Plan — fix/wp-1-mechanical

Branch: `fix/wp-1-mechanical` off main @ `79858ee` (v0.7.2). One commit per lettered item, message cites finding ID. `docs/` stays untracked (vince's artifacts, incl. this plan + the audit).

JOURNAL RULE (revised, effective now): I DO commit my journal updates on this WP branch, but in their OWN commits (`journal: record WP-N nodes`), never mixed with code. After any merge/pull that touches the journal, run `cognition_reload` immediately (until C-3 is fixed in WP-4 — note this in report).

## Branch-point baselines (measured)
- pytest: **129 passed**
- ruff: **23 errors** — 9 UP017, 4 E741, 3 F401, 2 SIM102, 2 SIM105, 2 UP042, 1 I001
- pyright: **31 errors**

## Commits

### 1 — `E-1: disable ChromaDB telemetry`
- `embeddings/storage.py`: `from chromadb.config import Settings`; change line 28 to
  `chromadb.PersistentClient(path=str(persist_directory), settings=Settings(anonymized_telemetry=False))`.
- New test `tests/test_embeddings_storage.py`: instantiate `ChromaDBStorage(tmp_path)`, assert `storage._client.get_settings().anonymized_telemetry is False`. CONFIRMED working API on chromadb 1.5.5 (live-client accessor, fast/sub-second, not fragile internals). Test count +≥1.

### 2 — `H-6: drop dead dep httpx, dedupe dev deps, document einops, refresh lock`
- Remove `"httpx>=0.27.0",` from `[project].dependencies` (zero imports repo-wide; satisfied transitively).
- Remove the `[project.optional-dependencies].dev` block (lines 23–30). KEEP `[dependency-groups].dev` (uv sync reads that one).
- Add comment above `einops` dep: required at runtime — nomic-embed-text-v1.5's `trust_remote_code` model code imports it (`generator.py:49`).
- `uv lock` to refresh `uv.lock`.

### 3 — `c: fix 21 of 23 ruff findings (defer 2 UP042)`
- `ruff check . --fix` → clears 13 safe (9 UP017, 3 F401, 1 I001).
- SIM102 (2) + SIM105 (2): apply via scoped `--fix --unsafe-fixes` OR by hand; manually verify each rewrite preserves behavior.
- E741 (4): rename ambiguous `l` → `line` at all 4 sites — CONFIRMED: test_cognition.py:343, test_deterministic_edges.py:364, test_multidigraph.py:290, test_multidigraph.py:291.
- LEAVE 2 UP042 (str,Enum→StrEnum changes `str()` semantics — not mechanical). Verify `ruff check .` ends with exactly 2 UP042, nothing else.
- pytest must still be 129.

### 4 — `H-6/d: unify authorship to Colton Dyck`
- `pyproject.toml:5` authors `BlckLvls` → `Colton Dyck`.
- `plugin.json:5` author.name `ColtonDyck` → `Colton Dyck`.
- Create `LICENSE`: MIT, "Copyright (c) 2026 Colton Dyck".
- NOTE: `coltondyck` in README/CLAUDE.md is the **marketplace name** (Claude Code slug `vibe-cognition@coltondyck`), NOT authorship — left untouched. CLAUDE.md off-limits regardless.

### 5 — `H-6/e: gitignore .ruff_cache/`
- Add `.ruff_cache/` (under Type checking / Testing). `.cognition/chromadb/` already present.

### 6 — `T-10/f: delete stale __version__`
- Remove `__version__ = "0.1.0"` from `src/vibe_cognition/__init__.py:3` (defined once, read nowhere — grep-verified).

### 7 — `g: fix 3 stale comments`
- `server.py:146`: REPO_PATH comes from plugin.json env now, not per-project `.mcp.json`.
- `hooks/session-start.sh:2`: header claims it auto-configures per-project MCP — it does the opposite (surgically removes stale entries).
- `hooks/post-commit.py:6-12`: docstring shows pre-plugin install path (`.claude/settings.json`, `python agents/hooks/...`) — update to **ACTUAL current wiring**: `hooks/hooks.json`, matcher `Bash`, command bare `python` (do NOT write `uv run` — that's the unfixed H-1 fix, separate WP; documenting it would describe a non-existent state). [Q for Vince]

### 8 — `h: create CHANGELOG.md`
- New `CHANGELOG.md` with `## [Unreleased]` covering every WP-1 item above.

### 9 — `i: add .gitattributes with journal union merge`
- New `.gitattributes` at repo root: `.cognition/journal.jsonl merge=union`. Union is a git built-in (no local config); keeps both sides' added lines. NOTE (report): union prevents *textual* merge conflicts only — it does NOT guarantee replay-order safety and can reorder/interleave lines, which interacts with unfixed C-3 (stale-offset replay). Mitigation: after any merge/pull touching the journal, full reload/restart (not just `cognition_reload`, which doesn't re-embed). Cross-reference C-3 (WP-4).

## Cross-cutting
- Standing standards: utf-8 encoding + pathlib on any file IO; no version bump; no new deps; no CLAUDE.md; no marketplace.json; doc-sync in same PR (none of README/SKILL.md describe changed behavior — verify).
- Final: re-run pytest/ruff/pyright; report branch-point vs final, deferred (2 UP042), flagged items.
- Open PR via `gh`. Merge only the SHA vince approves.
