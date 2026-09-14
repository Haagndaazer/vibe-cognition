"""Keep-both resolution of SVN conflicts on .cognition JSONL files (no svn binary)."""

import json

from vibe_cognition.cognition.resolve_journal import (
    find_conflicts,
    main,
    merge_sides,
    resolve,
    resolve_journal_command,
)


def _line(action, **data):
    return json.dumps({"action": action, "data": data})


BASE = [_line("add_node", id="n1", summary="shared")]
MINE_NEW = [_line("update_node", id="n1", summary="mine edited"), _line("add_node", id="m1")]
THEIRS_NEW = [
    _line("update_node", id="n1", summary="theirs edited"),
    _line("add_edge", from_id="n1", to_id="t1", edge_type="led_to"),
    _line("add_node", id="t1"),
]


def _conflict(tmp_path, name="journal.jsonl", sub=""):
    cognition = tmp_path / ".cognition"
    folder = cognition / sub if sub else cognition
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    (folder / f"{name}.mine").write_text("\n".join(BASE + MINE_NEW) + "\n", encoding="utf-8")
    (folder / f"{name}.r3").write_text("\n".join(BASE) + "\n", encoding="utf-8")
    (folder / f"{name}.r7").write_text("\n".join(BASE + THEIRS_NEW) + "\n", encoding="utf-8")
    target.write_text("<<<<<<< .mine\ngarbage\n=======\n>>>>>>> .r7\n", encoding="utf-8")
    return cognition, target


def test_updates_and_edges_sharing_a_node_id_are_all_kept():
    """Deduplicating by node id -- what the docs used to say -- would delete the
    other side's update_node (same id as the add) and every edge (no id at all)."""
    merged = merge_sides(BASE + MINE_NEW, BASE + THEIRS_NEW)
    assert merged == BASE + MINE_NEW + THEIRS_NEW


def test_identical_lines_appear_once():
    assert merge_sides(BASE, BASE) == BASE


def test_resolve_keeps_both_sides_and_drops_conflict_markers(tmp_path):
    _, target = _conflict(tmp_path)
    report = resolve(target)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines == BASE + MINE_NEW + THEIRS_NEW
    assert report["yours"] == 3 and report["theirs"] == 4 and report["result"] == 6
    assert report["only_theirs_added"] == 3
    assert not any(line.startswith(("<<<<<<<", "=======", ">>>>>>>")) for line in lines)


def test_without_a_mine_file_both_sides_are_rebuilt_from_the_conflicted_file(tmp_path):
    """Review finding: a conflict whose .mine was deleted (or never written) was
    invisible to the resolver while the session start still named it."""
    _, target = _conflict(tmp_path)
    marked = "\n".join([
        *BASE, "<<<<<<< .mine", *MINE_NEW, "=======", *THEIRS_NEW, ">>>>>>> .r7",
    ]) + "\n"
    target.write_text(marked, encoding="utf-8")
    target.with_name("journal.jsonl.mine").unlink()

    report = resolve(target)
    assert report["rebuilt_from_conflicted_file"] is True
    lines = target.read_text(encoding="utf-8").splitlines()
    assert set(lines) == set(BASE + MINE_NEW + THEIRS_NEW)
    assert len(lines) == len(set(lines))


def test_an_unreadable_side_is_reported_not_raised(tmp_path, monkeypatch):
    from pathlib import Path

    _, target = _conflict(tmp_path)

    def boom(self):
        raise PermissionError("locked by another program")

    monkeypatch.setattr(Path, "read_bytes", boom)
    report = resolve(target)
    assert "error" in report and "locked" in report["error"]


def test_the_newest_revision_file_is_theirs_not_the_base(tmp_path):
    _, target = _conflict(tmp_path)
    resolve(target)
    assert _line("add_node", id="t1") in target.read_text(encoding="utf-8")


def test_dry_run_changes_nothing(tmp_path):
    _, target = _conflict(tmp_path)
    before = target.read_bytes()
    report = resolve(target, dry_run=True)
    assert target.read_bytes() == before
    assert report["result"] == 6


def test_without_svn_the_remaining_step_is_spelled_out(tmp_path):
    _, target = _conflict(tmp_path)
    report = resolve(target)
    assert report["next_step"] == f"svn resolve --accept working {target}"


def test_people_files_are_found_and_local_is_not(tmp_path):
    cognition, journal = _conflict(tmp_path)
    _, profile = _conflict(tmp_path, name="a%40x.profile.jsonl", sub="people")
    local = cognition / "local"
    local.mkdir()
    (local / "x.jsonl").write_text("{}\n", encoding="utf-8")
    (local / "x.jsonl.mine").write_text("{}\n", encoding="utf-8")
    assert find_conflicts(cognition) == sorted([journal, profile])


def test_no_conflicts_is_a_clean_exit(tmp_path, capsys):
    (tmp_path / ".cognition").mkdir()
    assert main([str(tmp_path)]) == 0
    assert "No conflicted" in capsys.readouterr().out


def test_the_cli_reports_and_resolves(tmp_path, capsys):
    _, target = _conflict(tmp_path)
    assert main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "kept 6 lines" in out and "Reload the graph" in out
    assert len(target.read_text(encoding="utf-8").splitlines()) == 6


def test_the_command_names_this_interpreter_so_it_runs_outside_the_plugin_dir(tmp_path):
    import sys

    command = resolve_journal_command(tmp_path)
    assert sys.executable in command
    assert "-m vibe_cognition.cognition.resolve_journal" in command
