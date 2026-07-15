#!/usr/bin/env python3
"""WP-TC1 conflict-lens eval harness (brief doc:83c250e463ae, HIGH correction #2).

Stdlib only — deliberately has NO anthropic dependency and NO API keys, so it
cannot itself exercise curate-conflict-analyzer. That exercise is agent-driven
and happens in-session: the shipped agent definition is spawned once per
labeled pair via the Agent tool (haiku pin), and its verdict is transcribed
into a results JSON. This script only does the two things a standalone script
CAN do without an LLM:

  validate  — check tests/fixtures/conflict_lens_labeled_pairs.json's shape
              (ids unique, labels valid, a/b required fields present, counts
              match _meta.counts). Catches a corrupted or hand-edited fixture
              before it's trusted for scoring.

  score     — given a results JSON (one predicted label per pair id, written
              by hand from the agent-driven eval), compute contradicts
              precision/recall against the fixture's ground-truth labels and
              exit non-zero if precision is below the bar.

Results JSON shape (written by the eval runner, not this script):
    {
      "meta": {"model": "haiku", ...},
      "results": [{"id": "C01", "predicted": "contradicts"}, ...]
    }
"predicted" is one of "contradicts", "supersedes", "none" — the single
strongest edge type the analyzer proposed for that pair (empty proposal
list => "none").

Usage:
    uv run python scripts/eval_conflict_lens.py validate
    uv run python scripts/eval_conflict_lens.py score results.json
    uv run python scripts/eval_conflict_lens.py score results.json --threshold 0.9
"""

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_FIXTURE = _REPO / "tests" / "fixtures" / "conflict_lens_labeled_pairs.json"
_VALID_LABELS = {"contradicts", "supersedes", "none"}


def load_fixture(path: Path = _FIXTURE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_fixture(fixture: dict) -> list[str]:
    """Return a list of error strings; empty means the fixture is well-formed."""
    errors = []
    pairs = fixture.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        return ["fixture has no 'pairs' list"]

    seen_ids: set[str] = set()
    counts: dict[str, int] = {"contradicts": 0, "supersedes": 0, "none": 0}
    for i, pair in enumerate(pairs):
        pid = pair.get("id")
        if not pid:
            errors.append(f"pair[{i}] missing 'id'")
            continue
        if pid in seen_ids:
            errors.append(f"duplicate id: {pid}")
        seen_ids.add(pid)

        label = pair.get("label")
        if label not in _VALID_LABELS:
            errors.append(f"{pid}: invalid label {label!r} (must be one of {sorted(_VALID_LABELS)})")
        else:
            counts[label] += 1

        for side in ("a", "b"):
            node = pair.get(side)
            if not isinstance(node, dict):
                errors.append(f"{pid}: missing node '{side}'")
                continue
            for field in ("type", "summary", "detail", "context", "recorded_by_email", "timestamp"):
                if not node.get(field):
                    errors.append(f"{pid}.{side}: missing/empty required field '{field}'")

    expected_counts = fixture.get("_meta", {}).get("counts")
    if expected_counts and counts != expected_counts:
        errors.append(f"label counts {counts} do not match _meta.counts {expected_counts}")

    return errors


def score_results(fixture: dict, results: dict, threshold: float) -> dict:
    """Compute contradicts precision/recall (and supersedes recall, informational
    only per the brief -- only contradicts precision is gated)."""
    truth = {pair["id"]: pair["label"] for pair in fixture["pairs"]}
    predicted = {r["id"]: r["predicted"] for r in results["results"]}

    missing = sorted(set(truth) - set(predicted))
    extra = sorted(set(predicted) - set(truth))

    tp = fp = fn = 0
    supersedes_tp = supersedes_total = 0
    confusions = []
    for pid, true_label in truth.items():
        pred = predicted.get(pid, "none")
        if true_label == "supersedes":
            supersedes_total += 1
            if pred == "supersedes":
                supersedes_tp += 1
        if pred == "contradicts" and true_label == "contradicts":
            tp += 1
        elif pred == "contradicts" and true_label != "contradicts":
            fp += 1
            confusions.append((pid, true_label, pred))
        elif pred != "contradicts" and true_label == "contradicts":
            fn += 1
            confusions.append((pid, true_label, pred))

    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    supersedes_recall = supersedes_tp / supersedes_total if supersedes_total else None

    return {
        "contradicts_precision": precision,
        "contradicts_recall": recall,
        "contradicts_tp": tp,
        "contradicts_fp": fp,
        "contradicts_fn": fn,
        "supersedes_recall": supersedes_recall,
        "threshold": threshold,
        "passes_bar": precision is not None and precision >= threshold,
        "missing_ids": missing,
        "extra_ids": extra,
        "confusions": confusions,
    }


def _cmd_validate(args: argparse.Namespace) -> int:
    fixture = load_fixture(Path(args.fixture))
    errors = validate_fixture(fixture)
    if errors:
        print(f"FIXTURE INVALID ({len(errors)} error(s)):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"Fixture OK: {len(fixture['pairs'])} pairs, counts match _meta.counts.")
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    fixture = load_fixture(Path(args.fixture))
    fixture_errors = validate_fixture(fixture)
    if fixture_errors:
        print("Refusing to score against an invalid fixture:")
        for e in fixture_errors:
            print(f"  - {e}")
        return 1

    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    report = score_results(fixture, results, args.threshold)

    print(f"contradicts precision: {report['contradicts_precision']}")
    print(f"contradicts recall:    {report['contradicts_recall']}")
    print(f"  tp={report['contradicts_tp']} fp={report['contradicts_fp']} fn={report['contradicts_fn']}")
    print(f"supersedes recall (informational, not gated): {report['supersedes_recall']}")
    if report["missing_ids"]:
        print(f"WARNING: results missing ids (scored as 'none'): {report['missing_ids']}")
    if report["extra_ids"]:
        print(f"WARNING: results contain ids not in fixture (ignored): {report['extra_ids']}")
    if report["confusions"]:
        print("Confusions (id, true_label, predicted):")
        for pid, true_label, pred in report["confusions"]:
            print(f"  - {pid}: true={true_label} predicted={pred}")

    if report["passes_bar"]:
        print(f"PASS: precision {report['contradicts_precision']:.3f} >= threshold {args.threshold}")
        return 0
    print(f"FAIL: precision {report['contradicts_precision']} < threshold {args.threshold}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="check the labeled fixture's shape")
    p_validate.add_argument("--fixture", default=str(_FIXTURE))
    p_validate.set_defaults(func=_cmd_validate)

    p_score = sub.add_parser("score", help="score a results JSON against the fixture")
    p_score.add_argument("results", help="path to a results JSON file")
    p_score.add_argument("--fixture", default=str(_FIXTURE))
    p_score.add_argument("--threshold", type=float, default=0.9)
    p_score.set_defaults(func=_cmd_score)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
