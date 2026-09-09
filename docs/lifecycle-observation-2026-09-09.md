# Lifecycle observation report — 2026-09-09

Only item 1 is authorized: read-only investigation and diagnostic logging.
Shutdown changes, process termination/signals, cleanup commands and kill-based
tests remain outside this authorization. No plugin deployment or restart occurred.

## Tracking

- Epic: `b2970b15de79`.
- Item 1, evidence/diagnostics: `58ee03bf2bc5` (in progress).
- Item 2, original leak task: `d2a62f38cc88` (moved under epic; shutdown work awaits approval).
- Item 3, client-alive reload investigation: `18bcd406bb61` (awaits approval).
- Item 4, full-tree validation: `f108340304b4` (awaits approval).
- Diagnostics follow-up under item 1: `75584b7fcf51` (sweep false zero).

## Evidence

Read-only query of Codex's `logs_2.sqlite` (SQLite `mode=ro`, MCP-target records
containing `[vibe-lifecycle]`) recovered 14 pipe-peer verdict records, all `ok`
with `codex.exe` as peer. These are retained records, not 14 proven unique
sessions or an exhaustive session sample. They establish neither correct
shutdown nor correctness across Claude sessions.

Existing per-PID startup logs contained generic resolution labels without
verdict/PID/image details. `_log_identity()` sent those details only to stderr.
Server stderr depends on harness retention. Sidecar stderr is explicitly
`DEVNULL`, so the supervisor identity detail was discarded. No representative
Claude identity sample was recovered in this investigation.

Read-only Windows process enumeration found seven server/sidecar trees: six
under Claude clients and one under Codex. The 14 real `python3.13.exe` workers
used about 4083.7 MiB combined working set; their 14 `python.exe` launchers used
about 145.8 MiB. This is summed working set, not unique/private memory, and
excludes uv/client memory. All seven client processes were present in the
snapshot; session liveness is unknown, so these are NOT classified as orphans.
The earlier python.exe/uv-only snapshot omitted versioned Python workers and
must not be treated as the total footprint.

Example current chain (PID snapshot, not stable process identifiers):
`codex 82832 -> uv 50396 -> python launcher 50872 -> real server 40332 ->
sidecar launcher 50272 -> real sidecar 27936`.

`get_status` reported zero servers despite its own running PID 40332.
`stale_sweep.py` filters executable names to `python.exe`/`pythonw.exe` and
interpreter paths to `\\uv\\python\\`; the observed Store Python workers fall
outside those assumptions. That count is not a reliable absence-of-leaks signal.

The workspace and installed Codex venv configuration both identify Store Python
3.13.14 and uv 0.9.27 as the venv creator. The creator version is not proof of
the currently installed uv version. The earlier uv-managed Python 3.12 topology
must not be assumed to describe this machine today.

## Local diagnostic patch

`lifecycle._log_identity()` now adds JSON identity details to the existing
in-memory breadcrumb buffer: kind, own PID, harness tag, executable, observation
wall time, and existing resolver fields. It does not perform disk I/O itself.
The existing server background flush persists these details. The sidecar now
flushes immediately after supervisor identification, before processing load
requests, since its stderr is discarded. Missing harness tags are labeled
`unspecified`, not guessed.

No resolver decisions, watch arming, process access rights, exit paths, spawn
commands or cleanup behavior were changed. This patch does not fix leaks and
is not installed in the running sessions.

## Validation and outstanding evidence

Three standalone unittest checks passed: identity survives the existing flush
without pre-yield disk writes; broken stderr preserves in-memory evidence; and
sidecar identity flush ordering is present. Tests mock filesystem writes and
do not spawn server processes or arm watches. Ruff on changed Python files and
the existing read-only containment source scan passed. Source diff whitespace
checks passed; the graph journal has generated line-ending warnings and was
not manually reformatted.

Sandbox access could not launch Store Python or query process metadata.
Narrow outside-sandbox commands were allowed by automatic approval review for
the observation-only tests, read-only SQLite query and process enumeration.
No environment repair, install, process termination or cleanup was attempted.

Field verification remains open: representative Claude and sidecar identity
records, source/version correlation, and session-disconnect evidence are still
needed. Separate approval is required before any shutdown implementation or
process-termination test. The logging patch is local and uncommitted.
