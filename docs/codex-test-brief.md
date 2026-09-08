# Codex self-test brief — vibe-cognition plugin install (v0.35.0)

You are a Codex CLI agent running on Colton's Windows machine. Your job is to
verify that the **vibe-cognition** plugin installs and works in Codex, and to
write the results to a markdown report file. Work through the steps in order,
record what you observe (not what you expect), and never fabricate a result:
if a step cannot be checked from where you are, write UNVERIFIABLE and why.

## Context — why you are doing this

vibe-cognition is an MCP server plugin that keeps a per-project knowledge
graph (decisions, failures, discoveries, tasks). It has shipped for Claude Code
for months; version 0.35.0 adds Codex support. The Codex design is unusual and
you should know it so your checks make sense:

- Codex cannot bind a plugin-declared MCP server to the project you opened
  (plugin servers run with their working directory forced to the plugin
  folder). So this plugin ships **skills + hooks only**, and its
  **session-start hook registers the MCP server itself** by running
  `codex mcp add vibe-cognition …` at user level. That entry starts the server
  with Codex's own working directory, which is the project.
- Consequence: the **first** session after install has no server yet. The
  hook registers it and injects a note telling the user to restart Codex.
  From the **second** session on, the tools are available.
- The hooks run through `cmd.exe` on Windows and need **Git for Windows**
  (Git Bash). The plugin's wrappers locate it by full path; the WSL `bash`
  launcher in System32 must not be used.
- Background curation (`/vibe-curate`) is **not** available on Codex yet.
  That is expected, not a bug.

A manual test rig was previously installed by hand and has been fully torn
down. Part of your job is to confirm nothing **from vibe-cognition** remains,
so the plugin install is judged on its own. Other plugins, other MCP servers
(for example `teammate-comms`, which is being ported to Codex in parallel),
another project's `~/.codex/hooks.json`, and `[projects.…]` trust entries in
`config.toml` are NOT leftovers — leave them alone and do not report them as
failures.

**Always invoke the Codex CLI as `codex.cmd`** (for example
`codex.cmd mcp list`). Your sandboxed PowerShell blocks the npm `codex.ps1`
shim ("running scripts is disabled"); the `.cmd` shim is not affected. If a
`codex.cmd` call fails with "failed to resolve CODEX_HOME / Could not find home
directory", your sandbox has stripped the home directory: re-run that command
outside the sandbox (request escalation) or ask the user to run it and paste
the output. `Get-CimInstance Win32_Process` is also denied in the sandbox —
ask the user for process inventory rather than retrying.

## Report file

Write your findings to `E:\E Drive Projects\vibe-cognition\docs\codex-test-report.md`.
Overwrite any previous report; create it at the start with the header below and append a section per phase
as you go, so a partial run still leaves evidence. Use this header:

```
# Codex plugin test report — vibe-cognition 0.35.0
Date: <today>   Codex version: <output of `codex.cmd --version`>   Windows: <version>
Launch directory for the test: <path>
```

For every check write one of: **PASS**, **FAIL**, **UNVERIFIABLE**, followed
by the evidence (command output, quoted text, file contents). Keep evidence
short; quote the exact lines that matter.

## Phase 0 — pre-install cleanliness (leftover check)

Run these and record the raw output:

```powershell
codex.cmd --version
codex.cmd mcp list
codex.cmd plugin list
codex.cmd plugin marketplace list
if (Test-Path "$HOME\.codex\hooks.json") { Select-String -Path "$HOME\.codex\hooks.json" -Pattern "vibe" }
Test-Path "$HOME\.codex\vibe-hooks"
Test-Path "$HOME\.codex\vibe-cognition-data"
Get-ChildItem "$HOME\.agents\skills" -Directory -Filter "vibe-*" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name
Select-String -Path "$HOME\.codex\config.toml" -Pattern "mcp_servers.vibe-cognition|vibe-hooks|vibe-cognition-data"
Get-ChildItem "$HOME\.codex\plugins\cache","$HOME\.codex\plugins\data" -Recurse -Depth 2 -Directory -Filter "vibe-cognition*" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
```

A leftover is any of: a `vibe-cognition` row in `codex.cmd mcp list`; a
`vibe-cognition` plugin or a `vibe-cognition-dev` marketplace already listed;
a `vibe` match inside `~/.codex/hooks.json`; `~/.codex/vibe-hooks` or
`~/.codex/vibe-cognition-data` existing; any `vibe-*` skill folder; any
`config.toml` line matching `mcp_servers.vibe-cognition`, `vibe-hooks`, or
`vibe-cognition-data`; any `vibe-cognition*` folder under the plugin cache or
data dirs. NOT leftovers: a `hooks.json` that mentions only other plugins, any
`[hooks.state…]` table, `[projects.'…']` trust lines (even ones naming
vibe-cognition), and the `teammate-comms` MCP server. If a real leftover is
present, record it as FAIL for Phase 0 and **stop** — do not install on top of
it. Report back.

## Phase 1 — install

```powershell
codex.cmd plugin marketplace add Haagndaazer/vibe-cognition
codex.cmd plugin marketplace list
codex.cmd plugin add vibe-cognition@vibe-cognition-dev
codex.cmd plugin list
```

Record: did the marketplace add succeed and list as `vibe-cognition-dev`? Did
the plugin install? Then inspect the cache:

```powershell
Get-ChildItem "$HOME\.codex\plugins\cache" -Recurse -Depth 3 -Directory | Select-Object -ExpandProperty FullName
Get-Content (Get-ChildItem "$HOME\.codex\plugins\cache" -Recurse -Filter plugin.json | Where-Object FullName -like "*\.codex-plugin\*" | Select-Object -First 1).FullName
```

PASS if the cached manifest shows `"version": "0.35.0"`, four skills, and
`"hooks": ["./adapters/codex/hooks.json"]`.

## Phase 2 — first session (registration)

**This needs a fresh Codex process.** Stop here, write the Phase 0–1 results,
and tell the user: "Please fully quit Codex, start it again inside a project
directory (for example `E:\E Drive Projects\linglang-teacher`), and give me
the prompt `continue the vibe-cognition test brief from Phase 2`."

When resumed in the new session, check and record:

1. Did a **"Vibe Cognition — Project Context"** digest (or the onboarding
   block for an empty graph) appear in your context before this turn? Quote
   its first line.
2. Did it contain the sentence **"vibe-cognition registered its MCP server
   with Codex"** with an instruction to restart? Quote it.
3. Run `codex.cmd mcp list` and quote the `vibe-cognition` row. PASS if the
   command column is `uv` and the args contain `--project` and
   `python -m vibe_cognition.server`, and the env column lists
   `UV_PROJECT_ENVIRONMENT`, `VIBE_DATA_DIR`, `VIBE_HARNESS`.
4. Check the data dir was created and the venv built:
   ```powershell
   Get-ChildItem "$HOME\.codex\plugins\data" -Directory | Select-Object -ExpandProperty Name
   Get-ChildItem "$HOME\.codex\plugins\data\vibe-cognition-vibe-cognition-dev" -Force | Select-Object -ExpandProperty Name
   Get-Content "$HOME\.codex\plugins\data\vibe-cognition-vibe-cognition-dev\codex-mcp.stamp"
   ```
   PASS if `.venv`, `codex-mcp.stamp`, and `.uv-sync-stamp` (inside `.venv`)
   exist. If the venv is missing, the first hook run may have been building it
   (a multi-gigabyte download); note the time and re-check after a few minutes.
5. If any "Hook failed" message appeared in the Codex UI, quote it. There is
   no plugin-side log file in this mode; if a hook failed, run the wrapper by
   hand and paste the stderr:
   ```powershell
   $root = (Get-ChildItem "$HOME\.codex\plugins\cache\vibe-cognition-dev\vibe-cognition" -Directory | Select-Object -First 1).FullName
   $env:CLAUDE_PLUGIN_ROOT = $root
   $env:CLAUDE_PLUGIN_DATA = "$HOME\.codex\plugins\data\vibe-cognition-vibe-cognition-dev"
   cmd /c "$root\adapters\codex\hooks\session-start.cmd"
   ```

Then stop again and tell the user: "Phase 2 recorded. Please fully quit Codex
and start it again in the same project, then prompt me with `continue the
vibe-cognition test brief from Phase 3`."

## Phase 3 — second session (the real test)

1. Run `/mcp` (or the equivalent status view) and record whether
   `vibe-cognition` shows as connected. If it shows an error or timeout, quote
   it verbatim.
2. Call the MCP tool `get_status`. Record `repo_path` and whether it equals the
   directory Codex was launched in. Record `embedding_status`.
3. State, in your own words, what the vibe-cognition server instructions tell
   you to do. PASS if you can name the four standing practices and the note
   that curation is not available on Codex yet.
4. Did the **Project Context** digest appear before your first turn in this
   session? Quote its first line. It should **not** contain the "registered
   its MCP server … restart" note this time.
5. Type `$vibe-cognition` and confirm the skill loads. Record the first line
   of its content. Then check `$vibe-curate` is **not** offered (expected).
6. Run `/compact`. After compaction, confirm a block titled **"Vibe Cognition -
   Standing Practices (re-injected after compaction)"** appeared, and whether
   the Project Context digest followed it.
7. Record a test node so the write path is exercised, then read it back:
   call `cognition_record` with node_type `discovery`, summary
   `Codex plugin install test 0.35.0`, detail `Written by the Codex self-test
   brief to verify the write path.`, context `codex-test`, author `Colton Dyck`;
   then call `cognition_search` with query `Codex plugin install test` and
   confirm the node is returned.
8. Open a **second** Codex session in a **different** project (ask the user to
   do this if you cannot), then from PowerShell:
   ```powershell
   Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'uv.exe' -and $_.CommandLine -match 'vibe_cognition\.server' -and $_.CommandLine -match '\.codex[\\/]plugins[\\/]cache[\\/]' } | Select-Object ProcessId, ParentProcessId, CommandLine | Format-List
   ```
   The plugin root in the command line uses FORWARD slashes
   (`C:/Users/…/.codex/plugins/cache/…`) and the python children reference the
   data-dir venv, so filter on the `uv.exe` launchers as above. PASS if two
   launchers appear, both from the `0.35.0` plugin cache. If `Get-CimInstance`
   is denied inside your sandbox, write UNVERIFIABLE and ask the user to run it
   and paste the output.

## Phase 4 — wrap up

Append a **Summary** section: a table of every check with PASS/FAIL/
UNVERIFIABLE, then the three most important observations, then any error
text you saw verbatim. Do not uninstall anything. Tell the user the report is
at `docs/codex-test-report.md` and that they should hand it back to the
Claude Code session that built the plugin.
