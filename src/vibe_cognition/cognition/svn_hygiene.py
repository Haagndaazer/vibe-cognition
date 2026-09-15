"""Automatic Subversion setup for .cognition/ -- the SVN counterpart of git_hygiene.

Git reads .cognition/.gitignore. SVN reads nothing, and never adds a new file to a
commit by itself, so on an SVN working copy this pass:

  1. puts .cognition/ under version control (depth empty first),
  2. sets svn:global-ignores on it from .cognition/.gitignore, trailing slashes
     stripped, merged with any value already there,
  3. schedules every new, non-ignored file for addition -- skipping documents marked
     local-only, which live in documents/.gitignore that SVN also does not read.

It NEVER commits; everything it does shows in `svn status` for the user to commit.
State is re-read every run rather than trusted to a flag, because `svn revert`
silently undoes a property change.

Subprocess rules: output goes to temporary files, never pipes (the v0.12.1 git
wedge); stdin is closed; --non-interactive; values and path lists go through files
(-F, --targets) because svn.exe glob-expands `*` in arguments on Windows, and the
two subcommands without --targets (status, propget) refuse a wildcard path. On timeout the child
is abandoned, never killed (shipped-code safety invariant); every command used is a
local working-copy operation.
"""

import contextlib
import fnmatch
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .documents import documents_dir, documents_gitignore_path
from .git_hygiene import _GITIGNORE_ENTRIES, vcs_hygiene_opted_out
from .local_paths import RELOCATED_FILENAMES

logger = logging.getLogger(__name__)

SVN_TIMEOUT_SECONDS = 30
# svn's default wait on a locked wc.db (TortoiseSVN cache, another svn command) is
# ~14s per call, measured. A busy working copy should cost a retry next session,
# not a stalled session start.
BUSY_TIMEOUT_MS = 2000
_MAX_ADD_ROUNDS = 12
IGNORE_PROPERTY = "svn:global-ignores"

STATUS_OK = "ok"
STATUS_NO_CLI = "no_svn_cli"
STATUS_PARENT_UNVERSIONED = "parent_unversioned"
STATUS_COGNITION_IGNORED = "cognition_ignored"
STATUS_FAILED = "failed"

_background_lock = threading.Lock()
_diagnostics = threading.local()
_SVN_ERROR_RE = re.compile(r"\b[EW]\d{6}\b")
_CONFLICT_ARTIFACT_RE = re.compile(
    r"\.(mine|working|prej|r\d+|merge-(left|right)\.r\d+)$", re.IGNORECASE,
)


@dataclass
class SvnHygieneReport:
    status: str
    property_set_now: bool = False
    added_now: list[str] = field(default_factory=list)
    pending_adds: int = 0
    property_pending: bool = False
    conflicted: list[str] = field(default_factory=list)
    journal_excluded: bool = False
    detail: str = ""


def svn_root(start: Path) -> Path | None:
    """The working-copy root at or above `start`; None under git or outside SVN.

    Requires `.svn/wc.db`, so a stray `.svn` folder is not mistaken for a checkout.
    """
    try:
        start = Path(start).resolve()
    except OSError:
        return None
    for candidate in (start, *start.parents):
        with contextlib.suppress(OSError):
            if (candidate / ".svn" / "wc.db").is_file():
                return candidate
            if (candidate / ".git").exists():
                return None
    return None


def _svn_command() -> str | None:
    return shutil.which("svn")


def _run(svn: str, args: list[str], cwd: Path, timeout: float = SVN_TIMEOUT_SECONDS) -> tuple[int, str] | None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed executable, list args, no shell
                [svn, args[0], "--non-interactive", "--config-option",
                 f"config:working-copy:busy-timeout={BUSY_TIMEOUT_MS}", *args[1:]],
                cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                creationflags=flags,
            )
        except OSError as exc:
            logger.debug("svn-hygiene: could not start svn: %s", exc)
            return None
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.debug("svn-hygiene: svn %s timed out; left to finish on its own", args[0])
            _diagnostics.last_error = f"svn {args[0]} did not finish within {timeout:.0f}s"
            return None
        out.seek(0)
        err.seek(0)
        if rc != 0:
            message = err.read().decode("utf-8", "replace")
            codes = [line.strip() for line in message.splitlines() if _SVN_ERROR_RE.search(line)]
            _diagnostics.last_error = (codes[-1] if codes else message.strip())[:300]
        return rc, out.read().decode("utf-8", "replace")


def _last_error() -> str:
    return getattr(_diagnostics, "last_error", "") or ""


def _failure(detail: str) -> SvnHygieneReport:
    cause = _last_error()
    hint = ""
    if "E155036" in cause:
        hint = " -- run `svn upgrade` in the working copy"
    elif "E200033" in cause or "database is locked" in cause:
        hint = " -- the working copy is busy with another svn or TortoiseSVN operation"
    return SvnHygieneReport(STATUS_FAILED, detail=f"{detail}: {cause}{hint}" if cause else detail)


def _with_targets(svn: str, args: list[str], cwd: Path, targets: list[str]) -> tuple[int, str] | None:
    fd, name = tempfile.mkstemp(suffix=".targets")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(f"{t}@\n" for t in targets))
        return _run(svn, [*args, "--targets", name], cwd)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)


def _with_path(svn: str, args: list[str], cwd: Path, target: str) -> tuple[int, str] | None:
    """For subcommands without --targets (status, propget). svn.exe glob-expands
    wildcards in argv on Windows, so a path containing one is refused."""
    if "*" in target or "?" in target:
        return None
    return _run(svn, [*args, f"{target}@"], cwd)


def _rel(root: Path, path: Path) -> str:
    return Path(path).resolve().relative_to(root).as_posix()


def parse_status_xml(text: str) -> dict[str, dict[str, str]]:
    """`svn status --xml` -> {posix path: {item, props, tree_conflicted}}."""
    entries: dict[str, dict[str, str]] = {}
    for entry in ET.fromstring(text).iter("entry"):
        status = entry.find("wc-status")
        if status is None:
            continue
        entries[Path(entry.get("path", "")).as_posix()] = {
            "item": status.get("item", ""),
            "props": status.get("props", ""),
            "tree_conflicted": status.get("tree-conflicted", "false"),
        }
    return entries


def _status(svn: str, root: Path, target: str, *, depth: str | None = None,
            no_ignore: bool = False, verbose: bool = False) -> dict[str, dict[str, str]] | None:
    args = ["status", "--xml"]
    if verbose:
        args.append("--verbose")
    if depth:
        args += ["--depth", depth]
    if no_ignore:
        args.append("--no-ignore")
    result = _with_path(svn, args, root, target)
    if result is None or result[0] != 0:
        return None
    try:
        return parse_status_xml(result[1])
    except ET.ParseError:
        return None


def ignore_globs(cognition_dir: Path) -> list[str]:
    """The ignore list for SVN: every .cognition/.gitignore entry plus the managed
    ones, comments dropped, trailing slashes stripped (SVN globs have no
    directory-only form, so `local/` would match nothing)."""
    lines: list[str] = []
    with contextlib.suppress(OSError):
        lines = (cognition_dir / ".gitignore").read_text(encoding="utf-8").splitlines()
    # Machine-local names by name too: a relocation into local/ that was deferred
    # (file in use) leaves identity.json at the legacy top-level path, where the
    # `local` glob does not reach it.
    legacy = [f"{name}*" for name in RELOCATED_FILENAMES]
    out: list[str] = []
    for raw in [*lines, *_GITIGNORE_ENTRIES, *legacy]:
        glob = raw.strip().rstrip("/")
        if glob and not glob.startswith("#") and glob not in out:
            out.append(glob)
    return out


def local_only_paths(root: Path, cognition_dir: Path) -> set[str]:
    """Working-copy-relative paths that must never be added: documents the user
    stored as local-only, and the per-machine list naming them."""
    docs_rel = f"{_rel(root, cognition_dir)}/{documents_dir(cognition_dir).name}"
    excluded = {f"{docs_rel}/.gitignore"}
    with contextlib.suppress(OSError):
        for line in documents_gitignore_path(cognition_dir).read_text(encoding="utf-8").splitlines():
            if line.strip():
                excluded.add(f"{docs_rel}/{line.strip()}")
    return excluded


def _only_excluded_inside(root: Path, rel_dir: str, excluded: set[str]) -> bool:
    files = [p for p in (root / rel_dir).rglob("*") if p.is_file()]
    return bool(files) and all(_rel(root, p) in excluded for p in files)


def is_conflict_artifact(path: str) -> bool:
    """The files SVN leaves beside a conflicted file (.mine, .r<N>, merge-left/right,
    .working, .prej). They are unversioned, so a blind add would commit them."""
    return bool(_CONFLICT_ARTIFACT_RE.search(path))


def matches_ignore(rel_path: str, target: str, globs: list[str]) -> bool:
    """Whether any path component below `target` matches an ignore glob, the way
    svn:global-ignores matches entry names."""
    below = rel_path[len(target):].strip("/").split("/") if rel_path.startswith(target) else []
    return any(fnmatch.fnmatchcase(part, glob) for part in below for glob in globs)


def add_new_files(svn: str, root: Path, cognition_dir: Path) -> list[str]:
    """Schedule every unversioned, non-ignored file under .cognition/ for addition,
    except local-only documents and conflict leftovers. Returns the paths added.

    Refuses to add anything unless .cognition/ is already versioned AND carries every
    ignore glob: adding before the property exists sweeps local/ -- the identity
    file -- into the commit. Found live: a profile written in a project's first
    session triggered exactly that. Adds nothing while a conflict is unresolved.
    """
    target = _rel(root, cognition_dir)
    globs = ignore_globs(cognition_dir)
    own = _status(svn, root, target, depth="empty", no_ignore=True, verbose=True)
    if own is None or own.get(target, {}).get("item") in ("unversioned", "ignored", ""):
        return []
    if any(g not in _propget(svn, root, target) for g in globs):
        return []
    excluded = local_only_paths(root, cognition_dir)
    added: list[str] = []
    attempted: set[str] = set()
    for _ in range(_MAX_ADD_ROUNDS):
        entries = _status(svn, root, target)
        if entries is None:
            break
        if any(e["item"] == "conflicted" or e["tree_conflicted"] == "true" for e in entries.values()):
            break
        todo = [
            p for p, e in entries.items()
            if e["item"] == "unversioned" and p not in excluded and p not in attempted
            and not is_conflict_artifact(p) and not matches_ignore(p, target, globs)
        ]
        dirs = [p for p in todo if (root / p).is_dir() and not _only_excluded_inside(root, p, excluded)]
        files = [p for p in todo if (root / p).is_file()]
        attempted.update(todo)
        if not dirs and not files:
            break
        if dirs and (res := _with_targets(svn, ["add", "--depth", "empty"], root, dirs)) and res[0] == 0:
            added.extend(dirs)
        if files and (res := _with_targets(svn, ["add"], root, files)) and res[0] == 0:
            added.extend(files)
    return added


def _propget(svn: str, root: Path, target: str) -> list[str]:
    result = _with_path(svn, ["propget", IGNORE_PROPERTY], root, target)
    if result is None or result[0] != 0:
        return []
    return [line.strip() for line in result[1].splitlines() if line.strip()]


def _propset(svn: str, root: Path, target: str, values: list[str]) -> bool:
    fd, name = tempfile.mkstemp(suffix=".svnprop")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(values) + "\n")
        result = _with_targets(svn, ["propset", IGNORE_PROPERTY, "-F", name], root, [target])
        return result is not None and result[0] == 0
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)


def _run_pass(svn: str, root: Path, cognition_dir: Path) -> SvnHygieneReport:
    _diagnostics.last_error = ""
    target = _rel(root, cognition_dir)
    own = _status(svn, root, target, depth="empty", no_ignore=True, verbose=True)
    if own is None:
        return _failure("svn status failed")
    item = own.get(target, {}).get("item", "")
    if item == "ignored":
        return SvnHygieneReport(STATUS_COGNITION_IGNORED)
    if item in ("unversioned", ""):
        parent = target.rpartition("/")[0]
        if parent:
            above = _status(svn, root, parent, depth="empty", no_ignore=True, verbose=True)
            if above is None or above.get(parent, {}).get("item") in ("unversioned", "ignored", ""):
                return SvnHygieneReport(STATUS_PARENT_UNVERSIONED)
        result = _with_targets(svn, ["add", "--depth", "empty"], root, [target])
        if result is None or result[0] != 0:
            again = _status(svn, root, target, depth="empty", no_ignore=True, verbose=True) or {}
            if again.get(target, {}).get("item") in ("unversioned", "ignored", ""):
                return _failure("could not add .cognition/")

    report = SvnHygieneReport(STATUS_OK)
    existing = _propget(svn, root, target)
    missing = [g for g in ignore_globs(cognition_dir) if g not in existing]
    if missing:
        if not _propset(svn, root, target, [*existing, *missing]):
            return _failure(f"could not set {IGNORE_PROPERTY}")
        report.property_set_now = True

    report.added_now = add_new_files(svn, root, cognition_dir)

    final = _status(svn, root, target) or {}
    report.pending_adds = sum(1 for p, e in final.items() if e["item"] == "added" and p != target)
    own_final = final.get(target, {})
    report.property_pending = own_final.get("item") == "added" or own_final.get("props") == "modified"
    report.conflicted = sorted(
        p for p, e in final.items() if e["item"] == "conflicted" or e["tree_conflicted"] == "true"
    )
    for journal, depth in ((cognition_dir / "journal.jsonl", "empty"), (cognition_dir / "journal", "files")):
        if not journal.exists():
            continue
        journal_rel = _rel(root, journal)
        seen = _status(svn, root, journal_rel, depth=depth, no_ignore=True) or {}
        if any(e.get("item") == "ignored" for e in seen.values()):
            report.journal_excluded = True
    return report


def ensure_svn_hygiene(cognition_dir: Path) -> SvnHygieneReport | None:
    """Run the SVN setup pass. None when this is not an SVN working copy (or the
    user opted out). Never raises."""
    try:
        if vcs_hygiene_opted_out() or not Path(cognition_dir).is_dir():
            return None
        root = svn_root(Path(cognition_dir).parent)
        if root is None:
            return None
        svn = _svn_command()
        if svn is None:
            return SvnHygieneReport(STATUS_NO_CLI)
        return _run_pass(svn, root, Path(cognition_dir))
    except Exception as exc:  # noqa: BLE001
        logger.debug("svn-hygiene: pass failed (swallowed): %s", exc)
        return SvnHygieneReport(STATUS_FAILED, detail=str(exc))


def add_new_files_in_background(cognition_dir: Path) -> threading.Thread | None:
    """Schedule newly written files (a profile, a document) for addition without
    blocking the caller, via the full ordered pass -- property before any add. One
    at a time; a run already in flight covers this one. Returns the thread started."""
    try:
        if vcs_hygiene_opted_out():
            return None
        root = svn_root(Path(cognition_dir).parent)
        if root is None:
            return None
        svn = _svn_command()
        if svn is None:
            return None
    except Exception:  # noqa: BLE001
        return None

    def _work() -> None:
        if not _background_lock.acquire(blocking=False):
            return
        try:
            _run_pass(svn, root, Path(cognition_dir))
        except Exception as exc:  # noqa: BLE001
            logger.debug("svn-hygiene: background pass failed (swallowed): %s", exc)
        finally:
            _background_lock.release()

    thread = threading.Thread(target=_work, name="vibe-svn-add", daemon=True)
    thread.start()
    return thread


def format_svn_announce(report: SvnHygieneReport, resolve_command: str) -> str:
    """The session-start line for an SVN working copy. Deliberately not reassuring:
    it says what was changed, what is still uncommitted, and what is wrong."""
    no_union = (
        "SVN has no union merge. Each person writes their own journal file, so teammates "
        "do not conflict; the same person in two checkouts, or the legacy journal, still "
        f"can. Resolve by keeping BOTH sides with `{resolve_command}` -- never 'use mine' "
        "or 'use theirs', which delete memories."
    )
    manual = "follow 'Team setup (svn)' in cognition_readme by hand"
    if report.status == STATUS_NO_CLI:
        return (
            "vibe-cognition (svn): this is a Subversion working copy, but the `svn` "
            "command-line client is not on PATH, so .cognition/ was NOT set up for "
            "version control. Install the command-line client (TortoiseSVN: re-run its "
            f"installer and enable 'command line client tools'), or {manual}. {no_union}"
        )
    if report.status == STATUS_PARENT_UNVERSIONED:
        return (
            "vibe-cognition (svn): the folder containing .cognition/ is not under version "
            "control, so .cognition/ was NOT added and the memory graph is not shared. Add "
            f"the project folder to SVN, then start a new session. {no_union}"
        )
    if report.status == STATUS_COGNITION_IGNORED:
        return (
            "vibe-cognition (svn): .cognition/ is excluded by an svn:ignore or "
            "svn:global-ignores rule above it, so the memory graph is NOT shared with "
            f"the team. Remove .cognition from that rule. {no_union}"
        )
    if report.status == STATUS_FAILED:
        return (
            "vibe-cognition (svn): automatic SVN setup could not finish this session "
            f"({report.detail or 'unknown error'}); it retries next session. {no_union}"
        )

    parts: list[str] = []
    if report.conflicted:
        parts.append(
            f"CONFLICT in {', '.join(report.conflicted)}. Resolve with `{resolve_command}`, "
            "which keeps both sides, then reload. TELL THE USER."
        )
        no_union = (
            "SVN has no union merge -- never 'use mine' or 'use theirs', which delete "
            "memories."
        )
    if report.journal_excluded:
        parts.append(
            "WARNING: the journal (.cognition/journal/ or .cognition/journal.jsonl) is "
            "excluded by an inherited ignore rule, so "
            "memories are never shared. Remove the rule that matches it. TELL THE USER."
        )
    changed = []
    if report.property_set_now:
        changed.append(f"set {IGNORE_PROPERTY} on .cognition/")
    if report.added_now:
        changed.append(f"scheduled {len(report.added_now)} new item(s) for addition")
    if changed:
        parts.append(
            "vibe-cognition (svn) changed your working copy: " + " and ".join(changed)
            + ". Nothing was committed."
        )
    if report.pending_adds or report.property_pending:
        pending = []
        if report.pending_adds:
            pending.append(f"{report.pending_adds} item(s) scheduled for addition")
        if report.property_pending:
            pending.append("the ignore property on .cognition/ (`M` in the SECOND column of `svn status`)")
        parts.append(
            "Uncommitted under .cognition/: " + " and ".join(pending)
            + ". Review with `svn status .cognition` and commit, or teammates never see them."
        )
    if not parts:
        parts.append("vibe-cognition (svn): .cognition/ is set up and nothing is pending.")
    parts.append(no_union)
    return " ".join(parts)
