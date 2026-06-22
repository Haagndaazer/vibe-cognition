# WP-Readme — Implementation Plan

Base: `main` @ `cbebd47`. Branch: `fix/wp-readme` (once green-lit).

Implements the BACKLOG parking-lot item **"Doc-serving tools for the LLM/user — esp. gated on
empty-graph detection"**. Models vibe-memory's `memory_readme` (returns `{guide, backfill}` — a
human-readable orientation guide + an embedded procedure so the LLM is never stuck).

**Status:** Plan peer-reviewed (decorrelated sonnet, APPROVE-WITH-CHANGES — 2 blockers + should-fixes
all folded in). Manager (Vince) approved. **Version HELD** per Colton: build + gate + merge to main,
but NO version bump / Loki pin in this WP — release cut decided later. Onboarding gate: **empty only**
(0 nodes / no `.cognition/`).

---

## Goal

Two complementary surfaces so an LLM dropped into a vibe-cognition project knows what it is and how
to use it without the user having to explain:

- **Pull**: a `cognition_readme` MCP tool the LLM calls on demand (the rich guide).
- **Push**: on session start, when the project's graph is EMPTY, proactively inject an onboarding
  block + an instruction telling the LLM to alert the user about the basics and to call
  `cognition_readme`.

These complement (do NOT replace) `instructions.py:SERVER_INSTRUCTIONS`, which is the always-pushed
standing-practices text. Pull = on-demand rich content; push-instructions = every-session practices.

---

## Component 1 — `cognition_readme` tool

- New tool registered following the `tools/dashboard_tool.py` template: a thin `@mcp.tool()` wrapper
  delegating to a module-level pure core. Register inside `register_service_tools` (it's a
  service/orientation surface, not a graph-mutation tool) **or** its own `register_readme_tool` —
  implementer's call; keep `register_all_tools` wiring consistent.
- **Signature:** `cognition_readme(ctx: Context) -> dict[str, str]`. **No `project` arg** — the
  docstring MUST state why: it serves THIS project's orientation; for a loaded foreign project, the
  guidance comes from that project's own server. (Prevents an XP-suite reader from trying
  `cognition_readme(project=...)` by analogy with the read tools.)
- **Returns** `{guide, getting_started}` (mirrors vibe-memory's `{guide, backfill}`):
  - `guide`: markdown orientation — what vibe-cognition is; that it's already active (the user sees
    this because the plugin is installed); the core loop (`cognition_record` as you work → `/vibe-curate`
    to link); the tool groups (record / search / history / curate / document / cross-project); node &
    edge types; when-to-record triggers.
  - `getting_started`: a short procedure the LLM can act on immediately to begin capturing cognition
    on this project — so it's never stuck (analogous to vibe-memory's backfill procedure).
- **Content source:** a single new canonical constant module
  `src/vibe_cognition/cognition/readme.py` — `COGNITION_GUIDE` / `COGNITION_GETTING_STARTED`
  (or a single dict). **ASCII-only, stdlib-only imports** (it is also imported by `prime.py`, which
  feeds a JSON-to-stdout hook path on Windows). Do NOT runtime-read README.md / SKILL.md (packaging
  fragility + drift). Do NOT copy instructions.py's known-false "stdlib-only / mirrors prime" comment
  into this module's docstring (that claim is wrong and tracked as S-3).
- **Docstring** is fully self-sufficient (passes the standing tool-surface audit): one-line summary,
  usage paragraph stating pull-vs-push relationship to `instructions.py`, `Args:`/`Returns:` with the
  exact key shape.

## Component 2 — empty-graph onboarding via `prime.py` (NO change to `session-start.sh`)

`session-start.sh` already echoes prime's stdout as `hookSpecificOutput.additionalContext`. All logic
stays in `prime.py:main()` — pure Python, fully testable, **keeps the install-mechanics gate untouched**.

Extend `main()` (current behavior: appends migration `note` if set; appends `generate_prime(storage)`
only if `.cognition/` exists; exits silently if `sections` empty):

1. Compute "empty" = `.cognition/` ABSENT, **or** `.cognition/` exists and
   `storage.get_statistics()["nodes"] == 0`. (Empty-only gate — confirmed by Colton; no near-empty
   threshold.)
2. **When empty:** append the ONBOARDING block (from `readme.py`): a short "what this is + graph is
   empty" + an INSTRUCTION line directing the LLM to alert the user about the basics and call
   `cognition_readme` for the full guide.
3. **Blocker-1 fix — migration-note interaction:** the onboarding append is INDEPENDENT of the note
   append. If `.cognition/` is absent AND a migration note is set, emit BOTH (note + onboarding), not
   one-or-the-other. Order: migration note first, then onboarding.
4. **Nit-7 fix — no double signal:** when the onboarding block is emitted for the `nodes == 0` case,
   SUPPRESS `generate_prime`'s `"No cognition history recorded yet."` fallback (don't show two
   empty-graph messages). Cleanest: only call `generate_prime` when `nodes > 0`.
5. Otherwise (graph has content): behavior UNCHANGED.

Concurrency note (should-fix 4): `get_statistics()` goes through `_synced()`/`_catch_up()`, which only
replays complete newline-terminated journal lines — safe under a concurrent server mid-write. No new
locking needed; implementer should be aware, not act.

## Component 3 — tests + drift posture (IN SCOPE)

- **Blocker-2 fix — SKILL.md is a deliverable, not deferred:** the doc-drift GUARD test reads
  `skills/vibe-cognition/SKILL.md` and asserts every registered tool name appears there. Add the
  `cognition_readme` row to the SKILL.md tool table in THIS WP, or CI fails immediately.
- Tests (pure-Python, no install harness needed):
  - prime emits onboarding when `.cognition/` absent;
  - prime emits onboarding + migration note together when `.cognition/` absent AND note set (blocker 1);
  - prime emits onboarding when `.cognition/` exists and nodes == 0, and does NOT emit the
    "No cognition history recorded yet." fallback in that case (nit 7);
  - prime does NOT emit onboarding when nodes > 0 (existing behavior preserved);
  - `cognition_readme` returns a dict with keys `guide` + `getting_started`, both non-empty str;
  - `readme.py` constants are ASCII-clean — assert via `constant.isascii()` (actionable failure),
    not a bare encode try/except.
  - doc-drift GUARD test passes with the new tool (SKILL.md row present).
- **Whole-repo** `uv run pyright` (NOT a path — test files get silently skipped otherwise) held at the
  1 pre-existing error baseline (`server.py:167`). 0 new.

---

## Out of scope (noted adjacencies — separate WPs)

- Full S-3 doc-drift cleanup (README standalone-dashboard instructions broken for plugin users;
  SKILL.md tool-table drift beyond adding our one row; edge-type-list drift; instructions.py false
  stdlib-only claim + non-existent PreCompact hook reference).
- The recurring tool-surface self-sufficiency audit re-run (a new tool TRIGGERS it — schedule
  separately).
- Refactoring existing docs to source from the new canonical constant. We CREATE one canonical
  onboarding source; we do not migrate README/SKILL to it here.
- Version bump / release — HELD by Colton.

## Gate (standard)

SHA-pinned merge · fix+proof in the same commit · journal stays uncommitted on the WP branch (manager
flushes via worktree) · decorrelated peer review (done) · CI green 3 legs · whole-repo pyright held ·
SHA → Vince → SHA-pinned merge. Voiding clause in force (hold + root-cause on any material
post-sign-off info).
