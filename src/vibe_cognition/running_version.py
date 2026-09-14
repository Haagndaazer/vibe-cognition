"""Which version of the code is ACTUALLY running, versus what is installed.

After a plugin update the MCP server was observed running the PREVIOUS version for
an entire session. The server shares one virtualenv across versions, holding an
editable install that points into one version's directory; it launches with
`uv run --no-sync`, so only the SessionStart hook's `uv sync` moves that pointer --
and the two start at the same moment. The server lost the race by three seconds,
and nothing anywhere said so: every manifest, the install record and the digest all
reported the new version.

The launch now pins the code path, so the race should not recur. This module makes
it DETECTABLE regardless, which is what was missing: get_status reports what is
running, and the session-start digest warns when the server that is answering is not
the version installed.

Never raises. A version check that crashes startup is worse than none.
"""

import json
import logging
import os
import tomllib
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import vibe_cognition

from . import harness
from .cognition.local_paths import read_path, write_path

logger = logging.getLogger(__name__)

RUNNING_SERVER_FILENAME = "running-server.json"

#: A server stamp older than this is from an earlier session, not the one being
#: primed now, so a mismatch against it says nothing about the current server.
FRESH_STAMP_SECONDS = 15 * 60


def code_root() -> Path:
    """The directory the imported package actually lives under."""
    return Path(vibe_cognition.__file__).resolve().parents[2]


def code_version() -> str | None:
    """The version of the code that is running, read from ITS OWN pyproject.toml.

    Not importlib.metadata alone: with an editable install the dist-info in the
    shared venv describes whatever was last synced, which is exactly the thing that
    can disagree with the code on disk.
    """
    try:
        data = tomllib.loads((code_root() / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return metadata.version("vibe-cognition")
    except metadata.PackageNotFoundError:
        return None


def installed_version() -> str | None:
    """What the host installed, from the plugin root the host launched us with."""
    root = os.environ.get("VIBE_PLUGIN_ROOT", "").strip()
    if not root:
        return None
    for manifest in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        try:
            data = json.loads((Path(root) / manifest).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        version = data.get("version") if isinstance(data, dict) else None
        if isinstance(version, str) and version.strip():
            return version.strip()
    return None


def running_report() -> dict[str, Any]:
    """For get_status. `matches` is None when the installed version is unknown."""
    running = code_version()
    installed = installed_version()
    return {
        "version": running,
        "path": str(code_root()),
        "installed_version": installed,
        "matches": None if installed is None or running is None else running == installed,
    }


def _stamp_name() -> str:
    """One stamp per harness: Claude Code and Codex are updated separately, so one
    harness's older server must not be reported to the other's session."""
    stem, _, ext = RUNNING_SERVER_FILENAME.rpartition(".")
    return f"{stem}.{harness.current().name}.{ext}"


def stamp_running_server(cognition_dir: Path) -> None:
    """Record, for the digest, which version THIS server process is running."""
    try:
        write_path(Path(cognition_dir), _stamp_name()).write_text(
            json.dumps({
                "version": code_version(),
                "path": str(code_root()),
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat(),
            }),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("running-version: could not stamp: %s", exc)


def stale_server_warning(cognition_dir: Path, expected_version: str | None) -> str:
    """Session-start warning when the answering server is not the installed version.

    `expected_version` is the digest's OWN version: prime runs from the hook after the
    sync, so it is always the installed code. Empty string when there is nothing to
    say, when the stamp is from an earlier session, or when either side is unknown.
    """
    if not expected_version:
        return ""
    try:
        data = json.loads(
            read_path(Path(cognition_dir), _stamp_name()).read_text(encoding="utf-8")
        )
        started = datetime.fromisoformat(str(data["started_at"]))
        running = data.get("version")
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    if (datetime.now(UTC) - started).total_seconds() > FRESH_STAMP_SECONDS:
        return ""
    if not running or running == expected_version:
        return ""
    return (
        "## Plugin update may not be active yet\n"
        f"A vibe-cognition server started for this project in the last few minutes is "
        f"running v{running}, but v{expected_version} is installed. If that server is "
        "this session's, the session is using the OLD code and none of the new "
        "version's fixes apply. Confirm with get_status: `running_code.matches` false "
        "means it is. If so, RESTART the session (or reload plugins) and TELL THE USER."
    )
