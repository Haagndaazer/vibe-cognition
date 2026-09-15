"""Per-person journal files through a real git union merge (git is on every CI leg)."""

import os
import shutil
import subprocess

import pytest

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.git_hygiene import ensure_git_hygiene
from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def _git(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.com"}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=env, check=True).stdout


def _decision(node_id):
    return CognitionNode(
        id=node_id, type=CognitionNodeType.DECISION, summary=node_id, detail="d",
        context=[], references=[], timestamp="2026-01-01T00:00:00+00:00", author="t",
    )


def test_one_persons_shard_edited_in_two_clones_union_merges_into_one_graph(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    a = tmp_path / "a"
    subprocess.run(["git", "clone", "-q", str(origin), str(a)], check=True, capture_output=True)
    store = CognitionStorage(a / ".cognition")
    store.add_node(_decision("base"))
    ensure_git_hygiene(a, a / ".cognition")
    assert ".cognition/journal/*.jsonl merge=union" in (a / ".gitattributes").read_text(encoding="utf-8")
    _git(a, "add", "-A")
    _git(a, "commit", "-q", "-m", "base")
    _git(a, "push", "-q", "origin", "HEAD")

    b = tmp_path / "b"
    subprocess.run(["git", "clone", "-q", str(origin), str(b)], check=True, capture_output=True)

    CognitionStorage(a / ".cognition").add_node(_decision("from-a"))
    _git(a, "commit", "-q", "-am", "a")
    _git(a, "push", "-q", "origin", "HEAD")
    CognitionStorage(b / ".cognition").add_node(_decision("from-b"))
    _git(b, "commit", "-q", "-am", "b")
    _git(b, "pull", "-q", "--no-rebase", "origin", "HEAD")

    ids = {n["id"] for n in CognitionStorage(b / ".cognition", read_only=True).get_all_nodes()}
    assert {"base", "from-a", "from-b"} <= ids
    assert "<<<<<<<" not in "".join(p.read_text(encoding="utf-8") for p in (b / ".cognition" / "journal").iterdir())
