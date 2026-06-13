# WP-D1 Execution Plan — first-class document storage (v0.8.0 foundation)

Brief = `docs/DESIGN-document-storage.md` §1–§9 (§9 = acceptance criteria verbatim; §8 = operator decisions). Reference-mode-first (§8(d)).

## SPLIT — seam ADJUSTED per peer review (each PR creates nothing it cannot delete)
The first cut put artifact *creation* (copy blobs, sidecars, `.gitignore` entries) in D1a but their *deletion* in D1b — leaving `main` in a "store without delete" state (orphan blobs/sidecars) for the inter-PR window, contra §4. **Adjusted seam: D1a creates only the sidecar, and deletes it.** Copy-mode blobs move to D1b, paired with blob deletion.

### WP-D1a — reference mode + sidecar + tools + sidecar deletion (built first; self-contained)
- `CognitionNodeType.DOCUMENT = "document"` (models.py).
- `cognition/documents.py` (stdlib-only: hashlib/pathlib/shutil): sha256; sidecar path `.cognition/documents/text/<sha>.txt`; extension sanitization = **whitelist-or-DROP** (ext kept only if it matches `^\.[A-Za-z0-9]{1,10}$`, else dropped — never fail the store on a hostile/odd MIME; the `<sha>` part is server-generated so only the agent ext is untrusted). (Blob-path + size-policy helpers DEFERRED to D1b with copy mode.)
- `cognition_store_document(file_path | content_text, title, document_text, context, author, references?, mime?, force_new=false)` — **reference mode only in D1a** (`store_copy` lands in D1b):
  - sha256 of file bytes (or content_text). **Dedup O(1)** via `_reference_index["doc:<sha[:12]>"]`, then confirm the FULL sha from node metadata before returning existing + `already_stored:true` (12-char ref alone is not proof); `force_new` overrides. Dedup returns the existing node as-is (does NOT merge new context — state it).
  - node stores path(absolute)+filename+mime+size+sha256+mode="reference"; the file stays in place.
  - text sidecar (`document_text`, agent-extracted) written to the sidecar path (both modes; in D1a only reference exists).
  - node: `type=document`, `detail`=abstract capped ~2 KB, metadata, **`references=["doc:<sha[:12]>"]` ONLY** (agent refs → `context`, §9 S4 — starves old matchers + prevents ref-vacuum). **Calls `create_deterministic_edges(node_id)` exactly like `_record_node` (cognition_tools.py:84).**
  - result: `{node_id, doc_ref, mode, size, indexed_text_chars, already_stored?}` (§9 S5).
- `cognition_get_document(node_id | doc_ref)`: resolve `doc_ref` via `_reference_index`, `node_id` via `storage.get_node`. Returns metadata + full sidecar text + path; **re-hash staleness** → `unchanged | modified | missing`. The re-hash reads the full referenced file (note cost); a missing/unreadable path returns `missing`, never raises.
- `.gitattributes`: `.cognition/documents/** -text` (NO `merge=union`; §9 S6 — greenfield dir verified, no cut-over hazard).
- **Graph-inert guard (CRITICAL placement — C1):** the wrong edge fires from OTHER nodes' record calls (an episode citing `doc:<hash>` → existing matcher treats the document as an entity → mints `part_of`). So the guard is **pair-level INSIDE `create_deterministic_edges`** (after `other_type` is read, ~storage.py:567): `if "document" in (node_type, other_type): continue`. NOT a top-of-function guard on the current node. D1b replaces it with the pair rules.
- **Sidecar deletion in D1a:** extend `delete_cognition_node` so deleting a document node unlinks its sidecar (the only managed artifact D1a creates — §9 N2 lists the sidecar as deletion-managed; reference mode never touches the original file). Keeps §4 honest within D1a.
- Tests (rule 20 — assertions name their failure mode; fix+proof same commit): store reference + retrieve; sidecar written + returned; dedup returns existing (full-sha confirmed, no twin) and `force_new` overrides; whitelist-or-drop (traversal/`..`/separators/reserved → dropped, store still succeeds); staleness unchanged/modified/missing (incl. missing path doesn't raise); result reports mode/size/indexed_chars; **graph-inert: record a document THEN an episode citing its `doc:` ref → assert ZERO deterministic edges** (proves the pair-level guard); sidecar removed on delete.

### WP-D1b — copy mode + graph integration (proposed second PR; detailed plan + peer review before build)
- **Copy mode** (`store_copy`): content-addressed blob `.cognition/documents/<sha[:2]>/<sha><ext>` (write-once) + size policy (≥50MB→auto `local_only`+warn; ≥95MB→refuse default-commit, §9 S1) + `.gitignore` write for `local_only`. Paired in the SAME PR with: refcounted blob unlink on delete + `.gitignore` removal on refcount-zero (§9 S3) + unlink reporting + git-history privacy caveat in the docstring (§9 N2).
- **Matcher pair rules** (replace D1a's guard) — full 6-pair truth table with directions: entity↔episode→`part_of` (entity→episode, existing, any ref); entity↔document→`part_of` (entity→document, `doc:` ref only); document↔episode→`relates_to` (direction: document→episode, `doc:` ref only); document↔document skip; episode↔episode skip; entity↔entity skip. **Skip the pair if any existing edge has `source=="deterministic"`** (NOT type-based — the current PART_OF check at storage.py:586 changes to a source check; a manual/curate edge must NOT block). `doc:` keys pass through `_normalize_refs` as exact (commit-SHA prefix behavior untouched — rule 21 / `99b8a9f50164`).
- **chunk purge** on delete: `collection.delete(where={"node_id": id})` — forward-compatible no-op until D2 writes `node_id` metadata on chunks (today Chroma carries no `node_id` field — C3); lands now per §9 N1c.
- **N1 ghost-search fix** (own commit, rule 20/12): `cognition_search` drops hits whose graph node is absent — strip the Chroma id STRING (`_id.split("#chunk-")[0]`) and check `storage.has_node`; startup `_sync_cognition_embeddings` gains an orphan-reconciliation sweep deleting Chroma ids absent from the graph. **Test harness: two CognitionStorage + INDEPENDENT embedding stores over ONE journal** (chromadb is gitignored) — delete on A → replay on B → search B empty. Must FAIL against current main (run it).
- **Composition review (rule 11):** matcher × dedup × deletion. **§9 S4 vacuum test (rule 12):** document + episode sharing ONLY `issue:X` (no `doc:` ref) → NO edge; must FAIL against an any-ref impl.

## Binding rules
- 20: assertions name their failure mode; fix+proof same commit (esp. N1).
- 21: re-searched the graph — `99b8a9f50164` binds `_normalize_refs`/matcher; don't break commit-SHA prefix normalization. (Done; documented above.)
- 11: D1b composition review. 12: N1 + S4 vacuum fails-before RUN.

## Baselines / mechanics
- Re-measure pytest/ruff/pyright at branch point; pyright ratchet at 31 — if new code drops it, lower `.github/pyright-baseline.txt` in the same PR; if new pyright errors appear, fix them (don't raise the baseline).
- No new Python deps (agent extracts text — §2). Journal off-branch; branch-switch via Vince. CHANGELOG [Unreleased] as I go.
- This is a FEATURE (0.8.0) — "no store without delete" (§4) holds at RELEASE: D1a (store) + D1b (delete) both land before 0.8.0 ships.

## Sequence
1. Vince: approve the split + branch (fix/wp-d1a-document-store off main @ 893e80e) via the switch protocol.
2. Build D1a → PR → review → merge.
3. Plan D1b (detailed) + peer review → build → PR.
