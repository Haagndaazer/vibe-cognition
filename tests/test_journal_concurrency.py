"""WP-4: cross-process journal atomicity (C-1), identity check (C-3), H-2 contract.

The cross-process test uses real OS subprocesses (not threads or multiprocessing —
the audit notes existing concurrency tests are single-process, and Windows
`multiprocessing` spawn re-imports/pickles the module, a pytest footgun). Records
are far larger than any write buffer so the OLD buffered text-mode append would
interleave with near-certainty.

FAILS-BEFORE (ledger 12) is verified MANUALLY by reverting `_append_journal` to
the buffered text-mode write and running `test_cross_process_append_no_interleave`
repeatedly — it must fail (corrupt JSON / count mismatch). A concurrency negative
is probabilistic, so a single green of the reverted code is insufficient; the
N=5-green of the fixed code is only meaningful because the same config reliably
fails the revert.
"""

import json
import pathlib
import subprocess
import sys

from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
from vibe_cognition.cognition.storage import CognitionStorage

_REPO = pathlib.Path(__file__).resolve().parents[1]
_JIO = _REPO / "src" / "vibe_cognition" / "cognition" / "journal_io.py"
_HOOK = _REPO / "hooks" / "post-commit.py"

# Path-loads journal_io (as the hook does) and appends N large records.
_APPENDER = """
import sys, json, importlib.util
spec = importlib.util.spec_from_file_location("jio", sys.argv[1])
jio = importlib.util.module_from_spec(spec); spec.loader.exec_module(jio)
journal, worker, n, size = sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
pad = "X" * size
for i in range(n):
    jio.append_journal_line(journal, json.dumps({"w": worker, "i": i, "pad": pad}))
"""


def test_cross_process_append_no_interleave(tmp_path):
    """C-1: concurrent large appends from real processes must not interleave/lose."""
    journal = tmp_path / "journal.jsonl"
    nproc, nrec, size = 4, 60, 70_000  # 70 KiB records >> any write buffer

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _APPENDER, str(_JIO), str(journal), str(w), str(nrec), str(size)],
            stderr=subprocess.PIPE,
        )
        for w in range(nproc)
    ]
    worker_failures = []
    for w, p in enumerate(procs):
        _, err = p.communicate(timeout=180)
        if p.returncode != 0:
            worker_failures.append(f"worker {w} exited {p.returncode}: {err.decode(errors='replace')}")

    # ledger 6: a dead worker must fail LOUD as its own labeled assertion, not
    # surface later as a count mismatch.
    assert worker_failures == [], f"worker process(es) failed: {worker_failures}"

    lines = journal.read_bytes().decode("utf-8").splitlines()
    expected = nproc * nrec
    assert len(lines) == expected, f"line count {len(lines)} != {expected} (interleave/loss)"

    seen = set()
    for line in lines:
        obj = json.loads(line)  # interleaving corrupts JSON -> raises here
        seen.add((obj["w"], obj["i"]))
    assert len(seen) == expected, "missing/duplicated records after concurrent append"


def _ids(store):
    """Synced set of node ids (snapshot() triggers catch-up)."""
    return {n["id"] for n in store.snapshot()["nodes"]}


def _build_journal_for(tmp_path, summaries):
    """Build a real journal (via a throwaway store) for the given node summaries."""
    src = tmp_path / "src_store"
    s = CognitionStorage(src)
    for i, summ in enumerate(summaries):
        s.add_node(
            CognitionNode(
                id=f"node{i:04d}", type=CognitionNodeType.DISCOVERY, summary=summ, detail=summ * 4,
                context=[], references=[], severity=None,
                timestamp="2026-06-11T00:00:00+00:00", author="t",
            )
        )
    return (src / "journal.jsonl").read_bytes()


def test_identity_check_detects_same_or_larger_replacement(tmp_path):
    """C-3: a replacement (different first line, >= size) must trigger a rebuild,
    not a stale-offset replay."""
    store = CognitionStorage(tmp_path)
    store.add_node(
        CognitionNode(
            id="orig0001", type=CognitionNodeType.DISCOVERY, summary="original", detail="original detail",
            context=[], references=[], severity=None,
            timestamp="2026-06-11T00:00:00+00:00", author="t",
        )
    )
    assert "orig0001" in _ids(store)

    # Replace the journal with unrelated, larger content (different first line).
    replacement = _build_journal_for(tmp_path, [f"replacement-{i}" for i in range(5)])
    assert len(replacement) >= (tmp_path / "journal.jsonl").stat().st_size
    (tmp_path / "journal.jsonl").write_bytes(replacement)

    ids = _ids(store)  # triggers catch-up -> identity mismatch -> rebuild
    assert "orig0001" not in ids, "stale-offset replay: original survived a replacement"
    assert "node0000" in ids and "node0004" in ids, "replacement content not hydrated"


def test_append_after_replacement_converges(tmp_path):
    """Composition (ledger 11): the C-1 atomic append and the C-3 identity check
    must compose — an append issued AFTER a replacement rebuilds first, then
    lands on the replaced journal, and the store converges."""
    store = CognitionStorage(tmp_path)
    store.add_node(
        CognitionNode(
            id="before01", type=CognitionNodeType.DISCOVERY, summary="before", detail="d",
            context=[], references=[], severity=None,
            timestamp="2026-06-11T00:00:00+00:00", author="t",
        )
    )
    # Replace the journal (C-3) ...
    (tmp_path / "journal.jsonl").write_bytes(_build_journal_for(tmp_path, ["r0", "r1", "r2"]))
    # ... then a new write: add_node catches up (rebuild) THEN appends (C-1).
    store.add_node(
        CognitionNode(
            id="after001", type=CognitionNodeType.DISCOVERY, summary="after", detail="d",
            context=[], references=[], severity=None,
            timestamp="2026-06-11T00:00:01+00:00", author="t",
        )
    )
    ids = _ids(store)
    assert "before01" not in ids, "replaced-away node survived"
    assert {"node0000", "node0002", "after001"} <= ids, "replacement + post-rebuild append lost"


def test_post_commit_hook_imports_only_stdlib():
    """H-2 contract: importing the hook must not pull any heavy third-party module
    (it must run against a possibly-bare venv)."""
    probe = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('pch', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "heavy = sorted(x for x in "
        "('networkx','pydantic','chromadb','torch','numpy','sentence_transformers') "
        "if x in sys.modules)\n"
        "print(','.join(heavy))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe, str(_HOOK)],
        capture_output=True, text=True, check=True, timeout=60,
    )
    assert out.stdout.strip() == "", f"hook pulled heavy modules at import: {out.stdout!r}"
