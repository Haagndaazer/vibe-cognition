"""Pyright baseline ratchet for CI (WP-2).

Runs pyright, compares its error count to a committed baseline, and:
  - FAILS (exit 1) if the count EXCEEDS the baseline — new type errors block merge.
  - WARNS (exit 0) if the count is BELOW the baseline — lower the baseline in the
    same PR to ratchet monotonically toward zero. When it hits 0, delete the
    baseline file and make pyright strictly gating.

Crude by design (a +1/-1 swap could mask), but honest, near-zero machinery, and
ratchets to zero. Run inside the uv environment: `uv run python <this>`.
"""

import json
import pathlib
import subprocess
import sys

BASELINE_PATH = pathlib.Path(__file__).resolve().parents[1] / "pyright-baseline.txt"


def main() -> int:
    baseline = int(BASELINE_PATH.read_text(encoding="utf-8").strip())

    # pyright exits non-zero when errors exist; capture stdout regardless.
    proc = subprocess.run(
        ["pyright", "--outputjson"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("Failed to parse pyright --outputjson output:", file=sys.stderr)
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        return 2

    count = int(report["summary"]["errorCount"])
    print(f"pyright errors: {count} (baseline: {baseline})")

    if count > baseline:
        print(
            f"::error::pyright error count {count} exceeds baseline {baseline}. "
            "Fix the new type errors (or, if intentional, justify and raise the baseline)."
        )
        return 1
    if count < baseline:
        print(
            f"::warning::pyright errors dropped to {count} (baseline {baseline}). "
            "Lower .github/pyright-baseline.txt in this PR to ratchet the baseline down."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
