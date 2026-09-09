# WP-P1 — Server-side curation containment token

Status: SHIPPED (2026-09-08) — sonnet code review APPROVE-WITH-CHANGES, all findings applied
Parent: `docs/codex-parity-plan.md` rev 3; supersedes WP-H0a from the Hermes plan
Date: 2026-09-08

## Scope

1. `cognition_begin_curation()` tool: mints a per-server-process curation
   session (`session_id`, `curation_token`), records it in the lifespan
   context, returns the current `uncurated` count.
2. `cognition_add_edge`, `cognition_add_edges_batch`, `cognition_mark_curated`
   take `curation_token`; without a valid one they refuse with an actionable,
   harness-aware error (`{{invoke:vibe-curate}}` on Claude Code; "curation is
   not available on Codex yet" there). Every token-authorized edge write is
   stamped with the session id (`CognitionEdge.curation_session`, journaled;
   `None` for legacy and deterministic edges).
3. `get_status` reports `curation_sessions: {active, writes}`;
   `edges_outside_curation` and `edge_sources` unchanged.
4. Orchestrator template: step 0 calls `cognition_begin_curation` and passes
   the token on every write; tool whitelist gains the new tool. Skill tool
   table, README, and `parity.json` gain the tool.
5. Deterministic `part_of`/`relates_to` edges and task-parent edges write
   through `storage.add_edge` directly and are untouched.

## Acceptance criteria

- AC1 Token-less `cognition_add_edge`/`_batch`/`mark_curated` return an error
  whose text names the curation token and the harness's curate invocation;
  no edge/mark is written (ledger 20: the test asserts the specific reason,
  and asserts graph state unchanged).
- AC2 A wrong token is refused the same way; a valid token succeeds and the
  written edge carries `curation_session` == the session id, visible through
  `cognition_get_neighbors` and surviving journal replay.
- AC3 `get_status().curation_sessions.writes` counts token-authorized writes.
- AC4 Claude Code orchestrator render includes `cognition_begin_curation` in
  its whitelist and instructs passing the token; Codex render of the
  cognition skill documents the tool as curator-only.
- AC5 Existing tests that drive the write tools pass with a token; the wedge
  smoke table still reaches every tool.
- AC6 Full suite green (modulo the two known lifecycle failures), ruff clean,
  `render_harness --check` and `render_parity --check` pass.

## KNOWN-INTENTIONAL

- The token is friction + audit, not an ACL: any client of this server can
  call `cognition_begin_curation`. Claude Code's subagent whitelist remains the
  hard boundary there; Codex has only this. PARITY.md says so.
- No env kill switch: prompts and server ship in the same plugin version.
- Tokens live in process memory; a server restart invalidates them and the
  orchestrator simply begins a new session. `get_status.curation_sessions.
  started` is a lifetime count for this server process (no end-of-session
  call exists), not a live in-flight count. On Codex each spawned agent
  thread runs its own MCP server process, so a launcher's counters stay 0
  while the orchestrator's process holds the session (Gate P2, episode
  97d97da257c3); `uncurated`, `edge_sources`, and the edge's
  `curation_session` are the cross-process evidence.
- `cognition_remove_edge` / `cognition_remove_node` stay ungated: they are
  repair tools for humans and the curator alike, and gating deletes would
  block legitimate cleanup; `edge_sources` and journal provenance still record
  every write. Revisit if deletes ever show up as a containment vector.
- Sonnet code review 2026-09-08: APPROVE-WITH-CHANGES; all five findings
  applied (uncurated count via count_uncurated_nodes, this section, per-site
  token reminders in the orchestrator, stronger refusal assertions, counter
  renamed to `started`).
