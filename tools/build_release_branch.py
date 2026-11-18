"""Build the trimmed `release` branch that Anthropic's plugin directory tracks.

The directory scans every file on the branch it tracks, so the listing points
at a generated branch holding only what an installed plugin runs. The branch
is built from a commit with git plumbing: the working tree and the checked-out
branch are never touched, and nothing is pushed.

    uv run python tools/build_release_branch.py --check     verify only
    uv run python tools/build_release_branch.py             build `release` from HEAD
    uv run python tools/build_release_branch.py --source <sha>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

INCLUDE_DIRS = (".claude-plugin/", "agents/", "hooks/", "skills/", "src/")
INCLUDE_FILES = frozenset({"CHANGELOG.md", "LICENSE", "README.md", "pyproject.toml", "uv.lock"})

MAX_FILES = 512
MAX_TEXT_BYTES = 256 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
FONT_SUFFIXES = frozenset({".woff", ".woff2", ".ttf", ".otf"})
JUNK_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", "__MACOSX"})
REGULAR_MODES = frozenset({"100644", "100755"})

# Oversized files accepted as reviewer holds; any other is an error.
EXPECTED_HOLDS = {
    "uv.lock": "dependency lock file",
    "src/vibe_cognition/dashboard/static/vendor/cytoscape.min.js": "vendored graph library",
}

MANIFEST_PATH = ".claude-plugin/plugin.json"
PYPROJECT_PATH = "pyproject.toml"
ICON_PATH = ".claude-plugin/icon.png"
REQUIRED_PATHS = (
    MANIFEST_PATH, ICON_PATH, PYPROJECT_PATH, "uv.lock", "hooks/hooks.json", "README.md", "LICENSE",
)
_MARKDOWN_LINK = re.compile(r"\]\(([^)\s]+)\)")
_HEAD_BYTES = 8192


@dataclass(frozen=True)
class Entry:
    path: str
    mode: str
    size: int
    head: bytes
    sha: str = ""


def is_included(path: str) -> bool:
    return path in INCLUDE_FILES or path.startswith(INCLUDE_DIRS)


def select(entries: list[Entry]) -> list[Entry]:
    return sorted((e for e in entries if is_included(e.path)), key=lambda e: e.path)


def _is_media(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES | FONT_SUFFIXES


def find_problems(entries: list[Entry]) -> list[str]:
    """Why this file set would be stopped or rejected, one line per reason."""
    problems: list[str] = []
    paths = {e.path for e in entries}
    if len(entries) > MAX_FILES:
        problems.append(f"{len(entries)} files, the directory allows {MAX_FILES}")
    for required in REQUIRED_PATHS:
        if required not in paths:
            problems.append(f"{required}: required file is missing")
    for entry in entries:
        if entry.mode not in REGULAR_MODES:
            problems.append(f"{entry.path}: not a regular file (mode {entry.mode})")
            continue
        if JUNK_NAMES.intersection(entry.path.split("/")):
            problems.append(f"{entry.path}: operating-system junk file")
        if entry.size > MAX_FILE_BYTES:
            problems.append(f"{entry.path}: {entry.size} bytes, over the {MAX_FILE_BYTES} byte cap")
        if _is_media(entry.path):
            continue
        if b"\0" in entry.head:
            problems.append(f"{entry.path}: binary file that is not an image or a font")
        if entry.size > MAX_TEXT_BYTES and entry.path not in EXPECTED_HOLDS:
            problems.append(
                f"{entry.path}: {entry.size} bytes, over the {MAX_TEXT_BYTES} byte limit "
                "for a file that is not an image or a font"
            )
    by_path = {e.path: e for e in entries}
    for path in EXPECTED_HOLDS:
        held = by_path.get(path)
        if held is None or held.size <= MAX_TEXT_BYTES:
            problems.append(f"{path}: listed as an expected reviewer hold but no longer oversized")
    return problems


def broken_readme_links(readme_text: str, paths: set[str]) -> list[str]:
    """Relative README links that point outside the release tree."""
    broken = []
    for target in _MARKDOWN_LINK.findall(readme_text):
        if target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        path = target.split("#", 1)[0].removeprefix("./")
        if path not in paths:
            broken.append(f"README.md: link to {target} leaves the release tree")
    return sorted(set(broken))


def expected_holds(entries: list[Entry]) -> list[str]:
    return [
        f"{e.path}: {e.size} bytes ({EXPECTED_HOLDS[e.path]})"
        for e in entries
        if e.path in EXPECTED_HOLDS and e.size > MAX_TEXT_BYTES
    ]


def version_problems(manifest_text: str, pyproject_text: str) -> tuple[str, list[str]]:
    """(version, problems): the two version strings the release tree carries."""
    try:
        manifest_version = json.loads(manifest_text).get("version")
    except ValueError:
        manifest_version = None
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject_text)
    pyproject_version = match.group(1) if match else None
    if not manifest_version or not pyproject_version:
        return "", [f"{MANIFEST_PATH} or {PYPROJECT_PATH} has no version"]
    if manifest_version != pyproject_version:
        return "", [
            f"version mismatch: {MANIFEST_PATH} says {manifest_version}, "
            f"{PYPROJECT_PATH} says {pyproject_version}"
        ]
    return manifest_version, []


def _git(*args: str, repo: Path, stdin: bytes | None = None, env: dict | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=repo, input=stdin, capture_output=True, env=env, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _read_blobs(shas: list[str], repo: Path) -> dict[str, bytes]:
    out = _git("cat-file", "--batch", repo=repo, stdin="".join(f"{s}\n" for s in shas).encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    for sha in shas:
        end = out.index(b"\n", pos)
        size = int(out[pos:end].split()[2])
        blobs[sha] = out[end + 1 : end + 1 + size]
        pos = end + 1 + size + 1
    return blobs


def entries_at(commit: str, repo: Path = REPO_ROOT) -> list[Entry]:
    """Every included file in `commit`, with its size and first bytes."""
    listing = _git("ls-tree", "-r", "-z", commit, repo=repo).decode("utf-8")
    rows = []
    for record in listing.split("\0"):
        if not record:
            continue
        meta, path = record.split("\t", 1)
        mode, kind, sha = meta.split()
        if is_included(path):
            rows.append((path, mode, kind, sha))
    blobs = _read_blobs([sha for _, _, kind, sha in rows if kind == "blob"], repo)
    return sorted(
        (
            Entry(path, mode, len(blobs.get(sha, b"")), blobs.get(sha, b"")[:_HEAD_BYTES], sha)
            for path, mode, _kind, sha in rows
        ),
        key=lambda e: e.path,
    )


def worktree_entries(repo: Path = REPO_ROOT) -> list[Entry]:
    """Every included file git tracks or has staged, read from disk and sized
    as git stores it (LF line endings)."""
    listing = _git("ls-files", "-z", "--cached", repo=repo).decode("utf-8")
    entries = []
    for path in sorted(set(filter(None, listing.split("\0")))):
        file = repo / path
        if not is_included(path) or not file.is_file():
            continue
        data = file.read_bytes()
        if not _is_media(path) and b"\0" not in data[:_HEAD_BYTES]:
            data = data.replace(b"\r\n", b"\n")
        entries.append(Entry(path, "100644", len(data), data[:_HEAD_BYTES]))
    return entries


def _blob_text(commit: str, path: str, repo: Path) -> str:
    return _git("show", f"{commit}:{path}", repo=repo).decode("utf-8")


def check_commit(commit: str, repo: Path = REPO_ROOT) -> tuple[list[Entry], str, list[str]]:
    """(entries, version, problems) for the release tree `commit` would produce."""
    entries = entries_at(commit, repo)
    problems = find_problems(entries)
    version = ""
    paths = {e.path for e in entries}
    if MANIFEST_PATH in paths and PYPROJECT_PATH in paths:
        version, found = version_problems(
            _blob_text(commit, MANIFEST_PATH, repo), _blob_text(commit, PYPROJECT_PATH, repo)
        )
        problems += found
    if "README.md" in paths:
        problems += broken_readme_links(_blob_text(commit, "README.md", repo), paths)
    return entries, version, problems


def _resolve(ref: str, repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo, capture_output=True, check=False
    )
    return result.stdout.decode().strip()


def _branch_tip(branch: str, repo: Path) -> tuple[str, str]:
    """(local tip, tip to build on): whichever of the local and origin branch
    is ahead. Raises RuntimeError when they have diverged."""
    local = _resolve(f"refs/heads/{branch}", repo)
    remote = _resolve(f"refs/remotes/origin/{branch}", repo)
    if not local or not remote or local == remote:
        return local, local or remote
    for behind, ahead in ((local, remote), (remote, local)):
        merged = subprocess.run(
            ["git", "merge-base", "--is-ancestor", behind, ahead], cwd=repo, check=False
        )
        if merged.returncode == 0:
            return local, ahead
    raise RuntimeError(
        f"local {branch} and origin/{branch} have diverged; reconcile them before building"
    )


def build(
    commit: str, branch: str, repo: Path = REPO_ROOT
) -> tuple[str, bool, list[Entry], str]:
    """Commit the release tree for `commit` onto `branch`, on top of the local
    or origin branch. Returns (branch tip sha, changed, entries, version).
    Raises RuntimeError when the tree has problems."""
    ref = f"refs/heads/{branch}"
    _git("check-ref-format", ref, repo=repo)
    checked_out = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=repo, capture_output=True, check=False
    ).stdout.decode().strip()
    if checked_out == ref:
        raise RuntimeError(f"{branch} is the checked-out branch; build onto another branch")
    source_sha = _git("rev-parse", "--verify", f"{commit}^{{commit}}", repo=repo).decode().strip()
    entries, version, problems = check_commit(source_sha, repo)
    if problems:
        raise RuntimeError("release tree has problems:\n" + "\n".join(f"  {p}" for p in problems))

    index_lines = [f"{e.mode} {e.sha}\t{e.path}" for e in entries]

    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(
            "update-index", "-z", "--add", "--index-info",
            repo=repo, env=env, stdin=("\0".join(index_lines) + "\0").encode("utf-8"),
        )
        tree_sha = _git("write-tree", repo=repo, env=env).decode().strip()

    local, existing = _branch_tip(branch, repo)
    old_value = local or "0" * 40
    if existing:
        existing_tree = _git("rev-parse", f"{existing}^{{tree}}", repo=repo).decode().strip()
        if existing_tree == tree_sha:
            if local != existing:
                _git("update-ref", ref, existing, old_value, repo=repo)
            return existing, False, entries, version

    message = (
        f"Release v{version}\n\n"
        f"Built from {source_sha} by tools/build_release_branch.py.\n"
    )
    parents = ["-p", existing] if existing else []
    commit_sha = (
        _git("commit-tree", tree_sha, *parents, "-m", message, repo=repo).decode().strip()
    )
    _git("update-ref", ref, commit_sha, old_value, repo=repo)
    return commit_sha, True, entries, version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", default="HEAD", help="commit to build from (default HEAD)")
    parser.add_argument("--branch", default="release", help="branch to build (default release)")
    parser.add_argument("--check", action="store_true", help="verify only, write nothing")
    args = parser.parse_args(argv)

    try:
        if args.check:
            entries, version, problems = check_commit(args.source, REPO_ROOT)
            if problems:
                print("Release tree has problems:", file=sys.stderr)
                for problem in problems:
                    print(f"  {problem}", file=sys.stderr)
                return 1
            print(f"Release tree for v{version} is buildable: {len(entries)} files.")
        else:
            tip, changed, entries, version = build(args.source, args.branch, REPO_ROOT)
            state = "updated to" if changed else "already at"
            print(f"Branch {args.branch} {state} {tip} (v{version}, {len(entries)} files).")
            print(f"Nothing was pushed. Publish with: git push origin {args.branch}")
    except (RuntimeError, UnicodeDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for hold in expected_holds(entries):
        print(f"Expected reviewer hold: {hold}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
