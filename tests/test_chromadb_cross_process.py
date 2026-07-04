"""Cross-process shared-ChromaDB convergence test (extends open task
c34c788b8d5b's "what remains" ask, gates ced8f0401bd2 / decision 9022f7de94e9).

Colton's PRIMARY HARD GATE #1: concurrent multi-agent use against the SAME
shared venv + ChromaDB + journal is a primary feature, not an edge case, and
must be demonstrated with a cross-process test, not argued. This mirrors
test_journal_concurrency.py's pattern -- real OS subprocesses (not threads or
multiprocessing; Windows `multiprocessing` spawn re-imports/pickles the
module, a pytest footgun already noted there) -- because ChromaDBStorage's
retry wrapper (WP-A 1a) and any real SQLite/rust-backend contention only
manifest under GENUINE separate-process concurrent access, which threads
within one interpreter cannot reproduce.

MANUAL TWO-AGENT CHECK (documented, not automated -- the plan's "minimum"
bar alongside this automated test): open two Claude Code sessions in the
same project, at the same time, and in each one record a cognition node
(cognition_record) back to back within a few seconds of each other. Confirm:
(1) neither session's MCP connection drops or errors, (2) both nodes appear
in `cognition_list_tasks` / `cognition_search` from EITHER session after a
few seconds, (3) `.cognition/journal.jsonl` has both entries with no
corruption (parses cleanly line-by-line). This exercises the real venv +
server-launch path (--no-sync + the pre-import guard) that no in-process or
subprocess-level test can substitute for.
"""

import subprocess
import sys

from vibe_cognition.embeddings import ChromaDBStorage

# Path-loads the vibe_cognition package normally (installed editable in this
# venv), so each worker is a genuine, independent Python process -- not a
# thread or a multiprocessing-spawned child of this test process.
_WORKER = """
import sys
from vibe_cognition.embeddings import ChromaDBStorage

persist_dir, worker_id, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
storage = ChromaDBStorage(persist_directory=__import__("pathlib").Path(persist_dir))
for i in range(n):
    storage.upsert_embedding(
        f"w{worker_id}-{i}", [0.1, 0.2, 0.3], {"worker": worker_id, "i": i}
    )
storage.close()
"""


def test_cross_process_chromadb_concurrent_open_and_write_no_collision(tmp_path):
    """N independent OS processes each construct their OWN ChromaDBStorage
    against the SAME persist_directory concurrently (the exact contention
    shape N concurrent agent sessions create), then write disjoint vectors.
    Zero collisions/corruption means the shared-ChromaDB gate holds.

    This exercises the retry-wrapped open path (WP-A 1a) under genuine
    concurrent-open pressure, though the assertion is on CONVERGENCE
    (correctness), not on whether the flake specifically fired this run --
    the flake is inherently probabilistic and covered directly by the
    mocked-InternalError unit tests in test_embeddings_storage.py.
    """
    persist_dir = tmp_path / "chromadb"
    nproc, nrec = 6, 25

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, str(persist_dir), str(w), str(nrec)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for w in range(nproc)
    ]

    failures = []
    try:
        for w, p in enumerate(procs):
            _, err = p.communicate(timeout=180)
            if p.returncode != 0:
                failures.append(f"worker {w} exited {p.returncode}: {err.decode(errors='replace')}")
    finally:
        for p in procs:
            if p.poll() is None:  # leaked (e.g. timeout) -- don't orphan it
                p.kill()

    assert failures == [], (
        f"worker process(es) failed -- concurrent open/write did NOT converge cleanly "
        f"(this is a REAL finding about the primary concurrent-use feature, not noise): "
        f"{failures}"
    )

    # Convergence check: every worker's vectors landed, none missing/duplicated.
    storage = ChromaDBStorage(persist_directory=persist_dir)
    ids = set(storage._collection.get()["ids"])
    expected = {f"w{w}-{i}" for w in range(nproc) for i in range(nrec)}
    missing = expected - ids
    unexpected = ids - expected
    assert not missing and not unexpected, (
        f"concurrent cross-process writes did not converge: "
        f"missing={sorted(missing)[:10]} unexpected={sorted(unexpected)[:10]}"
    )
    storage.close()
