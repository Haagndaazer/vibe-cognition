# Investigation + Hardening Plan — MCP first-connect reliability (task ced8f0401bd2)

**Owner (implementer):** Vorpid · **Gate:** Vince · **Priority:** high
**Status:** APPROVED — ship autonomously (Colton 2026-07-04). Peer-reviewed (Sonnet, conditional-go, corrections folded).

## MANDATE (Colton 2026-07-04, verbatim intent)
"Do everything you can to make this more stable to launch. Ship it autonomously. Ensure current
use ability remains as-is. Concurrent agents still need to be able to use it at the same time
without any issues or collisions — this is a PRIMARY FEATURE of the plugin. We can revert or fix
after shipping+testing. I won't upgrade other currently running agents until you confirm the latest
works as intended."

Implications for this work:
- **PRIMARY HARD GATE #1 — concurrency safety.** Multiple agents each run their OWN MCP server
  against the SAME shared venv (`${CLAUDE_PLUGIN_DATA}/.venv`), the SAME shared ChromaDB, and the
  SAME shared journal. NOTHING here may introduce a collision, lock, or corruption across concurrent
  server processes. This must be demonstrated with a cross-process concurrency test, not argued.
- **PRIMARY HARD GATE #2 — zero regression.** Existing capability stays exactly as-is; full suite
  stays green; existing concurrent behavior is preserved. A stability change that breaks a working
  case is a failure even if it "fixes" launch.
- **Autonomous ship:** WP-A and WP-B ship TOGETHER this release (Colton's controlled rollout — he
  holds his running fleet on the prior version until confirmation — replaces the human-reproduction
  gate that would otherwise hold WP-B). Vince still hard-gates the diff (concurrency + regression)
  before release; "autonomous" means no per-step human approval, NOT no gate.
- Every change below is chosen to HELP concurrency, not just launch: `--no-sync` removes per-spawn
  venv lock/sync contention (fewer collisions when N agents launch at once); the ChromaDB bounded
  retry absorbs exactly the transient InternalErrors that get MORE likely under concurrent opens.

## Problem
Connecting the vibe-cognition MCP server at session start is fragile/random: it often fails to
connect on a fresh session and needs a manual `/mcp` reconnect. Acceptance (from the task node):
N>=10 fresh-session starts all connect first try on the primary dev machine; any residual failure
is diagnosable from a log the user can read.

## Evidence from the graph (searched before planning)
- **17a9148fb887** (discovery, high): refined hypothesis is **venv-reuse contention** with the
  live session's own concurrently-running real server — NOT a pure cold-start timing race. 2/2
  scratch-server connect attempts failed under contention.
- **c98a5f3b6d7f / 07f231aa00eb**: `--plugin-dir` cold-start flakiness; scratch server failed to
  connect the same way. Never confirmed production-safe.
- **b076d80a7b41** (root cause, high): `subprocess.run(git, timeout=)` never returns in a
  detached/windowless MCP server (Git-for-Windows pipe drain) — a startup-blocking class if git
  identity resolves during init.
- **e09d4f4a9a23** (open task): intermittent chromadb rust-backend InternalError flake.
- **a54b0191e362** (open task): WP-Server-Lifecycle — orphan server self-exit on parent death
  (Windows process pileup).
- **361d6c2b638b** (WP-5): existing session-start venv-health probe (half-swapped venv self-heal).

## Startup path (read end-to-end)
1. `plugin.json` mcpServers launches the server: `uv run --directory ${CLAUDE_PLUGIN_ROOT} python
   -m vibe_cognition.server`, env `UV_PROJECT_ENVIRONMENT=${CLAUDE_PLUGIN_DATA}/.venv`.
   **NOTE: this launch has NO `--no-sync`** — every other `uv run` in the hook uses `--no-sync`.
2. `session-start.sh` fires ~concurrently and makes THREE `uv run --no-sync` calls against the
   SAME venv (Step 2 health probe on stamp-miss also runs a full `uv sync`; Steps 3/4 migrate + prime).
3. `server.py` `lifespan` (async) opens cognition ChromaDB **synchronously** (line ~413) BEFORE
   spawning the background embedding thread. The embedding MODEL load (2-30s) is correctly
   backgrounded (`_load_embeddings_and_sync`) and gated behind `embedding_ready` — so it is NOT in
   the handshake path. But synchronous ChromaDB open IS in the lifespan/handshake path.

## Root-cause hypotheses (ranked, to confirm — do NOT blind-fix)
Ranking revised after peer review (H1/H2 co-primary; H4 demoted to closed prior-art; H5 added).
- **H1 (co-primary): server-launch `uv run` contends on / re-syncs the venv.** With no `--no-sync`,
  Claude Code's server spawn makes `uv run` re-resolve/lock the venv at the same moment the hook's
  `uv run` calls (esp. a stamp-miss `uv sync`) touch the same venv → lock contention on Windows →
  handshake stalls past Claude Code's connect timeout → "random" first-connect failure. Matches the
  17a9148fb887 venv-contention hypothesis directly.
- **H2 (co-primary — promoted): synchronous ChromaDB open in `lifespan` blocks the handshake.**
  Peer review traced the installed mcp SDK (`server/lowlevel/server.py:657,674`): the lifespan
  async-context runs to its `yield` BEFORE the server begins handling `initialize`. `server.py`
  `lifespan()` makes two synchronous, unretried chromadb calls before that yield
  (`embeddings/storage.py:68` get_collection, `:77-80` get_or_create_collection) — so by
  construction any slowness/transient failure there IS handshake-blocking. Lands squarely on the
  OPEN rust-backend InternalError flake (e09d4f4a9a23, Windows, chromadb 1.5.5). Its mitigation is
  self-verifiable (simulate the InternalError; confirm a bounded retry absorbs it) — no human gate.
- **H3: orphan/pileup servers** (a54b0191e362) from prior sessions hold the venv/handles, adding
  contention to H1/H2.
- **H4 (CLOSED prior-art, NOT an open candidate): git-identity subprocess hang.** The
  `subprocess.run(git, timeout=)` wedge (b076d80a7b41) was FIXED in v0.12.1 — `git_identity.py` now
  reads git-config files directly and never shells out. Repo-wide grep confirms no `subprocess` in
  the server startup path (only in the unrelated `backfill.py` CLI). Cite as resolved history; do
  not re-investigate unless breadcrumbs show a new blocking subprocess.
- **H5 (added): baseline `uv run` cold-start tax, independent of contention.** Even with zero
  contention, the un-`--no-sync`'d server launch pays uv's dependency-tree freshness check
  (stat+resolve over the ~2-4GB PyTorch tree) on EVERY launch, plus Windows Defender real-time
  scanning of `.venv` DLLs/rust `.pyd` on first touch after an update → multi-second latency spikes
  that alone can burn the connect-timeout budget. Falsifiable with the SAME Phase-1 breadcrumbs: a
  failure whose timeline shows the hook's `uv run` calls already finished (no overlap) yet the
  server still stalls at "uv resolve done" REFUTES H1 for that instance and confirms H5. (Note: the
  `--no-sync` fix in Phase 3 also kills H5, since the freshness check is what it removes.)

## Phase 1 — Instrument + ship the self-verifiable fix (no human gate)
Two things ship together here; both are risk-reducing and verifiable in dev without Colton's machine.
- **1a. Bounded-retry wrapper around the ChromaDB open** (`embeddings/storage.py:46-80`, the two
  calls in `lifespan`'s pre-yield path). Strictly risk-reducing — a retry cannot make a working case
  worse — and directly targets co-primary H2 + the open flake e09d4f4a9a23. Self-verify by
  simulating the rust-backend InternalError and confirming the retry absorbs it. Ships ungated by
  Phase 2.
- **1b. Startup breadcrumbs** across the path (ties to open task H-3 33719f0d26bb): server process
  start → uv resolve done → lifespan enter → ChromaDB open start/done → handshake ready (yield) →
  background thread start/model-loaded. Have the hook also stamp its three `uv run` start/end times
  so hook-vs-server venv overlap is VISIBLE in the timeline (this is what distinguishes H1 from H5).
- **HEISENBUG GUARD (mandatory):** do NOT do synchronous flushed disk writes inside the pre-yield
  critical section (that IS the window under suspicion — adding open+write+fsync there, all
  AV-scannable on Windows, can mask or induce a marginal timing failure and bias Phase 2). Capture
  each breadcrumb as an in-memory `time.monotonic()` stamp (cheap), and defer the actual file write
  until AFTER the yield (handshake already past) or onto a background thread. For a pre-yield crash
  trail, route through the stderr stream Claude Code already captures (`config.py:212` basicConfig
  already targets stderr, separate from the stdout JSON-RPC channel) — never a bespoke synchronous
  second file. (stdout buffering is NOT a factor — mcp `stdio.py:80-81` flushes after every write.)
- Phase 1 is independently valuable: the acceptance requires a diagnosable log even if a failure
  remains, and 1a reduces the flake surface immediately.

## Phase 2 — Reproduce + isolate (HUMAN-RUN — install-mechanics can't be self-verified)
- Active constraint: install-mechanics fixes gate on a human's machine. Vorpid/Vince cannot
  self-verify connect reliability. Colton runs the reproduction: N>=10 fresh-session starts,
  capturing the Phase-1 log each time; classify each start connect/fail and which breadcrumb the
  failures stall at. This tells us which hypothesis actually fires before we harden.

## Phase 3 — Targeted hardening (gated behind Phase 2's evidence)
Candidate fixes, mapped to hypotheses (implement the ones the evidence supports):
- **H1/H5 → add `--no-sync` to the server launch args in `plugin.json`** so the server spawn stops
  re-syncing/locking the venv. **MANDATORY PRECONDITION — do NOT ship bare `--no-sync`:** the hook's
  sync is NOT guaranteed to finish before Claude Code spawns the server (the 600s hook timeout for
  ~8-min first-install syncs is strong evidence the two are concurrent, and no code/graph node
  documents a serialization guarantee). Bare `--no-sync` would make the server launch against an
  absent/partial venv and fail FAST and DETERMINISTICALLY on every first-install / post-update
  session — worse than today's intermittent-but-self-healing behavior, on exactly the new/updating
  users the fix targets. So `--no-sync` ships ONLY paired with a fast pre-import venv-ready check in
  `server.py` `main()` (~line 502): attempt importing the heavy natives (torch/chromadb) and, if
  broken/missing, emit a CLEAR failure message and exit — mirroring the health-probe pattern in
  `session-start.sh:79-101`. **CONCURRENCY-SAFE GUARD (do NOT have the server run `uv sync` itself):**
  the hook owns healing (it has the 600s budget); if the server also synced, N concurrent servers +
  the hook could all `uv sync` the same venv at once. So the server's guard is READ-ONLY (import
  probe) + fast-fail-with-message on broken; the hook/next session heals. Healthy venv (the
  steady-state and every existing install) → import passes → server proceeds fast, never touching
  the venv lock → strictly BETTER for N concurrent launches. That guard is self-verifiable; the
  connect-reliability effect of `--no-sync` is confirmed by Colton's post-ship controlled rollout.
- **H2 → (bounded retry already shipped in Phase 1a).** If Phase 2 shows the open still stalls, the
  heavier option is moving the ChromaDB open off the handshake path entirely (lazy / background with
  a readiness gate mirroring embedding_ready).
- **H3 → reap orphan servers** (coordinate with a54b0191e362; may just cite/depend on it).
- **H4 → nothing to do** (closed prior-art; see hypothesis list). Only revisit if Phase-1
  breadcrumbs surface a NEW blocking subprocess in the startup path.

## Verification (HUMAN gate — cannot self-verify)
- Colton re-runs the N>=10 fresh-start protocol after Phase 3; acceptance met when all connect
  first try. Residual failures must be diagnosable from the Phase-1 log.

## Known-intentional / do NOT "fix"
- venv lives at `${CLAUDE_PLUGIN_DATA}/.venv` on purpose (outside the version-pinned cache dir;
  Windows update-lock avoidance — v0.6.0 d0362d89d295). Do not relocate it.
- Server is plugin.json-declared, NOT per-project `.mcp.json` (a project entry outranks the plugin
  — a906f12a6ef7). Do not reintroduce a per-project entry.
- The stamp-gated conditional sync + health probe in session-start.sh is deliberate (pays nothing
  on the steady-state happy path). Instrumentation must not force the probe on every start.
- Embedding model load is intentionally backgrounded behind `embedding_ready`; do not move it into
  the handshake path.

## Sequencing (ship WP-A + WP-B together, autonomously — per the mandate)
- **WP-A:** ChromaDB bounded retry (1a) + concurrency-safe breadcrumbs with the heisenbug guard
  (1b). Strictly risk-reducing.
- **WP-B:** the `--no-sync` flip + its MANDATORY read-only concurrency-safe pre-import guard in
  `server.py` main(). (H3 orphan-reap is OUT of scope for this release — depends on a54b0191e362;
  cite, don't bundle.)
- **Concurrency acceptance (blocks release):** a cross-process test that launches multiple server
  processes / opens against the same venv + ChromaDB + journal concurrently and asserts no
  collision, lock error, or corruption — extend/loosely mirror the intent of open task
  c34c788b8d5b (cross-process shared-ChromaDB convergence). If a full multi-process harness is too
  heavy, at minimum an in-process concurrent-open + concurrent-append test plus a documented manual
  two-agent check. Bare "the suite passes" does NOT satisfy this gate.
- **Regression acceptance (blocks release):** full suite green; no existing test modified to pass;
  the `--no-sync` guard proven not to fire on a healthy venv (existing installs unaffected).
- **Post-ship (Colton):** controlled rollout — Colton tests the shipped build before upgrading his
  running fleet; revert is the fallback if the connect-reliability effect doesn't hold on real
  machines. This replaces the human-reproduction pre-gate.
Version: 0.15.3 (patch). Release = bump pyproject.toml + plugin.json, land on main, push, Loki re-pin.
