The data-loss WP: journal append cross-process atomicity (C-1), replacement detection (C-3), routed through a shared helper that also closes H-2 (the hook forked the format) and the C-2 short-write hazard. One mechanism, four findings.

## C-1 — atomic cross-process append
New stdlib-only `cognition/journal_io.py`: `append_journal_line` writes each record to an `O_APPEND` fd **under an exclusive byte-range lock** (`fcntl.flock` POSIX / `msvcrt` sentinel-byte Windows), with a loop-to-completion that is interleave-safe **only because the lock is held**. (A single `os.write` is atomic in practice for small records, but a short write would leave a truncated record the next appender concatenates onto — audit C-2. The lock closes that; the loop is safe under it.)
- **Lock fallback residual window (documented):** on lock contention (Windows `msvcrt` times out after ~10s) it falls back to a single un-looped `os.write` of the whole record + a loud log — never blocks, never drops. Residual: a *rare* short write in that fallback could truncate one record. Bounded and far better than blocking/dropping.
- **Byte format preserved:** writes CRLF on Windows (via `O_BINARY`) exactly as text-mode did, so the `-text` byte-determinism, the byte-offset replay math, and cross-machine sharing are unaffected. (LF-normalization considered and deferred — it's a whole-file rewrite, ledger 5.)

## H-2 — both writers share the helper
`storage._append_journal` and the post-commit hook both call `append_journal_line`. The hook **path-loads** it (no package install, no heavy import chain) so it stays standard-library-only. One unlocked writer would defeat the lock; sharing the format closes H-2.

## C-3 — replacement detection
`_catch_up` gains a journal identity check: first-line `sha256` + `st_mtime_ns` (already in the hot-path `stat()`). A same-or-larger replacement (git merge of the committed journal) now re-hydrates instead of replaying from a stale offset; mtime catches an equal-size swap; and a from-0 read with a non-empty graph re-hydrates too (the `offset==0` window — found by the composition test, see below).

## Composition review (ledger 11) — the two fixes against each other
- The rebuild read is **NOT** under the cross-process append lock (that lock serializes appends only). Rebuild-vs-append safety rests on **torn-tail parking + idempotent replay + per-process identity checks**, never mutual exclusion — stated as an invariant in `_catch_up`'s docstring so a future fix can't reason from a false "it's under the lock" premise.
- The composition test surfaced a real gap: a replacement landing while `offset==0` (a store that appended but hadn't caught up past its own writes) evaded the identity check (gated on `offset>0`). Closed by re-hydrating on a from-0 read with a non-empty graph.

## Verification (ledger 6 + 12)
- **Fails-before, run repeatedly:** old buffered append → 4/4 runs interleaved (145–192 of 240 lines, corrupt JSON). C-3 identity branch reverted → its test fails. offset==0 branch reverted → composition test fails. (Concurrency negatives are probabilistic; verified across multiple runs.)
- **N=5 consecutive green** full-suite runs (135 passed each).
- pyright 31 (baseline), ruff clean, pytest 131 → **135**.

## Scope note
Atomicity guarantee covers multiple processes on a **local** filesystem; `O_APPEND`/advisory locks are unreliable on live network mounts (NFS/SMB) — out of scope. The hook routing is install-mechanics and the cross-platform lock can't be fully self-verified — **gated on human release test** (b8ec24fe9107).
