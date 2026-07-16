"""Tests for legacy identity backfill (task 962ab7b442d5, design doc rev 2).

Fixture corpus mirrors the design doc's own test-plan sketch: a real git repo
with controlled per-commit authorship, built via the ACTUAL storage.add_node
write path (never hand-crafted JSONL) so blame sees exactly the bytes
production code would have written.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from vibe_cognition.backfill_identity import (
    BackfillPlan,
    _run_git,
    apply_plan,
    blame_suggestions,
    eligibility,
    main,
    parse_map_args,
    parse_map_file,
    roster_suggestions,
)
from vibe_cognition.cognition import CognitionNode, CognitionNodeType, CognitionStorage

# ── Fixture helpers ───────────────────────────────────────────────────────────


def _git(repo: Path, *args: str, env_overrides: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True,
    )
    return result.stdout


def _init_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    cognition = repo / ".cognition"
    cognition.mkdir()
    return cognition


def _commit_journal(repo: Path, name: str, email: str, when: str, message: str = "journal") -> None:
    """Commit whatever new bytes are currently in .cognition/journal.jsonl under
    the given author/committer identity and timestamp (ISO-ish 'epoch +0000')."""
    _git(repo, "add", "-f", ".cognition/journal.jsonl")
    env = {
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
        "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
    }
    _git(repo, "commit", "-q", "-m", message, env_overrides=env)


def _node(node_id: str, author: str, node_type: CognitionNodeType = CognitionNodeType.DECISION,
          metadata: dict | None = None, summary: str = "s") -> CognitionNode:
    return CognitionNode(
        id=node_id, type=node_type, summary=summary, detail="d", context=[], references=[],
        timestamp="2026-01-01T00:00:00+00:00", author=author, metadata=metadata or {},
    )


def _person(storage: CognitionStorage, node_id: str, name: str, email: str) -> None:
    storage.add_node(CognitionNode(
        id=node_id, type=CognitionNodeType.PERSON, summary=f"Person: {name}", detail="",
        context=[], references=[], timestamp="2026-01-01T00:00:00+00:00", author=name,
        metadata={"person": {"name": name, "email": email}},
    ))


# ── eligibility (H2 skip predicate) ──────────────────────────────────────────


def test_eligibility_out_of_scope_for_document_and_person():
    doc = {"type": "document", "metadata": {}}
    person = {"type": "person", "metadata": {}}
    assert eligibility(doc, recompute_backfilled=False) == (False, "out-of-scope")
    assert eligibility(person, recompute_backfilled=False) == (False, "out-of-scope")


def test_eligibility_server_stamped_non_empty_email_never_eligible():
    node = {"type": "decision", "metadata": {"recorded_by": {"name": "A", "email": "a@x.com"}}}
    assert eligibility(node, recompute_backfilled=False) == (False, "server-stamped")
    assert eligibility(node, recompute_backfilled=True) == (False, "server-stamped")


def test_eligibility_empty_email_legacy_stamp_is_eligible():
    """Fails-before: a key-presence-only skip predicate would wrongly treat a
    pre-P13n-1 empty-email stamp as 'already stamped'."""
    node = {"type": "decision", "metadata": {"recorded_by": {"name": "unknown", "email": ""}}}
    assert eligibility(node, recompute_backfilled=False) == (True, "unstamped")


def test_eligibility_unstamped_task_uses_created_by_key():
    node = {"type": "task", "metadata": {}}
    assert eligibility(node, recompute_backfilled=False) == (True, "unstamped")


def test_eligibility_backfilled_marker_only_recomputable_with_flag():
    node = {"type": "decision", "metadata": {
        "recorded_by": {"name": "A", "email": "a@x.com", "backfilled": True, "backfill_source": "roster"},
    }}
    assert eligibility(node, recompute_backfilled=False) == (False, "already-backfilled")
    assert eligibility(node, recompute_backfilled=True) == (True, "already-backfilled")


# ── roster_suggestions ────────────────────────────────────────────────────────


def test_roster_suggestions_unambiguous_match(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p1", "Casey Lead", "casey@x.com")
    assert roster_suggestions(storage) == {"casey lead": "casey@x.com"}


def test_roster_suggestions_collision_excluded(tmp_path):
    """Two registered persons sharing a casefolded name -> unmappable by this
    source, falls through entirely (never picked arbitrarily)."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _person(storage, "p1", "Colton Dyck", "colton.dyck@acryliccode.com")
    _person(storage, "p2", "colton dyck", "colton.dyck@studiomonsoon.net")
    assert roster_suggestions(storage) == {}


# ── parse_map_args / parse_map_file ──────────────────────────────────────────


def test_parse_map_args_basic():
    out = parse_map_args(["Vince=vince@x.com", " Vorpid = vorpid@x.com "])
    assert out == {"vince": ("vince@x.com", "manual"), "vorpid": ("vorpid@x.com", "manual")}


def test_parse_map_args_rejects_malformed_entry():
    with pytest.raises(ValueError):
        parse_map_args(["not-a-mapping"])


def test_parse_map_file_many_aliases_to_one_email(tmp_path):
    p = tmp_path / "map.json"
    p.write_text(json.dumps([
        {"email": "colton.dyck@acryliccode.com", "aliases": ["Colton Dyck", "colto"], "source": "git-history"},
        {"email": "", "aliases": ["Nobody"]},  # unfinished skeleton row -- skipped
    ]), encoding="utf-8")
    out = parse_map_file(p)
    assert out == {
        "colton dyck": ("colton.dyck@acryliccode.com", "git-history"),
        "colto": ("colton.dyck@acryliccode.com", "git-history"),
    }


def test_parse_map_file_defaults_source_to_manual_when_absent(tmp_path):
    p = tmp_path / "map.json"
    p.write_text(json.dumps([{"email": "a@x.com", "aliases": ["A"]}]), encoding="utf-8")
    assert parse_map_file(p) == {"a": ("a@x.com", "manual")}


# ── blame_suggestions (real git repo fixtures) ───────────────────────────────


def test_blame_single_consistent_email_suggested(tmp_path):
    repo = tmp_path / "repo"
    cognition = _init_repo(repo)
    storage = CognitionStorage(cognition)
    storage.add_node(_node("n1", "Vince"))
    _commit_journal(repo, "Vince", "vince@x.com", "1700000000 +0000")

    suggestions, drift = blame_suggestions(repo, cognition, {"n1": "Vince"})
    assert suggestions == {"vince": "vince@x.com"}
    assert drift == set()


def test_blame_name_disagreement_gate_excludes_flusher_attribution(tmp_path):
    """Fails-before: an ungated blame match would wrongly attribute this node
    to the flusher's identity instead of leaving it unsuggested (the exact
    shared-worktree flush-protocol failure mode the design doc calls out)."""
    repo = tmp_path / "repo"
    cognition = _init_repo(repo)
    storage = CognitionStorage(cognition)
    storage.add_node(_node("n1", "Someone Else"))
    _commit_journal(repo, "The Flusher", "flusher@x.com", "1700000000 +0000")

    suggestions, _drift = blame_suggestions(repo, cognition, {"n1": "Someone Else"})
    assert suggestions == {}


def test_blame_email_drift_suggests_most_recent(tmp_path):
    """Same author NAME blamed to two distinct emails across the node set ->
    flagged as drift; the SUGGESTION is the most-recent email (ruling 6),
    never auto-picked as a write."""
    repo = tmp_path / "repo"
    cognition = _init_repo(repo)
    storage = CognitionStorage(cognition)
    storage.add_node(_node("n1", "Colton Dyck"))
    _commit_journal(repo, "Colton Dyck", "colton.dyck@studiomonsoon.net", "1600000000 +0000")
    storage.add_node(_node("n2", "Colton Dyck"))
    _commit_journal(repo, "Colton Dyck", "colton.dyck@acryliccode.com", "1700000000 +0000")

    suggestions, drift = blame_suggestions(
        repo, cognition, {"n1": "Colton Dyck", "n2": "Colton Dyck"},
    )
    assert drift == {"colton dyck"}
    assert suggestions["colton dyck"] == "colton.dyck@acryliccode.com"  # later author-time wins


def test_blame_bulk_rewrite_commit_excluded(tmp_path):
    """A single commit introducing many journal lines at once, well past both
    the absolute floor and the ratio-of-file-at-that-commit threshold, is
    excluded from blame entirely -- a flush/rehydrate dump, not organic
    per-node authorship."""
    repo = tmp_path / "repo"
    cognition = _init_repo(repo)
    storage = CognitionStorage(cognition)
    node_authors = {}
    for i in range(25):
        nid = f"bulk{i}"
        storage.add_node(_node(nid, "Bulk Author"))
        node_authors[nid] = "Bulk Author"
    _commit_journal(repo, "The Flusher", "flusher@x.com", "1700000000 +0000")

    suggestions, _drift = blame_suggestions(repo, cognition, node_authors)
    assert suggestions == {}


def test_run_git_decodes_non_cp1252_bytes_without_crashing(tmp_path, monkeypatch):
    """Fails-before (observed live on this repo's own history, via git blame's
    porcelain output): `text=True` decodes subprocess output with the
    platform-locale codec -- cp1252 on Windows -- which raises
    UnicodeDecodeError on real commit content containing e.g. byte 0x9d.
    `_run_git` must decode as UTF-8 (replacing, not raising) instead."""
    class _FakeResult:
        returncode = 0
        stdout = b"line one\nauthor bad-byte-\x9d-here\n"

    def _fake_run(*a, **kw):
        assert "text" not in kw  # must decode manually, not rely on text=True
        return _FakeResult()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    out = _run_git(tmp_path, ["blame", "--line-porcelain", "--", ".cognition/journal.jsonl"])
    assert out is not None
    assert "bad-byte" in out  # decoded (with replacement), never raised


def test_blame_returns_empty_when_not_a_git_repo(tmp_path):
    cognition = tmp_path / "no-repo" / ".cognition"
    cognition.mkdir(parents=True)
    (cognition / "journal.jsonl").write_text("", encoding="utf-8")
    suggestions, drift = blame_suggestions(tmp_path / "no-repo", cognition, {"n1": "Anyone"})
    assert suggestions == {} and drift == set()


# ── BackfillPlan / apply_plan integration ────────────────────────────────────


def test_dry_run_never_writes_journal_byte_identical(tmp_path):
    repo = tmp_path / "repo"
    cognition = _init_repo(repo)
    storage = CognitionStorage(cognition)
    storage.add_node(_node("n1", "Vince"))
    _commit_journal(repo, "Vince", "vince@x.com", "1700000000 +0000")
    before = (cognition / "journal.jsonl").read_bytes()

    plan = BackfillPlan(storage, recompute_backfilled=False, confirmed={}, repo_path=repo)
    assert plan.to_write == []  # nothing confirmed yet -- dry run only

    after = (cognition / "journal.jsonl").read_bytes()
    assert after == before


def test_apply_writes_confirmed_mapping_and_marks_backfilled(tmp_path):
    repo = tmp_path / "repo"
    cognition = _init_repo(repo)
    storage = CognitionStorage(cognition)
    storage.add_node(_node("n1", "Vince"))
    _commit_journal(repo, "Vince", "vince@x.com", "1700000000 +0000")

    confirmed = {"vince": ("vince@x.com", "git-history")}
    plan = BackfillPlan(storage, recompute_backfilled=False, confirmed=confirmed, repo_path=repo)
    assert len(plan.to_write) == 1

    written = apply_plan(plan)
    assert written == 1
    node = storage.get_node("n1")
    assert node is not None
    assert node["metadata"]["recorded_by"] == {
        "name": "Vince", "email": "vince@x.com", "backfilled": True, "backfill_source": "git-history",
    }
    assert node["author"] == "Vince"  # untouched


def test_apply_appends_exactly_n_update_node_events_and_replay_reproduces(tmp_path):
    repo = tmp_path / "repo"
    cognition = _init_repo(repo)
    storage = CognitionStorage(cognition)
    storage.add_node(_node("n1", "Vince"))
    storage.add_node(_node("n2", "Vince"))
    _commit_journal(repo, "Vince", "vince@x.com", "1700000000 +0000")

    lines_before = (cognition / "journal.jsonl").read_text(encoding="utf-8").strip().splitlines()
    plan = BackfillPlan(
        storage, recompute_backfilled=False,
        confirmed={"vince": ("vince@x.com", "manual")}, repo_path=repo,
    )
    written = apply_plan(plan)
    assert written == 2

    lines_after = (cognition / "journal.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines_after) - len(lines_before) == 2
    assert all(json.loads(line)["action"] == "update_node" for line in lines_after[len(lines_before):])

    replayed = CognitionStorage(cognition)
    for nid in ("n1", "n2"):
        replayed_node = replayed.get_node(nid)
        assert replayed_node is not None
        assert replayed_node["metadata"]["recorded_by"]["email"] == "vince@x.com"


def test_apply_is_idempotent_second_run_writes_nothing(tmp_path):
    repo = tmp_path / "repo"
    cognition = _init_repo(repo)
    storage = CognitionStorage(cognition)
    storage.add_node(_node("n1", "Vince"))
    _commit_journal(repo, "Vince", "vince@x.com", "1700000000 +0000")
    confirmed = {"vince": ("vince@x.com", "manual")}

    plan1 = BackfillPlan(storage, recompute_backfilled=False, confirmed=confirmed, repo_path=repo)
    assert apply_plan(plan1) == 1

    plan2 = BackfillPlan(storage, recompute_backfilled=False, confirmed=confirmed, repo_path=repo)
    assert plan2.to_write == []  # now server-stamped (backfilled), not eligible again
    assert apply_plan(plan2) == 0


def test_recompute_backfilled_only_overwrites_marker_carrying_stamps(tmp_path):
    repo = tmp_path / "repo"
    cognition = _init_repo(repo)
    storage = CognitionStorage(cognition)
    storage.add_node(_node("backfilled", "Vince", metadata={
        "recorded_by": {"name": "Vince", "email": "old@x.com", "backfilled": True, "backfill_source": "roster"},
    }))
    storage.add_node(_node("server_stamped", "Vince", metadata={
        "recorded_by": {"name": "Vince", "email": "server@x.com"},  # no marker -- server-resolved
    }))
    _commit_journal(repo, "Vince", "vince@x.com", "1700000000 +0000")

    confirmed = {"vince": ("new@x.com", "manual")}
    plan = BackfillPlan(storage, recompute_backfilled=True, confirmed=confirmed, repo_path=repo)
    written_ids = {n["id"] for n, _e, _s in plan.to_write}
    assert written_ids == {"backfilled"}  # server_stamped stays untouchable under every flag

    apply_plan(plan)
    backfilled_node = storage.get_node("backfilled")
    server_stamped_node = storage.get_node("server_stamped")
    assert backfilled_node is not None and server_stamped_node is not None
    assert backfilled_node["metadata"]["recorded_by"]["email"] == "new@x.com"
    assert server_stamped_node["metadata"]["recorded_by"]["email"] == "server@x.com"


def test_no_author_row_left_unstamped(tmp_path):
    repo = tmp_path / "repo"
    cognition = _init_repo(repo)
    storage = CognitionStorage(cognition)
    storage.add_node(_node("n1", ""))
    _commit_journal(repo, "Someone", "someone@x.com", "1700000000 +0000")

    plan = BackfillPlan(storage, recompute_backfilled=False, confirmed={}, repo_path=repo)
    assert plan.to_write == []
    assert plan.node_counts_by_name["(no author)"] == 1
    assert "(no author)".casefold() not in plan.unconfirmed_names()  # never asked to be mapped


def test_auto_flip_forecast_flips_on_confirmed_backfill(tmp_path):
    repo = tmp_path / "repo"
    cognition = _init_repo(repo)
    storage = CognitionStorage(cognition)
    storage.add_node(_node("solo", "Solo Writer", metadata={
        "recorded_by": {"name": "Solo Writer", "email": "solo@x.com"},
    }))
    storage.add_node(_node("legacy", "Legacy Writer"))
    _commit_journal(repo, "Solo Writer", "solo@x.com", "1700000000 +0000")

    plan = BackfillPlan(storage, recompute_backfilled=False, confirmed={}, repo_path=repo)
    before, after_noop = plan.stamped_email_forecast()
    assert before == 1 and after_noop == 1  # nothing confirmed -- no flip yet

    plan2 = BackfillPlan(
        storage, recompute_backfilled=False,
        confirmed={"legacy writer": ("legacy@x.com", "manual")}, repo_path=repo,
    )
    before2, after2 = plan2.stamped_email_forecast()
    assert before2 == 1 and after2 == 2  # confirmed backfill flips distinct-email count 1 -> 2


# ── CLI end-to-end ────────────────────────────────────────────────────────────


def test_cli_dry_run_writes_skeleton_and_no_journal_change(tmp_path, capsys):
    repo = tmp_path / "repo"
    cognition = _init_repo(repo)
    storage = CognitionStorage(cognition)
    storage.add_node(_node("n1", "Vince"))
    _commit_journal(repo, "Vince", "vince@x.com", "1700000000 +0000")
    before = (cognition / "journal.jsonl").read_bytes()

    rc = main([str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Vince" in out
    assert "Skeleton map file written" in out

    skeleton_path = cognition / "backfill-identity-map.skeleton.json"
    assert skeleton_path.exists()
    skeleton = json.loads(skeleton_path.read_text(encoding="utf-8"))
    assert skeleton[0]["aliases"] == ["Vince"]
    assert skeleton[0]["email"] == "vince@x.com"  # git-history suggestion pre-filled
    assert skeleton[0]["source"] == "git-history"

    assert (cognition / "journal.jsonl").read_bytes() == before


def test_cli_apply_with_map_file_stamps_and_reports_count(tmp_path, capsys):
    repo = tmp_path / "repo"
    cognition = _init_repo(repo)
    storage = CognitionStorage(cognition)
    storage.add_node(_node("n1", "Vince"))
    _commit_journal(repo, "Vince", "vince@x.com", "1700000000 +0000")

    map_file = tmp_path / "map.json"
    map_file.write_text(json.dumps([
        {"email": "vince@x.com", "aliases": ["Vince"], "source": "git-history"},
    ]), encoding="utf-8")

    rc = main([str(repo), "--map-file", str(map_file), "--apply"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Stamped 1 node" in out

    replayed = CognitionStorage(cognition)
    node = replayed.get_node("n1")
    assert node is not None
    assert node["metadata"]["recorded_by"]["email"] == "vince@x.com"
    assert node["metadata"]["recorded_by"]["backfilled"] is True


def test_cli_apply_aborts_on_journal_mtime_change(tmp_path, capsys, monkeypatch):
    """Concurrency honesty (peer-review H5): a journal that changed since this
    run started aborts the apply, writing nothing, rather than racing a live
    session's append. Simulated by bumping the journal's mtime as a side
    effect of storage construction -- the real window a concurrent writer
    would land in between main()'s pre-load snapshot and its pre-apply
    recheck."""
    repo = tmp_path / "repo"
    cognition = _init_repo(repo)
    storage = CognitionStorage(cognition)
    storage.add_node(_node("n1", "Vince"))
    _commit_journal(repo, "Vince", "vince@x.com", "1700000000 +0000")

    map_file = tmp_path / "map.json"
    map_file.write_text(json.dumps([
        {"email": "vince@x.com", "aliases": ["Vince"]},
    ]), encoding="utf-8")

    import vibe_cognition.backfill_identity as bi
    real_storage_cls = bi.CognitionStorage

    class _RacingStorage(real_storage_cls):
        def __init__(self, cognition_dir):
            super().__init__(cognition_dir)
            p = cognition_dir / "journal.jsonl"
            future = p.stat().st_mtime + 1000
            os.utime(p, (future, future))

    monkeypatch.setattr(bi, "CognitionStorage", _RacingStorage)
    rc = main([str(repo), "--map-file", str(map_file), "--apply"])
    assert rc == 3
    err = capsys.readouterr().err
    assert "journal changed" in err

    replayed = CognitionStorage(cognition)
    assert "recorded_by" not in (replayed.get_node("n1") or {}).get("metadata", {})
