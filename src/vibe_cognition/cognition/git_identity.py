"""Server-side git identity resolution for task attribution.

A ``task`` node's ``created_by`` (and each status transition's ``by``) MUST be
resolved server-side from the repo's git config — NEVER trusted from a
client-supplied value (decision d1192f7e7bf8). There is no ``created_by`` tool
parameter; the client cannot override it.

WHY NO SUBPROCESS (P0, v0.12.1)
-------------------------------
The original implementation shelled ``git -C <repo> config user.name`` and claimed
to "mirror hooks/post-commit.py" (the post-commit hook, now removed). That
precedent did NOT transfer (ledger 21 — a recorded pattern does not travel to a
new execution context unexamined): post-commit.py ran as a git HOOK in a
console-attached context, whereas this MCP server runs DETACHED with no console
and piped stdio. In that context the
Git-for-Windows launcher never closes the stdout pipe, so ``subprocess`` blocks
forever joining its reader thread — and the ``timeout=`` cannot fire, because the
block is in the pipe drain, not the process wait. A live ``cognition_add_task`` hung
indefinitely on exactly this (a multi-minute orphan ``git.exe`` confirmed via a
thread dump parked in ``subprocess._communicate`` → ``join``).

The fix removes the subprocess entirely: we read the git config FILES directly
(pure filesystem, like ``git_hygiene.py``). This cannot hang, never raises — and as
a bonus resolves the real identity even on machines where the detached git spawn
would have hung and degraded to the OS user.

We read only ``[user] name``/``email`` with git's precedence (local overrides
global). System config and ``include``/``includeIf`` directives are NOT followed; an
identity reachable only through those degrades to the OS-user fallback (no hang,
slightly less precise) — acceptable, and revisitable if it ever bites.
"""

import getpass
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _clean_value(v: str) -> str:
    """Resolve a git-config value per git-config(1): a quoted value yields its literal
    contents; an unquoted value has a whitespace-preceded inline ``#``/``;`` comment
    trimmed. ``v`` is assumed already stripped of surrounding whitespace."""
    if v.startswith('"'):
        end = v.find('"', 1)
        return v[1:end] if end != -1 else v[1:]
    for i in range(1, len(v)):
        if v[i] in "#;" and v[i - 1] in " \t":
            return v[:i].strip()
    return v


def _parse_user_section(text: str) -> dict[str, str]:
    """Extract ``user.name``/``user.email`` from git-config file text.

    Git config is INI-like but NOT ``configparser``-compatible (tab-indented keys,
    ``[section "subsection"]`` headers, duplicate keys), so we scan by hand. Only the
    bare ``[user]`` section's ``name``/``email`` are read — a ``[user "sub"]`` subsection
    is NOT the identity section. Tolerant: unknown/garbage syntax is skipped, never
    raised. Returns only the keys actually found (value may be "").
    """
    found: dict[str, str] = {}
    in_user = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            inner = line[1:].split("]", 1)[0].strip()
            # Only the bare [user] section (case-insensitive). [user "sub"] is ignored.
            in_user = inner.lower() == "user"
            continue
        if not in_user or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key in ("name", "email"):
            found[key] = _clean_value(value.strip())
    return found


def _read_config_file(path: Path) -> dict[str, str]:
    """Read + parse one git-config file. Missing/unreadable/garbage → ``{}``. Never raises."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    except Exception as exc:  # noqa: BLE001 - defensive: a read must never surface to callers
        logger.debug("git_identity: reading %s failed: %s", path, exc)
        return {}
    try:
        return _parse_user_section(text)
    except Exception as exc:  # noqa: BLE001 - defensive: a parse must never surface to callers
        logger.debug("git_identity: parsing %s failed: %s", path, exc)
        return {}


def _global_config_paths() -> list[Path]:
    """Global git-config candidates, LOW→HIGH precedence (later wins), mirroring git.

    Honors ``GIT_CONFIG_GLOBAL`` (git's own override — when set, git ignores the
    default global files; tests use it too). Otherwise XDG
    (``$XDG_CONFIG_HOME/git/config`` or ``~/.config/git/config``) then ``~/.gitconfig``,
    which git reads last and so takes precedence. ``Path.home()`` can raise when no home
    env is set — guarded, so this returns ``[]`` rather than propagating.
    """
    override = os.environ.get("GIT_CONFIG_GLOBAL")
    if override:
        return [Path(override)]
    try:
        home = Path.home()
    except Exception as exc:  # noqa: BLE001 - Path.home() raises with no home env (RuntimeError)
        logger.debug("git_identity: Path.home() failed: %s", exc)
        return []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    xdg_base = Path(xdg) if xdg else home / ".config"
    return [xdg_base / "git" / "config", home / ".gitconfig"]


def _local_config_path(repo_path: Path) -> Path | None:
    """``<repo>/.git/config`` when ``.git`` is a normal directory, else ``None``.

    A ``.git`` FILE (submodule/worktree gitlink) is not followed; local identity there
    degrades to global/OS-user — never a hang.
    """
    git_dir = repo_path / ".git"
    return git_dir / "config" if git_dir.is_dir() else None


def resolve_git_identity(repo_path: Path | str) -> dict[str, str]:
    """Resolve the committer identity for task attribution.

    NEVER trusts a client value, NEVER shells out, NEVER hangs, NEVER raises.

    Reads ``[user] name``/``email`` from the git config FILES in precedence order
    (global, then local — local overrides, including an explicit empty value), then:
      1. name still unset → OS user (``getpass.getuser()``)
      2. that fails too   → ``"unknown"``

    Args:
        repo_path: The repo root (``REPO_PATH`` / ``CLAUDE_PROJECT_DIR``, already
            resolved in config.py; in the tool we derive it from the storage dir).

    Returns:
        ``{"name": <non-empty str>, "email": <str, may be "">}``.
    """
    repo = Path(repo_path)
    name = ""
    email = ""
    # LOW → HIGH precedence: each PRESENT key (even "") overrides the previous file's,
    # so a higher-precedence file that explicitly clears a value wins (membership, not
    # truthiness).
    candidates = _global_config_paths()
    local = _local_config_path(repo)
    if local is not None:
        candidates.append(local)
    for cfg in candidates:
        found = _read_config_file(cfg)
        if "name" in found:
            name = found["name"]
        if "email" in found:
            email = found["email"]
    if not name:
        logger.debug("git_identity: no user.name in config files; falling back to OS user")
        try:
            name = getpass.getuser()
        except Exception as exc:  # noqa: BLE001 - getpass can raise if no user env is set
            logger.debug("resolve_git_identity: getpass.getuser() failed: %s", exc)
            name = ""
    return {"name": name or "unknown", "email": email or ""}
