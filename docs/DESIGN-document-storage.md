# Design: First-Class Document Storage (v0.8.0)

**Status:** Design complete, peer-reviewed (2026-06-10). Operator decisions resolved same day (§8) — cleared for the seam-gate and WP-D1.
**Request (Colton):** "Store documents (client docs, etc.) as a first-class episode that would then get nodes created attached to it describing the actual document."

## 1. Shape

A document becomes a **`document` node** — a new `CognitionNodeType` with episode semantics (verbose detail allowed, hub for `part_of` links). **Default mode is REFERENCE (operator decision §8(d)): the node stores the file PATH + metadata + content sha256 — the bytes stay where they live.** The knowledge lives in the attached descriptor nodes; the document node is the durable pointer + hub. Storing one creates:

- **Reference record** (default): absolute path, filename, mime, size, sha256-at-registration. The hash makes staleness detectable (`get_document` re-hashes and reports `unchanged | modified | missing` honestly). Known limitation, documented: paths are machine-specific — a teammate pulling the journal gets the node, descriptors, and extracted text, not the file.
- **Bytes** (opt-in, `store_copy=true`) in a content-addressed blob store: `.cognition/documents/<sha256[:2]>/<sha256><ext>` — immutable, write-once, dedup and integrity-check free, add-only. For documents that must survive the original file moving or must travel via git. **No size cap** (§8(b)); the tool reports stored size, and anything over GitHub's ~100MB push limit gets a stated warning when not `local_only`.
- **Extracted text** in a sidecar: `.cognition/documents/text/<sha256>.txt` — stored in BOTH modes (it's what makes search-inside-documents work and it's small, bounded by the agent extraction ceiling) — NOT in the journal. (Journal lines must stay small: audit C-1 — lines over ~8 KiB can interleave across concurrent writers and be lost; full text in `detail` would also bloat every `get_history`/`get_neighbors` response and every replay.)
- **The node**, journaled normally: `detail` = abstract (capped ~2 KB), metadata = filename/mime/size/sha256/source, canonical reference `doc:<sha256[:12]>`.
- **Descriptor nodes** (agent-recorded entities: the decisions, constraints, facts inside the doc) that include `doc:<hash>` in their references → auto-linked `part_of` the document node by the deterministic matcher. **Requires a matcher extension** (peer review falsified the original "zero new edge machinery" claim): `create_deterministic_edges` currently links only entity↔EPISODE pairs (storage.py:541-561). New rules: entity↔document → `part_of` (document is a hub, like an episode); **document↔episode → `relates_to`** (operator decision §8(c): documents auto-link to the episodes that pertain to them — neither contains the other, so `relates_to`, not `part_of`). document↔document stays unlinked (versioning uses explicit `supersedes`).

Old plugin versions replay unknown node types safely (verified: `_replay_entry` writes raw type strings, no enum validation on replay) — graceful degradation, no compat break.

## 2. Text extraction: agent-driven, server parser-free

The **agent** extracts text (Claude reads PDFs natively in its own context) and passes it to the tool. The server never parses binary → **zero new Python dependencies** (consistent with the slim-install work and the no-background-LLM decision — descriptor generation is agent work, like curation).

**Stated limit (peer review):** the agent passes text token-by-token, so per-call practical ceiling is tens of KB. Blob bytes are uncapped (§8(b)); searchable text for big PDFs is whatever the agent extracts. Mitigations: optional multi-call append in WP-D2 if needed. Phase-2 escape hatch (deferred, NOT in scope): `vibe-cognition[documents]` optional extras with pypdf/python-docx for verbatim CLI-side extraction.

## 3. Search inside documents (WP-D2)

Sidecar text is chunked into ChromaDB as `<node_id>#chunk-N` entries (~1000-token windows, 100 overlap), chunk text stored as Chroma `documents` (today's upsert stores no text — must change), metadata carries `node_id` + `entity_type`. `cognition_search` over-queries (limit×k), dedupes to best-hit-per-node, returns the node with a `matched_excerpt`.

**Integration debts the peer review surfaced (all in WP-D2 acceptance):**
- Startup re-sync is id-based and would NEVER rebuild chunks for a teammate who pulled the journal+blobs (chromadb/ is gitignored) — extend `_sync_cognition_embeddings` to detect document nodes lacking chunks and re-chunk from the sidecar.
- `delete_cognition_node` purges only the exact node-id embedding — chunk purge via `collection.delete(where={"node_id": ...})` (this lands in WP-D1 with deletion, not D2).
- `get_status` embedding counts: report node vectors and chunk vectors separately (chunks would inflate the current count).

## 4. Lifecycle

- **Dedup:** same sha stored twice → tool returns the existing node with `already_stored: true` (node IDs are timestamped, so without this, re-storing creates an unlinkable twin). `force_new=true` overrides.
- **Versioning:** new version of a client doc = new document node + `supersedes` edge to the old one. `get_superseded_chain` already exists in queries.py to walk it (currently unexposed — natural synergy with audit T-11).
- **Deletion (ships IN WP-D1 — no store without delete):** `cognition_remove_node` on a document node also purges chunk embeddings and unlinks the blob+sidecar after a refcount scan (another node may reference the same sha). **Privacy caveat, stated in the tool docstring:** git-committed blobs survive in git history and on the remote after deletion — deleting the node does not un-publish a client document.

## 5. Files and git

- `.gitattributes`: `.cognition/documents/** -text` — **required, not cosmetic**: autocrlf would rewrite text sidecars at checkout, breaking the sha-named-content invariant (same bug class as the journal's -text fix).
- Agent-supplied file extension is sanitized/whitelisted (it composes a filesystem path — traversal/invalid-char risk on Windows).
- Default: blobs committed to git (same shareability rationale as the journal), with a per-call `local_only` opt-out that also writes a `.gitignore` entry — pending operator decision §8(a).

## 6. MCP surface (+2 tools → 17)

- `cognition_store_document(file_path | content_text, title, document_text, context, author, references?, mime?, store_copy=false, local_only?)` → reference record (default) or blob copy (`store_copy=true`) + text sidecar + node + chunks; returns `{node_id, doc_ref, mode, size, indexed_text_chars, already_stored?}`.
- `cognition_get_document(node_id | doc_ref)` → metadata + full sidecar text + blob path. (Also the graph's first get-by-id surface — audit G1 synergy.)
- Browsing folds into the existing `cognition_get_history(node_type="document")` — no third tool.

## 7. Phasing (work packages, each gated as usual)

| WP | Scope |
|----|-------|
| WP-D1 | Reference-mode + opt-in blob store, sidecar, DOCUMENT type + matcher pair rules, store/get tools, dedup, **deletion incl. chunk purge + ghost-search fix (§9 N1)**, extension sanitization, gitattributes, tests |
| WP-D2 | Chunked embeddings + search excerpts, teammate re-sync chunking, status count split, text-append if needed |
| WP-D3 | `/vibe-document` skill (store → descriptor entities → curate), README/SKILL docs |
| WP-D4 | Dashboard: document list + token-gated download |

Version: **0.8.0** (feature release).

## 8. Operator decisions — RESOLVED (Colton, 2026-06-10)

(a) **Blob git policy** (applies to opt-in copies only, see (d)): **default-commit** with per-call `local_only` opt-out + prominent "git history retains deleted blobs" warning.
(b) **Size cap: NONE.** No hard cap; the tool reports stored size in its result; GitHub ~100MB push-limit warning when a committed copy exceeds it.
(c) **Document↔episode auto-linking: YES** — documents will be relevant to many connections, especially episodes that pertain to them. Matcher gains a document↔episode → `relates_to` pair rule (see §1); descriptor entities still get `part_of`.
(d) **Storage mode default: REFERENCE, not copy** (Colton, after initial design): the node stores the file path + metadata + content hash — "avoid storing too much in the graph; the nodes for the information inside the document will be in the attached nodes anyways." Byte-copy into the blob store is opt-in (`store_copy=true`). Extracted-text sidecar is kept in both modes (it powers search-inside-documents and is small). Staleness is detected by re-hash on access, reported honestly (`unchanged | modified | missing`).

## 9. Seam-gate findings (adversarial plan review, 2026-06-10 — ledger #15 applied)

Six named seams + four found. The load-bearing results, folded into WP acceptance criteria:

- **N1 (live bug, pre-dates documents): `cognition_search` serves ghosts.** It formats hits straight from Chroma metadata with no graph-existence check, and cross-process replay of `remove_node` never un-embeds (replay touches only the graph; `_sync_cognition_embeddings` only ADDS). Every cross-process deletion ever made still surfaces in other machines' search. Documents escalate this to verbatim deleted client text. **WP-D1 fixes it generally:** (a) search drops hits whose (chunk-stripped) node_id isn't in the graph; (b) the startup sync gains a reconciliation sweep deleting orphan Chroma ids incl. `#chunk-*`; (c) local delete purges `where={"node_id"}` chunks. Test: delete on store A → replay on B → search on B returns nothing.
- **S4/N3 (matcher discipline):** doc↔episode `relates_to` fires **only on `doc:`-prefixed shared refs** (any-ref linking = vacuum via popular refs); the store tool restricts the document node's own `references` to `doc:<hash>` (agent-supplied refs go to context); the matcher skips a pair if ANY deterministic edge already exists (old plugin versions classify `document` as entity and would mint `part_of` doc→episode edges on any shared ref — mixed-version pollution; the ref restriction also starves old matchers). Consequence: **episodes link to documents by citing `doc:<hash>` in their references** — the skill/docstring guidance (WP-D3) is load-bearing, not optional.
- **S1 (opt-in copy mode only, post-(d)):** git-policy thresholds at store time, not push time — a >100MB committed blob bricks every later push of main *including journal flushes*, and the failure is post-commit (history rewrite to undo). ≥50MB → auto `local_only` + warning; ≥95MB → default-commit refused, `local_only` required. No-cap survives; only git policy flips.
- **S3 (copy mode):** dedup compares git policy: local_only→default **promotes** (de-gitignore, reported); default→local_only warns `already_committed: true`. Refcount-zero delete removes its .gitignore entry.
- **N2 (copy mode):** blob unlink is local-only — delete result lists unlinked paths + "commit the removal; other clones retain until they pull." Reference mode: deletion **never touches the referenced original file** — managed artifacts only (node, chunks, sidecar).
- **S5:** store result reports `indexed_chars` next to `blob_bytes`/`file_bytes` — partial text coverage must be visible.
- **S6 refuted:** documents dir is greenfield — `-text` lands with the dir's first existence, no cut-over needed; **no `merge=union`** (binary, content-addressed, conflicts impossible).
- **N4 (stated limit):** the startup edgeless pass skips nodes with ≥1 edge, so doc↔episode links form at record/store time, not retroactively.
- Verified for free: unknown node_type replays safely on old servers; deterministic edges are journaled as explicit add_edge entries — **the matcher extension needs zero replay-side changes**.
