# Codex self-test brief — WP-P2 spike (subagent spawning facts)

You are a Codex CLI agent running on Colton's Windows machine with the
vibe-cognition plugin installed (v0.35.0 or later). Your job is to establish
four facts about Codex's subagent behavior that a design depends on, and to
write the results to a markdown report. Record what you observe, not what you
expect; write UNVERIFIABLE with a reason when you cannot check something.

## Context — why

vibe-cognition's background curation on Claude Code uses an orchestrator
subagent that spawns analyzer subagents. We are porting it to Codex. From the
Codex source we believe: (a) a stock install caps subagent depth at 1, so a
subagent's own `spawn_agent` is refused with "Agent depth limit reached. Solve
the task yourself."; (b) `spawn_agent`'s `model` must exactly match an
available model or it errors with "Unknown model … Available models: …";
(c) a finished subagent is observed only by polling (`wait_agent`), not by a
push notification. The design ("flat by default, adaptive fan-out") is
correct only if these hold. Test them.

Always call the Codex CLI as `codex.cmd` if you need it; if a command fails
with "Could not find home directory" inside your sandbox, ask the user to run
it outside the sandbox and paste the output.

## Report file

Write to `E:\E Drive Projects\vibe-cognition\docs\codex-spike-report-p2.md`,
overwriting any previous copy. Header:

```
# Codex spike report — WP-P2 subagent facts
Date: <today>   Codex version: <output of `codex.cmd --version`>
Launch directory: <path>   Model in this session: <name>
```

For every check write **PASS**, **FAIL**, or **UNVERIFIABLE**, with the
evidence quoted verbatim (tool call, error text, timing).

## Step 1 — root spawn with an explicit model

Call `spawn_agent` with `message` = "Reply with exactly the word READY and
nothing else." and `model` = `gpt-5.6-luna`. Record: did the spawn succeed,
what identifier came back, how long until the agent finished, and how you
found out it finished (did anything arrive unprompted, or did you have to
call `wait_agent` / `list_agents`?). Then repeat with `model` =
`not-a-real-model` and quote the error verbatim, including the list of
available models if it prints one.

## Step 2 — nested spawn (depth limit)

Call `spawn_agent` with `model` = `gpt-5.6-sol` and `message` = "You are a
test agent. Call spawn_agent once with message 'say HI' and no model
override. Report verbatim whether the call succeeded or what error text it
returned. Then finish." Wait for it and quote its report. PASS for our design
means the nested spawn was refused with the depth-limit text; if it
SUCCEEDED, record that prominently — it changes the design.

## Step 3 — completion observation

From Step 1 and Step 2 combined: state plainly whether a subagent's
completion reached you without polling. Quote whatever appeared. If you had
to poll, say which tool you used and how many calls it took.

## Step 4 — inline pass timing

Ask the vibe-cognition MCP server for `get_status` and `cognition_get_uncurated_nodes(limit=10)`.
Then, without spawning anything, perform for ONE node the read-only part of a
curation edge pass: `cognition_get_node`, `cognition_get_neighbors`,
`cognition_search` on its summary. Record wall-clock time for the whole
sequence and the longest single tool call. Do NOT call `cognition_add_edge`,
`cognition_add_edges_batch`, `cognition_begin_curation`, or
`cognition_mark_curated`.

## Step 5 — summary

Append a table of the four facts (root spawn with model; unknown model error;
nested spawn refused; completion by polling only) with PASS/FAIL/UNVERIFIABLE
and one line of evidence each, then the three most important observations.
Tell the user the report is at `docs/codex-spike-report-p2.md` and to hand it
back to the Claude Code session that wrote this brief.
