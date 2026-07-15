#!/usr/bin/env python3
"""Gate C1 Part B measurement harness (WP-C1-harness, brief doc:d404a39edcca).

Stdlib + project imports only. NOT packaged (pyproject.toml's
``[tool.hatch.build.targets.wheel] packages`` is ``["src/vibe_cognition"]`` —
``scripts/`` is outside the wheel) and NOT a pytest test — a runnable report
generator, not a gate itself. Two modes:

  synthetic (default): builds a ~1,000-node fixture (10 persons, 100 tasks,
    the rest mixed entity types) in a temp dir and runs:
      B2 — replay wall time
      B3 — prime cost decomposition (onboarding-detection share)
      B4 — embeds-per-operation (register_person / update_person /
           add_task-with-assignment / update_task assignment change)
      B5 — cross-instance convergence (two in-process CognitionStorage on one
           journal dir, no multiprocessing)

  --journal-dir PATH: A1's distinct-SET arithmetic check against a REAL
    .cognition journal (Vince runs this against production data, per the
    brief: "manager runs a script, writes none").

Usage:
    uv run python scripts/bench_wave1.py
    uv run python scripts/bench_wave1.py --journal-dir /path/to/.cognition
"""

import argparse
import json
import logging
import random
import statistics
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from vibe_cognition.cognition.models import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    generate_node_id,
)
from vibe_cognition.cognition.prime import PrimeConfig, generate_prime
from vibe_cognition.cognition.storage import CognitionStorage
from vibe_cognition.embeddings import ChromaDBStorage
from vibe_cognition.tools.cognition_tools import _update_task, register_cognition_tools
from vibe_cognition.tools.project_registry import build_registry

SEED = 1337
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)


def _ts(offset_seconds: int) -> str:
    """Deterministic, monotonically increasing timestamp — never datetime.now()
    (B1 requires a reproducible fixture across runs, including the node ids
    minted from these timestamps)."""
    return (BASE_TS + timedelta(seconds=offset_seconds)).isoformat()


# ── B1: synthetic fixture ────────────────────────────────────────────────────

_MIXED_TYPES = [
    CognitionNodeType.DECISION,
    CognitionNodeType.FAIL,
    CognitionNodeType.DISCOVERY,
    CognitionNodeType.ASSUMPTION,
    CognitionNodeType.CONSTRAINT,
    CognitionNodeType.INCIDENT,
    CognitionNodeType.PATTERN,
    CognitionNodeType.EPISODE,
    CognitionNodeType.DOCUMENT,
    CognitionNodeType.WORKFLOW,
]
_SEVERITIES = [None, "low", "normal", "high", "critical"]
_TASK_SEVERITIES = ["critical", "high", "normal", "low"]
_TASK_STATUSES = ["open", "open", "open", "in_progress", "blocked"]


def build_synthetic_fixture(
    cognition_dir: Path, *, total_nodes: int = 1000, n_persons: int = 10, n_tasks: int = 100
) -> dict[str, Any]:
    """B1: ~1,000 mixed nodes, 10 persons, 100 tasks (some assigned — requires
    WP-TC8's assigned_to on the task metadata shape). Built directly via
    ``storage.add_node``/``add_edge`` — no tool layer, no embeddings backend
    (B2/B3 are chromadb-free, per the brief). Deterministic: fixed seed +
    fixed base timestamp with monotonic per-node offsets, so re-runs are
    byte-for-byte reproducible.
    """
    rng = random.Random(SEED)
    storage = CognitionStorage(cognition_dir)
    t = 0

    person_emails: list[str] = []
    for i in range(n_persons):
        email = f"person{i}@bench.local"
        person_emails.append(email)
        who = {"name": f"Bench Person {i}", "email": email}
        storage.add_node(CognitionNode(
            id=generate_node_id("person", email, _ts(t)),
            type=CognitionNodeType.PERSON,
            summary=f"Bench Person {i} — engineer",
            detail="synthetic fixture node",
            context=[], references=[],
            timestamp=_ts(t), author="bench",
            metadata={
                "person": {
                    "email": email, "name": f"Bench Person {i}", "role": "engineer",
                    "seniority": rng.choice(["owner", "senior", "mid", "junior"]),
                    "reports_to_email": None,
                },
                "profile_history": [], "recorded_by": who, "from_agent": False,
            },
        ))
        t += 1

    task_ids: list[str] = []
    assigned_task_ids: list[str] = []
    n_epics = max(1, n_tasks // 20)
    epics: list[str] = []
    for i in range(n_epics):
        who = {"name": "bench", "email": person_emails[0]}
        tid = generate_node_id("task", f"epic {i}", _ts(t))
        storage.add_node(CognitionNode(
            id=tid, type=CognitionNodeType.TASK, summary=f"epic {i}",
            detail="synthetic epic", context=[], references=[],
            severity=rng.choice(_TASK_SEVERITIES), timestamp=_ts(t), author="bench",
            metadata={
                "status": "open", "created_by": who, "owner": None, "parent_id": None,
                "transitions": [{"status": "open", "at": _ts(t), "by": who}],
            },
        ))
        epics.append(tid)
        task_ids.append(tid)
        t += 1

    for i in range(n_tasks - n_epics):
        creator_email = rng.choice(person_emails)
        who = {"name": "bench", "email": creator_email}
        parent = rng.choice(epics) if rng.random() < 0.3 else None
        assignee = rng.choice(person_emails) if rng.random() < 0.4 else None

        meta: dict[str, Any] = {
            "status": rng.choice(_TASK_STATUSES),
            "created_by": who, "owner": None, "parent_id": parent,
            "transitions": [{"status": "open", "at": _ts(t), "by": who}],
        }
        if assignee:
            meta["assigned_to"] = assignee
            meta["assignments"] = [{"to": assignee, "at": _ts(t), "by": who}]

        tid = generate_node_id("task", f"task {i}", _ts(t))
        storage.add_node(CognitionNode(
            id=tid, type=CognitionNodeType.TASK, summary=f"synthetic task {i}",
            detail="synthetic fixture task", context=[], references=[],
            severity=rng.choice(_TASK_SEVERITIES), timestamp=_ts(t), author="bench",
            metadata=meta,
        ))
        task_ids.append(tid)
        if assignee:
            assigned_task_ids.append(tid)
        if parent:
            storage.add_edge(CognitionEdge(
                from_id=tid, to_id=parent, edge_type=CognitionEdgeType.PART_OF,
                timestamp=_ts(t), source="task-parent",
            ))
        t += 1

    n_mixed = total_nodes - n_persons - n_tasks
    for i in range(n_mixed):
        ntype = _MIXED_TYPES[i % len(_MIXED_TYPES)]
        nid = generate_node_id(ntype.value, f"{ntype.value} {i}", _ts(t))
        storage.add_node(CognitionNode(
            id=nid, type=ntype, summary=f"synthetic {ntype.value} {i}",
            detail="synthetic fixture detail", context=["bench"], references=[],
            severity=rng.choice(_SEVERITIES), timestamp=_ts(t), author="bench",
        ))
        t += 1

    stats = storage.get_statistics()
    return {
        "person_emails": person_emails,
        "task_ids": task_ids,
        "assigned_task_ids": assigned_task_ids,
        "stats": stats,
    }


# ── B2: replay wall time ─────────────────────────────────────────────────────


def bench_b2_replay_wall_time(cognition_dir: Path) -> dict[str, Any]:
    """Time a FRESH CognitionStorage construction against an already-built
    fixture — this IS a full replay from offset 0 (a cold process opening an
    existing journal). Soft regression indicator (<2s on a dev machine), not
    a hard gate."""
    start = time.perf_counter()
    fresh = CognitionStorage(cognition_dir)
    elapsed = time.perf_counter() - start
    stats = fresh.get_statistics()
    return {
        "replay_wall_time_s": elapsed,
        "nodes": stats["nodes"], "edges": stats["edges"],
        "pass_soft": elapsed < 2.0,
    }


# ── B3: prime cost decomposition ─────────────────────────────────────────────


def bench_b3_prime_cost(cognition_dir: Path, current_email: str, n_runs: int = 10) -> dict[str, Any]:
    """generate_prime wall time; onboarding-detection share isolated via
    prime_onboard on/off (10-run median each — prime.py:419's early return is
    the ONE branch point this A/B toggles). PASS: detection < 10% of prime
    time (one O(total-nodes) person scan + one file read, per the brief)."""
    storage = CognitionStorage(cognition_dir)

    on_times = []
    for _ in range(n_runs):
        start = time.perf_counter()
        generate_prime(storage, PrimeConfig(prime_onboard=True), current_email=current_email)
        on_times.append(time.perf_counter() - start)

    off_times = []
    for _ in range(n_runs):
        start = time.perf_counter()
        generate_prime(storage, PrimeConfig(prime_onboard=False), current_email=current_email)
        off_times.append(time.perf_counter() - start)

    on_median = statistics.median(on_times)
    off_median = statistics.median(off_times)
    detection_delta = on_median - off_median
    detection_share = (detection_delta / on_median) if on_median > 0 else 0.0

    return {
        "n_runs": n_runs,
        "prime_onboard_true_median_s": on_median,
        "prime_onboard_false_median_s": off_median,
        "detection_delta_s": detection_delta,
        "detection_share": detection_share,
        # At 1k nodes / 10 persons, absolute times are ~10ms and the on/off
        # delta is ~1ms — comparable to OS scheduling jitter on a dev machine.
        # Raw per-run times are included (not just the median) so a PASS=False
        # here can be told apart from genuine regression: across repeated
        # invocations this share has been observed to range roughly -11% to
        # +17%, straddling the 10% gate — see the raw lists before treating a
        # single run's verdict as final.
        "prime_onboard_true_times_s": on_times,
        "prime_onboard_false_times_s": off_times,
        "pass": detection_share < 0.10,
    }


# ── shared ctx/lifespan scaffold (B4/B5) ─────────────────────────────────────
#
# A standalone script cannot depend on pytest fixture machinery (calling a
# @pytest.fixture-decorated function directly outside pytest's DI is not
# supported), so this reuses the PATTERN from tests/conftest.py's build_lc
# (90-134), make_ctx (137-152), and _MockMcp (62-78) inline, per the brief's
# seam guidance — not a literal import.


class _CountingGen:
    """Counting embedder: N calls to .generate() so B4 can assert exact
    embed counts per operation. Mirrors tests/conftest.py's _TextKeyedGen
    shape (generate/generate_query_embedding), minus the marker-word logic —
    B4 only cares about the CALL COUNT, not the vector content."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, text: str, input_type: str = "document") -> list[float]:
        self.calls += 1
        return [1.0, 0.0, 0.0]

    def generate_query_embedding(self, text: str) -> list[float]:
        return self.generate(text, input_type="query")


class _MockMcp:
    """Minimal MCP stub: .tool() captures registered closures by name.
    Mirrors tests/conftest.py:62 _MockMcp, including the async-unwrap (every
    dispatch_tool-registered tool is an async def routed to an executor —
    tools/dispatch.py:92 — so a plain sync call here would just return an
    un-awaited coroutine without this)."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):
        import asyncio
        import functools
        import inspect

        def decorator(fn):
            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                def sync_shim(*args, **kwargs):
                    return asyncio.run(fn(*args, **kwargs))
                self.tools[fn.__name__] = sync_shim
            else:
                self.tools[fn.__name__] = fn
            return fn
        return decorator


def _build_lc(cognition_dir: Path, chroma_dir: Path, generator: Any, *, embeddings_ready: bool) -> dict[str, Any]:
    """Mirrors tests/conftest.py:90-134 build_lc's factory body. Takes explicit
    cognition_dir/chroma_dir (rather than deriving both from one tmp_path like
    conftest does) so B5 can point TWO separate lc dicts at the SAME
    cognition_dir while keeping their chroma dirs apart."""
    cognition_dir.parent.mkdir(parents=True, exist_ok=True)
    cognition_storage = CognitionStorage(cognition_dir)
    chroma = ChromaDBStorage(
        persist_directory=chroma_dir, embedding_model="m", embedding_dimensions=3,
    )
    config = SimpleNamespace(
        embedding_model="m", embedding_dimensions=3,
        repo_path=cognition_dir.parent, effective_repo_name="bench",
    )
    registry = build_registry(
        home_path=cognition_dir.parent, home_tag="home",
        home_storage=cognition_storage, home_embeddings=chroma,
    )
    event = threading.Event()
    if embeddings_ready:
        event.set()
    return {
        "config": config,
        "cognition_storage": cognition_storage,
        "cognition_embedding_storage": chroma,
        "loaded_projects": registry,
        "embedding_generator": generator,
        "embedding_ready": event,
        "embedding_error": None,
    }


def _make_ctx(lc: dict[str, Any]) -> Any:
    """Mirrors tests/conftest.py:137-152 make_ctx."""
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=lc))


# ── B4: embeds-per-operation ─────────────────────────────────────────────────


def bench_b4_embeds_per_operation(tmp_path: Path) -> dict[str, Any]:
    """register_person==1; update_person==1; add_task-with-assignment==1;
    update_task assignment change==1 (no double re-embed). The first three go
    through the real ctx/mock_mcp tool-dispatch layer (they're ctx-based
    cores); update_task uses its EXPLICIT-PARAM testable core directly (the
    counting generator passed straight in), per the brief's seam note."""
    counter = _CountingGen()
    lc = _build_lc(
        tmp_path / ".cognition", tmp_path / "chromadb", counter, embeddings_ready=True,
    )
    try:
        ctx = _make_ctx(lc)
        mock_mcp = _MockMcp()
        register_cognition_tools(mock_mcp)

        results: dict[str, int] = {}

        counter.calls = 0
        person = mock_mcp.tools["cognition_register_person"](
            ctx, name="Bench Person", role="engineer", seniority="mid",
            email="bencher@bench.local", from_agent=False,
        )
        assert "error" not in person, person
        results["register_person"] = counter.calls

        counter.calls = 0
        updated = mock_mcp.tools["cognition_update_person"](
            ctx, email_or_id="bencher@bench.local", role="staff engineer",
        )
        assert "error" not in updated, updated
        results["update_person"] = counter.calls

        counter.calls = 0
        task = mock_mcp.tools["cognition_add_task"](
            ctx, summary="bench task", detail="d", context="c",
            assigned_to_email="bencher@bench.local",
        )
        assert "error" not in task, task
        results["add_task_with_assignment"] = counter.calls

        counter.calls = 0
        storage: CognitionStorage = lc["cognition_storage"]
        chroma: ChromaDBStorage = lc["cognition_embedding_storage"]
        upd = _update_task(
            storage, chroma, counter,
            node_id=task["id"], embeddings_ready=True,
            assigned_to_email="second-bencher@bench.local",
        )
        assert "error" not in upd, upd
        results["update_task_assignment_change"] = counter.calls

        return {
            **results,
            "pass": all(v == 1 for v in results.values()),
        }
    finally:
        # Windows: chromadb's sqlite/HNSW file handles must be released before
        # the caller's TemporaryDirectory cleanup tries to rmtree this path,
        # else cleanup raises PermissionError (WinError 32).
        lc["cognition_embedding_storage"].close()


# ── B5: cross-instance convergence ───────────────────────────────────────────


def bench_b5_cross_instance_convergence(tmp_path: Path) -> dict[str, Any]:
    """Two IN-PROCESS CognitionStorage instances on one journal dir (no
    multiprocessing) — trigger is any public storage method under _synced(),
    which runs _catch_up() (storage.py:177-193). A registers a person +
    assigns a task (via the real tool layer); B (a SEPARATE Python object,
    same journal dir) reads both back with a plain storage.get_node() call
    (no tool layer needed for a read). B then updates the person; A's next
    call converges too. PASS: field-exact both directions.

    embeddings_ready=False for both — B5 is a storage-convergence test, not
    an embedding test (orthogonal to B4)."""
    shared_cognition_dir = tmp_path / "shared" / ".cognition"
    gen_a, gen_b = _CountingGen(), _CountingGen()
    lc_a = _build_lc(shared_cognition_dir, tmp_path / "chroma-a", gen_a, embeddings_ready=False)
    lc_b = _build_lc(shared_cognition_dir, tmp_path / "chroma-b", gen_b, embeddings_ready=False)
    try:
        ctx_a = _make_ctx(lc_a)
        mcp_a = _MockMcp()
        register_cognition_tools(mcp_a)

        person = mcp_a.tools["cognition_register_person"](
            ctx_a, name="Convergence Bench", role="engineer", seniority="mid",
            email="converge@bench.local", from_agent=False,
        )
        assert "error" not in person, person
        task = mcp_a.tools["cognition_add_task"](
            ctx_a, summary="convergence task", detail="d", context="c",
            assigned_to_email="converge@bench.local",
        )
        assert "error" not in task, task

        storage_b: CognitionStorage = lc_b["cognition_storage"]
        b_person = storage_b.get_node(person["id"])
        b_task = storage_b.get_node(task["id"])
        b_sees_person = (
            b_person is not None
            and b_person["metadata"]["person"]["email"] == "converge@bench.local"
        )
        b_sees_task = (
            b_task is not None
            and b_task["metadata"].get("assigned_to") == "converge@bench.local"
        )

        ctx_b = _make_ctx(lc_b)
        mcp_b = _MockMcp()
        register_cognition_tools(mcp_b)
        person_update = mcp_b.tools["cognition_update_person"](
            ctx_b, email_or_id="converge@bench.local", role="staff engineer",
        )
        assert "error" not in person_update, person_update

        storage_a: CognitionStorage = lc_a["cognition_storage"]
        a_person = storage_a.get_node(person["id"])
        a_sees_update = (
            a_person is not None
            and a_person["metadata"]["person"]["role"] == "staff engineer"
        )

        return {
            "b_sees_person_after_a_register": b_sees_person,
            "b_sees_task_assignment_after_a_add": b_sees_task,
            "a_sees_person_role_update_after_b_update": a_sees_update,
            "pass": b_sees_person and b_sees_task and a_sees_update,
        }
    finally:
        # Windows: chromadb's sqlite/HNSW file handles must be released before
        # the caller's TemporaryDirectory cleanup tries to rmtree this path,
        # else cleanup raises PermissionError (WinError 32).
        lc_a["cognition_embedding_storage"].close()
        lc_b["cognition_embedding_storage"].close()


# ── A1: journal replay arithmetic (--journal-dir mode) ───────────────────────


class _WarningCapture(logging.Handler):
    """Captures WARNING+ records emitted during a real replay — storage.py's
    _catch_up logs "Skipping malformed journal line" and "Dropped journal
    entry during replay" via logger.warning; both are replay errors for A1's
    "zero replay errors" requirement."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


def bench_a1_journal_arithmetic(journal_dir: Path) -> dict[str, Any]:
    """A1: distinct-SET arithmetic invariant (NOT raw line counts — add_edge
    lines aren't deduped at the tool layer while graph edge-add IS idempotent
    by (from, to, type) triple, storage.py add_edge). Independently parses the
    raw journal (own JSON parse, no CognitionStorage involved) to compute
    expected_nodes = added ids - removed ids, expected_edges = added triples -
    removed triples - triples incident to a removed node (remove_node cascades
    ALL incident edges via networkx's remove_node, storage.py:328, WITHOUT a
    separate remove_edge journal line for each — confirmed empirically: a
    hand-built journal with an add_edge whose target is later remove_node'd
    replayed to 0 edges while the naive added-minus-removed-edges arithmetic
    said 1, a false A1 FAIL); then constructs a REAL CognitionStorage against
    the same dir (an actual full replay, capturing any replay warnings) and
    compares against get_statistics()."""
    journal_path = journal_dir / "journal.jsonl"
    added_nodes: set[str] = set()
    removed_nodes: set[str] = set()
    added_edges: set[tuple[str, str, str]] = set()
    removed_edges: set[tuple[str, str, str]] = set()
    malformed_lines = 0
    total_lines = 0

    if journal_path.exists():
        with journal_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total_lines += 1
                try:
                    entry = json.loads(line)
                    action = entry["action"]
                    data = entry["data"]
                except (json.JSONDecodeError, KeyError):
                    malformed_lines += 1
                    continue
                if action == "add_node":
                    added_nodes.add(data["id"])
                elif action == "remove_node":
                    removed_nodes.add(data["id"])
                elif action == "add_edge":
                    added_edges.add((data["from_id"], data["to_id"], data["edge_type"]))
                elif action == "remove_edge":
                    removed_edges.add((data["from_id"], data["to_id"], data["edge_type"]))
                # update_node doesn't change set membership — not counted.

    expected_nodes = len(added_nodes - removed_nodes)
    live_edges = added_edges - removed_edges
    # remove_node cascades ALL incident edges in-memory without journaling a
    # separate remove_edge per edge — an edge touching a removed node (on
    # either side; edges are directed but cascade removal isn't) is gone too.
    live_edges = {
        (f, to, et) for (f, to, et) in live_edges
        if f not in removed_nodes and to not in removed_nodes
    }
    expected_edges = len(live_edges)

    storage_logger = logging.getLogger("vibe_cognition.cognition.storage")
    capture = _WarningCapture()
    storage_logger.addHandler(capture)
    prior_level = storage_logger.level
    storage_logger.setLevel(logging.WARNING)
    try:
        start = time.perf_counter()
        storage = CognitionStorage(journal_dir)
        elapsed = time.perf_counter() - start
    finally:
        storage_logger.removeHandler(capture)
        storage_logger.setLevel(prior_level)

    stats = storage.get_statistics()
    actual_nodes = stats["nodes"]
    actual_edges = stats["edges"]
    nodes_match = expected_nodes == actual_nodes
    edges_match = expected_edges == actual_edges

    return {
        "journal_dir": str(journal_dir),
        "total_journal_lines": total_lines,
        "malformed_lines_in_raw_parse": malformed_lines,
        "replay_wall_time_s": elapsed,
        "replay_warning_count": len(capture.records),
        "replay_warnings": capture.records,
        "expected_nodes": expected_nodes,
        "actual_nodes": actual_nodes,
        "nodes_match": nodes_match,
        "expected_edges": expected_edges,
        "actual_edges": actual_edges,
        "edges_match": edges_match,
        "pass": nodes_match and edges_match and len(capture.records) == 0,
    }


# ── report printing + CLI ────────────────────────────────────────────────────


def _print_section(title: str, d: dict[str, Any]) -> None:
    print(f"\n=== {title} ===")
    for k, v in d.items():
        if k == "replay_warnings":
            for w in v:
                print(f"    ! {w}")
            continue
        if isinstance(v, float):
            print(f"  {k}: {v:.4f}")
        else:
            print(f"  {k}: {v}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bench_wave1.py",
        description=(
            "Gate C1 Part B measurement harness. Default: synthetic 1k-node "
            "fixture, runs B2-B5. --journal-dir: A1's distinct-SET arithmetic "
            "check against a REAL .cognition journal."
        ),
    )
    parser.add_argument(
        "--journal-dir", type=Path, default=None,
        help=(
            "Path to a REAL .cognition directory (containing journal.jsonl) to "
            "run A1's replay-arithmetic check against, instead of the synthetic "
            "fixture."
        ),
    )
    args = parser.parse_args(argv)

    report: dict[str, Any] = {}

    if args.journal_dir is not None:
        result = bench_a1_journal_arithmetic(args.journal_dir)
        report["A1"] = result
        _print_section("A1: journal replay arithmetic", result)
    else:
        with tempfile.TemporaryDirectory(prefix="vibe-cognition-bench-") as tmp:
            tmp_path = Path(tmp)
            fixture_dir = tmp_path / "fixture" / ".cognition"

            handles = build_synthetic_fixture(fixture_dir)
            report["B1"] = {"fixture_dir": str(fixture_dir), "stats": handles["stats"]}
            _print_section("B1: synthetic fixture built", report["B1"])

            report["B2"] = bench_b2_replay_wall_time(fixture_dir)
            _print_section("B2: replay wall time", report["B2"])

            report["B3"] = bench_b3_prime_cost(fixture_dir, "unregistered-bencher@bench.local")
            _print_section("B3: prime cost decomposition", report["B3"])

            report["B4"] = bench_b4_embeds_per_operation(tmp_path / "b4")
            _print_section("B4: embeds-per-operation", report["B4"])

            report["B5"] = bench_b5_cross_instance_convergence(tmp_path / "b5")
            _print_section("B5: cross-instance convergence", report["B5"])

    overall_pass = all(
        section.get("pass", section.get("pass_soft", True))
        for section in report.values()
    )

    print("\n=== Full report (JSON) ===")
    print(json.dumps(report, indent=2, default=str))
    print(f"\nOverall: {'PASS' if overall_pass else 'CHECK ABOVE — one or more sections failed'}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
