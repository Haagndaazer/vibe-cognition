# WP: Prime token trim + post-commit hook removal

**Branch:** `wp-prime-hook` · **Implementer:** Vorpid · **Reviewer/release:** Vince
**Version bump:** 0.12.3 → **0.13.0** (`pyproject.toml` + `.claude-plugin/plugin.json`)
**Two parts, one release.** Both are user-facing (session-start behavior).

This brief is peer-reviewed (two sonnet passes). The **CRITICAL** items below are release-breakers
the reviews caught — do not skip them.

---

## PART 1 — Trim the session-start `prime` injection (~1,346 → ~575 tok target)

Steady-state hook cost is entirely `prime.py` output. Make prime a lean "get your bearings"
digest; full context is pulled on demand via cognition_search/get_history/list_tasks.

### Config threading — `PrimeConfig` dataclass (NOT Settings-in-formatters)
Do NOT read `Settings()` inside the `_format_*` functions (couples formatting to live env, breaks
test isolation, and lets a formatter exception crash the hook into silence). Instead:
- Add `@dataclass(frozen=True) class PrimeConfig` in `prime.py` with the 7 fields below as defaults.
  These defaults ARE the single source of truth for the trimmed values.
- `generate_prime(storage, config: PrimeConfig | None = None)` — defaults to `PrimeConfig()`.
- `_format_*` keep taking plain `limit=/maxlen=/...` params; `generate_prime` passes them from
  `config`. No Settings import in formatting logic.
- `main()` builds the config FROM Settings inside ONE try/except; on ANY error falls back to
  `PrimeConfig()`. Because the dataclass defaults == the new trimmed targets, a Settings failure
  degrades to the SAME trimmed output — never silent, never reverts to the old fat output.

### E — Config knobs (`config.py` `Settings`; env-overridable; defaults MUST equal PrimeConfig)
- `prime_constraint_limit: int = 5`
- `prime_task_cap: int = 5`        (was 10)
- `prime_pattern_limit: int = 3`   (was 5)
- `prime_decision_limit: int = 3`  (was 5)
- `prime_incident_days: int = 14`  (was 30)
- `prime_summary_maxlen: int = 110` (0 = no truncation)
- `prime_incident_min_severity: Literal["critical","high","normal","low"] = "high"`
  (validated `Literal`, NOT raw str; named *incident* so it can't be mis-applied to constraints)

### B — Truncation helper
`_truncate(text, maxlen)`: if `maxlen<=0` or `len<=maxlen` → return text unchanged. Else find last
whitespace before maxlen via `rfind(' ',0,maxlen)`; if found (>0) cut there; **if NOT found (-1 or
0) HARD-cut at `maxlen`**, then rstrip + append `"…"`. The hard-cut branch is REQUIRED — without it
a no-whitespace string (long URL/hash) drops only its last char and emits a misleadingly
near-complete bullet. Apply in `_format_node` (constraints/patterns/decisions/incidents) AND
`_format_task`.

### A — Counts
Wire the limit params / task cap to `PrimeConfig` values (replace the hardcoded `_TASK_INJECT_CAP`,
`limit=5`, `days=30`).

### C — Severity gating (DECIDED: C2 for constraints — human-confirmed)
- **Incidents:** keep only severity ≥ `prime_incident_min_severity` (high+critical) AND within
  `prime_incident_days`.
- **Constraints:** keep ALL except `low` (explicit `severity != "low"` filter — NOT the incident
  gate). severity None/normal/high/critical all KEPT (None must NOT be dropped). Do not reuse the
  incident threshold here — the two must not collide.

### Part 1 tests (`tests/test_prime.py`)
- `_truncate`: NO-WHITESPACE hard-cut (`_truncate("a"*200, 50)` → 50 chars + `"…"`), short string
  untouched, `maxlen=0` no-op.
- Task cap honored via `PrimeConfig(prime_task_cap=…)` override (no env monkeypatch needed).
- Incident severity gate: a `normal` incident dropped, a `high` one kept.
- Constraint C2: `low` dropped; `normal`/`None`/`high` kept.
- `PrimeConfig()` default field values == the `Settings` defaults (guard the equivalence).
- REWRITE the incident-window test: seed a **10-day** incident (KEPT) and a **20-day** incident
  (EXCLUDED) so it pins the real 14d boundary — the current 35-day node passes trivially against
  both old and new windows and must not stand.

---

## PART 2 — Remove the post-commit journal hook

`hooks/post-commit.py` (wired in `hooks/hooks.json` PostToolUse→Bash via `post-commit.sh`) appends
an episode to `.cognition/journal.jsonl` after every `git commit`, re-dirtying the tree right after
a clean commit. It's redundant with deliberate `cognition_record`. Retire auto-capture; keep
`/vibe-backfill` (opt-in recovery) and `journal_io` (server uses it).

### Remove
- **`hooks/hooks.json` — CRITICAL:** delete the `PostToolUse` block AND strip the trailing comma on
  the `SessionStart` array's closing line (`    ],` → `    ]`). Deleting the block without fixing
  the comma yields invalid JSON → Claude Code fails to parse hooks.json → **silently kills
  SessionStart too.** Validate the file parses as JSON after the edit. Leave `SessionStart` intact.
- `hooks/post-commit.sh` — delete.
- `hooks/post-commit.py` — delete.
- `hooks/__pycache__/post-commit.*.pyc` — delete stale bytecode.
- `tests/test_post_commit_hook.py` — delete (100% about the removed hook).
- **`tests/test_journal_concurrency.py` — CRITICAL (partial):** it also path-loads the hook and
  WILL crash pytest after removal. Delete from THIS file: `test_post_commit_hook_imports_only_stdlib`,
  `test_hook_append_line_wiring`, the `_HOOK` module var, and the now-unused `import importlib.util`.
  KEEP `test_journal_io_imports_only_stdlib` (uses `_JIO`). Re-verify line numbers before editing.

### Reference cleanup (no dangling mentions)
- `src/vibe_cognition/server.py` (~L27) — comment "nodes created by the post-commit hook or other
  paths"; drop the hook clause. Behavior unchanged (reconcile still runs).
- `README.md` — remove the "Auto-Capture" feature bullet, the "PostToolUse hook" table row, and the
  "(or automatically via the post-commit hook)" clause.
- `src/vibe_cognition/cognition/journal_io.py` module docstring — remove the "imported by the
  post-commit git hook / must run against a bare venv" rationale; restate stdlib-only as
  forward-compat posture (server is now the only caller, inside the full venv). Do NOT change code.
- `tests/test_journal_concurrency.py` — docstring of the surviving `test_journal_io_imports_only_stdlib`:
  drop the "the post-commit hook path-loads it" justification.
- `src/vibe_cognition/cognition/git_identity.py` (~L11) — comment references `hooks/post-commit.py`;
  change to "the post-commit hook (now removed)". Logic stays valid.
- `CHANGELOG.md` — add the 0.13.0 entry (prime trim + hook removal).

### Keep (verified independent)
`journal_io.append_journal_line` (server caller), `backfill.py` + `/vibe-backfill`, server
`_reconcile`/part_of matching. Packaging is safe — `hooks/` is not force-included (hatch wheel packs
only `src/vibe_cognition`); `plugin.json` references only the MCP entry point.

---

## Acceptance criteria (report these back to Vince)
1. `uv run pytest` — FULL suite green (incl. the pruned `test_journal_concurrency.py`).
2. `uv run ruff check .` clean; pyright clean if run in your loop.
3. `hooks/hooks.json` parses as valid JSON; SessionStart block byte-identical except the comma.
4. Re-measure prime: `REPO_PATH=<repo> uv run --no-sync python -m vibe_cognition.cognition.prime`,
   report the `additionalContext` char count and char/4 token estimate (target ~575; report the
   real number, don't assert 575).
5. Show that an env override changes output (e.g. `PRIME_TASK_CAP=2` yields ≤2 task bullets).
6. Grep sweep: `post.commit|post_commit|PostToolUse|Auto-Capture` returns only historical `docs/`
   + `.cognition/` — no live src/test/README/hook/docstring hits.
7. Version bumped to 0.13.0 in BOTH `pyproject.toml` and `.claude-plugin/plugin.json`.
8. Do NOT commit `.cognition/journal.jsonl` on this branch (shared-journal protocol) — Vince flushes
   journal to main at closeout. Commit code only.

Vince reviews the diff adversarially + merges to main + pings Loki. Do not merge or push to main
yourself.
