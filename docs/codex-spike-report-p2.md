# Codex spike report — WP-P2 subagent facts
Date: 2026-09-08   Codex version: codex-cli 0.153.4
Launch directory: E:\E Drive Projects\vibe-cognition   Model in this session: GPT-6 (exact runtime model slug UNVERIFIABLE from the exposed session metadata)

Scope: empirical observations from this session's `collaboration.spawn_agent` surface. These observations do not establish stock-install defaults or another harness's behavior. No configuration was changed.

Version command: `codex.cmd --version`, exit code 0. Verbatim output:
```text
WARNING: proceeding, even though we could not create PATH aliases: Could not find home directory
codex-cli 0.153.4
```
The command succeeded despite the warning, so the brief's instruction for a failed command did not apply.

Required workflow lookup returned the manual WP-C1a installation procedure, not a P2 test procedure. The supplied P2 brief governed this test. No AGENTS.md was found at the workspace root or in docs.

## Step 1 — root spawn with an explicit model

**PASS** — root spawn with explicit `gpt-5.6-luna` succeeded.

Verbatim call to `collaboration.spawn_agent`:
```json
{
  "task_name": "ready_probe",
  "fork_turns": "none",
  "model": "gpt-5.6-luna",
  "message": "Reply with exactly the word READY and nothing else."
}
```
Verbatim return:
```json
{"task_name":"/root/ready_probe"}
```

`fork_turns="none"` was necessary because this tool disallows model overrides with a full-history fork. The required message and model were unchanged.

Timing evidence from `Date.now()` / `new Date().toISOString()`:
```json
{"afterSpawn":"2026-09-08T21:39:49.803Z","elapsedMs":7830}
{"observed":"2026-09-08T21:39:52.677Z","elapsedMs":10704}
```
The baseline was 2026-09-08T21:39:41.973Z, immediately before issuing the spawn. Completion was observed by 10.704 seconds after that baseline. This is an upper bound including parent scheduling/tool overhead; the exact child finish timestamp and pure execution duration are **UNVERIFIABLE** because the notification carries no timestamp.

Completion arrived unprompted between parent tool calls, before any `wait_agent` or `list_agents` call:
```text
Message Type: FINAL_ANSWER
Task name: /root
Sender: /root/ready_probe
Payload:
READY
```

**PASS** — unknown model rejected, including the available-model list.

Verbatim call:
```json
{
  "task_name": "invalid_model_probe",
  "fork_turns": "none",
  "model": "not-a-real-model",
  "message": "Reply with exactly the word READY and nothing else."
}
```
Verbatim error:
```text
Unknown model `not-a-real-model` for spawn_agent. Available models: gpt-6-astra, gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5
```

## Step 2 — nested spawn (depth limit)

**FAIL — NESTED SPAWN SUCCEEDED.** The design's expected refusal was not observed in this session.

Verbatim root call:
```json
{
  "task_name": "depth_probe",
  "fork_turns": "none",
  "model": "gpt-5.6-sol",
  "message": "You are a test agent. Call spawn_agent once with message 'say HI' and no model override. Report verbatim whether the call succeeded or what error text it returned. Then finish."
}
```
Verbatim root return:
```json
{"task_name":"/root/depth_probe"}
```
The test agent's report arrived unprompted:
```text
Message Type: FINAL_ANSWER
Task name: /root
Sender: /root/depth_probe
Payload:
{"task_name":"/root/depth_probe/say_hi"}
```
This is the child agent's verbatim reported tool result. Its internal call arguments/transcript were not separately inspected. No depth-limit error was reported. Notification arrived after the parent clock read at 2026-09-08T21:40:10.362Z and before the next parent call at 2026-09-08T21:40:16.662Z. Exact child execution timing is **UNVERIFIABLE**.

Whether a stock install uses depth 1 is **UNVERIFIABLE** from this test: effective configuration/default provenance was not measured. The observed success is sufficient to invalidate an unconditional refusal assumption for this environment.

## Step 3 — completion observation

**FAIL** for “completion by polling only.” Both root test agents delivered `FINAL_ANSWER` notifications without polling, quoted above.

Parent observation-tool counts across Steps 1–3: `wait_agent` = **0**; `list_agents` = **0**. The parent continued local work and received completion at message boundaries. Push while the parent is idle or after the root turn ends was not tested.

## Step 4 — inline pass timing

**PASS** — completed the requested five-call read-only sequence, with no spawning during this step.

Calls were awaited sequentially, with `Date.now()` immediately before and after each awaited MCP invocation. These are client-observed wall-clock durations including transport; they are not server CPU timings. A workflow lookup had already warmed the embedding search.

| Tool | Start (UTC) | Wall-clock ms |
| --- | --- | ---: |
| get_status | 2026-09-08T21:40:16.662Z | 3088 |
| cognition_get_uncurated_nodes | 2026-09-08T21:40:19.751Z | 3485 |
| cognition_get_node | 2026-09-08T21:40:34.418Z | 2980 |
| cognition_get_neighbors | 2026-09-08T21:40:37.398Z | 2341 |
| cognition_search | 2026-09-08T21:40:39.739Z | 2886 |

- Whole sequence, first status invocation through final search completion: **25965 ms (25.965 s)**.
- Sum of the five awaited tool durations: **14780 ms**.
- Three-call node pass, including in-script overhead: **8208 ms (8.208 s)**.
- Longest individual call overall: **cognition_get_uncurated_nodes, 3485 ms**.
- Longest call within the three-call node pass: **cognition_get_node, 2980 ms**.
- Whole-sequence timing includes the parent inspection/selection gap between fetching the worklist and reading the chosen node. It excludes earlier workflow lookup, agent tests, and report writing. One warm sample does not establish typical or cold-start latency.

Verbatim call arguments:
\`get_status\`
```json
{}
```

\`cognition_get_uncurated_nodes\`
```json
{
  "limit": 10
}
```

\`cognition_get_node\`
```json
{
  "node_id": "3bbf6312ee20"
}
```

\`cognition_get_neighbors\`
```json
{
  "node_id": "3bbf6312ee20"
}
```

\`cognition_search\`
```json
{
  "query": "WP-P3 shipped (15f36ca): update nudge works on Codex (harness-keyed marketplace + manifest paths, \"restart Codex\", CTA = marketplace upgrade + re-add), the Codex hook preserves user-added MCP env keys across re-registration (codex mcp get --json → sed parse → merged into codex mcp add), and the Plan agent ships as an installed Codex role (agents-src/plan.md → adapters/codex/agents/vibe-plan.toml → ~/.codex/agents); sonnet review APPROVE-WITH-CHANGES with two HIGH fixes applied; 1391 tests pass"
}
```

Selected node: `3bbf6312ee20` (the first returned uncurated node).
Verbatim response excerpts:
```text
"embedding_status":"ready"
"uncurated":2
"count":2,"total_uncurated":2
```
`cognition_get_node` returned the requested ID and full narrative. `cognition_get_neighbors` returned an empty outgoing list and 16 incoming entries. `cognition_search` returned the selected node as its first result, with:
```text
"count":10,"total_found":46,"exhaustive":false
```
The summary used for the search is quoted verbatim in the call arguments above.

No calls to `cognition_add_edge`, `cognition_add_edges_batch`, `cognition_begin_curation`, or `cognition_mark_curated` were made. No project-memory nodes were recorded; this test's results are delivered in this report.

## Step 5 — summary

PASS/FAIL below evaluates each proposition named in the brief, not whether the test ran successfully.

| Fact | Result | Evidence |
| --- | --- | --- |
| Root spawn with model | **PASS** | Explicit gpt-5.6-luna returned /root/ready_probe and delivered READY. |
| Unknown model error | **PASS** | “Unknown model” error named all five available models. |
| Nested spawn refused | **FAIL** | Child reported {"task_name":"/root/depth_probe/say_hi"}. |
| Completion by polling only | **FAIL** | Both FINAL_ANSWER messages arrived with zero wait_agent/list_agents calls. |

Three most important observations:

1. Nested spawning succeeded in this environment; the design must account for that observed capability. Stock-default depth remains unverified.
2. Completion notifications reached the active parent without polling. A polling-only model does not describe this session's collaboration surface.
3. Both requested real model overrides were accepted, and an invalid name produced an explicit available-model list. The read-only node pass also completed successfully in 8.208 seconds, providing one warm timing sample.

Hand this report back to the Claude Code session that wrote `docs/codex-spike-brief-p2.md`.

