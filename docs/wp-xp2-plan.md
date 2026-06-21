# WP-XP2 — Cross-Project Cognition: read routing + provenance + semantic search over B

**Base:** `main` @ 05b0797. Branch: `fix/xp2-routing`.
**Depends on:** WP-XP1 (registry + load/unload/list; `resolve_project` stub; `ProjectEntry` with
`storage` / `embeddings: ChromaDBStorage | None` / `model_guard`).
**This is the payoff layer:** XP1 made B *loadable*; XP2 makes B *queryable* — read tools gain an
optional `project` arg, results carry provenance, and semantic search runs over B (gated by the
guard state). Writes are untouched (no `project` arg, ever — home only).

Design is Colton-approved: read tools take `project`; default = home; `"*"` fans across all loaded
(aggregate queries only); semantic search over an "unknown"-guard B proceeds WITH a
degraded-confidence caveat; a confirmed dim/model mismatch disables semantic for B (structural
still works); always-live.

---

## Commit 1 — extend the resolver + a provenance tagger

- **`resolve_project(lc, project: str | None)`** (`tools/project_registry.py`, currently a
  home-only stub): 
  - `None` / omitted → home entry (unchanged default).
  - a tag or resolved path → that entry (via `registry.resolve_tag`); error sentinel if unknown.
  - `"*"` → ALL entries (home + foreign). Only the aggregate tools accept `"*"` (see C3).
  - Return shape: a single `ProjectEntry`, or for `"*"` a `list[ProjectEntry]`, or an error
    marker the tool surfaces as `{"error": "no loaded project '<x>'; use cognition_list_projects"}`.
- **`tag_results(rows, tag)`** helper: stamp each result dict with `"project": tag` so the agent
  always knows provenance. Additive field — existing consumers (dashboard) ignore unknown keys.

## Commit 2 — `cognition_search` over a project (the hard one)

`cognition_search` gains `project: str | None = None`.

- Resolve to entry/entries. For each target entry:
  - **`entry.embeddings is None`** (no-index / dim-mismatch / model-mismatch from XP1): do NOT
    error. Return (for that project) `{"project": tag, "semantic_unavailable": "<model_guard
    reason>", "results": []}` — structural tools remain the way to read B. (The agent gets a clear
    signal, never a crash.)
  - **`entry.embeddings` present, `model_guard="match"`**: run semantic search.
  - **`model_guard="unknown"`** (pre-stamp): run semantic search BUT add
    `"confidence": "degraded (no model provenance for <tag>)"` to the response.
- **THE correctness trap (XP0/design review): pair the FOREIGN storage with the FOREIGN
  embeddings.** `_search_cognition` uses the storage for the N1 ghost-filter
  (`search_hit_is_live` / `_format_search_results`). If B's collection is queried but A's storage
  is passed as the filter, EVERY B hit is dropped (A's graph has none of B's node ids) → silent
  empty result. The routing MUST pass `entry.storage` AND `entry.embeddings` together. The shared
  `generator` embeds the query once (it's home's; safe — XP0 confirmed the encoder lock serializes
  but is correct).
- **`"*"` fan:** embed the query once; query each target's embeddings (skipping `None` bindings,
  noting them as semantic_unavailable); each result tagged with its project; merge into one list
  (keep per-project `semantic_unavailable`/`confidence` notes). No cross-project dedup (distinct
  graphs; a shared content-hash id in two projects is two legitimately different nodes — the
  `project` tag disambiguates).
- Provenance: every hit carries `"project": <tag>`.

**Result envelope (pin the shape):** `cognition_search` returns
`{"query", "results": [...hits each tagged "project"], "count", "project_notes": {<tag>:
{"semantic_unavailable": "<reason>"} | {"confidence": "degraded ..."}}}`. Per-project notes live in
`project_notes` (a map), NOT as sentinel rows inside `results` — so the caller never has to filter
non-hit dicts out of the hit list. Default single-home search keeps today's flat shape plus an
(empty) `project_notes` — verify existing callers tolerate the additive key.

**Fails-before tests:**
- **N1-pairing (THE load-bearing proof) — be precise about which assertion discriminates:** seed B
  with TWO embedded nodes; one LIVE in B's graph, one DELETED from B's graph but still embedded
  (a ghost). Search B. **The discriminating assertion is that the LIVE B node SURVIVES** — because
  if A's storage were wrongly used as the filter, the live B node would ALSO be dropped (A's graph
  has neither B id). The ghost-drop assertion is CONFIRMATORY ONLY — it passes with EITHER storage
  (A's storage also lacks the ghost id), so it does NOT prove correct pairing. The test must assert
  both but the spec/test comments must state that "live survives" is the sole proof of B-storage
  pairing; a green ghost-drop alone is not enough.
- Search over a no-index / dim-mismatch B → `project_notes[tag].semantic_unavailable` set, no
  exception, that project's `results` empty.
- Search over an unknown-guard B → results present + `project_notes[tag].confidence` caveat.
- `"*"` → results from home AND B present, each correctly tagged; per-project notes in
  `project_notes`.
- Default (no `project`) → byte-identical hit list to today's home search (regression guard).

## Commit 3 — `project` arg on the structural read tools + provenance

Add `project: str | None = None` to: `cognition_get_node`, `cognition_get_chain`,
`cognition_get_superseded_chain`, `cognition_get_incident_resolution`, `cognition_get_history`,
`cognition_get_neighbors`, `cognition_get_edgeless_nodes`, `cognition_get_uncurated_nodes`,
`cognition_get_document`. Each resolves to the entry and uses `entry.storage`; results tagged with
the project.

- **`"*"` cut by tool SHAPE, not an ad-hoc list:** the **list-returning aggregate tools**
  (`get_history`, `get_edgeless_nodes`, `get_uncurated_nodes`) **accept `"*"`** with fan-and-merge
  (each row tagged by source project; envelope carries `"projects_queried": [<tags>]`). The
  **single-node tools** (`get_node`, `get_chain`, `get_superseded_chain`, `get_incident_resolution`,
  `get_neighbors`, `get_document`) **reject `"*"`** with a clear error — node ids are content-derived
  and NOT project-namespaced, so the same id can exist in two projects; resolving a single node
  across "*" is ambiguous. They require a single project (default home or an explicit tag). (This is
  a deliberate, consistent rule — aggregate worklists fan; id lookups don't.)
- **Provenance on nested envelopes:** the single-node tools return one project's data, so tag the
  TOP-LEVEL envelope with `project=<tag>` (the nested lists in `get_superseded_chain` /
  `get_incident_resolution` are all from that same project — no per-node tagging needed). The
  aggregate tools tag each ROW.
- **`get_document` freshness is cross-project-aware:** `_get_document` re-hashes
  `metadata["path"]`, which is a path on B's MACHINE — on A it's absent/different, so `freshness`
  would always read `"missing"` and mislead. For a foreign `project`, emit
  `"freshness": "cross-project: unavailable"` instead of running the local re-hash. (Home unchanged.)
- `get_chain` / `get_neighbors` traverse edges WITHIN the resolved graph only — no cross-project
  traversal (cross-project edges are out of scope, design decision). Route to `entry.storage`.
- Always-live: each read goes through `CognitionStorage._synced()` → `_catch_up()`, so a B-targeted
  read reflects appends to B's journal since load (XP0 Q5: cheap; no throttle).

**Fails-before tests:** `get_node(id, project=B)` returns B's node tagged `project=<B>` (and the
SAME id in home returns home's node — proves routing, not id leakage); `get_history(project="*")`
and `get_edgeless_nodes(project="*")` merge home + B tagged per-source with `projects_queried`; a
single-node tool with `project="*"` returns the rejection error; `get_document(project=B)` emits
`freshness="cross-project: unavailable"`; default (no project) unchanged.

## Write-isolation (unchanged, re-asserted)

NO write tool (`cognition_record`, `store_document`, `update_node`, `add_edge`, `add_edges_batch`,
`mark_curated`, `remove_edge`, `remove_node`) gains a `project` arg. They continue to use
`lc["cognition_storage"]` (home). `cognition_reload` is also home-only and gains NO `project` arg
(it rebuilds home's in-memory graph from disk — not a journal write, but not foreign-routed in XP2
either); include it in the assertion set.

**Write-isolation test — pin the mechanism (the weak forms don't count):** register the tools
against a MOCK MCP object that captures each `@mcp.tool()`-registered callable, then assert via
`inspect.signature` that none of the write/home-only tools above expose a `project` parameter. Do
NOT substitute (a) `ast.parse` grepping the source (fragile to renames) or (b) inspecting only the
`_core` helpers (a future edit could add `project` to the wrapper but not the core) — the mock-MCP
capture is the only form that tests the actual registered tool surface.

## Out of scope

- Cross-project EDGES (linking a home node to a B node) — reopens the id-namespace problem; future.
- Dashboard visualization of loaded projects — MCP-tools-only for now.
- Writing to B (forever — read-only is the core guarantee).

## Known-intentional (do NOT "fix")

- Default (no `project`) read behavior is byte-identical to today — existing callers unaffected.
- `"*"` excluded from node-specific tools is deliberate (id-collision ambiguity), not an oversight.
- Semantic freshness over B is bounded by B's own embedding sync (accepted always-live ceiling);
  structural reads ARE live via catch-up.
- An `unknown`-guard B is searched WITH a caveat, not refused (Colton decision).

## Acceptance criteria

- **N1-pairing PROVEN**: search over B uses B's storage as the ghost-filter. The DISCRIMINATING
  assertion is a LIVE B node SURVIVES (with A's storage it would be wrongly dropped); the
  ghost-node-dropped assertion is confirmatory only (passes with either storage) — the test/comments
  must say so. Load-bearing proof.
- Default-home regression: search + every read tool with no `project` == today's behavior (hit list
  byte-identical; additive envelope keys tolerated by existing callers).
- Semantic-disabled paths (no-index/dim/model mismatch) → `project_notes[tag].semantic_unavailable`,
  no crash; structural reads over the same B still work.
- `unknown`-guard search → results + `project_notes[tag].confidence` caveat.
- `"*"` fans + tags per source on the aggregate tools (search, get_history, get_edgeless_nodes,
  get_uncurated_nodes) with `projects_queried`; single-node tools reject `"*"`.
- Provenance `project` tag on every result (row-level for aggregate, envelope-level for single-node).
- `get_document(project=B)` → `freshness="cross-project: unavailable"` (no misleading local re-hash).
- Write/home-only tools (incl. `reload`) assert-tested via the mock-MCP capture to have NO
  `project` param.
- Always-live: append to B's journal mid-session → reflected in a B-targeted structural read.
- Whole-repo `uv run pyright` at baseline (server.py:167 only); full suite green; journal not on
  branch (manager flushes).
