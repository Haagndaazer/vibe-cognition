---
description: You MUST use this skill whenever you store a document (a client doc, PDF, spec, contract, report, transcript) as project memory, or retrieve/attach one. Storing a document is only half the job — the knowledge inside it connects to the graph ONLY when you record its facts as descriptor nodes that cite the document's returned doc_ref in THEIR references. This skill makes that workflow the default; skipping it leaves an inert, disconnected document. Curation is YOUR job to TRIGGER — after recording descriptor nodes you MUST run /vibe-curate (launches the background curator); never author semantic edges yourself.
---

# Vibe Document — First-Class Document Storage

Store an actual document as a first-class `document` node, then capture what's INSIDE it as ordinary cognition entities that auto-link to it. The document node is a durable **hub + pointer**; the knowledge lives in the attached descriptor nodes.

## Tools

| Tool | Purpose |
|------|---------|
| `cognition_store_document` | Store a document (reference or copy mode) + its extracted text; returns a `doc_ref` |
| `cognition_get_document` | Retrieve a stored document: metadata + full text + freshness |

Documents are also found by `cognition_search` (search-inside, returns a `matched_excerpt`) and browsed via `cognition_get_history(node_type="document")`.

## The workflow (do all three — this is the point of the skill)

### 1. Store the document
YOU extract the text (Claude reads PDFs/docs natively — the server never parses binaries). Then:

```
cognition_store_document(
  file_path="/abs/path/to/client-spec.pdf",   # OR content_text="...inline text..."
  title="Acme client spec v2",
  document_text="<the full text you extracted>",
  context="acme, contract, legal",
  author="<git user name>",
)
→ returns { "doc_ref": "doc:1a2b3c4d5e6f", "node_id": "...", "mode": "reference", ... }
```

Default is **reference mode**: the node stores the file PATH + metadata + a content sha256; the bytes stay where they live. Pass `store_copy=true` to also copy the bytes into a content-addressed blob (survives the original moving / travels via git); add `local_only=true` to keep that copy out of git. **Privacy:** a committed blob survives in git history and on the remote even after you delete the node — deleting does NOT un-publish it.

### 2. Record the document's CONTENTS as descriptor nodes — citing the `doc_ref` in `references`
This is the load-bearing step. A document is **inert** until descriptor nodes cite its `doc_ref`. The deterministic matcher links an entity `part_of` the document (and an episode `relates_to` it) ONLY when the node carries the `doc:<hash>` key in its **`references`** field.

For each decision / constraint / discovery / fact the document contains, record an entity with `cognition_record` and put the returned `doc_ref` in `references`:

```
cognition_record(
  node_type="constraint",
  summary="Acme requires data residency in EU-West",
  detail="Section 4.2 of the spec: all PII stored and processed in EU-West only.",
  context="acme, data residency, compliance",
  author="<git user name>",
  references="doc:1a2b3c4d5e6f"        # ← the doc_ref from step 1, in references
)
```

#### WRONG vs RIGHT — the single mistake that breaks everything
The `doc_ref` MUST go in `references`, NOT in `context`. This is the difference between a linked document and an inert one:

- ❌ WRONG: `context="acme, doc:1a2b3c4d5e6f"`, `references="issue:ACME-1"`
  → the matcher never sees the `doc:` key in references → the entity does NOT link to the document. The document sits disconnected forever.
- ✅ RIGHT: `context="acme, compliance"`, `references="doc:1a2b3c4d5e6f"`
  → entity auto-links `part_of` the document, instantly, no LLM.

(You may include other refs alongside it: `references="doc:1a2b3c4d5e6f, issue:ACME-1"`.)

### 3. Curate — MANDATORY
After recording the descriptor nodes, run the `/vibe-curate` skill to launch the background curator, which adds the semantic edges (led_to, resolved_by, supersedes, contradicts, relates_to). Same hard rule as `/vibe-cognition`: recording without curating leaves the new entities semantically disconnected. Don't wait to be asked, and never author these edges yourself.

## Retrieving a document

```
cognition_get_document(node_id="...")          # or doc_ref_arg="doc:1a2b3c4d5e6f"
→ { metadata, text, path, freshness }
```

`freshness` re-hashes the referenced file and reports `unchanged | modified | missing` honestly (reference mode; a missing/moved file never raises). The full extracted text comes from the sidecar, so it's available even to a teammate who pulled the journal but not the original file.

## Deletion

`cognition_remove_node(<document node id>)` reclaims the server-MANAGED artifacts (text sidecar, copied blob if any, search chunks) — but **never** the referenced original file. Privacy caveat above still applies to already-committed blobs.

## Worked example (end to end)

```
# 1. store
r = cognition_store_document(
      file_path="/work/acme/spec-v2.pdf", title="Acme spec v2",
      document_text="<extracted text>", context="acme, contract", author="Colton Dyck")
# r["doc_ref"] == "doc:1a2b3c4d5e6f"

# 2. record its facts, each citing the doc_ref in references
cognition_record(node_type="decision",
  summary="Acme onboarding uses SSO via their Okta tenant",
  detail="Spec §3.1 mandates SAML SSO; no local accounts.",
  context="acme, auth, sso", author="Colton Dyck",
  references="doc:1a2b3c4d5e6f")

cognition_record(node_type="constraint",
  summary="Acme requires EU-West data residency for all PII",
  detail="Spec §4.2.", context="acme, compliance", author="Colton Dyck",
  references="doc:1a2b3c4d5e6f")

# 3. curate
/vibe-curate
```

Result: a `document` hub node with two descriptor entities linked `part_of` it — searchable, navigable, and connected to the rest of the graph.
