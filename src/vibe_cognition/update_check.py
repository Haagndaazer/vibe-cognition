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
dependencies.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

STAMP_FILENAME = "update-check.json"
REQUEST_TIMEOUT_SECONDS = 3.0

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

NUDGE_TEMPLATE = (
    "vibe-cognition v{remote} is available (you have v{installed}). To "
    "update: run {cta}, then restart Claude Code. Updating is always your "
    "call — this notice is informational only. Disable it with "
    "VIBE_UPDATE_NUDGE=off."
)


def _http_get_json(url: str, timeout: float) -> dict | list | None:
    """GET a URL and parse it as JSON. Any failure (timeout, DNS, non-200,
    malformed JSON) collapses to None -- callers treat "couldn't check" and
    "checked, nothing to report" identically."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vibe-cognition-update-check"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            raw = resp.read()
        return json.loads(raw)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
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
    None for anything unparsable (empty, non-numeric parts, etc.) -- callers
    treat an unparsable version as "equal" (no nudge), never a crash."""
    if not value:
        return None
    parts = value.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


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
    Returns "" on any failure (e.g. a non-cache dev layout with too few
    parents) so callers can fall back to plain phrasing."""
    try:
        name = Path(plugin_root).parent.parent.name
    except (OSError, ValueError):
        return ""
    return name


def _format_cta(marketplace: str) -> str:
    if marketplace:
        return f"/plugin update {_PLUGIN_NAME}@{marketplace}"
    return f"/plugin update {_PLUGIN_NAME}"


def _write_stamp(plugin_data: str, remote_version: str | None) -> None:
    """Write the throttle stamp unconditionally (even on a failed check) --
    the bash-side gate keys off this file's MTIME, so a failed check must
    still push the 24h window out, or every session would re-attempt the
    network call. Atomic (temp sibling + os.replace) so a crash mid-write, or
    two session-starts racing, never leaves torn JSON. Never raises -- a
    read-only filesystem must not fail the hook."""
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


def check(plugin_root: str, plugin_data: str, timeout: float = REQUEST_TIMEOUT_SECONDS) -> str:
    """Run one version check and return a nudge line, or "" when there is
    nothing to report (up to date, pin rolled back, or any failure along the
    way). Always writes the throttle stamp before returning."""
    installed = _read_installed_version(plugin_root)
    sha = _fetch_marketplace_sha(timeout)
    remote = _fetch_remote_version(sha, timeout) if sha else None
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

    note = check(plugin_root, plugin_data)
    if note:
        print(note, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
