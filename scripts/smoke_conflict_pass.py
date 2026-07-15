#!/usr/bin/env python3
"""WP-TC1 real-graph smoke test (brief doc:83c250e463ae acceptance criteria).

Runs the conflict-pass SCAFFOLDING (Step 1 candidate capture, the timing
invariant Step 1.5 depends on, and edge-commit mechanics) against a COPY of a
real .cognition journal -- never the live shared graph. Drives CognitionStorage
directly (same pattern as bench_wave1.py's --journal-dir mode) rather than the
MCP tool layer, since there is no supported way to point a loaded MCP project
at an arbitrary directory for read-write testing (cognition_load_project is
read-only by design).

This script cannot itself invoke curate-conflict-analyzer (no LLM access, same
HIGH correction #2 rationale as eval_conflict_lens.py) -- the analyzer's
lens judgment was exercised separately, in-session, via the Agent tool against
the labeled fixture (see scripts/eval_conflict_lens.py and the WP-TC1 handoff).
What THIS script proves, empirically, against real production-scale data:

  1. The Step-1 capture timing invariant: a stance-bearing candidate list
     captured BEFORE simulated Step-2 mark_curated calls differs from a naive
     re-fetch AFTER those calls -- the exact silent-zero-edge failure mode
     the brief's HIGH correction #1 exists to prevent.
  2. Edge-commit mechanics: _add_edges_batch_core with source="curate-conflict"
     round-trips correctly against a real graph copy.
  3. Prints the mandated final-report line shape with real counts.

Usage:
    uv run python scripts/smoke_conflict_pass.py --source-cognition-dir path/to/.cognition
"""

import argparse
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from vibe_cognition.cognition.models import CognitionNodeType
from vibe_cognition.cognition.storage import CognitionStorage
from vibe_cognition.tools.cognition_tools import _add_edges_batch_core

_STANCE_TYPES = {
    CognitionNodeType.DECISION.value,
    CognitionNodeType.CONSTRAINT.value,
    CognitionNodeType.PATTERN.value,
    CognitionNodeType.ASSUMPTION.value,
}


def run(source_dir: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="vibe-cognition-conflict-smoke-") as tmp:
        copy_dir = Path(tmp) / ".cognition"
        shutil.copytree(source_dir, copy_dir)
        storage = CognitionStorage(copy_dir)

        stats_before = storage.get_statistics()
        uncurated_before = storage.get_uncurated_nodes(limit=500)
        captured_candidates = [n for n in uncurated_before if n.get("type") in _STANCE_TYPES]

        # Simulate Step 2: mark every fetched uncurated node curated, exactly as
        # the orchestrator's per-batch cognition_mark_curated calls would.
        for n in uncurated_before:
            storage.mark_curated_by_skill(n["id"])

        # The failure mode HIGH correction #1 exists to prevent: a naive
        # re-fetch at the conflict-pass insertion point.
        uncurated_after = storage.get_uncurated_nodes(limit=500)
        stale_refetch_candidates = [n for n in uncurated_after if n.get("type") in _STANCE_TYPES]

        # Edge-commit mechanics only -- no LLM judgment here (the lens itself
        # was exercised separately). Proves a curate-conflict-sourced edge
        # round-trips against a real graph copy. Prefer two real captured
        # candidates; if the live backlog happens to have fewer than 2
        # stance-bearing uncurated nodes (as it may on any given day), fall
        # back to any two stance-bearing nodes in the whole graph purely to
        # exercise the commit mechanics -- flagged distinctly, since the real
        # conflict pass only ever operates on the captured worklist.
        edge_commit_used_fallback_pair = False
        pair = [n["id"] for n in captured_candidates[:2]]
        if len(pair) < 2:
            edge_commit_used_fallback_pair = True
            pool: list[str] = []
            for stance_type in _STANCE_TYPES:
                pool.extend(n["id"] for n in storage.get_nodes_by_type(CognitionNodeType(stance_type)))
                if len(pool) >= 2:
                    break
            pair = pool[:2]

        commit_result = None
        if len(pair) >= 2:
            edges_json = json.dumps([{
                "from_id": pair[0], "to_id": pair[1], "edge_type": "supersedes",
                "reason": "smoke-test plumbing check, not a real lens judgment",
                "source": "curate-conflict",
            }])
            commit_result = _add_edges_batch_core(storage, edges_json)

        stats_after = storage.get_statistics()

        return {
            "copy_dir_was_isolated_temp": True,
            "stats_before": stats_before,
            "uncurated_before_count": len(uncurated_before),
            "captured_stance_bearing_candidates": len(captured_candidates),
            "captured_candidate_ids_sample": [n["id"] for n in captured_candidates[:10]],
            "stale_refetch_after_simulated_step2": len(stale_refetch_candidates),
            "timing_invariant_holds": (
                len(stale_refetch_candidates) < len(captured_candidates)
                or len(captured_candidates) == 0
            ),
            "edge_commit_used_fallback_pair": edge_commit_used_fallback_pair,
            "edge_commit_smoke_result": commit_result,
            "stats_after": stats_after,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--source-cognition-dir", type=Path, required=True,
        help="Path to a REAL .cognition directory to copy into an isolated temp dir and test against.",
    )
    args = parser.parse_args()

    result = run(args.source_cognition_dir)
    print(json.dumps(result, indent=2))

    proposed = 1 if result["edge_commit_smoke_result"] else 0
    committed = result["edge_commit_smoke_result"]["created"] if result["edge_commit_smoke_result"] else 0
    discarded = proposed - committed
    print(
        f"\nConflict pass (smoke, plumbing-only): "
        f"{result['captured_stance_bearing_candidates']} candidates captured, "
        f"{proposed} proposed / {committed} committed / {discarded} discarded"
    )
    if not result["timing_invariant_holds"]:
        print("WARNING: timing invariant did NOT hold -- investigate before shipping.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
