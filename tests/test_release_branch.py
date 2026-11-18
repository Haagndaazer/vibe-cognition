"""The trimmed `release` branch that Anthropic's plugin directory tracks."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tools"))

import build_release_branch as brb  # noqa: E402

_VERSION = "1.2.3"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _write(repo: Path, path: str, content: str | bytes) -> None:
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        file.write_bytes(content)
    else:
        file.write_bytes(content.encode("utf-8"))


def _commit(repo: Path, message: str = "change") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, ".claude-plugin/plugin.json", json.dumps({"name": "p", "version": _VERSION}))
    _write(root, ".claude-plugin/icon.png", b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + b"\0" * 64)
    _write(root, "pyproject.toml", f'[project]\nname = "p"\nversion = "{_VERSION}"\n')
    _write(root, "uv.lock", "x" * (brb.MAX_TEXT_BYTES + 1))
    _write(
        root,
        "src/vibe_cognition/dashboard/static/vendor/cytoscape.min.js",
        "y" * (brb.MAX_TEXT_BYTES + 1),
    )
    _write(root, "src/vibe_cognition/server.py", "print('server')\n")
    _write(root, "hooks/hooks.json", "{}")
    _write(root, "hooks/session-start.sh", "#!/bin/bash\necho hi\n")
    _write(root, "skills/demo/SKILL.md", "# demo\n")
    _write(root, "agents/plan.md", "# plan\n")
    _write(root, "README.md", "# readme\n")
    _write(root, "LICENSE", "MIT\n")
    _write(root, "CHANGELOG.md", "# changes\n")
    _write(root, ".gitattributes", ".cognition/journal.jsonl merge=union -text\n")
    _write(root, ".cognition/journal.jsonl", '{"private": true}\n')
    _write(root, "tests/test_x.py", "def test_x(): pass\n")
    _write(root, "docs/plan.md", "# plan\n")
    _write(root, "CLAUDE.md", "# dev notes\n")
    _write(root, "tools/render.py", "pass\n")
    _write(root, "adapters/codex/x.md", "x\n")
    _commit(root, "initial")
    _git(root, "update-index", "--chmod=+x", "hooks/session-start.sh")
    _commit(root, "exec bit")
    return root


def _release_paths(repo: Path, branch: str = "release") -> list[str]:
    return _git(repo, "ls-tree", "-r", "--name-only", branch).splitlines()


def test_release_tree_holds_only_what_an_install_runs(repo):
    tip, changed, _entries, version = brb.build("HEAD", "release", repo)

    assert (changed, version) == (True, _VERSION)
    assert tip == _git(repo, "rev-parse", "release")
    assert _release_paths(repo) == [
        ".claude-plugin/icon.png",
        ".claude-plugin/plugin.json",
        "CHANGELOG.md",
        "LICENSE",
        "README.md",
        "agents/plan.md",
        "hooks/hooks.json",
        "hooks/session-start.sh",
        "pyproject.toml",
        "skills/demo/SKILL.md",
        "src/vibe_cognition/dashboard/static/vendor/cytoscape.min.js",
        "src/vibe_cognition/server.py",
        "uv.lock",
    ]


def test_release_tree_keeps_blobs_and_modes(repo):
    brb.build("HEAD", "release", repo)

    for path in ("hooks/session-start.sh", "src/vibe_cognition/server.py", ".claude-plugin/icon.png"):
        assert _git(repo, "ls-tree", "release", path) == _git(repo, "ls-tree", "HEAD", path)
    assert _git(repo, "ls-tree", "release", "hooks/session-start.sh").startswith("100755 ")


def test_build_leaves_the_checkout_alone(repo):
    head_before = _git(repo, "rev-parse", "HEAD")
    _write(repo, "src/vibe_cognition/server.py", "print('uncommitted edit')\n")

    brb.build("HEAD", "release", repo)

    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "status", "--porcelain") == "M src/vibe_cognition/server.py"
    assert _git(repo, "show", "release:src/vibe_cognition/server.py") == "print('server')"


def test_release_commit_names_the_version_and_source(repo):
    source = _git(repo, "rev-parse", "HEAD")

    brb.build("HEAD", "release", repo)

    message = _git(repo, "log", "-1", "--format=%B", "release")
    assert message.startswith(f"Release v{_VERSION}\n")
    assert source in message


def test_second_release_stacks_on_the_first_and_an_unchanged_tree_adds_nothing(repo):
    first = brb.build("HEAD", "release", repo)[0]
    _write(repo, "docs/plan.md", "# only an excluded file changed\n")
    _commit(repo)

    same, changed = brb.build("HEAD", "release", repo)[:2]
    assert (same, changed) == (first, False)

    _write(repo, "src/vibe_cognition/server.py", "print('v2')\n")
    _commit(repo)
    second, changed = brb.build("HEAD", "release", repo)[:2]

    assert changed is True
    assert _git(repo, "rev-parse", "release^") == first
    assert _git(repo, "show", "release:src/vibe_cognition/server.py") == "print('v2')"
    assert second != first


def test_release_blobs_stay_lf_when_the_checkout_is_crlf(repo):
    _git(repo, "config", "core.autocrlf", "true")
    _write(repo, "hooks/session-start.sh", "#!/bin/bash\r\necho crlf checkout\r\n")
    _commit(repo)

    brb.build("HEAD", "release", repo)

    blob = subprocess.run(
        ["git", "cat-file", "blob", "release:hooks/session-start.sh"],
        cwd=repo, capture_output=True, check=True,
    ).stdout
    assert blob == b"#!/bin/bash\necho crlf checkout\n"


def test_a_release_branch_that_exists_only_on_origin_is_continued(repo):
    first = brb.build("HEAD", "release", repo)[0]
    _git(repo, "update-ref", "refs/remotes/origin/release", first)
    _git(repo, "update-ref", "-d", "refs/heads/release")
    _write(repo, "src/vibe_cognition/server.py", "print('v2')\n")
    _commit(repo)

    changed = brb.build("HEAD", "release", repo)[1]

    assert changed is True
    assert _git(repo, "rev-parse", "release^") == first


def _two_releases(repo):
    first = brb.build("HEAD", "release", repo)[0]
    _write(repo, "src/vibe_cognition/server.py", "print('v2')\n")
    _commit(repo)
    second = brb.build("HEAD", "release", repo)[0]
    _write(repo, "src/vibe_cognition/server.py", "print('v3')\n")
    _commit(repo)
    return first, second


def test_a_local_release_branch_behind_origin_builds_on_origin(repo):
    first, second = _two_releases(repo)
    _git(repo, "update-ref", "refs/remotes/origin/release", second)
    _git(repo, "update-ref", "refs/heads/release", first)

    brb.build("HEAD", "release", repo)

    assert _git(repo, "rev-parse", "release^") == second


def test_a_release_branch_that_diverged_from_origin_is_refused(repo):
    first, second = _two_releases(repo)
    other = _git(repo, "commit-tree", f"{second}^{{tree}}", "-p", first, "-m", "elsewhere")
    _git(repo, "update-ref", "refs/remotes/origin/release", other)

    with pytest.raises(RuntimeError, match="local release and origin/release have diverged"):
        brb.build("HEAD", "release", repo)

    assert _git(repo, "rev-parse", "release") == second


def test_building_onto_the_checked_out_branch_is_refused(repo):
    head = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(RuntimeError, match="main is the checked-out branch"):
        brb.build("HEAD", "main", repo)

    assert _git(repo, "rev-parse", "main") == head


def test_a_path_with_spaces_and_non_ascii_characters_is_carried(repo):
    _write(repo, "skills/demo/notes d\u00e9j\u00e0 vu.md", "# notes\n")
    _commit(repo)

    brb.build("HEAD", "release", repo)

    listing = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "--name-only", "release"],
        cwd=repo, capture_output=True, check=True,
    ).stdout.decode("utf-8")
    assert "skills/demo/notes d\u00e9j\u00e0 vu.md" in listing.split("\0")


def test_a_readme_link_that_leaves_the_release_tree_is_refused(repo):
    _write(repo, "README.md", "# readme\n[guide](docs/plan.md#top) [ok](LICENSE) [web](https://x.invalid)\n")
    _commit(repo)

    with pytest.raises(RuntimeError, match=r"README.md: link to docs/plan.md#top leaves"):
        brb.build("HEAD", "release", repo)


def test_build_from_an_older_commit(repo):
    old = _git(repo, "rev-parse", "HEAD")
    _write(repo, "src/vibe_cognition/server.py", "print('newer')\n")
    _commit(repo)

    brb.build(old, "release", repo)

    assert _git(repo, "show", "release:src/vibe_cognition/server.py") == "print('server')"


@pytest.mark.parametrize(
    ("path", "content", "reason"),
    [
        pytest.param(
            "src/vibe_cognition/big.py", "z" * (brb.MAX_TEXT_BYTES + 1),
            "over the 262144 byte limit", id="oversized",
        ),
        pytest.param(
            "src/vibe_cognition/blob.bin", b"\x00\x01\x02binary",
            "binary file that is not an image", id="binary",
        ),
        pytest.param(
            "src/vibe_cognition/.DS_Store", "junk", "operating-system junk file", id="junk",
        ),
        pytest.param(
            "pyproject.toml", '[project]\nversion = "9.9.9"\n', "version mismatch",
            id="version",
        ),
    ],
)
def test_build_refuses_a_tree_the_directory_would_reject(repo, path, content, reason):
    _write(repo, path, content)
    _commit(repo)

    with pytest.raises(RuntimeError, match=reason):
        brb.build("HEAD", "release", repo)

    assert "release" not in _git(repo, "branch", "--list", "release")


def test_too_many_files_is_refused(repo):
    for i in range(brb.MAX_FILES):
        _write(repo, f"src/vibe_cognition/gen/m{i}.py", "pass\n")
    _commit(repo)

    with pytest.raises(RuntimeError, match=f"the directory allows {brb.MAX_FILES}"):
        brb.build("HEAD", "release", repo)


def test_a_required_file_missing_is_refused(repo):
    (repo / "hooks" / "hooks.json").unlink()
    _commit(repo)

    with pytest.raises(RuntimeError, match="hooks/hooks.json: required file is missing"):
        brb.build("HEAD", "release", repo)


def test_an_expected_hold_that_shrank_must_leave_the_list(repo):
    _write(repo, "uv.lock", "small\n")
    _commit(repo)

    with pytest.raises(RuntimeError, match="uv.lock: listed as an expected reviewer hold"):
        brb.build("HEAD", "release", repo)


def test_a_symlink_is_refused():
    entries = [brb.Entry("src/vibe_cognition/link.py", "120000", 10, b"target")]

    assert "src/vibe_cognition/link.py: not a regular file (mode 120000)" in brb.find_problems(entries)


def test_images_and_fonts_are_exempt_from_the_text_limit_but_not_the_file_cap():
    big = brb.MAX_TEXT_BYTES + 1
    entries = [
        brb.Entry("skills/a/shot.png", "100644", big, b"\x89PNG\0"),
        brb.Entry("skills/a/font.woff2", "100644", big, b"wOF2\0"),
        brb.Entry("skills/a/huge.png", "100644", brb.MAX_FILE_BYTES + 1, b"\x89PNG\0"),
    ]

    flagged = [p for p in brb.find_problems(entries) if p.startswith("skills/")]

    assert flagged == [f"skills/a/huge.png: {brb.MAX_FILE_BYTES + 1} bytes, over the {brb.MAX_FILE_BYTES} byte cap"]


def test_check_mode_writes_nothing(repo, monkeypatch, capsys):
    monkeypatch.setattr(brb, "REPO_ROOT", repo)

    assert brb.main(["--check"]) == 0

    assert _git(repo, "branch", "--list", "release") == ""
    out = capsys.readouterr().out
    assert f"Release tree for v{_VERSION} is buildable" in out
    assert "Expected reviewer hold: uv.lock" in out


def test_this_repository_fits_the_directory_limits():
    """Fails when a shipped file grows past a directory limit, at the point the
    change is still in front of its author. Reads tracked and staged files only:
    a new file must be `git add`ed to count."""
    entries = brb.worktree_entries(_REPO)

    assert len(entries) > 50, "the worktree scan found almost nothing: the scan is broken"
    assert brb.find_problems(entries) == []
    readme = (_REPO / "README.md").read_text(encoding="utf-8")
    assert brb.broken_readme_links(readme, {e.path for e in entries}) == []
    assert sorted(brb.EXPECTED_HOLDS) == sorted(
        e.path for e in entries if e.size > brb.MAX_TEXT_BYTES and not brb._is_media(e.path)
    )


def test_this_repository_ships_a_valid_icon():
    data = (_REPO / ".claude-plugin" / "icon.png").read_bytes()

    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    assert width == height and 512 <= width <= 2048
    assert len(data) < 2 * 1024 * 1024
