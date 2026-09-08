# WP-C1b — Codex-ready plugin (packaging + hook-managed MCP registration)

Status: PLAN rev 2 — sonnet adversarial review 2026-09-08 APPROVE-WITH-CHANGES,
all five corrections applied (marketplace `source` tag, data-dir mkdir,
drop redundant `codex mcp remove`, concurrent-session write race, Codex
branch for the `/vibe-curate` instruction text). Build authorized by Colton.
Author: Colton Dyck (solo session)
Date: 2026-09-08
Inputs: `docs/codex-parity-findings.md` rev 2 §1–§7a; live rounds 1–2
(episodes `bfe0cfadd2bf`, `4e588dd707e3`); rulings `faa1ffbf0ff6`,
`004232949d8c`; Colton's direction 2026-09-08: build the real Codex manifest,
install via the colton marketplace if possible (else straight from this repo),
tear down the manual rig first so the plugin test has no leftovers.

## Goal

Make this repo installable in Codex CLI as a plugin — `codex plugin
marketplace add …` then `codex plugin add vibe-cognition@…` — such that a
fresh Codex session in any project gets: the MCP server bound to that
project, the prime digest at session start, standing practices re-injected
after compaction, and the skills. Curation on Codex (D6) is explicitly OUT of
scope (WP-C1c). Claude Code behavior must be byte-for-byte unchanged.

## The one design decision: how the server learns the project root

Verified constraints (all **[src]**, openai/codex main 2026-09-08):

- A plugin-declared stdio MCP server runs with `cwd` forced inside the plugin
  root or plugin data dir; no project-directory variable is passed; Codex has
  no MCP `roots`; ordinary tool calls carry no `cwd`/thread id in `_meta`
  (the `_meta.threadId` seen earlier is added only by the hook executor).
- Plugin-declared servers cannot be `required` and cannot override
  `startup_timeout_sec`/`tool_timeout_sec`.
- For an Agent Plugins-format manifest, hooks receive
  `PLUGIN_DATA = plugins/data/<name>-<marketplace>` while MCP servers receive
  `PLUGIN_DATA = plugins/data/agent-plugins/<sha256 prefix>` — two different
  directories, so a hook-synced venv and a server-side `${PLUGIN_DATA}/.venv`
  would not meet.
- The hook-to-MCP bridge (`mcp_tool` handler) fails immediately when the
  server is not yet connected; SessionStart hooks run at the first turn.

Options weighed:

| | A9: plugin `mcp.json` + `cognition_bind_project` via `UserPromptSubmit` `mcp_tool` hook | **B-hook (chosen): plugin ships skills + hooks; the SessionStart hook registers the server at user level with `codex mcp add`** |
|---|---|---|
| Project root | late-bound per turn; unbound until first prompt; races on scripted first turns | inherited from Codex's cwd (proven live, round 2) |
| Server changes | deferred-storage start mode + new tool + rehome logic (large, touches `lifespan`) | none |
| Data dir | hash-derived, coupled to Codex internals | explicit absolute paths chosen by the hook |
| Timeouts / `required` | not controllable | controllable (`startup_timeout_sec`, `required`) |
| First session | server up but unbound until hook lands | no server until restart; prime tells the user to restart |
| Uninstall | clean | leaves a `[mcp_servers.vibe-cognition]` entry; documented `codex mcp remove` |
| Plugin update | none | hook re-registers when the plugin root changes; takes effect next restart |

B-hook is chosen for this WP. It is the only option with zero server-code
risk, a deterministic project root, and no coupling to Codex's data-dir
hashing. A9 stays on the roadmap as the "pure plugin" form for when Codex
grows a project-scoped MCP story (or when WP-H0b's restructure makes the
deferred-start server cheap). Colton already authorized hook-written machine
state under `~/.codex` (ruling `004232949d8c`); `codex mcp add` is Codex's
own supported CLI, so the hook writes nothing by hand.

## Deliverables

1. **`.codex-plugin/plugin.json`** (legacy Codex manifest — chosen over Agent
   Plugins v1 because we ship no plugin MCP server, so the portable format
   buys nothing and its split data dirs are a hazard):
   `name`, `version` (mirrors `pyproject.toml`), `description`, `keywords`
   (`author`/`repository`/`license` are NOT manifest fields on Codex — silently
   ignored, so omitted), `skills: ["./skills/vibe-cognition",
   "./skills/vibe-document", "./skills/vibe-workflow", "./skills/vibe-dashboard"]`
   (curate/backfill excluded — they drive the Claude Agent tool),
   `hooks: ["./adapters/codex/hooks.json"]`, `interface` (display name, short
   description, category "Other"). Every path MUST start with `./` — Codex
   silently drops paths that don't (`resolve_manifest_path`).
2. **`adapters/codex/hooks.json`** — Codex-only hook file (Claude's
   `hooks/hooks.json` untouched): SessionStart `startup|resume|clear` →
   `command` = `bash "${CLAUDE_PLUGIN_ROOT}/adapters/codex/hooks/session-start.sh"`,
   `commandWindows` = `"${CLAUDE_PLUGIN_ROOT}\adapters\codex\hooks\session-start.cmd"`,
   timeout 600, `additionalContextLimit` 20000; SessionStart `compact` →
   reinject equivalents, timeout 30, limit 20000. Exactly one quoted token per
   Windows command (round-1 lesson).
3. **`adapters/codex/hooks/session-start.sh`** — thin wrapper: FIRST
   `mkdir -p "$CLAUDE_PLUGIN_DATA"` (review finding b: Codex creates the plugin
   data dir only for Agent-Plugins manifests declaring a stdio server — never
   for us — so on a fresh machine the directory does not exist when the hook
   first runs), then exports `VIBE_HARNESS=codex`, `VIBE_UPDATE_NUDGE=off`
   (the update CTA is Claude-specific), computes
   `VENV_DIR=${CLAUDE_PLUGIN_DATA}/.venv`, then
   **registers the MCP server** (step below), sets `VIBE_HARNESS_NOTE` when a
   restart is needed, and `exec`s the shared `hooks/session-start.sh` (which
   syncs the venv, probes it, and prints the prime JSON — unchanged).
4. **`adapters/codex/hooks/reinject.sh`** — same env, `exec`s
   `hooks/reinject-instructions.sh`.
5. **`adapters/codex/hooks/session-start.cmd` / `reinject.cmd`** — Windows
   wrappers: locate Git Bash (from `where git.exe` → `<Git>\bin\bash.exe`,
   fallback `%ProgramFiles%\Git\bin\bash.exe`; if absent, print a valid
   SessionStart JSON telling the user Git for Windows is required and exit 0),
   then run the `.sh` wrapper.
6. **MCP registration step** (inside 3): desired entry =
   `command uv`, `args run --no-sync --project <CLAUDE_PLUGIN_ROOT> python -m vibe_cognition.server`,
   `env UV_PROJECT_ENVIRONMENT=<VENV_DIR> VIBE_DATA_DIR=<CLAUDE_PLUGIN_DATA> VIBE_HARNESS=codex`
   (no timeout field — see below). Idempotency via a stamp
   `${CLAUDE_PLUGIN_DATA}/codex-mcp.stamp` holding a hash of the desired
   entry; when absent/different: a single `codex mcp add vibe-cognition
   --env … -- uv run …` (review finding 4: `run_add` does a plain map
   `insert` — an existing entry is overwritten, so no `remove` step; halves
   the `config.toml` write count), then write the stamp and set
   `VIBE_HARNESS_NOTE` = "vibe-cognition registered its MCP server with Codex
   — restart Codex to activate it". **Registration is serialized behind a
   `mkdir`-based lock** (`${CLAUDE_PLUGIN_DATA}/codex-mcp.lock`, short
   bounded wait, stale-lock age check) because concurrent Codex sessions on
   different projects share this one data dir and `config.toml` writes are
   atomic-per-write but unlocked (last read-modify-write wins; review finding
   8/e). Paths go to `codex mcp add` as absolute Windows-native paths
   (`cygpath -m`), as the shared hook already does for uv. `codex mcp add`
   has no timeout flag (live `--help` + `AddArgs` in `mcp_cmd.rs`), so the
   entry keeps Codex's default startup timeout (30 s in source; 15.7 s cold
   start fits); the hook never edits `config.toml` by hand.
7. **`prime.py`**: read `VIBE_HARNESS_NOTE` and prepend it like the other
   notes (one line, mirrors the existing three). **`instructions.py`**: when
   `VIBE_HARNESS=codex`, the standing-practices text replaces the
   "run the /vibe-curate skill" sentence with a one-line note that curation
   runs from Claude Code for now (review finding d — the server already gets
   `VIBE_HARNESS` from the registration env, so this is a two-line branch at
   the single source of truth; `server.py` imports the same constant).
8. **`.agents/plugins/marketplace.json`** in THIS repo — a Codex marketplace
   named `vibe-cognition-dev` with one plugin entry `vibe-cognition`, source
   `{ "source": "url", "url": "https://github.com/Haagndaazer/vibe-cognition", "ref": "main" }`
   (review finding 5 — the accepted `source` tags are `local`, `url`,
   `git-subdir`, `npm`; a `git` tag is silently dropped and the plugin would
   never appear),
   `policy.installation`/`policy.authentication` defaults, `category`
   "Other". This is the "install straight from the repo" path for the first
   test. The colton marketplace path is a follow-up for Loki: add the same
   entry (sha-pinned, like the Claude pin) to `colton-claude-plugins` at
   `.agents/plugins/marketplace.json`.
9. **Version bump** to 0.35.0 in `pyproject.toml`, `.claude-plugin/plugin.json`,
   `.codex-plugin/plugin.json`; `uv lock` refresh.
10. **Tests**: (a) manifest-parity test — `.codex-plugin/plugin.json` and
    `.claude-plugin/plugin.json` agree on name/version/description and every
    Codex skill path exists; (b) `prime.py` note test for `VIBE_HARNESS_NOTE`
    (asserts the specific text is prepended — not just "output non-empty");
    (c) shell-level dry test of the Windows `.cmd` locating Git Bash is a
    human-gate item, not CI.
11. **Docs**: README section "Codex CLI" (install, restart-once note,
    uninstall incl. `codex mcp remove`), `docs/codex-parity-findings.md` §7b
    pointer, CLAUDE.md release procedure gains the Codex marketplace pin line.

## Teardown of the manual rig (before the plugin test)

`codex mcp remove vibe-cognition`; delete `~/.codex/hooks.json` (ours is the
only content) and `~/.codex/vibe-hooks/`; delete the six `vibe-*` dirs under
`~/.agents/skills/`; remove the junction `~/.codex/vibe-cognition-data/.venv`
then the folder. Verify with `codex mcp list` (empty), `Test-Path` on each.
Leave `~/.codex/config.toml` otherwise untouched (verify by diff against the
pre-rig content: `approvals_reviewer`, `[projects…]`, `[tui…]`, `[windows]`).

## Acceptance criteria (pre-committed)

- AC1 Claude Code unchanged: `hooks/hooks.json`, `hooks/*.sh`, `agents/`,
  `skills/` untouched; `.claude-plugin/plugin.json` differs only in `version`.
- AC2 Fresh Codex install (`codex plugin marketplace add Haagndaazer/vibe-cognition`,
  `codex plugin add vibe-cognition@vibe-cognition-dev`, restart): first
  session's prime shows the "registered … restart Codex" note and the
  project digest; **second** session: `/mcp` shows vibe-cognition connected,
  `get_status.repo_path` == launch dir, instructions surfaced, digest on
  first turn and after `/compact`, `$vibe-cognition` loads.
- AC3 No leftovers from the manual rig influence AC2 (teardown verified
  before install).
- AC4 Windows: hooks succeed via `commandWindows` (no "Hook failed"); Unix
  path exercised only by CI shell syntax check (`bash -n`).
- AC5 `pytest` green, `ruff check .` clean; new tests fail against the
  reverted change (ledger 12 checked by review).
- AC6 Plugin update simulation: change the stamp hash → hook re-registers and
  emits the restart note; unchanged hash → no `codex` subprocess spawned
  (breadcrumb shows skip).

## KNOWN-INTENTIONAL (not bugs)

- No plugin `mcp.json`: deliberate (B-hook). A Codex user sees the server
  under `codex mcp list`, not under the plugin's MCP servers.
- Curation/backfill skills omitted on Codex; the standing-practices text
  says so on Codex (deliverable 7) — the full curation flow is WP-C1c.
- First session after install/update has no server (restart required).
- `update_check` disabled on Codex (Claude-specific CTA); whats-new kept.
- Hook writes to `~/.codex/config.toml` only through `codex mcp add/remove`.

## Risks

1. `codex mcp add` semantics on an existing name (overwrite vs error) — the
   remove-then-add sequence covers both; verify live.
2. `commandWindows` quoting with a user-profile path containing spaces — one
   quoted token is the documented-safe shape; human gate covers it.
3. Git Bash absent on a Windows machine → hook degrades to a readable note.
4. Codex plugin cache copy for git sources is a full `git clone` copied
   verbatim, `.git` included (confirmed in `store.rs` `copy_dir_recursive`) —
   accepted; the plugin does not depend on `.git`, and the venv lives outside
   the cache.
6. Concurrent Codex sessions share one plugin data dir and race on
   `config.toml` (unlocked read-modify-write); mitigated by the single
   idempotent `codex mcp add`, the stamp check (steady state spawns nothing),
   and the mkdir lock around registration. Residual: an unrelated Codex
   config write landing inside the registration window can be lost — same
   exposure any `codex mcp add` has today.
5. Two marketplaces offering `vibe-cognition` once Loki adds the colton one —
   Codex dedups first-seen-wins; document removing the dev marketplace.
