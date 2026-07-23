"""Post-update "what's new" notice: the second half of the upgrade-UX pair.

update_check.py's nudge says "an update is available"; this module says
"here's what you got" -- surfaced once per version bump in the session-start
prime digest. Invoked from ``hooks/session-start.sh`` as a step BEFORE
update_check (order is load-bearing -- see the ROLLOUT-DAY section below),
mirroring migrate_mcp/update_check's standalone style: stdlib-only, no
network, prints one block (or nothing) to stdout, always exits 0.

CONTENT SOURCE: ``.claude-plugin/whats-new.json``, a curated
``{"0.29.0": ["bullet", ...], ...}`` map maintained by hand at release time --
NOT the CHANGELOG (dev prose, too verbose and too fragile to parse). A
version with no entry (or an empty/malformed one) is silently skipped; a
missing or malformed whats-new.json degrades to silence, never a crash.

SEEN-TRACKING: a per-MACHINE marker at ``${CLAUDE_PLUGIN_DATA}/whats-new-seen``
holding the plain bare version string last shown (e.g. ``0.29.0``) -- no JSON,
so the bash hook can string-compare it against the installed version without
spawning python. Deliberately per-machine, not per-project (.cognition/ would
show the notice once per project instead of once per machine) and deliberately
NOT the TC7 onboarding decline-file convention (that file's per-project scope
suits ITS purpose; aligning here would be the wrong lesson to copy).

ROLLOUT-DAY: a missing marker does NOT mean "fresh install" -- on the release
day this feature ships, EVERY existing user has no marker yet. Distinguish by
checking whether ``${CLAUDE_PLUGIN_DATA}/update-check.json`` exists (written
every session since v0.29.0's update_check step):
  - present  -> an existing user -- floor the seen version at "0.0.0" and show
    the capped backlog once (this is the ONLY way that AC is reachable without
    hand-seeding a marker that never exists in the wild).
  - absent   -> a truly fresh install -- write the marker at the installed
    version and stay silent (onboarding owns new users, not this module).
This distinction is valid ONLY because this module's hook step runs BEFORE
update_check's -- if update_check ran first, a fresh install's own
same-session stamp write would make it look like a returning user. A user who
had VIBE_UPDATE_NUDGE=off since v0.29.0 (so update-check.json never got
written) is misread as fresh once; it self-heals on the very next update.
"""

import contextlib
import json
import os
import sys
from pathlib import Path

WHATS_NEW_FILENAME = "whats-new.json"  # under {plugin_root}/.claude-plugin/
SEEN_MARKER_FILENAME = "whats-new-seen"  # under {plugin_data}/
UPDATE_CHECK_STAMP_FILENAME = "update-check.json"  # under {plugin_data}/ (update_check.py's own)

MAX_VERSIONS = 3
MAX_BULLETS = 9

_WHATSNEW_OFF_VALUES = frozenset({"off", "0", "false", "no"})

# The floor a returning-but-unmarked user (rollout day) is treated as having
# last seen -- deliberately lower than any real version, so every entry up to
# `installed` is included in that one-time capped backlog.
_UNSEEN_FLOOR = "0.0.0"

# Generous fixed width for version-tuple comparison: right-padding a shorter
# tuple with zeros to this width (rather than pairwise-padding each compared
# pair) makes "1.2" == "1.2.0" == "1.2.0.0" hold consistently across every
# comparison in this module, not just between whichever two happen to be
# compared together. Known limit (ruled acceptable -- unreachable with our
# 3-part maintainer-authored versions): a version with MORE than 6 parts
# compares by raw tuple, not by this fixed-width padding, so two such
# versions of unequal length can misorder (the shorter reads as older than
# its zero-extended equal would suggest).
_VERSION_TUPLE_WIDTH = 6

# Pure ASCII, exact text (an acceptance criterion) -- a Windows pipe emits
# stdout as cp1252 by default; a non-ASCII byte here would mangle the same
# way update_check.py's em-dash did. Kept as named constants so tests can
# assert .isascii() on the module's own template strings directly, not just
# on the maintainer-authored whats-new.json bullets.
_INSTRUCTION_LINE = "INSTRUCTION: Tell the user what's new in this plugin update before continuing."
_HEADER_TEMPLATE = "vibe-cognition updated ({seen_display} -> v{installed}) - new since your last session:"
_VERSION_OVERFLOW_LINE = "(...and older versions - see CHANGELOG.md.)"
_FOOTER_LINE = (
    "(Shown once per update. Full details: the plugin's CHANGELOG.md. "
    "Disable these notices with VIBE_WHATS_NEW=off.)"
)
# {seen} renders "first install"-style wording on the 0.0.0 floor rather than
# the literal (and confusing) "v0.0.0" -- the brief pins the surrounding block
# text exactly but leaves this substitution's floor-case wording to us.
_SEEN_FLOOR_DISPLAY = "your earlier version"


def _parse_version(value: str | None) -> tuple[int, ...] | None:
    """Parse a dotted-int version string. Each part must be ALL digits
    (str.isdigit()) -- rejects empty/non-numeric/negative/whitespace-padded
    parts. None for anything unparsable; callers must never crash on it."""
    if not value:
        return None
    parts = value.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _version_key(value: str | None) -> tuple[int, ...] | None:
    """A comparable, zero-padded-to-fixed-width version tuple, or None if
    `value` doesn't parse as a version at all."""
    parsed = _parse_version(value)
    if parsed is None:
        return None
    if len(parsed) >= _VERSION_TUPLE_WIDTH:
        return parsed
    return parsed + (0,) * (_VERSION_TUPLE_WIDTH - len(parsed))


def _read_installed_version(plugin_root: str) -> str | None:
    """The installed version from ``${plugin_root}/.claude-plugin/plugin.json``."""
    path = Path(plugin_root) / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    return version if isinstance(version, str) and version else None


def _load_whats_new_map(plugin_root: str) -> dict:
    """The curated version->bullets map. Any failure (missing file, malformed
    JSON, non-dict top level) collapses to {} -- this module never crashes on
    a broken whats-new.json, it just has nothing to show."""
    path = Path(plugin_root) / ".claude-plugin" / WHATS_NEW_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_seen_marker(plugin_data: str) -> str | None:
    """The marker's raw (stripped) content, or None if absent/unreadable."""
    path = Path(plugin_data) / SEEN_MARKER_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    value = raw.strip()
    return value or None


def _has_prior_use(plugin_data: str) -> bool:
    """Whether update_check.py's own throttle stamp exists -- the rollout-day
    signal that a missing whats-new marker means "existing user", not "fresh
    install". See the module docstring's ROLLOUT-DAY section."""
    return (Path(plugin_data) / UPDATE_CHECK_STAMP_FILENAME).exists()


def _write_seen_marker(plugin_data: str, version: str) -> None:
    """Write the marker as a PLAIN bare version string -- pinned
    ``write_text(version, encoding="utf-8", newline="")`` with NO trailing
    newline, ever: a trailing newline would round-trip through Windows CRLF
    conversion and break the bash-side string-compare gate (the same
    invisible-byte class as update_check.py's cp1252 lesson). Atomic (temp
    sibling + os.replace); two session-starts racing is last-writer-wins, not
    merged -- fine, since nothing else reads this file except by full
    replacement. Never raises -- a read-only filesystem must not fail the hook."""
    try:
        data_dir = Path(plugin_data)
        data_dir.mkdir(parents=True, exist_ok=True)
        marker_path = data_dir / SEEN_MARKER_FILENAME
        tmp_path = data_dir / f"{SEEN_MARKER_FILENAME}.tmp"
        tmp_path.write_text(version, encoding="utf-8", newline="")
        os.replace(tmp_path, marker_path)
    except OSError:
        pass


def _format_seen_display(seen: str) -> str:
    if seen == _UNSEEN_FLOOR:
        return _SEEN_FLOOR_DISPLAY
    return f"v{seen}"


def _format_block(seen: str, installed: str, bullets: list[str], version_overflow: bool) -> str:
    lines = [
        _INSTRUCTION_LINE,
        _HEADER_TEMPLATE.format(seen_display=_format_seen_display(seen), installed=installed),
    ]
    lines.extend(f"- {b}" for b in bullets)
    if version_overflow:
        lines.append(_VERSION_OVERFLOW_LINE)
    lines.append(_FOOTER_LINE)
    return "\n".join(lines)


def check(plugin_root: str, plugin_data: str) -> str:
    """Run one what's-new check and return the notice block, or "" when
    there is nothing to show (already current, a pin rollback, a truly fresh
    install, a corrupted marker self-healing, or every candidate version in
    range has no usable bullets). Advances the seen marker on every path
    except: a truly fresh install (marker set to installed, handled inline)
    and an unparsable INSTALLED version (nothing safe to write -- see the
    ruling below)."""
    installed = _read_installed_version(plugin_root)
    if not installed:
        return ""

    seen = _read_seen_marker(plugin_data)
    if seen is None:
        if _has_prior_use(plugin_data):
            seen = _UNSEEN_FLOOR  # rollout-day: existing user, backfill once
        else:
            _write_seen_marker(plugin_data, installed)
            return ""  # truly fresh install -- onboarding owns this, not us

    installed_key = _version_key(installed)
    if installed_key is None:
        # Unparsable INSTALLED version (a maintainer-broken plugin.json) ->
        # nothing safe to write, so this perpetual-spawns every session until
        # fixed. Ruled acceptable: a broken plugin.json breaks far more than
        # this feature, and persisting a garbage string to the marker isn't
        # worth the extra branch.
        return ""

    seen_key = _version_key(seen)
    if seen_key is None:
        # Unparsable SEEN marker (corrupted on disk) -- unlike the installed
        # case above, self-heal it: write marker = installed and stay silent
        # once, the same shape as the downgrade-normalize path below. Without
        # this, a corrupted marker would never bash-side fast-path-match
        # again, spawning the module every session while every candidate
        # version in range keeps comparing against a None key and returning
        # "" -- silently losing every future announcement forever.
        _write_seen_marker(plugin_data, installed)
        return ""

    if seen_key >= installed_key:
        # Already current, or a pin rollback -- normalize the marker and
        # stay silent either way.
        _write_seen_marker(plugin_data, installed)
        return ""

    whats_new_map = _load_whats_new_map(plugin_root)
    candidates: list[tuple[tuple[int, ...], list[str]]] = []
    for version_str, bullets in whats_new_map.items():
        if not isinstance(bullets, list):
            continue
        clean_bullets = [b for b in bullets if isinstance(b, str) and b]
        if not clean_bullets:
            continue
        v_key = _version_key(version_str)
        if v_key is None or not (seen_key < v_key <= installed_key):
            continue
        candidates.append((v_key, clean_bullets))

    _write_seen_marker(plugin_data, installed)  # marker always advances on this path

    if not candidates:
        return ""

    candidates.sort(key=lambda c: c[0], reverse=True)  # newest first

    version_overflow = len(candidates) > MAX_VERSIONS
    selected = candidates[:MAX_VERSIONS]

    bullets: list[str] = []
    for _, vb in selected:
        bullets.extend(vb)
    bullets = bullets[:MAX_BULLETS]  # bullet-only overflow: drop trailing oldest, no line

    return _format_block(seen, installed, bullets, version_overflow)


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m vibe_cognition.whats_new``.

    Reads ``CLAUDE_PLUGIN_ROOT``/``CLAUDE_PLUGIN_DATA``/``VIBE_WHATS_NEW`` from
    the environment (set by the SessionStart hook); prints the notice block
    (or nothing) and always exits 0. Takes no arguments.
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        with contextlib.suppress(Exception):
            reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:] if argv is None else list(argv)
    if args:
        print(f"unknown option: {args[0]}", file=sys.stderr)
        return 2

    setting = os.environ.get("VIBE_WHATS_NEW", "").strip().lower()
    if setting in _WHATSNEW_OFF_VALUES:
        return 0

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
    if not plugin_root or not plugin_data:
        return 0

    try:
        note = check(plugin_root, plugin_data)
    except Exception:  # noqa: BLE001
        return 0
    if note:
        print(note, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
