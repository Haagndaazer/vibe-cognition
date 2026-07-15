#!/usr/bin/env python3
"""Gate C2 Part B measurement harness (WP-C2-harness, brief doc:980816e8df8b).

Sibling to bench_wave1.py (Gate C1) -- same conventions: stdlib + project
imports only, NOT packaged (scripts/ is outside pyproject.toml's wheel
target), NOT a pytest test (a runnable report generator, not a gate itself),
deterministic/seeded fixtures, no product code changes. C1-harness precedent:
scripts/-only changes merge without a release (ed5d53a).

Part A (Vince, weighted-search/prime latency sweeps against these modes) vs
Part B (this file, the measurement harness itself) is a single-implementer
pipeline split, per the C2 brief -- splitting further was judged pointless.

Modes (any combination via flags; default runs --search + --prime together
against ONE shared fixture):

  --search         Times _search_cognition (WP-TC9's weighted core) over
                    --reps identical calls with an injected fixed-vector
                    query embedder (model load must never pollute timings).
                    Reports p50/p95 + a person-scan-count assertion: the
                    real cost driver A1 exists to catch is get_nodes_by_type's
                    O(total_nodes) whole-graph PERSON scan, built ONCE per
                    top-level search call (never once per adaptive-widening
                    round) -- scans_equal_calls asserts call_count == reps.

  --prime          Times generate_prime for --email over --reps identical
                    calls. p50/p95 + output byte size + per-section row
                    counts (parsed from the '## Header' / '- row' output
                    shape, same idea as TC16's tests/test_tc16_role_prime.py
                    _section helper, reimplemented here since scripts/
                    cannot import a tests/ module).

  --multi-project  Two independent temp graphs (home + one foreign entry,
                    added directly via registry.add_foreign -- bypassing
                    cognition_load_project's git/path validation, the same
                    shortcut test_xp1_registry.py's _make_entry takes), real
                    cognition_search(project="*") tool dispatch via the
                    ctx/mock_mcp scaffold (mirrors bench_wave1's B4/B5
                    pattern) -- fan-out timing over --reps reps. Builds TWO
                    --nodes-sized graphs (double the single-fixture cost);
                    size --nodes down for multi-project runs if needed (no
                    silent capping here).

Fixture knobs: --nodes (total node count, drives the 1k/5k/10k sweep) and
--persons (roster size, drives the 0/10/100 sweep) vary INDEPENDENTLY of each
other -- Part A's A1 scenario sweeps BOTH axes, since the real cost driver is
whole-graph node count, not roster size. Generated nodes get stamped
recorded_by/created_by authorship (roster emails plus a fixed pool of
unregistered "ghost" emails, so both human:registered and human:unregistered
search-weight bases appear), ~30% from_agent true, and a task population with
claimed_by + transitions including BOTH fresh and stale-aged in_progress
claims via timestamp backdating anchored to real wall-clock now() (never
time.sleep -- an "aged" claim is simply stamped with an already-past
timestamp at fixture-build time; day-offsets are seed-deterministic even
though the absolute anchor shifts run to run).

add_person_node() is exposed at module level (not just used internally by
build_wave3_fixture) so Part A can mint additional/rewired person nodes for
A2's custom-topology scenarios (a 3-level reports_to_email chain, a manager
with 10 reports) without duplicating this boilerplate -- building that exact
topology is Part A's job, this just hands over the primitive.

Usage:
    uv run python scripts/bench_wave3.py
    uv run python scripts/bench_wave3.py --search --nodes 5000 --persons 100
    uv run python scripts/bench_wave3.py --prime --nodes 1000 --persons 10 --email person3@bench.local
    uv run python scripts/bench_wave3.py --multi-project --nodes 500
    uv run python scripts/bench_wave3.py --search --prime --nodes 10000 --persons 0 --reps 50
"""

import argparse
import json
import math
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

from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType, generate_node_id
from vibe_cognition.cognition.prime import PrimeConfig, generate_prime
from vibe_cognition.cognition.storage import CognitionStorage
from vibe_cognition.embeddings import ChromaDBStorage
from vibe_cognition.tools import cognition_tools
from vibe_cognition.tools.project_registry import ProjectEntry, build_registry

SEED = 4242
DIMS = 8
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)
UNREGISTERED_EXTRAS = [f"ghost{i}@bench.local" for i in range(5)]
_SENIORITIES = ["owner", "senior", "mid", "junior"]
_MIXED_TYPES = [
    CognitionNodeType.DECISION,
    CognitionNodeType.FAIL,
    CognitionNodeType.DISCOVERY,
    CognitionNodeType.ASSUMPTION,
    CognitionNodeType.CONSTRAINT,
    CognitionNodeType.INCIDENT,
    CognitionNodeType.PATTERN,
    CognitionNodeType.EPISODE,
]
_TASK_SEVERITIES = ["critical", "high", "normal", "low"]


def _ts(offset_seconds: int) -> str:
    """Deterministic, monotonically increasing node-creation timestamp --
    never datetime.now() -- so node ids and counts are reproducible across
    runs of the same (seed, nodes, persons). Distinct from claimed_at
    backdating below, which is deliberately anchored to real now() (staleness
    is inherently wall-clock-relative)."""
    return (BASE_TS + timedelta(seconds=offset_seconds)).isoformat()


def _random_unit_vector(rng: random.Random, dims: int = DIMS) -> list[float]:
    v = [rng.gauss(0.0, 1.0) for _ in range(dims)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


# ── person nodes ─────────────────────────────────────────────────────────────


def add_person_node(
    storage: CognitionStorage, idx: int, email: str, *,
    name: str | None = None, seniority: str = "mid", reports_to_email: str = "",
    timestamp: str,
) -> None:
    """Mint one PERSON node. Exposed at module level (see file docstring) so
    Part A can call it directly for custom reports_to_email topologies beyond
    what build_wave3_fixture's default flat (no-hierarchy) roster creates."""
    name = name or f"Bench Person {idx}"
    who = {"name": name, "email": email}
    storage.add_node(CognitionNode(
        id=generate_node_id("person", email, timestamp),
        type=CognitionNodeType.PERSON,
        summary=f"{name} -- engineer",
        detail="synthetic C2 fixture person",
        context=[], references=[],
        timestamp=timestamp, author="bench",
        metadata={
            "person": {
                "email": email, "name": name, "role": "engineer",
                "seniority": seniority, "reports_to_email": reports_to_email,
            },
            "profile_history": [], "recorded_by": who, "from_agent": False,
        },
    ))


# ── fixture builder ──────────────────────────────────────────────────────────


def _pick_author(rng: random.Random, person_emails: list[str]) -> str:
    """80% roster (when non-empty), else an unregistered "ghost" email -- so
    both human:<seniority> and human:unregistered search-weight bases appear
    in the fixture, per the C2 brief's "roster ± unregistered extras"."""
    if person_emails and rng.random() < 0.8:
        return rng.choice(person_emails)
    return rng.choice(UNREGISTERED_EXTRAS)


def _pick_task_claim(rng: random.Random) -> tuple[str, float | None]:
    """(status, backdate_days). backdate_days is None for a never-claimed
    "open" task; otherwise the in_progress claim's age in days -- 0-3 (fresh,
    well under PrimeConfig's default 7-day stale cutoff), 8-30 (stale), or
    0-10 for a blocked task (staleness is not evaluated for blocked rows)."""
    r = rng.random()
    if r < 0.50:
        return "open", None
    if r < 0.70:
        return "in_progress", rng.uniform(0, 3)
    if r < 0.85:
        return "in_progress", rng.uniform(8, 30)
    return "blocked", rng.uniform(0, 10)


def build_wave3_fixture(
    storage: CognitionStorage, *, embedding_storage: ChromaDBStorage | None = None,
    total_nodes: int = 1000, n_persons: int = 10, seed: int = SEED,
) -> dict[str, Any]:
    """Builds, directly via storage.add_node (no tool layer): n_persons PERSON
    nodes (seniority round-robin across the 4 tiers, no reports_to_email --
    Part A layers custom hierarchies on top via add_person_node), ~10% of
    total_nodes as TASK nodes (created_by stamped roster±ghost, claimed_by +
    transitions per _pick_task_claim including backdated stale/fresh
    in_progress claims), and the remainder as mixed entity types (recorded_by
    stamped roster±ghost, ~30% from_agent true).

    When embedding_storage is given, every task and mixed-entity node is
    ALSO upserted with a deterministic per-node random unit vector (seeded --
    reproducible node CONTENT is deterministic even though vector direction
    is arbitrary; latency, not ranking quality, is what this harness
    measures) so _search_cognition has real candidates to traverse. Omit for
    a chromadb-free prime-only run (mirrors bench_wave1's B2/B3 precedent).

    n_persons is clamped to total_nodes if the caller over-asks. Returns
    handles: person_emails, task_ids, stale_claimed_task_ids,
    fresh_claimed_task_ids, blocked_task_ids.
    """
    rng = random.Random(seed)
    n_persons = min(n_persons, total_nodes)
    n_tasks = max(1, int(total_nodes * 0.10)) if total_nodes > n_persons else 0
    n_tasks = min(n_tasks, max(0, total_nodes - n_persons))
    n_mixed = max(0, total_nodes - n_persons - n_tasks)
    t = 0

    person_emails: list[str] = []
    for i in range(n_persons):
        email = f"person{i}@bench.local"
        add_person_node(storage, i, email, seniority=_SENIORITIES[i % 4], timestamp=_ts(t))
        person_emails.append(email)
        t += 1

    task_ids: list[str] = []
    stale_claimed_task_ids: list[str] = []
    fresh_claimed_task_ids: list[str] = []
    blocked_task_ids: list[str] = []
    now = datetime.now(UTC)
    for i in range(n_tasks):
        creator_email = _pick_author(rng, person_emails)
        creator = {"name": "bench", "email": creator_email}
        status, backdate_days = _pick_task_claim(rng)
        transitions: list[dict[str, Any]] = [{"status": "open", "at": _ts(t), "by": creator}]
        claimant: dict[str, str] | None = None
        if backdate_days is not None:
            claimant_email = _pick_author(rng, person_emails)
            claimant = {"name": "bench", "email": claimant_email}
            claimed_at = (now - timedelta(days=backdate_days)).isoformat()
            transitions.append({"status": status, "at": claimed_at, "by": claimant})

        tid = generate_node_id("task", f"task {i}", _ts(t))
        summary = f"synthetic C2 task {i}"
        storage.add_node(CognitionNode(
            id=tid, type=CognitionNodeType.TASK, summary=summary,
            detail="synthetic C2 fixture task", context=[], references=[],
            severity=rng.choice(_TASK_SEVERITIES), timestamp=_ts(t), author="bench",
            metadata={
                "status": status, "created_by": creator, "claimed_by": claimant,
                "owner": None, "parent_id": None, "transitions": transitions,
            },
        ))
        task_ids.append(tid)
        if status == "blocked":
            blocked_task_ids.append(tid)
        elif status == "in_progress" and backdate_days is not None and backdate_days > 7:
            stale_claimed_task_ids.append(tid)
        elif status == "in_progress":
            fresh_claimed_task_ids.append(tid)
        if embedding_storage is not None:
            embedding_storage.upsert_embedding(
                tid, _random_unit_vector(rng), {"entity_type": "task", "summary": summary},
            )
        t += 1

    for i in range(n_mixed):
        ntype = _MIXED_TYPES[i % len(_MIXED_TYPES)]
        author_email = _pick_author(rng, person_emails)
        who = {"name": "bench", "email": author_email}
        from_agent = rng.random() < 0.30
        nid = generate_node_id(ntype.value, f"{ntype.value} {i}", _ts(t))
        summary = f"synthetic C2 {ntype.value} {i}"
        storage.add_node(CognitionNode(
            id=nid, type=ntype, summary=summary,
            detail="synthetic C2 fixture entity", context=["bench-c2"], references=[],
            timestamp=_ts(t), author="bench",
            metadata={"recorded_by": who, "from_agent": from_agent},
        ))
        if embedding_storage is not None:
            embedding_storage.upsert_embedding(
                nid, _random_unit_vector(rng),
                {"entity_type": ntype.value, "summary": summary, "from_agent": from_agent},
            )
        t += 1

    return {
        "person_emails": person_emails,
        "task_ids": task_ids,
        "stale_claimed_task_ids": stale_claimed_task_ids,
        "fresh_claimed_task_ids": fresh_claimed_task_ids,
        "blocked_task_ids": blocked_task_ids,
    }


# ── search benchmark ─────────────────────────────────────────────────────────


class _FixedGen:
    """Every query embeds to the SAME fixed vector -- model load must never
    pollute a latency measurement (mirrors tests/test_tc9_seniority_weighting.py's
    _FixedGen; document vectors here are real per-node random unit vectors
    upserted directly by build_wave3_fixture, not generated through this)."""

    def __init__(self, vec: list[float]) -> None:
        self._vec = vec

    def generate(self, text: str, input_type: str = "document") -> list[float]:
        return self._vec

    def generate_query_embedding(self, text: str) -> list[float]:
        return self._vec


def bench_search(
    storage: CognitionStorage, embed: ChromaDBStorage, *,
    n_reps: int = 20, limit: int = 10, query_vec: list[float], query_text: str,
) -> dict[str, Any]:
    """Times _search_cognition over n_reps identical calls. Also asserts the
    memo: cognition_tools._person_seniority_map is built ONCE per top-level
    search call (never once per adaptive-widening round) -- the exact
    O(total_nodes)-not-O(rounds) cost model A1 exists to verify. Scan-count
    via a monkeypatched counting wrapper around the module-level function,
    restored in `finally` (bench_wave1.py:491-503's _WarningCapture
    attach/detach precedent) -- fragile (private-name patching), acceptable
    ONLY in a scripts/ measuring device, never in tests."""
    gen = _FixedGen(query_vec)

    call_count = 0
    original = cognition_tools._person_seniority_map

    def _counting_wrapper(s: CognitionStorage) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return original(s)

    cognition_tools._person_seniority_map = _counting_wrapper  # type: ignore[attr-defined]
    times: list[float] = []
    last_result: dict[str, Any] | None = None
    try:
        for _ in range(n_reps):
            start = time.perf_counter()
            last_result = cognition_tools._search_cognition(
                storage, embed, gen, query_text, limit=limit,
            )
            times.append(time.perf_counter() - start)
    finally:
        cognition_tools._person_seniority_map = original  # type: ignore[attr-defined]

    times_sorted = sorted(times)
    p50 = statistics.median(times_sorted)
    p95_idx = min(len(times_sorted) - 1, int(len(times_sorted) * 0.95))
    p95 = times_sorted[p95_idx]
    scans_equal_calls = call_count == n_reps

    return {
        "n_reps": n_reps,
        "limit": limit,
        "p50_s": p50,
        "p95_s": p95,
        "person_scan_count": call_count,
        "scans_equal_calls": scans_equal_calls,
        "result_count": len(last_result["results"]) if last_result else 0,
        "total_found": last_result.get("total_found") if last_result else None,
        "pass": scans_equal_calls,
    }


# ── prime benchmark ──────────────────────────────────────────────────────────


def _section_row_counts(output: str) -> dict[str, int]:
    """'## Header' -> count of '- ' rows in its body, up to the next header.
    Reimplements the isolate-by-header idea from
    tests/test_tc16_role_prime.py's _section helper (scripts/ can't import
    tests/), generalized to count ALL sections in one pass."""
    counts: dict[str, int] = {}
    current: str | None = None
    for line in output.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            counts[current] = 0
        elif current is not None and line.startswith("- "):
            counts[current] += 1
    return counts


def bench_prime(storage: CognitionStorage, *, email: str, n_reps: int = 20) -> dict[str, Any]:
    """Times generate_prime for `email` over n_reps identical calls (default
    PrimeConfig -- personalization on). p50/p95 + output byte size +
    per-section row counts."""
    config = PrimeConfig()
    times: list[float] = []
    last_output = ""
    for _ in range(n_reps):
        start = time.perf_counter()
        last_output = generate_prime(storage, config, current_email=email)
        times.append(time.perf_counter() - start)

    times_sorted = sorted(times)
    p50 = statistics.median(times_sorted)
    p95_idx = min(len(times_sorted) - 1, int(len(times_sorted) * 0.95))
    p95 = times_sorted[p95_idx]

    return {
        "n_reps": n_reps,
        "email": email,
        "p50_s": p50,
        "p95_s": p95,
        "output_bytes": len(last_output.encode("utf-8")),
        "section_row_counts": _section_row_counts(last_output),
        "pass": True,
    }


# ── multi-project fan-out benchmark ──────────────────────────────────────────
#
# Reuses the ctx/mock_mcp scaffold pattern from bench_wave1.py:284-345
# (_MockMcp, _build_lc-equivalent) -- not a literal import (a standalone
# script cannot depend on pytest fixture DI), duplicated inline per that
# file's own documented convention.


class _MockMcp:
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


def _make_ctx(lc: dict[str, Any]) -> Any:
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=lc))


def bench_multi_project(
    tmp_path: Path, *, total_nodes: int = 500, n_persons: int = 5,
    n_reps: int = 10, limit: int = 10, query: str = "synthetic bench query", seed: int = SEED,
) -> dict[str, Any]:
    """Two independent --nodes-sized graphs (home + one foreign entry added
    directly via registry.add_foreign -- bypassing cognition_load_project's
    git/path validation, test_xp1_registry.py's _make_entry shortcut), real
    cognition_search(project="*") tool dispatch over n_reps reps -- fan-out
    timing, not per-project timing (mirrors how a real multi-project search
    is actually invoked)."""
    home_dir = tmp_path / "home"
    foreign_dir = tmp_path / "foreign"
    home_cognition = home_dir / ".cognition"
    foreign_cognition = foreign_dir / ".cognition"

    home_storage = CognitionStorage(home_cognition)
    home_chroma = ChromaDBStorage(
        persist_directory=home_dir / "chromadb", embedding_model="bench-m", embedding_dimensions=DIMS,
    )
    foreign_storage = CognitionStorage(foreign_cognition)
    foreign_chroma = ChromaDBStorage(
        persist_directory=foreign_dir / "chromadb", embedding_model="bench-m", embedding_dimensions=DIMS,
    )
    try:
        build_wave3_fixture(
            home_storage, embedding_storage=home_chroma,
            total_nodes=total_nodes, n_persons=n_persons, seed=seed,
        )
        build_wave3_fixture(
            foreign_storage, embedding_storage=foreign_chroma,
            total_nodes=total_nodes, n_persons=n_persons, seed=seed + 1,
        )

        config = SimpleNamespace(
            embedding_model="bench-m", embedding_dimensions=DIMS,
            repo_path=home_cognition.parent, effective_repo_name="bench-home",
        )
        registry = build_registry(
            home_path=home_cognition.parent, home_tag="home",
            home_storage=home_storage, home_embeddings=home_chroma,
        )
        registry.add_foreign(ProjectEntry(
            path=foreign_cognition.parent, tag="foreign", storage=foreign_storage,
            embeddings=foreign_chroma, pinned=False, model_guard="match",
        ))
        event = threading.Event()
        event.set()
        lc = {
            "config": config,
            "cognition_storage": home_storage,
            "cognition_embedding_storage": home_chroma,
            "loaded_projects": registry,
            "embedding_generator": _FixedGen(_random_unit_vector(random.Random(seed + 999))),
            "embedding_ready": event,
            "embedding_error": None,
        }
        ctx = _make_ctx(lc)
        mock_mcp = _MockMcp()
        cognition_tools.register_cognition_tools(mock_mcp)

        times: list[float] = []
        last: dict[str, Any] | None = None
        for _ in range(n_reps):
            start = time.perf_counter()
            last = mock_mcp.tools["cognition_search"](ctx, query=query, project="*", limit=limit)
            times.append(time.perf_counter() - start)

        assert last is not None
        times_sorted = sorted(times)
        p50 = statistics.median(times_sorted)
        p95_idx = min(len(times_sorted) - 1, int(len(times_sorted) * 0.95))
        p95 = times_sorted[p95_idx]
        projects_seen = sorted({r.get("project") for r in last.get("results", [])})
        no_error = "error" not in last

        return {
            "n_reps": n_reps,
            "limit": limit,
            "p50_s": p50,
            "p95_s": p95,
            "result_count": len(last.get("results", [])),
            "projects_seen": projects_seen,
            "pass": no_error,
        }
    finally:
        # Windows: chromadb's sqlite/HNSW file handles must be released before
        # TemporaryDirectory cleanup rmtrees this path, else PermissionError.
        home_chroma.close()
        foreign_chroma.close()


# ── report printing + CLI ────────────────────────────────────────────────────


def _print_section(title: str, d: dict[str, Any]) -> None:
    print(f"\n=== {title} ===")
    for k, v in d.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.4f}")
        else:
            print(f"  {k}: {v}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bench_wave3.py",
        description=(
            "Gate C2 Part B measurement harness. Default: --search + --prime "
            "together against one shared synthetic fixture."
        ),
    )
    parser.add_argument("--search", action="store_true", help="Run the weighted-search latency benchmark.")
    parser.add_argument("--prime", action="store_true", help="Run the generate_prime latency benchmark.")
    parser.add_argument(
        "--multi-project", action="store_true",
        help="Run the two-graph project='*' fan-out search benchmark (builds TWO --nodes-sized graphs).",
    )
    parser.add_argument(
        "--nodes", type=int, default=1000,
        help="Total node count for the synthetic fixture (drives the 1k/5k/10k sweep).",
    )
    parser.add_argument("--persons", type=int, default=10, help="Roster size (drives the 0/10/100 sweep).")
    parser.add_argument("--reps", type=int, default=20, help="Repetitions per timed call.")
    parser.add_argument("--limit", type=int, default=10, help="cognition_search limit.")
    parser.add_argument(
        "--email", type=str, default=None,
        help="current_email for --prime (default: first roster email, or an unregistered "
             "bencher email when --persons 0).",
    )
    parser.add_argument(
        "--query", type=str, default="synthetic bench query",
        help="Query text for --search/--multi-project (embeds to a fixed vector regardless of content).",
    )
    parser.add_argument("--seed", type=int, default=SEED, help="Fixture seed.")
    args = parser.parse_args(argv)

    no_mode_flag = not (args.search or args.prime or args.multi_project)
    run_search = args.search or no_mode_flag
    run_prime = args.prime or no_mode_flag
    run_multi = args.multi_project

    report: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="vibe-cognition-bench3-") as tmp:
        tmp_path = Path(tmp)

        if run_search or run_prime:
            fixture_dir = tmp_path / "fixture" / ".cognition"
            storage = CognitionStorage(fixture_dir)
            embed: ChromaDBStorage | None = None
            if run_search:
                embed = ChromaDBStorage(
                    persist_directory=tmp_path / "fixture" / "chromadb",
                    embedding_model="bench-m", embedding_dimensions=DIMS,
                )
            try:
                handles = build_wave3_fixture(
                    storage, embedding_storage=embed,
                    total_nodes=args.nodes, n_persons=args.persons, seed=args.seed,
                )
                report["fixture"] = {
                    "stats": storage.get_statistics(),
                    "n_persons": len(handles["person_emails"]),
                    "n_tasks": len(handles["task_ids"]),
                    "stale_claimed": len(handles["stale_claimed_task_ids"]),
                    "fresh_claimed": len(handles["fresh_claimed_task_ids"]),
                    "blocked": len(handles["blocked_task_ids"]),
                }
                _print_section("Fixture built", report["fixture"])

                email = args.email or (
                    handles["person_emails"][0] if handles["person_emails"]
                    else "unregistered-bencher@bench.local"
                )

                if run_search:
                    assert embed is not None
                    query_vec = _random_unit_vector(random.Random(args.seed + 999))
                    report["search"] = bench_search(
                        storage, embed, n_reps=args.reps, limit=args.limit,
                        query_vec=query_vec, query_text=args.query,
                    )
                    _print_section("Search: weighted latency + scan-count memo", report["search"])

                if run_prime:
                    report["prime"] = bench_prime(storage, email=email, n_reps=args.reps)
                    _print_section("Prime: generate_prime latency", report["prime"])
            finally:
                if embed is not None:
                    embed.close()

        if run_multi:
            report["multi_project"] = bench_multi_project(
                tmp_path / "multi", total_nodes=args.nodes, n_persons=args.persons,
                n_reps=args.reps, limit=args.limit, query=args.query, seed=args.seed,
            )
            _print_section("Multi-project: fan-out search latency", report["multi_project"])

    overall_pass = all(
        section.get("pass", True) for section in report.values() if isinstance(section, dict)
    )

    print("\n=== Full report (JSON) ===")
    print(json.dumps(report, indent=2, default=str))
    print(f"\nOverall: {'PASS' if overall_pass else 'CHECK ABOVE — one or more sections failed'}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
