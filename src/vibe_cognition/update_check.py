"""One-shot check for a newer released plugin version, with a user-facing nudge.

Claude Code's third-party-marketplace auto-update is off by default and the
maintainer cannot enable it (no ``autoUpdate`` in the marketplace.json schema;
``extraKnownMarketplaces.autoUpdate`` is managed-settings-only), so users get
no signal that a new version exists. This module is invoked from
``hooks/session-start.sh`` as a step between ``migrate_mcp`` and ``prime``,
mirroring ``migrate_mcp`` exactly: it prints one line (or nothing) to stdout,
which the hook captures and forwards to ``prime`` as a surfaced note.

Nudge-only: this module performs a couple of read-only HTTPS GETs and writes
its own throttle stamp. It never invokes ``claude``, never touches the plugin
cache, and never mutates the install — updating is always the user's call.

Version comparison is done on VERSION STRINGS, never SHAs — the installed
plugin's own ``gitCommitSha`` need not equal the marketplace pin's SHA at the
same released version (a verified real-machine case), so a SHA compare would
false-nudge. The marketplace PIN is read, never this repo's ``main`` (main
runs ahead of the pin; pin-relay batching means main can carry unreleased
versions).

Stdlib-only by design (mirrors ``migrate_mcp``'s standalone style) — this
module must stay import-light and never pull in the server's heavy embedding
dependencies. The printed nudge text itself is pure ASCII (no em-dash) --
Windows pipes stdout as cp1252 by default, which mangles non-ASCII bytes on
the way to bash/env/prime; `main()` also reconfigures stdout to UTF-8 as
belt-and-braces for any future non-ASCII text.

The two-fetch network phase runs on a bounded wall clock (a hard ~8s ceiling,
not just per-socket-op timeouts) via a manual daemon thread -- see `check()`.
"""

import contextlib
import http.client
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

STAMP_FILENAME = "update-check.json"
REQUEST_TIMEOUT_SECONDS = 3.0
NETWORK_PHASE_WALL_CLOCK_SECONDS = 8.0

_MARKETPLACE_URL = (
    "https://raw.githubusercontent.com/Haagndaazer/colton-claude-plugins/"
    "main/.claude-plugin/marketplace.json"
)
_PLUGIN_JSON_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/Haagndaazer/vibe-cognition/{sha}/"
    ".claude-plugin/plugin.json"
)
_PLUGIN_NAME = "vibe-cognition"

_NUDGE_OFF_VALUES = frozenset({"off", "0", "false", "no"})

# Pure ASCII (no em-dash): a Windows pipe emits stdout as cp1252 by default,
# and a non-ASCII char (verified: the em-dash arrives as raw byte 0x97) rides
# through bash/env toward prime as invalid UTF-8. See also main()'s
# sys.stdout.reconfigure() for defense in depth.
NUDGE_TEMPLATE = (
    "vibe-cognition v{remote} is available (you have v{installed}). To "
    "update: run {cta}, then restart Claude Code. Updating is always your "
    "call - this notice is informational only. Disable it with "
    "VIBE_UPDATE_NUDGE=off."
)


def _http_get_json(url: str, timeout: float) -> dict | list | None:
    """GET a URL and parse it as JSON. Any failure (timeout, DNS, non-200,
    malformed JSON, or a connection that drops mid-read) collapses to None --
    callers treat "couldn't check" and "checked, nothing to report"
    identically. http.client.HTTPException (e.g. IncompleteRead,
    BadStatusLine, raised by resp.read() on a truncating connection) is NOT a
    URLError/OSError/ValueError subclass, so it's caught explicitly -- an
    uncaught exception here would crash main() and skip the stamp write,
    making a persistently-truncating connection retry every single session."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vibe-cognition-update-check"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            raw = resp.read()
        return json.loads(raw)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException):
        return None


def _find_marketplace_entry(data: object) -> dict | None:
    """Locate the vibe-cognition entry in marketplace.json, tolerating either
    a `{"plugins": [{"name": ..., ...}, ...]}` list shape or a keyed-by-name
    dict shape -- defensive since this reads a file owned by a separate repo."""
    if not isinstance(data, dict):
        return None
    plugins = data.get("plugins")
    if isinstance(plugins, list):
        for entry in plugins:
            if isinstance(entry, dict) and entry.get("name") == _PLUGIN_NAME:
                return entry
    entry = data.get(_PLUGIN_NAME)
    return entry if isinstance(entry, dict) else None


def _fetch_marketplace_sha(timeout: float) -> str | None:
    """The marketplace's current release pin SHA for vibe-cognition, or None
    on any failure (network, malformed JSON, entry absent, no source.sha)."""
    data = _http_get_json(_MARKETPLACE_URL, timeout)
    entry = _find_marketplace_entry(data)
    if entry is None:
        return None
    source = entry.get("source")
    if not isinstance(source, dict):
        return None
    sha = source.get("sha")
    return sha if isinstance(sha, str) and sha else None


def _fetch_remote_version(sha: str, timeout: float) -> str | None:
    """The released ``version`` string from plugin.json at the pinned SHA."""
    data = _http_get_json(_PLUGIN_JSON_URL_TEMPLATE.format(sha=sha), timeout)
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    return version if isinstance(version, str) and version else None


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


def parse_version(value: str | None) -> tuple[int, ...] | None:
    """Parse a dotted-int version string ("0.28.0" -> (0, 28, 0)). Returns
    None for anything unparsable -- empty, non-numeric parts, negative parts
    ("1.-1.0"), or whitespace-padded parts -- callers treat an unparsable
    version as "equal" (no nudge), never a crash.

    Each part must be ALL digits (``str.isdigit()``), not just int()-parsable
    -- ``int("-1")`` succeeds, which previously let "1.-1.0" parse as
    (1, -1, 0) and made version_gt("1.-1.0", "0.28.0") spuriously True."""
    if not value:
        return None
    parts = value.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def version_gt(remote: str | None, installed: str | None) -> bool:
    """Whether `remote` is a strictly newer version than `installed`.

    Either side failing to parse -> False (no nudge; unparsable is treated as
    equal, never as "newer"). Unequal-length tuples are zero-padded to the
    same length before comparing, so "1.2" and "1.2.0" compare equal rather
    than the shorter tuple spuriously losing to the longer one."""
    r = parse_version(remote)
    i = parse_version(installed)
    if r is None or i is None:
        return False
    length = max(len(r), len(i))
    r_padded = r + (0,) * (length - len(r))
    i_padded = i + (0,) * (length - len(i))
    return r_padded > i_padded


def _derive_marketplace_name(plugin_root: str) -> str:
    """The marketplace name from the plugin cache layout
    (``.../cache/<marketplace>/vibe-cognition/<version>`` -- plugin_root IS
    the ``<version>`` dir, so the marketplace name is its grandparent's name).

    Only trusted when the cache-layout invariant actually holds -- i.e.
    plugin_root's PARENT is literally named "vibe-cognition" -- else returns
    "" so callers fall back to plain phrasing. Without this guard, a dev
    checkout (e.g. .../vibe-cognition/some-branch-dir) would derive a
    wrong-but-plausible-looking marketplace name from whatever the checkout's
    grandparent happens to be named."""
    try:
        root = Path(plugin_root)
        if root.parent.name != _PLUGIN_NAME:
            return ""
        return root.parent.parent.name
    except (OSError, ValueError):
        return ""


def _format_cta(marketplace: str) -> str:
    if marketplace:
        return f"/plugin update {_PLUGIN_NAME}@{marketplace}"
    return f"/plugin update {_PLUGIN_NAME}"


def _write_stamp(plugin_data: str, remote_version: str | None) -> None:
    """Write the throttle stamp unconditionally (even on a failed check) --
    the bash-side gate keys off this file's MTIME, so a failed check must
    still push the 24h window out, or every session would re-attempt the
    network call. Each individual write is atomic (temp sibling + os.replace),
    so a crash mid-write never leaves torn JSON -- but two session-starts
    racing is last-writer-wins, not merged; that's fine here since nothing
    ever reads this JSON back (only its MTIME matters), so a lost write is a
    no-op, not data loss. Never raises -- a read-only filesystem must not
    fail the hook."""
    try:
        data_dir = Path(plugin_data)
        data_dir.mkdir(parents=True, exist_ok=True)
        stamp_path = data_dir / STAMP_FILENAME
        tmp_path = data_dir / f"{STAMP_FILENAME}.tmp"
        payload = {
            "checked_at": datetime.now(UTC).isoformat(),
            "remote_version": remote_version or "",
        }
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp_path, stamp_path)
    except OSError:
        pass


def _fetch_remote_bounded(plugin_root: str, timeout: float, wall_clock_timeout: float) -> str | None:
    """Run the two-fetch network phase (marketplace sha -> plugin.json
    version) on a manual daemon thread, bounded by a HARD wall-clock ceiling
    -- `timeout` is per-socket-operation (urlopen's own), which does not
    bound a slow-drip response that keeps trickling bytes forever. Returns
    None if the phase is still running when `wall_clock_timeout` elapses
    (treated identically to any other failure); the thread is daemon, so it
    is abandoned rather than joined further -- it dies with the process,
    which exits immediately after this returns.

    Deliberately NOT a ThreadPoolExecutor: its implicit atexit worker-join
    would block process exit on a hung thread -- exactly the unbounded-wait
    bug this function exists to avoid."""
    result: dict[str, str | None] = {}

    def _worker() -> None:
        sha = _fetch_marketplace_sha(timeout)
        result["remote"] = _fetch_remote_version(sha, timeout) if sha else None

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(wall_clock_timeout)
    if thread.is_alive():
        return None
    return result.get("remote")


def check(
    plugin_root: str,
    plugin_data: str,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    wall_clock_timeout: float = NETWORK_PHASE_WALL_CLOCK_SECONDS,
) -> str:
    """Run one version check and return a nudge line, or "" when there is
    nothing to report (up to date, pin rolled back, or any failure along the
    way -- including the network phase exceeding `wall_clock_timeout`).
    Always writes the throttle stamp before returning."""
    installed = _read_installed_version(plugin_root)
    remote = _fetch_remote_bounded(plugin_root, timeout, wall_clock_timeout)
    _write_stamp(plugin_data, remote)

    if installed and remote and version_gt(remote, installed):
        marketplace = _derive_marketplace_name(plugin_root)
        return NUDGE_TEMPLATE.format(
            remote=remote, installed=installed, cta=_format_cta(marketplace)
        )
    return ""


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m vibe_cognition.update_check``.

    Reads ``CLAUDE_PLUGIN_ROOT``/``CLAUDE_PLUGIN_DATA``/``VIBE_UPDATE_NUDGE``
    from the environment (set by the SessionStart hook); prints a one-line
    nudge (or nothing) and always exits 0. Takes no arguments.
    """
    # Belt-and-braces for a Windows pipe's default cp1252 stdout encoding --
    # NUDGE_TEMPLATE is pure ASCII so this shouldn't matter today, but any
    # future non-ASCII text would otherwise mangle on its way to bash/env/prime.
    # getattr (not a direct .reconfigure() call): sys.stdout's static type is
    # the plain TextIO protocol, which doesn't declare reconfigure() (a real
    # io.TextIOWrapper method) -- this keeps pyright clean without a cast.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        with contextlib.suppress(Exception):
            reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:] if argv is None else list(argv)
    if args:
        print(f"unknown option: {args[0]}", file=sys.stderr)
        return 2

    nudge_setting = os.environ.get("VIBE_UPDATE_NUDGE", "").strip().lower()
    if nudge_setting in _NUDGE_OFF_VALUES:
        return 0

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
    if not plugin_root or not plugin_data:
        return 0

    # Mechanically enforces the "always exits 0" contract at the boundary --
    # any exception this module's own code failed to anticipate must still
    # degrade to "no note", never crash the SessionStart hook.
    try:
        note = check(plugin_root, plugin_data)
    except Exception:  # noqa: BLE001
        return 0
    if note:
        print(note, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
