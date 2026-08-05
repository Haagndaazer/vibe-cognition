"""WP-Lifecycle-2 destructive-op containment gate (rev 4, machine-checked).

After the rev-3 implementation incident (a machine's Windows install
destroyed and its main drive wiped by implementation-adjacent activity),
the containment rules K1-K5/FS1-FS5 forbid, in this repo's shipped code,
tests, and hooks alike: name-based process kills, tree-kills of foreign
roots, and recursive deletes outside pytest tmp_path. This test turns those
rules from prose into a scan: no source file may contain the forbidden
patterns, except explicitly allowlisted (file, pattern) pairs, each of which
must satisfy K1/K4 (acts only on a pid/path the code itself created).

Patterns are assembled by concatenation so this file never matches itself.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SELF = Path(__file__).name

# Assembled, never literal — shell/PowerShell name-kills and recursive
# deletes, Python-level recursive deletes, and out-of-band escape hatches.
_FORBIDDEN: list[str] = [
    "taskkill " + "/IM",
    "/IM ",
    "Stop-" + "Process",
    "Get-Process " + "|",
    "rm " + "-rf",
    "rd " + "/s",
    "del " + "/s",
    "Remove-" + "Item",
    "-" + "Recurse",
    "git " + "clean",
    "shutil." + "rmtree",
    "rmtree" + "(",
    "os." + "system",
    "send2" + "trash",
    "Terminate" + "Process",
    "wmic " + "process",
]

# (filename, pattern) pairs sanctioned under K1/K4 — every entry names a
# harness that acts ONLY on a process it spawned itself, validated by
# creation time / its own Popen handle. Additions require the same review
# as a K-rule change.
_ALLOWLIST: set[tuple[str, str]] = {
    # Pre-existing WPL integration harness: TerminateProcess on the harness's
    # OWN spawned stand-in chain (creation-time-validated, exact handles).
    ("test_wp_lifecycle_integration.py", "Terminate" + "Process"),
}

_SCAN_DIRS = ("src", "tests", "hooks")
_SCAN_SUFFIXES = {".py", ".sh", ".ps1", ".psm1", ".cmd", ".bat"}


def test_no_forbidden_destructive_patterns_in_source():
    violations: list[str] = []
    for scan_dir in _SCAN_DIRS:
        base = _ROOT / scan_dir
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix.lower() not in _SCAN_SUFFIXES or not path.is_file():
                continue
            if path.name == _SELF:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in _FORBIDDEN:
                if pattern in text and (path.name, pattern) not in _ALLOWLIST:
                    violations.append(f"{path.relative_to(_ROOT)}: contains {pattern!r}")
    assert not violations, (
        "Destructive-op containment violation(s) — K1-K5/FS1-FS5 forbid these "
        "patterns anywhere in shipped code, tests, or hooks (see this test's "
        "docstring and docs/wp-lifecycle2-plan.md rev 4):\n" + "\n".join(violations)
    )
