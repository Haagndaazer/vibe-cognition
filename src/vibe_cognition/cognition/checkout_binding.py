"""Where a machine-local file was written, so a copy that travelled can be told apart.

`.cognition/local/` is machine-local only by ignore-rule convention. SVN never reads
.gitignore, and copying a project folder copies it, so files in it record the
machine, the OS account and the checkout folder they were written for.

The folder is matched by path OR by file identity (volume serial + file index). The
path alone is not stable: a mapped network drive resolves to its UNC form while the
same folder opened by drive letter does not, and a removable drive can come back
under a different letter. Verified on Windows that local, UNC, mapped-drive and
subst paths to one folder share one file identity, while a copy gets a new one.

Values are stored as short digests, never literally: the file is exactly the one
that leaks, and a checkout path usually embeds the OS username.

Known limits: a container rebuilt with a fresh random hostname is a different machine
every time; Windows and WSL are different accounts and folder routes; FAT/exFAT has
no stable file identity, so a removable drive under a new letter re-confirms once.
"""

import contextlib
import getpass
import hashlib
import os
import platform
from pathlib import Path
from typing import Any

try:
    import pwd
except ImportError:
    pwd = None

MISMATCH_MACHINE = "machine"
MISMATCH_ACCOUNT = "account"
MISMATCH_FOLDER = "folder"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def machine_name() -> str:
    return platform.node().strip()


def _account() -> str:
    """getpass.getuser() prefers LOGNAME/USER over USERNAME, so a shell that sets
    USER on Windows would change the answer. Read the platform's own source."""
    with contextlib.suppress(Exception):
        if os.name == "nt":
            name = os.environ.get("USERNAME", "").strip()
            if name:
                return name.casefold()
        elif pwd is not None:
            return pwd.getpwuid(os.getuid()).pw_name.strip().casefold()
    with contextlib.suppress(Exception):
        return getpass.getuser().strip().casefold()
    return ""


def _folder(cognition_dir: Path | str) -> Path:
    root = Path(cognition_dir)
    with contextlib.suppress(OSError, RuntimeError):
        root = root.resolve()
    return root.parent


def _folder_path_key(cognition_dir: Path | str) -> str:
    return _digest(os.path.normcase(os.path.abspath(str(_folder(cognition_dir)))))


def _folder_id_key(cognition_dir: Path | str) -> str | None:
    try:
        st = os.stat(_folder(cognition_dir))
    except OSError:
        return None
    if not st.st_ino:
        return None
    return _digest(f"{st.st_dev}:{st.st_ino}")


def current_binding(cognition_dir: Path | str) -> dict[str, Any]:
    """The binding fields to write alongside a machine-local record."""
    return {
        "machine": machine_name(),
        "account": _digest(_account()),
        "checkout": _folder_path_key(cognition_dir),
        "checkout_id": _folder_id_key(cognition_dir),
    }


def is_bound(recorded: dict[str, Any]) -> bool:
    """Whether a record carries a complete binding at all."""
    return all(isinstance(recorded.get(k), str) for k in ("machine", "account", "checkout"))


def binding_mismatch(recorded: dict[str, Any], cognition_dir: Path | str) -> str | None:
    """None when the record belongs here; else which part differs. Call is_bound first."""
    if str(recorded.get("machine", "")).strip().casefold() != machine_name().casefold():
        return MISMATCH_MACHINE
    if recorded.get("account") != _digest(_account()):
        return MISMATCH_ACCOUNT
    if recorded.get("checkout") == _folder_path_key(cognition_dir):
        return None
    recorded_id = recorded.get("checkout_id")
    if isinstance(recorded_id, str) and recorded_id == _folder_id_key(cognition_dir):
        return None
    return MISMATCH_FOLDER
