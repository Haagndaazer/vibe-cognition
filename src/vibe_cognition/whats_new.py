"""Post-update "what's new" notice: the second half of the upgrade-UX pair.

update_check.py's nudge says "an update is available"; this module says
"here's what you got" -- surfaced in the session-start prime digest at every
session start until an agent confirms it has told the user
(cognition_readme(changes_seen=true)), at most MAX_SHOWINGS times. Invoked from ``hooks/session-start.sh`` as a step BEFORE
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

CONFIRMATION: printing the note does NOT mark it seen -- a hook's output can
be discarded by the harness (seen in the field after an in-session update),
so "printed" is not "seen". The marker advances only when (a) an agent
confirms via ``confirm()``, (b) the note has been printed MAX_SHOWINGS times
unconfirmed, or (c) there is nothing to show. The unconfirmed-showings count
lives in ``${CLAUDE_PLUGIN_DATA}/whats-new-pending.json``.

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
import secrets
import sys
from collections.abc import Callable
from pathlib import Path

WHATS_NEW_FILENAME = "whats-new.json"  # under {plugin_root}/.claude-plugin/
SEEN_MARKER_FILENAME = "whats-new-seen"  # under {plugin_data}/
UPDATE_CHECK_STAMP_FILENAME = "update-check.json"  # under {plugin_data}/ (update_check.py's own)
PENDING_FILENAME = "whats-new-pending.json"  # under {plugin_data}/

MAX_SHOWINGS = 5

MAX_VERSIONS = 3
MAX_BULLETS = 9

_WHATSNEW_OFF_VALUES = frozenset({"off", "0", "false", "no"})

# The floor a returning-but-unmarked user (rollout day) is treated as having
# last seen -- deliberately lower than any real version, so every entry up to
# `installed` is included in that capped backlog.
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
    f"(Shown at session start until confirmed, at most {MAX_SHOWINGS} times. "
    "Full details: the plugin's CHANGELOG.md. "
    "Disable these notices with VIBE_WHATS_NEW=off.)"
)
_CONFIRM_LINE = (
    "Once the user has been told, call cognition_readme(changes_seen=true) so this "
    "note stops appearing."
)

CONFIRMED = "Recorded: the what's-new note for v{version} will not be shown again."
NOTHING_TO_CONFIRM = "Nothing to confirm: this session has no what's-new note."
STALE_SERVER = (
    "Not recorded: this server is running v{running} but the installed plugin is "
    "v{installed}. Reconnect or restart the session, then confirm again."
)
NEWER_CONFIRMED = (
    "Not recorded: v{seen} is already confirmed and this server is the older "
    "v{installed}. Restart the session to run the installed version."
)
NOT_RECORDED = "Could not record the confirmation; the note may appear once more."
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


def _write_atomic(plugin_data: str, filename: str, text: str) -> bool:
    """Atomic write via a uniquely named temp sibling: two hook runs and a
    server can write the same file at once. Never raises."""
    data_dir = Path(plugin_data)
    tmp_path = data_dir / f"{filename}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp_path.write_bytes(text.encode("utf-8"))
        os.replace(tmp_path, data_dir / filename)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        return False


def _write_seen_marker(plugin_data: str, version: str) -> bool:
    """Write the marker as the bare version string, no trailing newline: the
    bash hook string-compares it against the installed version."""
    return _write_atomic(plugin_data, SEEN_MARKER_FILENAME, version)


def _read_shown(plugin_data: str, installed: str) -> int:
    """Unconfirmed showings of the note for `installed`. A missing, corrupt
    or other-version file counts as 0."""
    try:
        data = json.loads((Path(plugin_data) / PENDING_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict) or data.get("version") != installed:
        return 0
    shown = data.get("shown")
    if not isinstance(shown, int) or isinstance(shown, bool) or shown < 0:
        return 0
    return shown


def _write_shown(plugin_data: str, installed: str, shown: int) -> bool:
    return _write_atomic(
        plugin_data, PENDING_FILENAME, json.dumps({"version": installed, "shown": shown})
    )


def _mark_seen(plugin_data: str, version: str) -> bool:
    """Advance the marker and drop the showings count, unless that count
    belongs to a newer version than the one being marked."""
    ok = _write_seen_marker(plugin_data, version) or _write_seen_marker(plugin_data, version)
    if ok and not _pending_is_newer(plugin_data, version):
        with contextlib.suppress(OSError):
            (Path(plugin_data) / PENDING_FILENAME).unlink()
    return ok


def _pending_is_newer(plugin_data: str, version: str) -> bool:
    try:
        data = json.loads((Path(plugin_data) / PENDING_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    pending_key = _version_key(data.get("version")) if isinstance(data, dict) else None
    version_key = _version_key(version)
    return pending_key is not None and version_key is not None and pending_key > version_key


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
    lines.append(_CONFIRM_LINE)
    return "\n".join(lines)


def check(plugin_root: str, plugin_data: str) -> str:
    """The note to show, or "" when there is nothing new. Records the showing."""
    note, commit = plan(plugin_root, plugin_data)
    commit()
    return note


def plan(plugin_root: str, plugin_data: str) -> tuple[str, Callable[[], object]]:
    """(note, commit). commit() records the outcome -- one more unconfirmed
    showing, or the marker advancing -- and must be called only once the note
    has been printed. An unparsable INSTALLED version writes nothing."""
    installed = _read_installed_version(plugin_root)
    installed_key = _version_key(installed)
    if not installed or installed_key is None:
        return "", lambda: None

    def finish() -> bool:
        return _mark_seen(plugin_data, installed)

    seen = _read_seen_marker(plugin_data)
    if seen is None:
        if not _has_prior_use(plugin_data):
            return "", finish
        seen = _UNSEEN_FLOOR

    seen_key = _version_key(seen)
    if seen_key is None or seen_key >= installed_key:
        return "", finish

    note = _build_note(plugin_root, seen, seen_key, installed, installed_key)
    if not note:
        return "", finish

    shown = _read_shown(plugin_data, installed) + 1
    if shown >= MAX_SHOWINGS:
        return note, finish
    return note, lambda: _write_shown(plugin_data, installed, shown)


def _build_note(
    plugin_root: str,
    seen: str,
    seen_key: tuple[int, ...],
    installed: str,
    installed_key: tuple[int, ...],
) -> str:
    candidates: list[tuple[tuple[int, ...], list[str]]] = []
    for version_str, bullets in _load_whats_new_map(plugin_root).items():
        if not isinstance(bullets, list):
            continue
        clean_bullets = [b for b in bullets if isinstance(b, str) and b]
        if not clean_bullets:
            continue
        v_key = _version_key(version_str)
        if v_key is None or not (seen_key < v_key <= installed_key):
            continue
        candidates.append((v_key, clean_bullets))
    if not candidates:
        return ""

    candidates.sort(key=lambda c: c[0], reverse=True)
    version_overflow = len(candidates) > MAX_VERSIONS
    bullets = [b for _, vb in candidates[:MAX_VERSIONS] for b in vb][:MAX_BULLETS]
    return _format_block(seen, installed, bullets, version_overflow)


def confirm(plugin_root: str, plugin_data: str, running_version: str | None) -> tuple[bool, str]:
    """Record that the user has been told. Returns (recorded, message).

    Refuses when the calling server is not running the installed version, and
    never moves the marker backwards."""
    if not plugin_root or not plugin_data:
        return False, NOTHING_TO_CONFIRM
    installed = _read_installed_version(plugin_root)
    installed_key = _version_key(installed)
    if not installed or installed_key is None:
        return False, NOTHING_TO_CONFIRM
    if installed != running_version:
        return False, STALE_SERVER.format(running=running_version or "unknown", installed=installed)
    seen = _read_seen_marker(plugin_data)
    seen_key = _version_key(seen)
    if seen_key is not None and seen_key > installed_key:
        return False, NEWER_CONFIRMED.format(seen=seen, installed=installed)
    if not _mark_seen(plugin_data, installed):
        return False, NOT_RECORDED
    return True, CONFIRMED.format(version=installed)


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
        note, commit = plan(plugin_root, plugin_data)
        if note:
            sys.stdout.write(note)
            sys.stdout.flush()
        commit()
    except Exception:  # noqa: BLE001
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
