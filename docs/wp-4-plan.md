# WP-4 Execution Plan — fix/wp-4-journal-atomicity (C-1 + C-3 + cross-process test) — REV 2

The highest-stakes WP (data-loss class). Two fixes land together, reviewed against each other (**ledger 11**). REV 2 folds in the decorrelated peer review (ledger 7) — which materially expanded scope on a data-loss path; **the scope changes are flagged for Vince before build.**

## Current mechanics (verified)
- **Append** (`storage.py:599-608`): buffered text-mode `open(..., "a")` + `f.write(line+"\n")`. A line > the buffer flushes as multiple OS writes → two concurrent processes interleave → both lost (**C-1**).
- **Catch-up** (`storage.py:610-672`): binary read from `self._offset`; advances only past the last `\n`; `if size < self._offset` → rebuild. No identity check → same-or-larger replacement replays from a stale offset (**C-3**).
- **Line endings**: journal uniformly CRLF on Windows (344/344). BOTH writers — server text-mode AND post-commit hook text-mode (`post-commit.py:100`) — emit CRLF; `.gitattributes -text` stores verbatim. A C-1 rewrite MUST preserve this byte format (ledger 11).

## SCOPE EXPANSION (peer review — confirm with Vince before build)
1. **The byte-range lock is MANDATORY, not optional.** A single `os.write` is atomic *in practice* for KB records on a local FS, but a short write (rare: signal/disk-pressure) leaves a truncated record that the next appender concatenates onto → audit C-2 (torn tail eats next entry). The only interleave-safe way to finish a short write is to loop UNDER an exclusive lock. So the lock is what makes the fix correct, not belt-and-suspenders. (Drop the earlier "well inside the platform's atomic-write size" claim — no such formal guarantee exists for regular files; scope the guarantee to a LOCAL filesystem, not a live network mount.)
2. **The post-commit hook must route through the same atomic helper** (audit C-1 names "session + post-commit hook" as the concurrent pair; audit H-2 = the hook forks the journal format). Extract a stdlib-only `append_journal_line(path, line_str)` helper both the server and the hook import. Otherwise: large commit messages exceed the buffer → the exact C-1 loss between the two named writers; and a mandatory Windows lock on the server would make the hook's plain `open` fail. Routing both through the helper makes the mutual exclusion real and closes H-2.

## (C-1) Atomic append helper (shared, stdlib-only)
New `cognition/journal_io.py` (importable by both the package and the stdlib-only hook):
```python
import os, sys
def journal_newline_bytes() -> bytes:
    return b"\r\n" if os.linesep == "\r\n" else b"\n"  # reproduce text-mode bytes per platform

def append_journal_line(path, line: str) -> None:
    blob = line.encode("utf-8") + journal_newline_bytes()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)  # mode POSIX-only; Windows ignores
    try:
        _lock(fd)                       # exclusive; see _lock/_unlock below
        try:
            mv = memoryview(blob)
            while mv:                    # loop-to-completion is safe ONLY under the lock
                mv = mv[os.write(fd, mv):]
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
```
- **`_lock`/`_unlock` (platform split):** POSIX `fcntl.flock(fd, LOCK_EX)` / `LOCK_UN` (whole-file advisory, blocks, auto-released). Windows `msvcrt.locking(fd, LK_LOCK, 1)` on a FIXED sentinel byte range (seek to 0, lock 1 byte, as a pure mutex — O_APPEND makes the real write region's position racy to lock). `fcntl` does not exist on Windows; `msvcrt` does not on POSIX — import per-platform, never a fake unified shim.
- **Contention handling (no silent except-pass):** `msvcrt.locking` raises `OSError` after ~10 retries/~10s. On lock failure: a bounded retry, then fall back to a SINGLE un-looped `os.write(fd, blob)` (still interleave-safe for the common case) and log loudly. Never drop the entry; never except-pass.
- **Format:** `journal_newline_bytes()` reproduces text-mode bytes exactly (verified: text-mode writes `\r\n` on Windows, `\n` on POSIX; `json.dumps(ensure_ascii=False)` escapes embedded newlines so the only newline is the terminator) → byte-identical journal; `-text`/offset-math/cross-machine unaffected. Comment why `os.linesep` keying is safe (per-checkout uniformity via `-text`). **LF-normalization was considered and DEFERRED** — normalizing the live CRLF journal is itself a whole-file rewrite under byte-offset replay (ledger 5: the defense is a write to the defended thing), not to be done inside the data-loss WP.
- `storage._append_journal` and `hooks/post-commit.py:_append_episode` both call `append_journal_line`. (Hook keeps its `journal_path.parent.exists()` guard.)

## (C-3) Offset identity check — `_catch_up`
- At hydrate-from-0, after reading, capture `self._journal_identity = sha256(first_complete_line_bytes)` (first line is immutable — storage is pure append-only, verified) AND `self._journal_mtime_ns = stat.st_mtime_ns`.
- Hot path stays one `stat()`. Change the cheap return: if `size == self._offset` AND `st_mtime_ns == self._journal_mtime_ns` → return 0 (unchanged). If `size == offset` but **mtime changed** → fall through to the identity check (closes the equal-byte-size replacement residual nearly free — mtime is already in the stat we call). N3.
- When replaying is about to happen (size > offset, or mtime-changed), re-read the first line; if `sha256 != self._journal_identity` → journal replaced → wipe + rebuild from 0 + re-capture identity & mtime. Else replay from offset as today.
- Note: a self-append also grows size>offset, so the first-line re-read fires once per post-own-append catch-up (audit C-6 self-replay path) — one small extra read; the true hot path (size==offset, mtime same) is untouched.

## Composition review (ledger 11) — corrected (peer review M1)
- The in-process **RLock** and the cross-process **file lock** are DIFFERENT locks. The catch-up READ is NOT under the file lock (lock guards appends only). A C-3 rebuild-from-0 reading while another process appends at EOF is safe **because of torn-tail parking + idempotent replay**, NOT mutual exclusion — state this correctly (don't claim "under the lock").
- **Convergence invariant after a replacement:** there is NO cross-process coordination; correctness rests on EVERY live process independently catching up and detecting the identity change, plus idempotent replay. Document it.
- C-1 preserves byte format → C-3's first-line read + offset math see identical bytes. C-1 appends are offset-independent (O_APPEND → real EOF, not `_offset`). The explicit cross-test: append (C-1) while triggering a replacement (C-3) in one store.

## Tests (ledger 6 + 12)
- **Cross-process append (C-1)** `tests/test_journal_concurrency.py`: **subprocess** (not multiprocessing — Windows spawn re-import/pickle footguns; subprocess gives unambiguous separate handles via a tiny stdlib appender invoked with `-c`/module). N≥4 procs × hundreds of records, each record **≥64 KiB** (>> any buffer, so the OLD bug interleaves with prob ≈1). After join: assert exact record count AND every line parses as valid JSON AND matches a known-emitted record. **Worker exceptions via child exit codes** — assert every `returncode == 0` and stderr empty, as its own labeled assertion (ledger 6).
- **Tautology / fails-before (ledger 12):** run the SAME test against the reverted buffered-text append; it must fail (corrupt JSON / count mismatch). Run it MULTIPLE times (a single green of the reverted test is insufficient — concurrency negatives are probabilistic); require failure in all/most. The N=5-green of the fixed code is only meaningful BECAUSE this same config reliably fails the revert — state the linkage.
- **Offset identity (C-3):** replace the journal with same-or-larger unrelated content (and an equal-size + bumped-mtime variant) → catch up → assert rebuild, no stale-offset double-apply / tombstone resurrection. Revert C-3 → assert it fails.
- **N=5 consecutive green full-suite runs** before done.

## Acceptance
- N=5 greens; worker-exception collection asserted; both new tests fail vs their reverts (ledger 12); composition reviewed (ledger 11, corrected); ledger 16 noted (append lock is not check-then-act — no pre-lock evidence; catch-up keeps the RLock). pyright ≤ baseline, ruff clean, pytest count up. CI green. Journal off-branch. No new deps (os/fcntl/msvcrt/hashlib stdlib). No version bump.

## Commits
1. `C-1: shared atomic journal-append helper (O_APPEND + byte-range lock)` — journal_io.py + storage + hook routed through it.
2. `C-3: journal identity + mtime check alongside the replay offset`.
3. `WP-4: cross-process append + journal-replacement regression tests`.
4. CHANGELOG (Unreleased).
