# WP-Sidecar (P0 endgame): the heavy import leaves the server process

Status: rev 2 — Vince, 2026-07-06. Rev 1 review FAIL (3 MAJOR / 3 MINOR:
mutex abandonment, write-side pipe deadlock, post-degrade recovery owner,
watchdog-collapse disciplines, Local\ namespace, time-to-ready honesty)
folded in.
Implementer: Vorpid, AFTER WP-Lifecycle gates (order: Wedge-2 → Lifecycle →
Sidecar; Lifecycle first because orphan pressure is the wedge amplifier and
its fix is small). Branch: `wp-sidecar` in a worktree at
`C:\Users\colto\Documents\Projects\Worktrees\vibe-cognition`.

## 1. Why

Everything since v0.15.3 established one truth: an in-process
torch/scipy/sentence_transformers import can wedge for 24+ minutes holding
process-wide locks (OS loader lock, import machinery), and no amount of
in-process cleverness makes that SAFE — WP-Wedge's probe didn't prevent it,
WP-Wedge-2 only makes dispatch SURVIVE it (degraded answers, no hangs). The
degraded window itself — embeddings unavailable for the wedge's duration —
remains. The only structural fix: the serving process NEVER imports the heavy
chain. A wedge then happens inside a child process the server can observe,
kill, and retry — converting "20-minute degraded window, uncontrollable" into
"bounded retry with a supervisor".

## 2. Direction

**Per-server sidecar subprocess, spawned lazily by the bg orchestration that
today does the in-process load.** The server process becomes permanently
heavy-import-free (the static AST guard flips from "only generator.py may
import the chain" to "NOTHING in the server process may import it; only the
sidecar entry module does, and it is never imported server-side").

- **Process model:** one sidecar per server, spawned like the WP-Wedge probe's
  hardened pattern but long-lived. Parent-death safety: the sidecar gets the
  same WP-Lifecycle parent-watch (server dies → sidecar dies), so the orphan
  problem is not doubled.
- **IPC — the v0.12.1 lesson is the design constraint, not a footnote.**
  stdin/stdout pipes with a DEDICATED drain thread per pipe on the server
  side, started before any request is sent; stderr → DEVNULL (or a file, not
  a pipe). Requests/responses are JSON lines with ids; every request has a
  timeout; response reading never happens on the event loop (drain thread +
  queue → loop via call_soon_threadsafe or an anyio memory stream). No pipe
  is ever left undrained. If Vorpid prefers a localhost socket/named pipe
  over stdio, that's craft — the non-negotiables are: dedicated reader
  thread; dedicated WRITER thread with a bounded outbound queue (a
  `generate()` payload of document chunks can exceed the pipe buffer; if the
  child stops draining stdin, a direct `write()` blocks IN THE OS on the
  requesting thread, unreachable by any response-wait timeout — the
  symmetric twin of the v0.12.1 drain lesson); round-trip timeout owned by
  the SUPERVISOR (a third party, never the possibly-blocked requesting
  thread), whose kill demonstrably closes handles and unblocks a stuck
  write; no reads or writes on the loop; no unbounded buffers.
- **Protocol surface (small, boring):** `load(model, dims)` → ok/err;
  `generate(texts: list, input_type)` → vectors; `ping` → ok. Bulk/batch is a
  list argument, not a second protocol. Version/model are pinned in the load
  call; a protocol-version field guards plugin-update skew (server and
  sidecar spawn from the same installed tree, but a running server may
  outlive an update — reject on mismatch and respawn).
- **Supervision:** load timeout generous (the point is the server doesn't
  care); on timeout/crash → kill (probe-style kill+wait), respawn with
  backoff, bounded attempts, then degraded state with the existing error-dict
  surface. Two rules the collapse of WP-Wedge's watchdog must NOT lose:
  1. **Recovery after degrade has an owner.** Production evidence says
     wedged loads eventually complete (24.4 min → full service); a
     supervisor that gives up after N attempts would leave that session
     degraded FOREVER — strictly worse than v0.15.4's late recovery. After
     the retry budget: lazy respawn on next embedding demand, plus a slow
     periodic retry (minutes-scale). Size the in-budget retries against the
     mutex queue: N concurrent sessions × healthy load time (~30s) must fit
     inside the budget, or serialized fleets mass-degrade on startup.
  2. **The supervisor is the SOLE writer of embedding-state transitions**
     (`embedding_error`/`embedding_ready`/generator installation), making
     the WP-Wedge stranding-interleave race structurally impossible — this
     replaces the `_wedge_lock` atomicity discipline, and the five AC2
     watchdog/late-recovery tests in test_wp_wedge.py are REPLACED by
     supervisor-equivalent coverage with the replacement called out
     explicitly (WP2-AC6 precedent).
  The 120/300s watchdog was a proxy for "is the load wedged" — the
  supervisor now KNOWS, and can act, which the watchdog never could. (The
  watchdog never covered post-ready sync/chroma phases — ready fires before
  sync by design — so nothing is silently lost there.)
- **Cross-process load serialization (stampede killer):** the sidecar
  acquires a named mutex — `Local\vibe-cognition-model-load` (the session
  namespace already spans every server on this machine since all sessions
  run as one user in one interactive session; `Global\` buys nothing here
  and invites ACL questions on multi-user boxes) — around the heavy import +
  model load, so N concurrent sessions load ONE at a time instead of
  stampeding the disk — today's evidence says concurrency is what turns a
  27s load into a 24-minute wedge. Non-negotiables (rev-1 MAJOR):
  - **`WAIT_ABANDONED` (0x80) IS successful acquisition.** Supervisor-kill-
    while-holding is DESIGNED behavior, so abandonment is a certainty, not
    an edge case; a `== WAIT_OBJECT_0` check turns one kill into fleet-wide
    "mutex stuck". Breadcrumb abandonment distinctly
    (`model_load_lock_acquired_abandoned`).
  - Acquire-timeout expiry → proceed WITHOUT the lock, breadcrumbed
    (stampede risk beats never loading).
  - Breadcrumbed wait (`model_load_lock_wait`/`_acquired`), and the
    supervisor's load timeout runs OUTSIDE the lock wait (waiting in line
    must not count as wedged).
- **What stays in-server:** chromadb (module-top import, unchanged — it is
  not the wedge source), the graph, the journal, all tools, WP-Wedge-2's
  spawn-free dispatch and import-free surface (they remain the backstop for
  whatever new first-use import surprises exist).
- **Ollama backend:** untouched — it already talks to an external process
  over HTTP; the sidecar path is for the sentence-transformers backend only.
  Backend selection logic stays where it is.

## 3. Scope

- §S-a: sidecar entry module (`python -m vibe_cognition.embeddings.sidecar`)
  + protocol + named-mutex load serialization + breadcrumbs (its own
  `pid-*.log`, tagged `sidecar`, same retention).
- §S-b: server-side client (spawn, drain threads, request/timeout machinery,
  supervisor with kill/respawn/backoff/degrade) replacing the in-process
  load path in `_load_embeddings_and_sync`; `EmbeddingGenerator`'s
  sentence-transformers backend becomes a thin proxy over the client (same
  interface, so tools/tests upstream don't change).
- §S-c: guard flip — the AST test now forbids the heavy chain EVERYWHERE in
  the served process (sanctioned set = the sidecar entry module only) and a
  runtime assertion that none of the heavy names are in `sys.modules` at
  handshake and at first tool call.
- §S-d: startup UX parity — `embedding_status` reflects sidecar states
  (`spawning`/`loading`/`ready`/`error: ...` and `waiting-for-load-lock`),
  get_status stays instant, late recovery (sidecar comes good after degrade)
  keeps working.
- §S-e: honesty items for the PR body — (1) per-server sidecars still cost
  one torch heap per session; the shared-daemon variant (one embedding
  process per machine) is a FOLLOW-UP epic if session counts make that heap
  cost bite; design nothing that precludes it (the protocol is already
  transport-agnostic if §S-a keeps framing separate from the pipe). (2)
  Serialization bounds the WEDGE, not time-to-ready: a session behind N
  queued loads still waits ~N × 30s with `waiting-for-load-lock` status;
  nothing in this architecture bounds time-to-ready under fleet load — the
  shared daemon is the real answer to that, deliberately deferred. No
  "embeddings become promptly available" language.

## 4. Acceptance criteria (WPS-AC*)

- **WPS-AC1:** the server process never imports the heavy chain: AST guard
  (flipped) + runtime `sys.modules` assertion pass; grep-level proof in PR.
- **WPS-AC2:** end-to-end embed flow works through the sidecar (record →
  embed → search finds it) in a real-subprocess integration test.
- **WPS-AC3:** sidecar killed mid-request → request returns the error dict
  within its timeout; supervisor respawns; next request succeeds. Sidecar
  wedged at load (simulated with a blocking stub sidecar) → supervisor kills
  at timeout, retries, then degrades; server dispatch stays instant
  throughout (reuse WP-Wedge-2's AC1 harness against the stub-wedged
  sidecar).
- **WPS-AC4:** two concurrent sidecars (two server processes) serialize their
  loads on the named mutex — second waits, both succeed, waiting is not
  killed as wedged (integration test with two real subprocesses). PLUS the
  abandonment variant: kill the holder mid-load → the waiter's acquisition
  returns WAIT_ABANDONED and proceeds successfully.
- **WPS-AC5:** pipe discipline, BOTH directions: (i) a sidecar flooding
  stdout/stderr cannot block either process (chatty stub); (ii) a stub child
  that STOPS READING stdin while the server sends an oversized `generate`
  payload — the request fails at its timeout via supervisor kill, the
  requesting thread unblocks, the server stays responsive. This is the
  v0.12.1 class made into an AC, write side included.
- **WPS-AC6:** sidecar dies with its server (parent-watch inherited from
  WP-Lifecycle) — kill the server, sidecar exits within 5s.
- **WPS-AC7:** zero regression — full suite green, ruff clean; ollama path
  untouched by behavior tests; handshake latency unchanged.

## 5. Known-intentional / constraints

- All WP-Wedge/Wedge-2/Lifecycle standing constraints carry over (worktree,
  no journal commits, exact-SHA report, voiding clause, `uv run` pytest,
  DEVNULL-for-fire-and-forget subprocesses — note the sidecar's PIPED stdio
  is the sanctioned exception BECAUSE of the dedicated drain threads; that
  reasoning must appear in a code comment at the spawn site).
- The probe (`_run_subprocess_import_probe`) is SUBSUMED: the sidecar IS the
  subprocess now. Remove the probe with its tests replaced by WPS-AC3
  coverage — call the replacement out explicitly, per the WP-Wedge-2 AC6
  precedent.
- fastmcp/anyio pins unchanged; any monkeypatch of third-party internals =
  HOLD (Wedge-2 rule).
- Do not build the shared machine-wide daemon in this WP (§S-e records why).

## 6. Out of scope

- Shared cross-session embedding daemon (follow-up epic; §S-e).
- Client-side anything; OS-level DLL forensics.
- Backend feature work (new models, dims, etc.).
