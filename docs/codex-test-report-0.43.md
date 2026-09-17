# Codex plugin test report — vibe-cognition 0.43.0

Date: 2026-09-17
Tester: Vince-Codex
Session: `01a0b163-08ba-76b3-914e-620ac25aebfa`
Test directory: `E:\E Drive Projects\vibe-cognition`
Status: A–C PASS; D functionality PASS with analyzer routing containment FAIL; E module checks PASS via Vince, real session-start UNVERIFIABLE; F PASS.

## Scope and setup

Vince's revised A–F brief supersedes the original scratch-project plan. Upgrade/install skipped because Colton reported the plugin already latest. The scratch repository at `C:\Users\colto\AppData\Local\Temp\codex043\proj` was initialized but abandoned without test nodes. No further Codex CLI commands were run after Vince prohibited them.

Earlier CLI environment error, exit 1:

```text
WARNING: proceeding, even though we could not create PATH aliases: Could not find home directory
Error: failed to resolve CODEX_HOME

Caused by:
    Could not find home directory
```

Initially this session exposed no cognition tools. After the user reloaded MCP tools, they became callable and testing resumed. That initial absence was expected startup behavior, not a plugin failure. Workflow lookup was waived by Vince in favor of his explicit brief.

All four created nodes use the exact prefix `CODEX LIVE TEST 0.43.0 — `. No other nodes, plugin edits, deletions or commits are authorized. Curation is restricted to these fixtures, edges between them, and no new cluster summary nodes. This limits the run's graph-wide curation coverage.

## Phase A — PASS

Actual server status:

```json
{
  "running_code": {
    "version": "0.43.0",
    "path": "C:\\Users\\colto\\.codex\\plugins\\cache\\coltondyck\\vibe-cognition\\0.43.0",
    "installed_version": "0.43.0",
    "matches": true
  },
  "harness": {
    "name": "codex",
    "display_name": "Codex",
    "skill_prefix": "$",
    "spawn_tool": "`spawn_agent` tool",
    "curation_available": true,
    "manifest_path": ".codex-plugin/plugin.json",
    "containment": "curation-session token only (Codex roles cannot restrict tools)",
    "models": {
      "small": {
        "model": "gpt-5.6-luna",
        "source": "default"
      },
      "mid": {
        "model": "gpt-5.6-sol",
        "source": "default"
      }
    }
  },
  "journal": {
    "legacy_journal_bytes": 2134069,
    "shards": [
      {
        "file": "colton.dyck%40acryliccode.com.jsonl",
        "bytes": 38532
      }
    ],
    "writing_to": "journal/colton.dyck%40acryliccode.com.jsonl",
    "adopted_at": "2026-09-15T00:25:14.989320+00:00",
    "stragglers": null,
    "unresolved_entries": 0,
    "id_collisions": 0,
    "glued_lines": 0
  }
}
```

The confirmed identity was already Colton Dyck; no identity mutation was needed. Baseline graph: 767 nodes, 0 uncurated; edges_outside_curation=85 was pre-existing. Initial embedding status: `waiting-for-load-lock`.

## Phase B — PASS

Decision recorded through MCP:

```json
{
  "id": "0fbcf2d42e78",
  "type": "decision",
  "summary": "CODEX LIVE TEST 0.43.0 — Verify per-person journal writes through MCP",
  "timestamp": "2026-09-17T22:33:32.614877+00:00"
}
```

Post-write journal status:

```json
{
  "legacy_journal_bytes": 2134069,
  "shards": [
    {
      "file": "colton.dyck%40acryliccode.com.jsonl",
      "bytes": 39186
    }
  ],
  "writing_to": "journal/colton.dyck%40acryliccode.com.jsonl",
  "adopted_at": "2026-09-15T00:25:14.989320+00:00",
  "stragglers": null,
  "unresolved_entries": 0,
  "id_collisions": 0,
  "glued_lines": 0
}
```

The expected shard is listed and `writing_to` names it. Shard size grew from 38532 to 39186 bytes; legacy size stayed 2134069. No journal was edited directly.

## Phase C — PASS

Default and explicit scopes, with full readback:

```json
{
  "id": "3d3a1db97412",
  "type": "constraint",
  "summary": "CODEX LIVE TEST 0.43.0 — I always want to be asked before committing",
  "detail": "Temporary live verification fixture for constraint scope; remove after testing.",
  "context": [
    "docs/codex-test-report-0.43.md",
    "codex-live-test"
  ],
  "references": [],
  "severity": null,
  "timestamp": "2026-09-17T22:33:48.088349+00:00",
  "author": "Colton Dyck",
  "metadata": {
    "recorded_by": {
      "name": "Colton Dyck",
      "email": "colton.dyck@acryliccode.com",
      "source": "confirmed",
      "confirmed": true
    },
    "from_agent": true,
    "scope": "personal"
  }
}
```

```json
{
  "id": "0868256d1400",
  "type": "constraint",
  "summary": "CODEX LIVE TEST 0.43.0 — The build fails if the Android SDK is older than 34",
  "detail": "Temporary live verification fixture for constraint scope; remove after testing.",
  "context": [
    "docs/codex-test-report-0.43.md",
    "codex-live-test"
  ],
  "references": [],
  "severity": null,
  "timestamp": "2026-09-17T22:33:51.224386+00:00",
  "author": "Colton Dyck",
  "metadata": {
    "recorded_by": {
      "name": "Colton Dyck",
      "email": "colton.dyck@acryliccode.com",
      "source": "confirmed",
      "confirmed": true
    },
    "from_agent": true,
    "scope": "project"
  }
}
```

Decision with `scope="personal"` was rejected; response returned no node ID:

```json
{
  "error": "scope applies only to constraint nodes; omit it for other types"
}
```

## Phase D — functional PASS; routing containment FAIL

Project-wide Roads fixture was deliberately recorded without scope:

```json
{
  "id": "395832a0ea24",
  "type": "constraint",
  "summary": "CODEX LIVE TEST 0.43.0 — The Google Roads API speedLimits endpoint returns 403 without an Asset Tracking licence, so speed-limit calls fail for every developer on this project",
  "timestamp": "2026-09-17T22:33:54.658590+00:00",
  "scope": "personal"
}
```

No-token containment call **PASS**, exact response:

```json
{
  "error": "curation token required: edge writes and curation marks belong to the curate-orchestrator. To get edges created, run $vibe-curate, which launches the curate-orchestrator; it calls cognition_begin_curation and passes curation_token on every write."
}
```

Launched `/root/vibe_curate` with `fork_turns="none"` and explicit `model="gpt-5.6-sol"`, instructed to read the installed orchestrator reference and process only the four test nodes. Four uncurated nodes at launch. Analyzer models, scope flag and ruling verified below.

### Orchestrator final report (verbatim)

> Curation complete. 4 uncurated nodes processed → 0 remain, 0 edges created, 0 proposals discarded, conflict pass: 0 proposed / 0 committed / 0 discarded, scope review: 3 examined / 1 flagged (flagged for your ruling: 395832a0ea24 -> project), 1 cluster found → 0 summary nodes created.

### Model pins and spawn handling — PASS with recovery limitation

Orchestrator operational telemetry confirmed explicit `fork_turns="none"` and these model overrides: edge, conflict and cluster analyzers `gpt-5.6-luna` (small); scope analyzer `gpt-5.6-sol` (mid). Scope's initial spawn was refused with exactly:

```text
agent thread limit reached
```

After an analyzer completed, scope spawn succeeded on one retry. Curation finished normally and performed scope review; it did not skip that pass. A permanently unavailable spawn or actual depth-limit clean-skip path is **UNVERIFIABLE / not exercised**. The installed reference explicitly names `Agent depth limit reached` but not the observed thread-limit text; Vince identified that as a Codex instruction gap to fix separately.

### Flag readback — PASS

```json
{
  "id": "395832a0ea24",
  "type": "constraint",
  "summary": "CODEX LIVE TEST 0.43.0 — The Google Roads API speedLimits endpoint returns 403 without an Asset Tracking licence, so speed-limit calls fail for every developer on this project",
  "detail": "Temporary live verification fixture for constraint scope; remove after testing.",
  "context": [
    "docs/codex-test-report-0.43.md",
    "codex-live-test"
  ],
  "references": [],
  "severity": null,
  "timestamp": "2026-09-17T22:33:54.658590+00:00",
  "author": "Colton Dyck",
  "metadata": {
    "recorded_by": {
      "name": "Colton Dyck",
      "email": "colton.dyck@acryliccode.com",
      "source": "confirmed",
      "confirmed": true
    },
    "from_agent": true,
    "scope": "personal"
  },
  "curated_by_skill_at": "2026-09-17T22:38:03.706389+00:00",
  "scope_review": {
    "suggested": "project",
    "reason": "This is a shared API/licensing constraint that can cause speed-limit calls to fail for any teammate working on the project.",
    "flagged_at": "2026-09-17T22:38:48.971566+00:00",
    "curation_session": "cur-2acaf50b2cd8"
  }
}
```

### Owner ruling — PASS

After Vince captured prime with the live flag, `cognition_update_node(node_id="395832a0ea24", scope="project")` returned scope project and scope_review null together:

```json
{
  "id": "395832a0ea24",
  "type": "constraint",
  "summary": "CODEX LIVE TEST 0.43.0 — The Google Roads API speedLimits endpoint returns 403 without an Asset Tracking licence, so speed-limit calls fail for every developer on this project",
  "detail": "Temporary live verification fixture for constraint scope; remove after testing.",
  "context": [
    "docs/codex-test-report-0.43.md",
    "codex-live-test"
  ],
  "references": [],
  "severity": null,
  "timestamp": "2026-09-17T22:33:54.658590+00:00",
  "author": "Colton Dyck",
  "metadata": {
    "recorded_by": {
      "name": "Colton Dyck",
      "email": "colton.dyck@acryliccode.com",
      "source": "confirmed",
      "confirmed": true
    },
    "from_agent": true,
    "scope": "project"
  },
  "curated_by_skill_at": "2026-09-17T22:38:03.706389+00:00",
  "scope_review": null,
  "reembed": "done"
}
```

### Analyzer result routing — FAIL

The conflict analyzer sent semantic proposal content outside the curation task through teammate-comms, despite parent instructions forbidding comms and requiring normal task results. Message received by Vince-Codex, verbatim:

> Conflict batch result (exact IDs 0fbcf2d42e78, 3d3a1db97412, 0868256d1400, 395832a0ea24): all four read in full; exhaustive get_history for codex-live-test returns exactly these four; all have no neighbors/edges. Subjects are distinct (MCP journal-write decision; ask-before-commit personal constraint; Android SDK>=34 build project constraint; Google Roads speedLimits licence personal constraint). No incompatible current stances or same-subject evolution. Proposed edges JSON: [] (source curate-conflict). Parent vibe_curate was not registered, so sent via queued teammate channel where possible.

The orchestrator confirmed it ALSO received and used the proper collaboration FINAL_ANSWER with JSON. This was an extra-channel containment leak, not a silently empty pass.

The orchestrator relayed the analyzer's self-audit: it explicitly invoked teammate_register twice (manager `/root/vibe_curate` failed; manager `vibe_curate` succeeded), then sent to `/root/vibe_curate` (failed), `vibe_curate` (queued), `Vince` (live), and `Vince-Codex` (queued). Therefore the evidence attributes registration to explicit analyzer tool calls, not automatic registration from Codex task_name. This report does not infer wider task_name behavior from this one run.

## Phase E — module checks PASS via Vince; actual Codex session-start UNVERIFIABLE

Attempted before ruling, using the installed venv Python with `-m vibe_cognition.cognition.prime`. Set `REPO_PATH` to the real repository, `PYTHONPATH` to installed plugin `src`, `VIBE_HARNESS=codex`, and installed plugin root/data paths. Exit 1, exact output:

```text
Unable to create process using '"C:\Users\colto\.codex\plugins\data\vibe-cognition-coltondyck\.venv\Scripts\python.exe" -m vibe_cognition.cognition.prime': The file cannot be accessed by the system.
```

No escalation or alternate interpreter attempted. No prime output was produced by this session's command. Vince supplied independent same-install module evidence below.

During locating the module, a read-only search initially assumed `src/vibe_cognition/prime.py` and returned `The system cannot find the file specified. (os error 2)`. File discovery then located the actual module at `src/vibe_cognition/cognition/prime.py`; the execution above used that correct module.

### Peer-run prime evidence — PASS

Vince ran the SAME Codex-installed plugin venv/module from his Claude-side shell, with `VIBE_HARNESS=codex` and `REPO_PATH` set to this repository. This verifies module output, not a real Codex session-start injection.

Before ruling, Vince supplied these raw sections, described as verbatim apart from console mojibake on em dashes:

```text
## Your Personal Constraints
- [constraint] CODEX LIVE TEST 0.43.0 - I always want to be asked before committing
- [constraint] CODEX LIVE TEST 0.43.0 - The Google Roads API speedLimits endpoint returns 403 without an Asset Tracking...

## Constraints to Review
Curation thinks these may have the wrong scope. Do not stop work for them: at a natural pause, ask the human for each, then call cognition_update_node(node_id, scope=...) with their answer (passing the current scope keeps it and clears the flag).
- 395832a0ea24 [personal -> project] CODEX LIVE TEST 0.43.0 - The Google Roads API speedLimits endpoint returns 403 without an Asset Tracking... - This is a shared API/licensing constraint that can cause speed-limit calls to fail for any teammate working...
```

After ruling, Vince reported:
- `## Constraints to Review` disappeared.
- `## Your Personal Constraints` listed only the ask-before-committing fixture.
- Roads no longer appeared personal. It was not in the visible Active Constraints slice because the five high-severity production constraints fill the configured five-item cap; the direct MCP scope readback above establishes project scope.

Actual fresh Codex session-start injection remains **UNVERIFIABLE**: neither session could launch a fresh Codex session. No raw after-ruling output was supplied; that check is explicitly peer-reported.

## Phase F — PASS

Report completed and phase verdicts, exact errors, orchestrator final text and all four cleanup IDs sent to Vince. No extra summary nodes, plugin edits, direct journal edits, commits, or parent deletions occurred.

Final pre-cleanup MCP status: 771 nodes, 1639 edges, uncurated=0. Compared with baseline: exactly four added nodes, zero added edges, and edges_outside_curation unchanged at 85. Legacy journal size stayed 2134069 bytes; personal shard grew to 42562 bytes.

Overall: requested 0.41/0.42/0.43 functional behaviors passed. Codex analyzer result routing failed containment. Thread-limit recovery worked in this run; permanent/depth-limit skip and true fresh-session injection were not tested.

Cleanup IDs (all nodes created by this test):

| ID | Fixture |
| --- | --- |
| 0fbcf2d42e78 | Decision |
| 3d3a1db97412 | Personal commit preference |
| 0868256d1400 | Project Android constraint |
| 395832a0ea24 | Roads constraint, initially personal |

No other nodes created by the parent. Vince owns cleanup; none deleted here.


### Direct analyzer audit (received through collaboration)

The analyzer's follow-up FINAL_ANSWER gave these exact registration calls:

```json
{"agent":"conflict_batch_1","manager":"/root/vibe_curate","role":"curation conflict analysis","status":"proposal-only pass complete"}
```

Reported result: `teammate_register failed: Invalid agent name '/root/vibe_curate'.`

```json
{"agent":"conflict_batch_1","manager":"vibe_curate","role":"curation conflict analysis","status":"proposal-only pass complete"}
```

Reported result excerpt: `Registered as 'conflict_batch_1'... You have 0 unread message(s)...`

It explicitly stated: `I was not registered automatically.` Recipients were `/root/vibe_curate` (send failed before registration), `vibe_curate` (queued), `Vince` (live), and `Vince-Codex` (queued). This direct audit corroborates the orchestrator's routing report; result excerpts with ellipses are the analyzer's excerpts, not full raw registration responses.

## Cleanup and closeout

Vince confirmed through teammate-comms that he deleted all four test nodes after the pre-cleanup status capture. Cleanup is peer-reported; this session performed no deletions. He accepted the final verdicts and instructed no further graph writes, code changes, or commits. Direct analyzer audit was received and included above. Testing is complete; the two unexercised paths remain explicitly unverified.
