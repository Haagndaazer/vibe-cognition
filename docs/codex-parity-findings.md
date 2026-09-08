# Codex Parity Findings — adapting the Hermes plan for OpenAI Codex first

Status: RESEARCH rev 2 (findings only — no implementation authorized)
Peer review: sonnet adversarial pass 2026-09-08 — APPROVE-WITH-CHANGES; both
load-bearing corrections applied (MCP timeout defaults are 30 s / 300 s in
source, not the documented 10 s / 60 s; `sandbox_mode` is stripped from agent
roles, not allowed) plus the hook-timing hole it found (§4, §6).
Author: Colton Dyck (solo session)
Date: 2026-09-08
Baseline: `docs/hermes-parity-plan.md` rev 2 (graph decision `c0c6c89a381c`)
Direction (Colton, 2026-09-08): do the Codex port BEFORE Hermes, so the plugin
works seamlessly across Claude Code and Codex with a framework that keeps the
per-harness diff minimal.

Evidence policy: every load-bearing claim below is tagged **[doc]** (official
Codex docs at learn.chatgpt.com), **[src]** (openai/codex `main` source, read
2026-09-08), or **[measured]** (this machine). Per ledger 23, doc/source claims
are confidence, not truth — the WP-C1a spike is the live smoke that must run
before any of this hardens into a brief.

## Headline

Codex is much closer to Claude Code than Hermes is. Its hook system uses the
same `hooks.json` shape and the same `additionalContext` output contract, it
even sets `CLAUDE_PLUGIN_ROOT`/`CLAUDE_PLUGIN_DATA` for plugin hooks "for OOTB
compat with existing plugins" **[src]**, it reads MCP `instructions` **[doc]**,
its `SKILL.md` loader accepts our skills unchanged **[src]**, and it natively
loads the Agent Plugins v1 portable manifest that the Hermes plan already
picked as the shared packaging core **[src]**. Per-agent model pins exist, so
the Sonnet/Haiku tiering transfers **[src]**.

Two things do not transfer and one is a genuine design gap:

1. **Project root.** A plugin-bundled MCP server is launched with
   `cwd = ${PLUGIN_ROOT}`, gets no project-directory variable, and Codex has
   no MCP `roots` support — there is no `${CLAUDE_PROJECT_DIR}` analog
   **[src]**. This is the one piece of new design the port needs (§4).
2. **Curation containment.** Custom agent roles cannot restrict tools —
   `mcp_servers` in a role file is stripped, roles may only *preserve* parent
   authority **[src]**. Same verdict as Hermes: WP-H0a server-side containment
   is the answer, already planned.
3. **Hook-time server availability.** Plugin MCP policy in `config.toml`
   "intentionally excludes transport settings" **[src]** and plugin servers
   cannot be marked `required`, so nothing waits for our server before the
   SessionStart hooks run; the `mcp_tool` hook executor fails immediately if
   the server is not yet connected **[src]**. The startup timeout itself is
   30 s in source (docs say 10 s) and our cold start measured 15.7 s
   **[measured]**, so the timeout is not the gate — the race is (§4, §6).

## 1. Verified Codex surface, mapped to the Harness Capability Contract

| # | Duty | Claude Code today | Codex (verified) | Verdict |
|---|------|-------------------|------------------|---------|
| D1 | Register MCP server + project root + data dir | `plugin.json` `mcpServers`, `${CLAUDE_PLUGIN_ROOT}`, `${CLAUDE_PROJECT_DIR}`, `${CLAUDE_PLUGIN_DATA}` | Plugin `mcp.json` (Agent Plugins v1) or `.mcp.json` (legacy `.codex-plugin`). Stdio `command` must be a bare executable (`uv`) or a contained `./` path; `${PLUGIN_ROOT}`/`${PLUGIN_DATA}` expand in `args`, `env`, `cwd`; both are also injected into the child env and cannot be overridden; `cwd` defaults to `${PLUGIN_ROOT}` and must stay inside plugin root or data root **[src]**. **No project-dir variable, no MCP roots** **[src]**. | **partial** — data dir + plugin root map 1:1; project root needs §4 |
| D2 | Session-start prime injection | SessionStart hook → `hookSpecificOutput.additionalContext` | Identical event + identical output contract; `source` ∈ `startup\|resume\|clear\|compact`, matcher is a regex on `source` **[doc]**. Hooks enabled by default; default timeout 600 s **[doc]**. Default spill threshold for `additionalContext` is **2,500 tokens** (`additionalContextLimit` per handler raises it) **[src]**. | **full** (raise the limit; see §5) |
| D3 | Post-compaction reinjection | SessionStart `matcher: compact` | Both `SessionStart source=compact` and a dedicated `PostCompact` event inject `additionalContext`; PostCompact injects into the very next model request even mid-turn **[doc]**. | **full** |
| D4 | Standing practices (server instructions) | MCP initialize `instructions` | "Codex reads the MCP `instructions` field returned during initialization and uses it as server-wide guidance" **[doc]**. | **full** |
| D5 | Dependency bootstrap + venv health | SessionStart hook, 600 s | Same hook, same 600 s default **[doc]**. MCP timeouts: docs say `startup_timeout_sec` 10 s / `tool_timeout_sec` 60 s **[doc]**, but `main` source has `DEFAULT_STARTUP_TIMEOUT = 30 s` and `DEFAULT_TOOL_TIMEOUT = 300 s` **[src]** — source wins (ledger 14/23) but the spike measures the live value. Neither is overridable for plugin-provided servers (`PluginMcpServerConfig` = enabled, approval mode, `enabled_tools`, `disabled_tools`, per-tool only) **[src]**, and plugin servers have no `required` flag, so session start never waits for them **[src]**. Optional servers get a shared `mcp_optional_startup_grace_ms` of 1,000 ms while the initial tool catalog is built **[src]**. | **partial** — spike (§6) |
| D6 | Curation runner (background, contained, tiered) | Agent tool + `agents/*.md` tool whitelists + model pins | `multi_agent` on by default; `spawn_agent(agent_type, model, reasoning_effort, fork_context)`, `wait_agent`, `send_input`, `close_agent`, … **[src]**. Custom roles = TOML files under `<config folder>/agents/` (`~/.codex/agents/`, project `.codex/agents/`), role name comes from the file's `name`, and the override struct (`core/src/agent/role.rs`) carries only `developer_instructions`, `model`, `model_reasoning_effort`, `model_reasoning_summary`, `model_verbosity`, `personality`, `service_tier`, `features`, `skills`; **`mcp_servers`, `approval_policy`, `sandbox_mode`, `notify`, `apps`, and authority-expanding `features` are stripped** — roles can only preserve parent authority **[src]**. Plugins cannot ship roles (no `agents` manifest field) **[src]**. | **partial** — tiering full, tool containment waived → server-side token (WP-H0a) |
| D7 | Skills | plugin `skills/` | Plugin `skills/` loaded by default (Agent Plugins default path `./skills`) **[src]**; agentskills.io-compatible **[doc]**; `name` optional (defaults to dir name), `description` mandatory **[src]**; user invocation is `$vibe-curate` or `/skills`, not `/vibe-curate` **[doc]**; the rendered skill list is capped at 8,000 chars **[src]** (docs also say "2 % of the context window" — not found in source) and long descriptions get shortened **[doc]**. | **full** (text overlay, WP-H0d) |
| D8 | Update / distribution + whats-new | marketplace pin + `update_check` | `codex plugin marketplace add owner/repo`, marketplace manifest at `.agents/plugins/marketplace.json`, cache at `~/.codex/plugins/cache/$MARKET/$PLUGIN/$VERSION/`, `codex plugin marketplace upgrade`, enable/disable via `[plugins."name@market"]` **[doc]**. | **partial** — Codex update-check variant + CTA text |
| D9 | Harness-migration hygiene | `migrate_mcp` | n/a (Claude-specific legacy) | **n/a** |

Environment the MCP child actually receives (allowlist, not full inheritance)
**[src]**: Windows `PATH, PATHEXT, SHELL, COMSPEC, SYSTEMROOT, WINDIR,
SYSTEMDRIVE, USERNAME, USERDOMAIN, USERPROFILE, HOMEDRIVE, HOMEPATH,
PROGRAMFILES*, PROGRAMDATA, LOCALAPPDATA, APPDATA, TEMP, TMP, TMPDIR,
POWERSHELL, PWSH`; Unix `HOME, LOGNAME, PATH, SHELL, USER, LANG, LC_ALL, TERM,
TMPDIR, TZ`, plus the manifest `env`. `uv` on PATH and its cache under
`LOCALAPPDATA` both survive this; anything else we rely on must be set in
`env` explicitly.

Hooks on Windows run through `cmd.exe /C` (Unix: `/bin/sh -lc`), with an
optional `commandWindows` alternative per handler **[src]**. Hook commands run
with the session `cwd`; the project dir arrives in the stdin JSON `cwd` field,
not an env var **[doc]** — our `PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"`
fallback in `hooks/session-start.sh` therefore already resolves correctly.
Hook output structs are `deny_unknown_fields` **[src]**: any Claude-only extra
key in our hook JSON would fail the hook on Codex.

## 2. Packaging: one plugin root, two tiny manifests

Codex's Agent Plugins loader **[src]**:

- Manifest `plugin.json` at plugin root with
  `"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"`;
  `name` must be lowercase/digits/`.`/`-`, ≤ 64 chars.
- Defaults: `skills` → `./skills`, `mcp_servers` → `./mcp.json`.
- `mcp.json` is `{"$schema": ..., "mcp_servers": {"vibe-cognition": {"type": "stdio", "command": "uv", "args": [...], "env": {...}, "cwd": "${PLUGIN_ROOT}"}}}` — only `command/args/env/cwd`, unknown keys rejected.
- Codex-specific extras (hooks, apps, interface) come from
  `extensions."com.openai"` inside `plugin.json`, or from a legacy
  `.codex-plugin/plugin.json` overlay in the same root; the overlay's
  `paths.hooks` can be a path list or inline.

This is exactly the portable core WP-H0b already prescribes, and Hermes reads
the same format — so **H0b's restructure now serves two harnesses on day one**.
Proposed root layout:

```
plugin.json                 Agent Plugins v1 (Codex + Hermes)  ← new
mcp.json                    Agent Plugins stdio, ${PLUGIN_ROOT}/${PLUGIN_DATA} ← new
.claude-plugin/plugin.json  Claude Code (unchanged)
.codex-plugin/plugin.json   overlay: paths.hooks → ./hooks/hooks.json, interface ← new, ~10 lines
skills/                     shared, harness-neutral bodies + invocation overlay (H0d)
hooks/hooks.json            shared as-is (Codex substitutes ${CLAUDE_PLUGIN_ROOT} from the env it injects)
hooks/*.sh                  shared; PROJECT_DIR fallback already correct
agents/*.md                 Claude Code roles (source of truth)
adapters/codex/agents/*.toml GENERATED from agents/*.md at CI time (see §3)
```

Variable mapping is mechanical: `${CLAUDE_PLUGIN_ROOT}`→`${PLUGIN_ROOT}`,
`${CLAUDE_PLUGIN_DATA}`→`${PLUGIN_DATA}`, `${CLAUDE_PROJECT_DIR}`→ **none**
(§4). Keep the two manifests hand-written (they are tiny) and add a CI lint
asserting `name`, `version`, skills list and server command agree — cheaper
and more legible than a generator, and it slots into H0c's completeness gate.

Distribution: the marketplace repo `colton-claude-plugins` gains a second
manifest at `.agents/plugins/marketplace.json` (different path from
`.claude-plugin/marketplace.json`, so no name collision). Loki's pin step
becomes "pin the SHA in both files"; one pin request per release train, as
today.

## 3. Curation on Codex

- **Tiering transfers.** Each role TOML pins its own `model` /
  `model_reasoning_effort`, and `spawn_agent` accepts `model` overrides
  **[src]**. Mapping table lives in the adapter (haiku-class → a small OpenAI
  model, sonnet-class → a mid model); recorded in `parity.yaml` as *full*.
- **Tool containment does not transfer.** Roles cannot scope MCP tools; the
  analyzers would see every `cognition_*` tool. Containment on Codex is
  therefore WP-H0a's curation-session token (friction + tamper-evident audit),
  stated plainly in `PARITY.md` exactly as the Hermes plan already states it.
- **Roles are generated, not duplicated.** `agents/*.md` stays the single
  source; CI renders `adapters/codex/agents/<name>.toml` with
  `developer_instructions` = body and `model` = mapped frontmatter model. The
  `tools:` whitelist is dropped with a lint note. A parity test diffs the
  rendered TOML against the committed one, so drift fails CI.
- **Install location needs Colton's ruling.** Plugins cannot ship roles;
  candidates are (a) a documented one-time `codex` setup command that copies
  the TOMLs into `~/.codex/agents/`, (b) the SessionStart hook writing them
  there when missing/stale (a machine-state write outside the plugin data dir
  — the kind our rules say to surface, not assume), or (c) project-scoped
  `.codex/agents/` (only loads in trusted projects, per-repo friction).
- **Background execution.** `spawn_agent` is asynchronous with `wait_agent`
  **[src]**, so `/vibe-curate`'s "launch and return" shape survives; the
  orchestrator's spawn-discipline text gets a Codex overlay (no `name`, no
  `subagent_type`; `agent_type` + `model` instead).

## 4. The project-root problem and the recommended answer

Verified facts **[src]**: plugin stdio servers get `cwd=${PLUGIN_ROOT}`; the
only Codex-provided env is `PLUGIN_ROOT`/`PLUGIN_DATA`; the rmcp client
advertises no `roots` capability; tool calls carry a `threadId` in `_meta`
but not a path.

Options, with a recommendation:

**A1 — bind the project root from an `mcp_tool` hook (race-prone as a
SessionStart-only hook).** Codex hooks support a `mcp_tool` handler:
`{"type": "mcp_tool", "server": "vibe-cognition", "tool": "<tool>", "input":
{...}, "timeout": N}`, whose `input` template expands `${field}` placeholders
from the hook event JSON (which includes `cwd`), and the executor is wired for
ordinary local sessions (`CoreHookMcpExecutor` in `session.rs`) **[src]**. A
hook calling a new `cognition_bind_project(path=${cwd})` tells *this
session's* server process which repo it serves (a late-bind of `REPO_PATH`,
close to the existing `cognition_load_project` machinery). **Peer-review
finding (b), confirmed in source:** the executor calls with
`wait_for_server=false` and `ready_client()` returns "MCP server is not
connected" immediately — no retry, no grace; session construction only waits
for `required: true` servers, and plugin servers have no `required` flag; the
shared optional-server grace is 1,000 ms. SessionStart hooks are dispatched at
the *first turn* (`session/turn.rs`), which gives an incidental buffer while a
human types, but none for a scripted first turn or for `resume`/`compact`. So
a SessionStart-only bind fails deterministically on any fast first turn. If A1
is used at all it needs a second, idempotent channel — an `mcp_tool` handler on
`UserPromptSubmit` re-sending the bind every turn until the server reports
bound — and hook-failure noise on turn one has to be acceptable.

**A2 (recommended) — bind from a `command` hook via the data dir, resolve on
the server by thread id.** A `command` SessionStart hook (all sources) writes
`${PLUGIN_DATA}/sessions/<session_id>.json` containing the stdin `cwd` — no
MCP connection needed, so no race. The server resolves the project lazily on
each tool call from `_meta.threadId`, which Codex's hook executor already
inserts for "session-scoped MCP tools" **[src]**. The spike must prove two
things: that ordinary model-driven tool calls also carry `_meta.threadId`
(only the hook path is confirmed in source), and that the hook's `session_id`
equals that thread id. If either fails, fall back to A1-with-retry. Either
variant also relies on one server process per session — `Session::new`
constructs a fresh `McpRuntime` per session with no cross-session registry
**[src]**, which strongly suggests per-session in the desktop app too, pending
live confirmation.

**B (fallback, documented) — non-plugin registration.** A user-level
`[mcp_servers.vibe-cognition]` entry (via `codex mcp add`) with no `cwd`
inherits Codex's own cwd, i.e. the project, and `config.py`'s existing
`REPO_PATH` fallback works unchanged. It also allows `startup_timeout_sec`
and `required = true` (which makes session start wait for the server).
Skills then come from `~/.agents/skills` symlinks and hooks from
`~/.codex/hooks.json`; we lose one-command install and marketplace updates.

**C (rejected as primary) — project-scoped `.codex/config.toml`** per repo:
trusted-projects-only and per-repo friction.

## 5. Adapter notes that fall out of the verification

- Raise `additionalContextLimit` on our SessionStart/compact handlers; the
  prime digest is routinely larger than 2,500 tokens and would be spilled to
  a file otherwise. Prime's size-budget parameter (Hermes plan, WP-H1b) is now
  wanted by two harnesses.
- `deny_unknown_fields` on hook output: keep our JSON to
  `hookSpecificOutput.{hookEventName, additionalContext}` (already true).
- Windows: hooks run under `cmd.exe /C`, so `bash` must be on PATH exactly as
  Claude Code on Windows already requires; `commandWindows` gives us a
  PowerShell escape hatch if Git Bash is absent. Codex's native Windows
  support is labelled experimental (March 2026) — our human field gate must
  cover native + WSL.
- Skill descriptions: front-load trigger words; Codex shortens long
  descriptions under its 8,000-char skill-list budget.
- CTA text is harness-keyed: `/plugin update` → `codex plugin marketplace
  upgrade`; `/vibe-curate` → `$vibe-curate`. Add a `VIBE_HARNESS=codex` env
  in `mcp.json`/hooks so prime, whats-new and update-nudge pick the right
  template without sniffing.
- `tool_timeout_sec` (60 s per docs, 300 s per source): `cognition_search`
  during embedding warm-up already fast-returns `loading_embeddings`
  (measured model load 16–58 s), so no tool should block long enough to trip
  either value; confirm in the conformance suite.

## 6. Startup timing versus the startup timeout and the hook race

From `%TEMP%/vibe-cognition-startup/pid-*.log` **[measured]**:

| Start | module import | handshake yield (from import start) |
|-------|---------------|--------------------------------------|
| warm  | 2.9 s         | 3.3 s                                |
| cold  | 14.8 s        | 15.7 s                               |

`uv run` launch overhead sits in front of both numbers. Against the
documented 10 s default a cold start would fail; against the 30 s source
default it clears with margin. Plugin-provided servers cannot change the
value either way **[src]**, so the spike measures the live timeout before
anything is designed around it. The gating number is not the timeout but the
hook race in §4: at 3.3 s warm the server is not connected when a fast first
turn fires SessionStart hooks, and the 1,000 ms optional-server grace only
covers the initial tool catalog. Deferring heavy imports behind the handshake
(`_heavy_import_guard.py` is the existing hook point) still helps — it
shrinks the race window and protects against a real 10 s deployment — but it
does not remove the need for A2 or A1-with-retry. This interacts with the
open stale-first-session task (`43d3c3dab10f`) and the H-4 first-install
race, so whatever ships gets its own brief.

## 7. Re-sequenced plan: Codex first, Hermes second

Phase 0 is unchanged and now pays for two harnesses:
H0a (server-side containment) → H0b (Agent Plugins v1 root — Codex loads it
natively) → H0c ∥ H0d.

Phase 1-Codex (new, replaces Hermes as harness #2):

- **WP-C1a — spike (go/no-go, live smoke per ledger 23).** Install real Codex
  CLI on Windows; load the plugin from a local marketplace; measure the live
  MCP startup timeout (10 s per docs vs 30 s per source) and our `initialize`
  latency warm + cold; prove whether ordinary tool calls carry
  `_meta.threadId` and whether it equals the hook's `session_id` (decides A2
  vs A1-with-retry); observe hook↔server ordering on a fast scripted first
  turn; confirm one server per session in CLI and desktop app; confirm
  `${CLAUDE_PLUGIN_ROOT}` substitution in `hooks.json` and `deny_unknown_fields`
  acceptance of our hook output; confirm skills load and `$vibe-cognition`
  invokes; confirm a server that connects after the 1,000 ms grace still gets
  its tools added to the catalog.
- **WP-C1b — packaging + project-root bind (D1, D7, D8).** `plugin.json` +
  `mcp.json` + `.codex-plugin` overlay; the A2 (or A1-with-retry) bind chosen
  by the spike; `.agents/plugins/marketplace.json` in `colton-claude-plugins`;
  manifest-parity lint; `VIBE_HARNESS`.
- **WP-C1c — curation (D6).** TOML role generator + CI drift test; model map;
  orchestrator/skill Codex overlays; install-location per Colton's ruling;
  `parity.yaml` waiver row for tool containment.
- **WP-C1d — update/whats-new (D8).** Codex `update_check` reading the Codex
  marketplace manifest; harness-keyed CTAs.
- **Gate C1 — human field verification** (Windows native + WSL), including
  the empty-project-root and cold-start cases.

Phase 1-Hermes follows, now cheaper: H0b's root already loads there, and the
Codex role/skill overlay mechanism is the template for the Hermes one.

Effort feel (churn caveat applies — Codex ships weekly): Phase 1-Codex ≈ 1–1.5
weeks vs the Hermes estimate of 2–3, because D2/D3/D4/D7 are near-identical
rather than adapter-shaped. Critical path: C1a → C1b → (C1c ∥ C1d) → Gate C1;
C1c additionally needs H0a and H0d, same as H1c did.

## 8. Decisions needed from Colton

1. Approve the re-order: Codex is harness #2, Hermes #3, Phase 0 unchanged.
2. Rule on where Codex agent-role TOMLs get installed (§3: setup command,
   hook-written, or project-scoped).
3. Approve WP-C1a (the spike) as the first Codex WP, gating everything else.

## Sources

- Config reference, MCP, hooks, skills, subagents, AGENTS.md, plugins:
  learn.chatgpt.com/docs (config-file/config-reference, extend/mcp, hooks,
  build-skills, agent-configuration/subagents, agent-configuration/agents-md,
  plugins); developers.openai.com/plugins/build/plugins.
- openai/codex `main` (2026-09-08): `codex-rs/codex-mcp/src/agent_plugin_config.rs`,
  `codex-rs/codex-mcp/src/plugin_config.rs`, `codex-rs/core-plugins/src/agent_plugin_manifest.rs`,
  `codex-rs/utils/plugins/src/plugin_namespace.rs`, `codex-rs/plugin/src/manifest.rs`,
  `codex-rs/config/src/hook_config.rs`, `codex-rs/config/src/types.rs` (PluginMcpServerConfig),
  `codex-rs/hooks/src/engine/{discovery,command_runner,mcp_runner}.rs`,
  `codex-rs/hooks/src/output_spill.rs`, `codex-rs/core/src/hook_mcp_executor.rs`,
  `codex-rs/core/src/session/session.rs`, `codex-rs/core/src/tools/handlers/multi_agents_spec.rs`,
  `codex-rs/core/src/agent/role_tests.rs`, `codex-rs/agent-roles/src/loader.rs`,
  `codex-rs/rmcp-client/src/{utils,stdio_server_launcher}.rs`,
  `codex-rs/protocol/src/shell_environment.rs`, `codex-rs/skills/src/parser.rs`.
- Windows status: learn.chatgpt.com/docs/windows/windows-sandbox and March 2026 launch coverage.
