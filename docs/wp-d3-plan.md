# WP-D3 Execution Plan — /vibe-document skill + docs (v0.8.0)

Brief = `docs/DESIGN-document-storage.md` §7 (WP-D3 row: "/vibe-document skill (store → descriptor entities → curate), README/SKILL docs") + §6 (MCP surface) + the §9 S4/N3 finding (the link-by-citing-`doc:` workflow is **load-bearing, not a footnote**). Last functional WP before the v0.8.0 cut (D4 = dashboard, then release). Builds on merged D2 (`dd11cd2`): documents are stored (reference/copy), searchable (chunked), and deletable.

This WP is **docs + a skill** — almost no runtime code. The one code artifact is a doc-drift GUARD test that turns audit S-3 into a regression guard so the surfaces can't silently drift again.

## Binding rules (carried)
Rule 20 (assertions name the failure mode), 12 (fails-before RUN — applies to the guard test), 21 (re-search constraints), 18 (seam-check). Journal protocol. pyright ≤ 29. SHA-pinned merge gate.

## The load-bearing workflow (DESIGN §9 S4/N3 — the spine of the skill)
A document connects to the graph **only** by descriptor nodes citing its `doc:<hash>` ref in THEIR `references`:
- The matcher gates document links on a shared `doc:` key (D1b); `_store_document` restricts the document node's OWN references to `[doc:<sha>]` and routes agent refs to context. So the document is an inert hub until something cites its `doc:` ref.
- A descriptor **entity** (`decision`/`discovery`/`constraint`/…) recorded via `cognition_record` with `doc:<hash>` in its `references` auto-links `part_of` the document (entity→document, deterministic). An **episode** citing it links `relates_to`.
- Therefore the skill MUST make "store the document, then record its facts as entities citing the returned `doc_ref` in `references`, then `/vibe-curate`" the DEFAULT, front-and-center workflow — not an aside. If the agent puts `doc:<hash>` anywhere but `references`, nothing links.

---

## Commit 1 — the `/vibe-document` skill (`skills/vibe-document/SKILL.md`)
Skills auto-discover from `skills/<name>/SKILL.md` (no `plugin.json` change). Model it on `skills/vibe-cognition/SKILL.md`.
- **Frontmatter `description`:** trigger on "store/attach/save a document, PDF, client doc, spec, contract as project memory." Make it imperative ("You MUST use this skill when storing a document …") matching the house style.
- **Core workflow (the spine, step-ordered + an example):**
  1. `cognition_store_document(file_path | content_text, title, document_text=<agent-extracted text>, context, author, [store_copy, local_only])` → returns `doc_ref` (`doc:<sha[:12]>`).
  2. Record the document's CONTENTS as separate descriptor **entity** nodes with `cognition_record`, each citing the returned `doc_ref` in `references` (NOT context) → they auto-link `part_of` the document. This is THE connection mechanism — state it as load-bearing.
  3. Run `/vibe-curate` to add semantic edges (MANDATORY, same rule as /vibe-cognition).
- **Cover:** agent-extracts-text (server never parses binaries); reference (default) vs `store_copy` + `local_only` + the size policy + the **git-history privacy caveat** (a committed blob survives deletion); `cognition_get_document` (metadata + text + `freshness` unchanged|modified|missing); search returns documents with a `matched_excerpt`; deletion reclaims managed artifacts (sidecar/blob/chunks) but never the referenced original.
- **A worked example** (store a client spec → record 2-3 decision/constraint entities citing its `doc_ref` → curate).

## Commit 2 — update `/vibe-cognition` SKILL + fix the cross-surface edge-type drift (audit S-3)
- **Tool table (S-3) — close the WHOLE gap, not just the doc tools (peer-review A1/A2):** the table lists **10 of the 17** registered tools; **7** are missing, not 2. Add the 5 missing cognition tools — `cognition_store_document`, `cognition_get_document`, `cognition_get_uncurated_nodes`, `cognition_mark_curated`, `cognition_reload` — to the main table, and a **"Service / dashboard"** row for `get_status` and `cognition_dashboard` (the audit explicitly flagged the SKILL "relies on the unlisted `get_status`"). After this the table documents all 17, so the Commit-4 guard can go green. Add a one-line "Documents" subsection pointing at `/vibe-document`.
- **Edge-type accuracy (S-3 "3 vs 4 vs 5 types"):** the SKILL under-counts in TWO spots — line ~22 lists 3 semantic (`led_to, resolved_by, supersedes`) and line ~135 lists 4 (adds `contradicts`); correct BOTH to the full proposer set of 5 (`led_to, resolved_by, supersedes, contradicts, relates_to`). Correct the deterministic claim to reality after D1b: deterministic edges are now `part_of` (entity↔episode any-ref; entity→document on `doc:`) **and** `relates_to` (document→episode on `doc:`). Note `relates_to` has THREE provenances now (deterministic doc→episode, curator-proposed, manual) so the prose doesn't mislabel it "semantic only." Mention `duplicate_of` is reserved/unsupported in `add_edge` (the SKILL omits it entirely today).
- Cross-reference `/vibe-document` from the Workflow Integration section.

## Commit 3 — README document-storage section + remaining S-3 doc-drift
- **README:** add `cognition_store_document`/`cognition_get_document` to the tool table (~line 113); a short "Document storage" feature section (reference vs copy, privacy caveat, search-inside, link-by-`doc:`-ref); update the edges section (~233-238) so the deterministic-edge claim includes the document pair rules (today it says `part_of` is the *only* automatic edge — now `relates_to` doc→episode is too).
- **vibe-backfill (S-3, LOW):** change "**consider** running /vibe-curate" → MANDATORY, matching the curate skill's own first-trigger + the hard rule in /vibe-cognition.
- **Out of S-3 scope (NOT this WP):** README standalone-dashboard instructions (MED, dashboard = WP-D4); CHANGELOG / prime.py / hook-list items (unrelated to the document surface) — leave, or note. State explicitly what's deferred so the asymmetry isn't read as intent.

## Commit 4 — doc-drift GUARD test (turn S-3 into a regression guard)
The audit found the SKILL tool table + edge-type lists drift from reality. Encode the invariant so it can't recur (the structural-binding discipline, ledger 11/20 applied to docs):
- **Tool-table guard:** enumerate the registered tools via `asyncio.run(register_all_tools(FastMCP(...)); mcp.list_tools())` — FastMCP 3.1.1 (installed; floor is `>=2.0.0`) exposes `await mcp.list_tools()` returning objects with `.name`; the 2.x `_tool_manager`/`get_tools()` are GONE, so use `list_tools()` and make the test async (peer-review B1; verified a bare instance needs no lifespan). Assert EVERY registered tool name (all 17) appears ANYWHERE in `skills/vibe-cognition/SKILL.md` (whole-file name match, so the service/dashboard row placement of `get_status`/`cognition_dashboard` satisfies it) — defined over the FULL set, matching Commit 2's full-gap fix, so it goes green. Fails if a future tool is added without documenting it (exactly the S-3 miss).
- **Edge-type guard:** assert the **6** non-`duplicate_of` `CognitionEdgeType` values are each named in the SKILL + README edge sections (the "3 vs 4 vs 5" drift can't reappear). NOTE this is a PRESENCE check, not a semantics check — it can't catch a wrong "How Created" label (e.g. `relates_to` mislabeled "semantic only"); that still needs manual prose review (peer-review B2). Pin the expected set to `CognitionEdgeType` minus `duplicate_of` so it tracks the enum.
- **Fails-before (rule 12):** RUN both against the PRE-commit-2/3 docs (tool table missing 7 tools / edge list at 3-4) → red → green after the doc fixes.

**Verification:** docs themselves have no unit tests, but the guard test covers the tool-table + edge-type invariants. Manually review the `/vibe-document` workflow prose for the S4/N3 default (doc_ref → references). Full suite + ruff + pyright ≤ 29.

---

## Out of scope (tracked)
- WP-D4 (dashboard document list + token-gated download; the dashboard document-search NAV deferral from D2; README standalone-dashboard fix S-3 MED).
- v0.8.0 version bump (`pyproject.toml` + `plugin.json`) + CHANGELOG — the RELEASE step after D4, per CLAUDE.md (Loki re-pins). Not a per-WP action; flag at the cut.
- Vince backlog #1/#2; audit E-7; the non-document S-3 LOW items (prime.py stdlib claim, hook list).

## Build order rationale
The skill (1) is the deliverable; the /vibe-cognition + README updates (2,3) align the existing surfaces with the post-D1b/D2 reality; the guard test (4) locks it. Each commit independently green.

## Verification gate (per push)
Full pytest + ruff + pyright ≤ 29; the guard test's fails-before RUN; push → CI green 3 legs → ping Vince the tip SHA → SHA-pinned merge gate.
