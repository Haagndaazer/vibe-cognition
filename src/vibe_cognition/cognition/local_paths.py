"""Where machine-local state lives under .cognition/ — stdlib-only, no deps.

Machine-local files (who is driving this checkout, when you last looked, whether
you declined onboarding) must never reach version control. Each one used to need
its own ignore entry, so the ignore list only ever covered names someone
remembered to add: a lab run showed `svn add --force` sweeping identity.json and
an unlisted file straight in while the enumerated ones were skipped.

They now live in `.cognition/local/`, which one ignore entry covers forever.

READ falls back to the legacy location so an existing working copy keeps its
state across the upgrade; WRITE always targets `local/`. Losing that fallback
would reset every install's last-seen marker (spurious "Since You Were Gone"),
onboarding decline (re-prompts) and hygiene flag (pass re-runs).

Kept stdlib-only and dependency-free because git_hygiene.py imports it and is
itself deliberately standalone.
"""

import contextlib
from pathlib import Path

LOCAL_DIRNAME = "local"

# Every machine-local filename the pass relocates. `*.lock` is deliberately
# absent: locks are written beside what they lock, and moving them mid-upgrade
# would leave two plugin versions locking different paths — the lock silently
# stops being mutually exclusive.
RELOCATED_FILENAMES: tuple[str, ...] = (
    "identity.json",
    "last-seen.json",
    "onboard-declined",
    ".last-rehydrate.json",
    ".git-hygiene-managed",
    "backfill-identity-map.skeleton.json",
)


def local_dir(cognition_dir: Path) -> Path:
    return Path(cognition_dir) / LOCAL_DIRNAME


def legacy_path(cognition_dir: Path, filename: str) -> Path:
    return Path(cognition_dir) / filename


def write_path(cognition_dir: Path, filename: str) -> Path:
    """Where to WRITE. Always local/; the directory is created if absent."""
    target = local_dir(cognition_dir)
    # A read-only checkout raises here; the path is returned anyway so the
    # caller's own error handling reports the real failure, not this one.
    with contextlib.suppress(OSError):
        target.mkdir(parents=True, exist_ok=True)
    return target / filename


def read_path(cognition_dir: Path, filename: str) -> Path:
    """Where to READ. local/ when present, else the legacy location.

    Returns the local path when neither exists, so a caller's "missing file"
    handling reports the location a write would actually use.
    """
    candidate = local_dir(cognition_dir) / filename
    try:
        if candidate.exists():
            return candidate
        legacy = legacy_path(cognition_dir, filename)
        if legacy.exists():
            return legacy
    except OSError:
        pass
    return candidate
