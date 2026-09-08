"""WP-P0: the parity registry must cover every tool, skill, hook event, and
agent for every harness, cite real evidence for `full`, and PARITY.md must be
a fresh render of it."""

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tools"))

import render_parity as rp  # noqa: E402


def test_registry_is_complete_and_evidence_resolves():
    problems = rp.validate(rp.load_registry())
    assert not problems, "\n".join(problems)


def test_parity_md_is_fresh():
    registry = rp.load_registry()
    assert rp.OUTPUT.read_text(encoding="utf-8") == rp.render_md(registry)


def test_validate_catches_missing_row_and_bad_evidence():
    registry = json.loads(json.dumps(rp.load_registry()))
    item = next(iter(registry["items"]))
    registry["items"][item]["codex"] = {"status": "full", "evidence": "tests/does_not_exist.py::nope"}
    del registry["items"][next(k for k in registry["items"] if k != item)]
    problems = rp.validate(registry)
    assert any("missing parity row" in p for p in problems)
    assert any("needs evidence" in p for p in problems)


def test_enumeration_covers_all_four_kinds():
    kinds = {i.split(":", 1)[0] for i in rp.enumerate_items()}
    assert kinds == {"tool", "skill", "hook", "agent"}
