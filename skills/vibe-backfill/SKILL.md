---
description: Find git commits without corresponding cognition episodes and create nodes for them. Run this to backfill the cognition graph with commit history.
---

# Vibe Backfill — Create Cognition Nodes from Git Commits

## What This Does

Finds recent commits on the main branch that don't have corresponding episode nodes in the cognition graph, then creates episodes and entity nodes for them.

What you extract is ultimately for YOUR benefit as an agent — store what would be most helpful to you in future sessions when you need to retrieve context about the project.

There is also a standalone `vibe-cognition-backfill` CLI (a plain report, no
LLM extraction) for quick/scripted checks. It is NOT a substitute for this
skill for a graph that hasn't been touched in a while: this skill finds the
WATERMARK (the newest tracked episode's commit reference) and walks forward
from there, however old, while the CLI only ever checks a fixed 30-day
window — an untracked commit older than that is invisible to the CLI but
would still be found by this skill.

## Concurrency Rules

- **Maximum 5 subagents running at a time.** Each subagent handles a chunk of ~5-10 commits.
- When a subagent completes, you may launch the next one (rolling pool of 5).
- Do NOT spawn more than 5 subagents simultaneously.
- **ALWAYS spawn these subagents with the Haiku model** (`model: "haiku"` on every Agent call). Do NOT let them inherit the main instance's model — backfill is mechanical per-commit extraction and running it on Opus/Sonnet is extremely wasteful.

## Steps

### Step 1: Find the last backfilled commit

Fetch the most recent episode nodes via `cognition_get_history(node_type="episode", limit=5)`. Look at their `references` fields for `commit:<hash>` entries. The newest commit hash among those is the **watermark** — the last commit already in the graph.

Then get all commits after it:

```bash
git log main --format="%H %s" --reverse <watermark_hash>..HEAD
```

If no episodes exist yet (fresh graph), fall back to recent history:

```bash
git log main --format="%H %s" --reverse --since="30 days ago"
```

### Step 1b: Filter out noise commits

Remove commits that don't warrant episodes:
- Cognition journal updates (`.cognition/` only changes)
- Empty merges with no file changes
- Commits that only touch generated or metadata files with no meaningful code/content changes

For borderline commits, check `git diff --stat <hash>~1 <hash>` — if the only changes are in `.cognition/`, skip it.

### Step 2: Chunk the commits

Split the untracked commits into chunks of ~5-10 commits each.

### Step 3: Launch subagents (max 5 concurrent)

Spawn up to 5 subagents in parallel, each given one chunk — **each spawned with `model: "haiku"`** (never inheriting the main instance's model). When a subagent finishes, launch the next one until all chunks are processed.

### Step 4: Per-commit workflow (inside each subagent)

For each commit in the chunk:

**4a. Get the full file list (stat):**

```bash
git diff --stat <hash>~1 <hash>
```

This shows ALL changed files including binary assets.

For the **initial commit** (no parent), use:

```bash
git diff --stat --root <hash>
```

**4b. Get the code diff (skip large binaries):**

```bash
git diff <hash>~1 <hash> -- . ':!*.png' ':!*.jpg' ':!*.jpeg' ':!*.gif' ':!*.bmp' ':!*.ico' ':!*.svg' ':!*.woff' ':!*.woff2' ':!*.ttf' ':!*.eot' ':!*.mp3' ':!*.mp4' ':!*.wav' ':!*.ogg' ':!*.zip' ':!*.tar' ':!*.gz' ':!*.pdf'
```

For the **initial commit**, use `git diff --root <hash> -- .` with the same exclusions.

Adjust the exclusion list for your project. For example, Unity projects should also exclude `.fbx`, `.obj`, `.blend`, `.mesh`, `.asset`, `.lighting` and similar binary formats.

**4c. Analyze the diff and commit message** to understand what the commit accomplished.

**4d. Record cognition nodes:**

**Create an EPISODE for the commit:**
```
cognition_record(
  node_type: "episode",
  summary: "<commit message, max 250 chars>",
  detail: "<full commit message + summary of what the commit accomplished>",
  context: "<changed files, comma-separated>",
  author: "<commit author>",
  references: "commit:<full hash>"
)
```

**Then, extract ENTITY nodes** for any decisions, discoveries, constraints, or patterns visible in the commit (0-10 per commit):
```
cognition_record(
  node_type: "decision" | "discovery" | "constraint" | "pattern" | ...,
  summary: "<concise fact, max 250 chars>",
  detail: "<1-3 sentence rationale>",
  context: "<relevant files from the commit>",
  author: "<commit author>",
  references: "commit:<full hash>"
)
```

Use the same `references` value (commit hash) for both the episode and its entities — deterministic matching will automatically create `part_of` edges linking them.

### Step 5: Report completion

After all subagents finish, report:
- How many commits were processed
- How many cognition nodes were created (episodes + entities)
- Any commits that were skipped and why

## Guidelines

- Not every commit needs entity nodes — simple refactors or typo fixes may only need the episode
- Look at the diff content and commit message to determine what entities to extract
- Keep entity summaries under 250 chars — concise, scannable facts
- Binary assets should still be mentioned in episode details even though their diffs are skipped
- After backfill completes, you MUST run `/vibe-curate` to create semantic edges between the new nodes — backfill creates many episodes carrying only their deterministic `part_of` edges, and high-volume new nodes is the curate skill's first-named trigger. Do not skip it.
