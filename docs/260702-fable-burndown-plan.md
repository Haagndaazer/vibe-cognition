# Fable-audit backlog burn-down — overnight autonomous run (2026-07-02)

Manager: Vince. Implementers: general-purpose subagents (one per WP), editing the single
shared checkout on `main` directly (human's explicit instruction). Implementers never
commit; Vince gates each diff (pytest w/ hard timeout, ruff, pyright-no-new-errors,
tautology + known-intentional checks) and commits+pushes per WP. Peer-reviewed by a
Sonnet subagent before execution; amendments folded in below.

Scope: the 39 children of fable-audit epic `74740daa02fa` (docs/260701-fable-audit-*.md).
No release tonight: single version bump 0.13.0 → 0.14.0 at the end; human releases.
Tasks needing a product decision I can't reconcile from the cognition graph are skipped
with the open question appended to the task node.

## Standing acceptance criteria (every WP)
- `uv run pytest` green under a hard wall-clock timeout; `uv run ruff check .` clean;
  `uv run pyright` no NEW errors (1 pre-existing dashboard/server.py error is known).
- New tests are non-tautological (assert the specific failure reason; fail on revert).
- No new test may spawn real subprocesses/sockets/network (mock them); Windows handle
  cleanup in fixtures (Chroma clients, lock files).
- Any WP touching `get_status` updates its docstring in the same diff; docstring diffed
  against the actual return dict at the gate (WPs 1, 2, 4).
- Diff cross-checked against the known-intentional list before commit.

## Known-intentional (must NOT be "fixed")
Task-delete child-orphaning storage behavior (F10, tested); document drift pull-only
detection; merge=union-only auto-config (decision 9f13a8099e03 — the -text item is
disclosure only); cognition_record rejects node_type=task; workflow in-place-edit
refusal (supersession only); node-id mint TOCTOU accepted residual; Haiku pin for
curate subagents; dashboard localhost-only design; embedding_ready.set() timing
(E-8 deferral is deliberate — do not make startup sync blocking).

## Work packages (execution order)
- **WP-0 (setup, folded into WP-1 dispatch):** add pytest-timeout + default timeout so no
  new test can wedge the unattended run.
- **WP-1 Loss visibility** — 1bce5542be63 (critical: rehydrate-reset WARNING w/ node-count
  delta + get_status field + next-prime flag), 74df98ebb3a4 (actor in remove_node
  tombstone; docstring gaps unlinked_artifacts/task-child-detachment — docstring-only;
  dashboard DELETE auth-rejection test), 914e4a354031 (**disclosure only**: residual
  autocrlf/byte-rewrite risk documented in readme.py team setup + README; NO change to
  git_hygiene auto-write).
- **WP-2 Honest search** — 379e3949d90f (_parse_node_type in cognition_search, home +
  multi-project paths), 6074dbe0875e (home-collection model/dim drift guard + get_status
  surfacing + regression test), 6ae6deb713e8 (fix getting-started example: node_type/
  context/author).
- **WP-3 Embedding write-path integrity** — b35e15766c6b (stamp embed_scheme at collection
  creation; file-lock the recreate migration REUSING git_hygiene's lock primitive;
  same-process two-instance lock test; true multi-process repro only as skip-by-default
  marker), 8606d59905a5 (re-embed on journal replay via the shared embed paths).
- **WP-4 Reconciler==writer + join visibility (code)** — 3e82d4ebc004 (route
  _sync_cognition_embeddings through _embed_entity_node/_embed_workflow; parity test),
  41ced8d1fa63 (expected chunk count in metadata; reconciler verifies full set),
  5340ae677931 code half (third "syncing" embedding_status value + progress surfacing in
  get_status; embedding_ready timing unchanged). Implementer reads post-WP-3 state.
- **WP-5 Merge-topology defense** — d6cd1495b23a (log dropped add_edge/remove_* replay
  actions + deferred-retry pass + merge-shaped journal tests), 7c1899fe59ed (sweep
  re-evaluates against reference index, not has-any-edge), b36e4a79113a dedup-contract
  half (commit-ref lookup before episode mint: warn/reuse). duplicate_of reachability +
  4800d5d16adb are SKIPPED with question (product call on merge semantics).
- **WP-6 Host/config contract** — b603f667130f (env_ignore_empty + validate repo_path +
  unify 3 env-read patterns; first confirm tests don't rely on empty-string fallback),
  d4a153f23a4c (pin env_file to repo_path/.env or drop — pick drop if ambiguous, it's
  inert today), b48927a30e66 (CI version-match check).
- **WP-7 Context pipeline** — 530adc9e6f3f (compact hook additionally runs generate_prime()
  — already the trimmed digest — single pinned direction), 0da12a816d7f (add tasks-first +
  workflow-first to SERVER_INSTRUCTIONS, tersely), 9aca47c5803d (onboarding single source
  of truth + include SERVER_INSTRUCTIONS in token accounting notes).
- **WP-8 Tool-surface contract** — 758a360230b7, 7d1c151b0372, c0e6afeddaf9 (document both
  keys in both docstrings if renaming is breaking — prefer documenting over renaming),
  e130ad211ebe (repo-wide self-sufficiency re-audit incl. get_status keys +
  store_document references asymmetry + drift tests).
- **WP-9 Ship-the-knowledge docs** — 519c240c279c (README accuracy pass), 5c9978ed8d29
  (topology guide doc), 5340ae677931 doc half (joining-an-existing-graph walkthrough +
  fix "auto-converges"), 4350a42fc4e5 (snapshot_journal wired into the shipped flush
  procedure), 73f750d8d528 (attribution honesty docs), bab6c5431a62 (harm-register
  amendment — docs-only, no lifecycle code).
- **WP-10 Skills layer** — 9d5a19b30055, 0ad4aa80bb15, 8645d67e7055.
- **WP-11 Install/lifecycle robustness** — 8e207087b093 (guard _atomic_write like the read
  path), 38a5914e6dc6 (graceful lifespan failure), c3074f43cd49 (timeout sizing +
  multi-cause probe message), 1a796b2be9b5 (shell-script harness — stub PATH binaries,
  never real uv/network).
- **WP-12 Data-integrity tail** — 4ae72cafb48c, db65f1568fa5 (surfacing + offer supersedes;
  pull-only detection stays), b9af2e60fe19 (document or retire the backfill CLI —
  reconcile with /vibe-backfill watermark), d999b4e3851a, 07fdfe725e7f.
- **WP-13 Low tail** — 4aaef22e25ea (argparse + --days, also closes 21232d2acaea),
  9cb745be2570 (Ollama prefix parity; tests mock the HTTP client, no live daemon),
  ebe050e78923 (stop INFO-logging token URL; document transcript exposure).

Closeout: version bump, journal flush, task statuses reconciled, cognition episode +
decisions recorded, /vibe-curate run, summary for the human.
