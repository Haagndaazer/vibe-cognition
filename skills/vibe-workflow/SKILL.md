---
description: You MUST use this skill whenever you work out a reusable multi-step procedure worth repeating (deploy steps, onboarding, release process, debugging runbook) — record it as a first-class `workflow` node so the full how-to is fetched whole next time, not reconstructed from scattered nodes or re-worked-out from memory. You MUST also use this skill BEFORE starting any multi-step task, to check cognition_get_workflow for an existing procedure first — skipping this retrieval check means silently reinventing a workflow that already exists. A workflow is versioned by supersession, never edited in place; this skill covers recording, retrieving, and updating one.
---

# Vibe Workflow — First-Class Procedure Storage

Store a step-by-step procedure as a first-class `workflow` node. The full procedure
is stored in `detail` and chunked for semantic search, so even long how-tos are fully
retrievable by topic.

## Tools

| Tool | Purpose |
|------|---------|
| `cognition_get_workflow` | Find a procedure by name or topic; resolves to the current HEAD version |
| `cognition_record` (node_type="workflow") | Store a new workflow procedure |
| `cognition_get_superseded_chain` | See the full version history of a workflow |
| `cognition_add_edge` | Add a `supersedes` edge when updating a workflow |

## Before a multi-step task — search first

**Always search for an existing workflow before starting a multi-step task:**

```
cognition_get_workflow("topic or procedure name")
```

This returns the current HEAD version (even if an old version was matched), plus the
full version chain. If a workflow exists, follow it. If it's outdated, update it (see below).

`cognition_get_workflow` does a semantic search under the hood, so it needs the
embedding model. If it returns `{"error": "...", "status": "loading_embeddings"}` (or
`"embedding_status": "loading"`/`"syncing"` from `get_status`), the model is still
loading/catching up — wait a few seconds and retry rather than concluding no workflow
exists.

## Storing a new workflow

```
cognition_record(
    node_type="workflow",
    summary="Brief title of the procedure",
    detail="Step 1: ...\nStep 2: ...\n...",  # FULL procedure, verbose
    context="relevant,topics,file paths",
    author="<git user name>",
)
```

- **summary**: Brief title ("deploy to production", "run the release process").
- **detail**: The FULL procedure — every step, every gotcha, every command. Verbose is correct.
- No 250-char cap on `detail` for workflows (unlike entities).

## Updating a workflow (versioning by supersession)

**Never edit a workflow in place** — `cognition_update_node` is blocked on `workflow` nodes.
Instead, record a new workflow carrying the FULL revised procedure, then link it:

```
# 1. Record the updated version
cognition_record(node_type="workflow", summary="same or new title", detail="FULL updated procedure...", ...)

# 2. Link it to the old version
cognition_add_edge(from_id="<new_id>", to_id="<old_id>", edge_type="supersedes")
```

The HEAD is always the node with no incoming `supersedes` edge. `cognition_get_workflow`
resolves to the HEAD automatically, regardless of which version was matched.

## When to create a workflow node

- You codify a deployment, release, onboarding, or debugging procedure.
- A multi-step task has a "right way" that should be followed consistently.
- A runbook exists in your head or in chat — write it down as a workflow so it's retrievable.

## After recording

Run `/vibe-curate` to add semantic edges linking the workflow to related nodes.
