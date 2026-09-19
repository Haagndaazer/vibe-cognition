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

import ast
import json
import pathlib
import subprocess
import sys

from tests.conftest import own_shard
from vibe_cognition.cognition import journal_io
from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
from vibe_cognition.cognition.storage import CognitionStorage

_REPO = pathlib.Path(__file__).resolve().parents[1]
_JIO = _REPO / "src" / "vibe_cognition" / "cognition" / "journal_io.py"

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
    try:
        for w, p in enumerate(procs):
            _, err = p.communicate(timeout=180)
            if p.returncode != 0:
                worker_failures.append(
                    f"worker {w} exited {p.returncode}: {err.decode(errors='replace')}"
                )
    finally:
        for p in procs:
            if p.poll() is None:  # leaked (e.g. timeout) — don't orphan it
                p.kill()

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
    return own_shard(src).read_bytes()


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
    assert len(replacement) >= own_shard(tmp_path).stat().st_size
    own_shard(tmp_path).write_bytes(replacement)

    ids = _ids(store)  # triggers catch-up -> identity mismatch -> rebuild
    assert "node0000" in ids and "node0004" in ids, "replacement content not hydrated"
    # WP-Append-Only-Replay: the original used to be DROPPED here. A replacement is a
    # file-level event, so it no longer destroys what we already knew; the C-3 check
    # this test guards is still what detects the replacement and hydrates its content.
    assert "orig0001" in ids, "a replacement destroyed a live node"


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
    own_shard(tmp_path).write_bytes(_build_journal_for(tmp_path, ["r0", "r1", "r2"]))
    # ... then a new write: add_node catches up (rebuild) THEN appends (C-1).
    store.add_node(
        CognitionNode(
            id="after001", type=CognitionNodeType.DISCOVERY, summary="after", detail="d",
            context=[], references=[], severity=None,
            timestamp="2026-06-11T00:00:01+00:00", author="t",
        )
    )
    ids = _ids(store)
    assert {"node0000", "node0002", "after001"} <= ids, "replacement + post-rebuild append lost"
    # WP-Append-Only-Replay: previously asserted the replaced-away node was gone.
    assert "before01" in ids, "a replacement destroyed a live node"


def _node(node_id, summary, detail):
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DISCOVERY, summary=summary, detail=detail,
        context=[], references=[], severity=None,
        timestamp="2026-06-11T00:00:00+00:00", author="t",
    )


def test_identity_detects_same_first_line_divergent_replacement(tmp_path):
    """C-3 / CR-1: a replacement that PRESERVES line 1 (the git pull/merge case —
    an append-only journal always shares its first line) must STILL be detected.
    A first-line-only check would evade it; the prefix hash catches it."""
    store = CognitionStorage(tmp_path)
    common = _node("common00", "shared", "shared detail")
    store.add_node(common)
    store.add_node(_node("localonly", "local", "local-only tail"))
    assert _ids(store) == {"common00", "localonly"}  # offset now past both lines

    journal = own_shard(tmp_path)
    shared_prefix = b"".join(journal.read_bytes().splitlines(keepends=True)[:2])

    # Build a divergent replacement that begins with the IDENTICAL leading lines
    # (the shard start and common00) but then carries unrelated, larger remote content.
    repl_dir = tmp_path / "repl"
    s2 = CognitionStorage(repl_dir)
    s2.add_node(_node("remoteAA", "remoteA", "remote detail A" * 50))
    s2.add_node(_node("remoteBB", "remoteB", "remote detail B" * 50))
    remote_lines = own_shard(repl_dir).read_bytes().splitlines(keepends=True)[1:]
    replacement = shared_prefix + b"".join(remote_lines)

    # The leading lines really are identical (so a first-line check WOULD pass).
    assert replacement.startswith(shared_prefix)
    journal.write_bytes(replacement)

    ids = _ids(store)  # catch-up -> prefix mismatch -> rehydrate
    assert {"common00", "remoteAA", "remoteBB"} <= ids, "divergent replacement not hydrated"
    # WP-Append-Only-Replay: the point of this test is that the prefix hash DETECTS a
    # same-first-line replacement (a first-line-only check would miss it) and hydrates
    # the divergent content. It used to also assert the local-only node was dropped;
    # that half was the destroy-on-shrink behaviour, and the node now survives.
    assert "localonly" in ids, "a divergent replacement destroyed a live node"


def test_journal_io_imports_only_stdlib():
    """H-2 contract, bound by AST (not by what's installed): journal_io.py must
    import nothing outside the standard library — forward-compat posture even
    though the server (inside the full venv) is now its only caller. (Adding
    e.g. `import networkx` to journal_io fails THIS test even if the venv has
    networkx — the import-time probe below would stay green because journal_io
    loads lazily.)"""
    tree = ast.parse(_JIO.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    nonstd = {m for m in imported if m not in sys.stdlib_module_names}
    assert nonstd == set(), f"journal_io.py imports non-stdlib modules: {nonstd}"


def test_snapshot_journal_copies_consistently(tmp_path):
    """snapshot_journal copies the journal byte-for-byte (CR-4: the manager flush
    reads via this so it never commits a torn mid-append tail)."""
    src = tmp_path / "journal.jsonl"
    for i in range(5):
        journal_io.append_journal_line(src, json.dumps({"i": i}))
    dst = tmp_path / "snap.jsonl"
    journal_io.snapshot_journal(src, dst)
    assert dst.read_bytes() == src.read_bytes()
    assert [json.loads(line)["i"] for line in dst.read_bytes().decode("utf-8").splitlines()] == list(range(5))


def test_append_fallback_writes_when_lock_unavailable(tmp_path, monkeypatch):
    """Cover the Windows-only fallback branch: with the lock forced unavailable,
    the record is still written (never dropped)."""
    monkeypatch.setattr(journal_io, "_acquire", lambda fd: False)
    journal = tmp_path / "journal.jsonl"
    journal_io.append_journal_line(journal, json.dumps({"x": 1}))
    assert json.loads(journal.read_bytes().decode("utf-8").splitlines()[0]) == {"x": 1}
