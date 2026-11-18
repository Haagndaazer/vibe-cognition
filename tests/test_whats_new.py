"""Tests for the post-update "what's new" notice (WP-WhatsNew-1).

No network involved at all (unlike update_check.py) -- everything here is
local file reads/writes, so no mocking layer is needed for that; tests build
real plugin_root/plugin_data directory trees under tmp_path.
"""

import json
from pathlib import Path

import pytest

from vibe_cognition import whats_new


def _write_plugin_json(plugin_root: Path, version: str) -> None:
    d = plugin_root / ".claude-plugin"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps({"version": version}), encoding="utf-8")


def _write_whats_new_json(plugin_root: Path, content) -> None:
    d = plugin_root / ".claude-plugin"
    d.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        (d / "whats-new.json").write_text(content, encoding="utf-8")
    else:
        (d / "whats-new.json").write_text(json.dumps(content), encoding="utf-8")


def _seed_prior_use(plugin_data: Path) -> None:
    """Simulate update_check.py having already run at least once (the
    rollout-day signal that a missing whats-new marker means "existing
    user", not "fresh install")."""
    plugin_data.mkdir(parents=True, exist_ok=True)
    (plugin_data / whats_new.UPDATE_CHECK_STAMP_FILENAME).write_text("{}", encoding="utf-8")


def _seed_marker(plugin_data: Path, version: str) -> None:
    plugin_data.mkdir(parents=True, exist_ok=True)
    (plugin_data / whats_new.SEEN_MARKER_FILENAME).write_text(version, encoding="utf-8", newline="")


def _read_marker(plugin_data: Path) -> str:
    return (plugin_data / whats_new.SEEN_MARKER_FILENAME).read_text(encoding="utf-8")


def _read_pending(plugin_data: Path) -> dict:
    return json.loads((plugin_data / whats_new.PENDING_FILENAME).read_text(encoding="utf-8"))


def _upgrade(tmp_path: Path, seen: str = "0.29.0", installed: str = "0.30.0") -> tuple[str, str]:
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, installed)
    _write_whats_new_json(plugin_root, {installed: ["New thing happened."]})
    _seed_marker(plugin_data, seen)
    return str(plugin_root), str(plugin_data)


# ── version parsing / comparison ────────────────────────────────────────────


def test_parse_version_basic():
    assert whats_new._parse_version("0.29.0") == (0, 29, 0)


@pytest.mark.parametrize("junk", ["", None, "abc", "1.-1.0", "1. 2.0", " 1.2.0", "1.2.0 ", "1..2"])
def test_parse_version_unparsable_returns_none(junk):
    assert whats_new._parse_version(junk) is None


def test_version_key_padding_equal():
    assert whats_new._version_key("1.2") == whats_new._version_key("1.2.0")


def test_version_key_numeric_not_lexicographic():
    newer = whats_new._version_key("1.10.0")
    older = whats_new._version_key("1.9.5")
    assert newer is not None and older is not None
    assert newer > older


def test_version_key_unparsable_is_none():
    assert whats_new._version_key("not-a-version") is None


# ── _format_seen_display / _format_block ────────────────────────────────────


def test_format_seen_display_floor_case_is_not_literal_zero_version():
    assert whats_new._format_seen_display("0.0.0") == whats_new._SEEN_FLOOR_DISPLAY
    assert "0.0.0" not in whats_new._format_seen_display("0.0.0")


def test_format_seen_display_normal_case():
    assert whats_new._format_seen_display("0.29.0") == "v0.29.0"


def test_format_block_exact_text_no_overflow():
    block = whats_new._format_block("0.29.0", "0.30.0", ["Bullet one.", "Bullet two."], False)
    assert block == (
        "INSTRUCTION: Tell the user what's new in this plugin update before continuing.\n"
        "vibe-cognition updated (v0.29.0 -> v0.30.0) - new since your last session:\n"
        "- Bullet one.\n"
        "- Bullet two.\n"
        "(Shown at session start until confirmed, at most 5 times. "
        "Full details: the plugin's CHANGELOG.md. "
        "Disable these notices with VIBE_WHATS_NEW=off.)\n"
        "Once the user has been told, call cognition_readme(changes_seen=true) so this "
        "note stops appearing."
    )
    assert block.isascii()


def test_format_block_version_overflow_line_present():
    block = whats_new._format_block("0.27.0", "0.30.0", ["Bullet."], True)
    assert whats_new._VERSION_OVERFLOW_LINE in block
    lines = block.splitlines()
    footer_idx = lines.index(whats_new._FOOTER_LINE)
    overflow_idx = lines.index(whats_new._VERSION_OVERFLOW_LINE)
    assert overflow_idx == footer_idx - 1, "overflow line must sit right before the footer"


def test_format_block_no_overflow_line_when_false():
    block = whats_new._format_block("0.29.0", "0.30.0", ["Bullet."], False)
    assert whats_new._VERSION_OVERFLOW_LINE not in block


def test_format_block_floor_seen_renders_first_install_style():
    block = whats_new._format_block("0.0.0", "0.30.0", ["Bullet."], False)
    assert "v0.0.0" not in block
    assert whats_new._SEEN_FLOOR_DISPLAY in block


# ── module's own static template strings: pure ASCII (the cp1252 lesson) ───


@pytest.mark.parametrize(
    "text",
    [
        whats_new._INSTRUCTION_LINE,
        whats_new._HEADER_TEMPLATE,
        whats_new._VERSION_OVERFLOW_LINE,
        whats_new._FOOTER_LINE,
        whats_new._CONFIRM_LINE,
        whats_new._SEEN_FLOOR_DISPLAY,
        whats_new.CONFIRMED,
        whats_new.NOTHING_TO_CONFIRM,
        whats_new.STALE_SERVER,
        whats_new.NEWER_CONFIRMED,
        whats_new.NOT_RECORDED,
    ],
)
def test_module_template_strings_are_ascii(text):
    assert text.isascii()


# ── shipped whats-new.json: pure ASCII, bullets <=200 chars ────────────────


def test_shipped_whats_new_json_bullets_are_ascii_and_bounded():
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / ".claude-plugin" / "whats-new.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data, "whats-new.json must be a non-empty dict"
    for version, bullets in data.items():
        assert whats_new._parse_version(version) is not None, f"bad version key: {version!r}"
        assert isinstance(bullets, list) and 1 <= len(bullets) <= 3, (
            f"{version}: expected 1-3 bullets, got {bullets!r}"
        )
        for bullet in bullets:
            assert isinstance(bullet, str) and bullet.isascii(), f"non-ASCII bullet: {bullet!r}"
            assert len(bullet) <= 200, f"bullet too long ({len(bullet)} chars): {bullet!r}"


def test_shipped_whats_new_json_covers_0290_and_0300():
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / ".claude-plugin" / "whats-new.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "0.29.0" in data
    assert "0.30.0" in data


# ── check(): core lifecycle ─────────────────────────────────────────────────


def test_check_upgrade_path_exact_block_and_marker_stays_until_confirmed(tmp_path):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.30.0")
    _write_whats_new_json(plugin_root, {"0.30.0": ["New thing happened."]})
    _seed_marker(plugin_data, "0.29.0")

    note = whats_new.check(str(plugin_root), str(plugin_data))

    assert note == (
        "INSTRUCTION: Tell the user what's new in this plugin update before continuing.\n"
        "vibe-cognition updated (v0.29.0 -> v0.30.0) - new since your last session:\n"
        "- New thing happened.\n"
        "(Shown at session start until confirmed, at most 5 times. "
        "Full details: the plugin's CHANGELOG.md. "
        "Disable these notices with VIBE_WHATS_NEW=off.)\n"
        "Once the user has been told, call cognition_readme(changes_seen=true) so this "
        "note stops appearing."
    )
    assert _read_marker(plugin_data) == "0.29.0", "printing must not mark the note seen"
    assert _read_pending(plugin_data) == {"version": "0.30.0", "shown": 1}


def test_check_silent_once_confirmed(tmp_path):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.30.0")
    _write_whats_new_json(plugin_root, {"0.30.0": ["New thing happened."]})
    _seed_marker(plugin_data, "0.29.0")

    assert whats_new.check(str(plugin_root), str(plugin_data)) != ""
    assert whats_new.confirm(str(plugin_root), str(plugin_data), "0.30.0") == (
        True, "Recorded: the what's-new note for v0.30.0 will not be shown again."
    )

    assert _read_marker(plugin_data) == "0.30.0"
    assert not (plugin_data / whats_new.PENDING_FILENAME).exists()
    assert whats_new.check(str(plugin_root), str(plugin_data)) == ""


def test_check_rollout_day_no_marker_but_prior_use_shows_capped_backlog(tmp_path):
    """AC2, the blocker fix: no marker at all, but update-check.json exists
    (an existing user) -> capped backlog shown WITHOUT a pre-seeded marker."""
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.30.0")
    _write_whats_new_json(plugin_root, {"0.29.0": ["Nudge feature."], "0.30.0": ["Whats-new feature."]})
    _seed_prior_use(plugin_data)
    assert not (plugin_data / whats_new.SEEN_MARKER_FILENAME).exists(), "marker must NOT be pre-seeded"

    note = whats_new.check(str(plugin_root), str(plugin_data))

    assert "Nudge feature." in note
    assert "Whats-new feature." in note
    assert whats_new._SEEN_FLOOR_DISPLAY in note
    assert not (plugin_data / whats_new.SEEN_MARKER_FILENAME).exists()
    assert whats_new.check(str(plugin_root), str(plugin_data)) == note, (
        "an unconfirmed rollout-day backlog must be shown again"
    )


def test_check_truly_fresh_install_silent_and_marker_written(tmp_path):
    """Neither file present (truly fresh install) -> silence, marker written
    to the installed version (not the floor) -- onboarding owns new users."""
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.30.0")
    _write_whats_new_json(plugin_root, {"0.30.0": ["Whats-new feature."]})

    note = whats_new.check(str(plugin_root), str(plugin_data))

    assert note == ""
    assert _read_marker(plugin_data) == "0.30.0"


def test_check_marker_equals_installed_no_note(tmp_path):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.30.0")
    _write_whats_new_json(plugin_root, {"0.30.0": ["Whats-new feature."]})
    _seed_marker(plugin_data, "0.30.0")

    assert whats_new.check(str(plugin_root), str(plugin_data)) == ""


def test_check_downgrade_silent_and_marker_rewritten(tmp_path):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.29.0")  # installed is OLDER than marker
    _write_whats_new_json(plugin_root, {"0.29.0": ["x"], "0.30.0": ["y"]})
    _seed_marker(plugin_data, "0.30.0")

    note = whats_new.check(str(plugin_root), str(plugin_data))

    assert note == ""
    assert _read_marker(plugin_data) == "0.29.0"


def test_check_entry_less_versions_skipped(tmp_path):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.31.0")
    # 0.30.0 has no entry at all in the map -- must not blow up or show blank.
    _write_whats_new_json(plugin_root, {"0.29.0": ["old"], "0.31.0": ["new"]})
    _seed_marker(plugin_data, "0.29.0")

    note = whats_new.check(str(plugin_root), str(plugin_data))

    assert "- old" not in note  # 0.29.0 == seen, excluded by the (seen, installed] range
    assert "- new" in note


def test_check_all_entry_less_span_no_block_marker_advances(tmp_path):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.31.0")
    _write_whats_new_json(plugin_root, {})  # nothing in range at all
    _seed_marker(plugin_data, "0.29.0")

    note = whats_new.check(str(plugin_root), str(plugin_data))

    assert note == ""
    assert _read_marker(plugin_data) == "0.31.0"


# ── caps ─────────────────────────────────────────────────────────────────────


def test_check_version_overflow_shows_newest_three_plus_overflow_line(tmp_path):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.33.0")
    _write_whats_new_json(plugin_root, {
        "0.29.0": ["v29"],
        "0.30.0": ["v30"],
        "0.31.0": ["v31"],
        "0.32.0": ["v32"],
        "0.33.0": ["v33"],
    })
    _seed_marker(plugin_data, "0.0.0")  # everything is in range: 5 versions

    note = whats_new.check(str(plugin_root), str(plugin_data))

    assert "v33" in note and "v32" in note and "v31" in note
    assert "v30" not in note and "v29" not in note
    assert whats_new._VERSION_OVERFLOW_LINE in note


def test_check_bullet_only_overflow_drops_oldest_silently_no_line(tmp_path):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.31.0")
    # 3 versions (within MAX_VERSIONS), but 4 bullets each = 12 > MAX_BULLETS (9).
    _write_whats_new_json(plugin_root, {
        "0.29.0": ["a1", "a2", "a3", "a4"],
        "0.30.0": ["b1", "b2", "b3", "b4"],
        "0.31.0": ["c1", "c2", "c3", "c4"],
    })
    _seed_marker(plugin_data, "0.0.0")

    note = whats_new.check(str(plugin_root), str(plugin_data))

    assert whats_new._VERSION_OVERFLOW_LINE not in note, "bullet-only overflow must NOT print the version-overflow line"
    bullet_lines = [line for line in note.splitlines() if line.startswith("- ")]
    assert len(bullet_lines) == whats_new.MAX_BULLETS
    # Newest-first, flattened, truncated at 9: c1-c4, b1-b4, a1 -- a2/a3/a4 (the
    # oldest version's tail) are the ones dropped.
    assert bullet_lines == [f"- {b}" for b in ["c1", "c2", "c3", "c4", "b1", "b2", "b3", "b4", "a1"]]
    assert "a2" not in note and "a3" not in note and "a4" not in note


def test_check_at_most_nine_bullets_always(tmp_path):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.29.0")
    _write_whats_new_json(plugin_root, {"0.29.0": [f"b{i}" for i in range(20)]})
    _seed_marker(plugin_data, "0.0.0")

    note = whats_new.check(str(plugin_root), str(plugin_data))

    bullet_lines = [line for line in note.splitlines() if line.startswith("- ")]
    assert len(bullet_lines) <= whats_new.MAX_BULLETS


# ── malformed whats-new.json: silence, exit 0 (via check()), marker advances ─


@pytest.mark.parametrize(
    "raw_content",
    [
        "not json at all {{{",
        "[]",  # valid JSON, but not a dict
        "null",
        '{"0.30.0": "not-a-list"}',
        '{"0.30.0": [123, null, "ok bullet"]}',  # mixed-type list, non-strings filtered
        '{"not-a-version": ["x"]}',
    ],
)
def test_check_malformed_whats_new_json_silent_no_crash_marker_advances(tmp_path, raw_content):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.30.0")
    _write_whats_new_json(plugin_root, raw_content)
    _seed_marker(plugin_data, "0.29.0")

    note = whats_new.check(str(plugin_root), str(plugin_data))

    # The one exception with real content: mixed-type list keeps its one
    # valid string bullet -- everything else here is expected empty.
    if raw_content == '{"0.30.0": [123, null, "ok bullet"]}':
        assert "ok bullet" in note
        assert _read_marker(plugin_data) == "0.29.0"
    else:
        assert note == ""
        assert _read_marker(plugin_data) == "0.30.0"


def test_check_missing_whats_new_json_silent_marker_advances(tmp_path):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.30.0")  # no whats-new.json written at all
    _seed_marker(plugin_data, "0.29.0")

    note = whats_new.check(str(plugin_root), str(plugin_data))

    assert note == ""
    assert _read_marker(plugin_data) == "0.30.0"


def test_check_missing_installed_plugin_json_silent_no_crash(tmp_path):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    plugin_root.mkdir(parents=True)  # no .claude-plugin/plugin.json at all
    _seed_marker(plugin_data, "0.29.0")

    assert whats_new.check(str(plugin_root), str(plugin_data)) == ""


def test_check_unparsable_installed_version_no_write_perpetual(tmp_path):
    """Unparsable INSTALLED (a maintainer-broken plugin.json version field):
    ruled acceptable to leave the marker untouched and stay silent -- see
    the module's `check()` docstring/comment. This is the one branch that
    is allowed to perpetual-spawn; it's covered here so the ruling itself
    stays pinned, not just asserted in a code comment."""
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "not-a-version")
    _seed_marker(plugin_data, "0.29.0")

    assert whats_new.check(str(plugin_root), str(plugin_data)) == ""
    assert _read_marker(plugin_data) == "0.29.0", "marker must be left untouched when INSTALLED is unparsable"


def test_check_unparsable_seen_marker_self_heals(tmp_path):
    """Vince's fix-required finding: a corrupted marker (SEEN unparsable,
    INSTALLED fine) must self-heal -- write marker = installed and stay
    silent once -- not return "" forever with the garbage marker left in
    place (which would silently drop every future announcement, since the
    bash-side fast path never matches a marker that never advances)."""
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.30.0")
    _write_whats_new_json(plugin_root, {"0.30.0": ["Whats-new feature."]})
    _seed_marker(plugin_data, "garbage")

    note = whats_new.check(str(plugin_root), str(plugin_data))

    assert note == ""
    assert _read_marker(plugin_data) == "0.30.0"

    # The self-heal must actually stick: the NEXT check() call (mirroring the
    # bash fast-path re-derivation) sees marker == installed and is silent.
    note_again = whats_new.check(str(plugin_root), str(plugin_data))
    assert note_again == ""
    assert _read_marker(plugin_data) == "0.30.0"


# ── marker write: exact bytes, atomicity ────────────────────────────────────


def test_write_seen_marker_raw_bytes_exactly_equal_version_no_newline(tmp_path):
    plugin_data = tmp_path / "data"
    whats_new._write_seen_marker(str(plugin_data), "0.30.0")

    raw = (plugin_data / whats_new.SEEN_MARKER_FILENAME).read_bytes()
    assert raw == b"0.30.0", f"marker bytes must be the bare version, no trailing newline: {raw!r}"


def test_write_seen_marker_atomic_no_leftover_tmp_file(tmp_path):
    plugin_data = tmp_path / "data"
    whats_new._write_seen_marker(str(plugin_data), "0.30.0")

    assert (plugin_data / whats_new.SEEN_MARKER_FILENAME).exists()
    assert [p.name for p in plugin_data.iterdir()] == [whats_new.SEEN_MARKER_FILENAME]


def test_write_atomic_failure_leaves_no_temp_file(tmp_path, monkeypatch):
    plugin_data = tmp_path / "data"

    def _boom(*a, **k):
        raise OSError("locked")

    monkeypatch.setattr(whats_new.os, "replace", _boom)

    assert whats_new._write_seen_marker(str(plugin_data), "0.30.0") is False
    assert list(plugin_data.iterdir()) == []


def test_write_seen_marker_twice_last_writer_wins_no_torn_file(tmp_path):
    plugin_data = tmp_path / "data"
    whats_new._write_seen_marker(str(plugin_data), "0.29.0")
    whats_new._write_seen_marker(str(plugin_data), "0.30.0")

    assert (plugin_data / whats_new.SEEN_MARKER_FILENAME).read_text(encoding="utf-8") == "0.30.0"


# ── main() / kill switch ─────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "FALSE", "no", "NO"])
def test_main_respects_kill_switch_no_marker_write(tmp_path, monkeypatch, value):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.30.0")
    _write_whats_new_json(plugin_root, {"0.30.0": ["x"]})

    monkeypatch.setenv("VIBE_WHATS_NEW", value)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

    assert whats_new.main(argv=[]) == 0
    assert not (plugin_data / whats_new.SEEN_MARKER_FILENAME).exists()


def test_main_missing_env_vars_returns_zero_no_crash(monkeypatch):
    monkeypatch.delenv("VIBE_WHATS_NEW", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

    assert whats_new.main(argv=[]) == 0


def test_main_prints_block_to_stdout(tmp_path, monkeypatch, capsys):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.30.0")
    _write_whats_new_json(plugin_root, {"0.30.0": ["New thing happened."]})
    _seed_marker(plugin_data, "0.29.0")

    monkeypatch.delenv("VIBE_WHATS_NEW", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

    assert whats_new.main(argv=[]) == 0
    out = capsys.readouterr().out
    assert "New thing happened." in out


def test_main_never_crashes_when_check_raises(tmp_path, monkeypatch):
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.30.0")

    def _boom(*a, **k):
        raise RuntimeError("unanticipated failure")

    monkeypatch.setattr(whats_new, "plan", _boom)
    monkeypatch.delenv("VIBE_WHATS_NEW", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

    assert whats_new.main(argv=[]) == 0


def test_main_stdout_reconfigure_failure_does_not_crash(monkeypatch):
    class _NoReconfigureStdout:
        def write(self, *a, **k):
            pass

        def flush(self):
            pass

    monkeypatch.setattr(whats_new.sys, "stdout", _NoReconfigureStdout())
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

    assert whats_new.main(argv=[]) == 0


# ── shown until confirmed, capped ───────────────────────────────────────────


def test_max_showings_is_five_and_footer_states_it():
    assert whats_new.MAX_SHOWINGS == 5
    assert f"at most {whats_new.MAX_SHOWINGS} times" in whats_new._FOOTER_LINE


def test_confirm_line_is_the_only_line_naming_a_tool(tmp_path):
    root, data = _upgrade(tmp_path)
    lines = whats_new.check(root, data).splitlines()

    assert lines[-1] == whats_new._CONFIRM_LINE
    assert "cognition_readme(changes_seen=true)" in lines[-1]
    assert not [line for line in lines[:-1] if "cognition_" in line or "get_status" in line]


def test_note_repeats_until_cap_then_marker_advances(tmp_path):
    root, data = _upgrade(tmp_path)
    data_dir = Path(data)

    for showing in range(1, whats_new.MAX_SHOWINGS):
        assert whats_new.check(root, data) != "", f"showing {showing} must print the note"
        assert _read_marker(data_dir) == "0.29.0"
        assert _read_pending(data_dir) == {"version": "0.30.0", "shown": showing}

    assert whats_new.check(root, data) != "", "the capped showing still prints the note"
    assert _read_marker(data_dir) == "0.30.0"
    assert not (data_dir / whats_new.PENDING_FILENAME).exists()
    assert whats_new.check(root, data) == ""


def test_plan_writes_nothing_until_commit(tmp_path):
    root, data = _upgrade(tmp_path)
    data_dir = Path(data)

    note, commit = whats_new.plan(root, data)

    assert note != ""
    assert not (data_dir / whats_new.PENDING_FILENAME).exists()
    assert _read_marker(data_dir) == "0.29.0"
    commit()
    assert _read_pending(data_dir)["shown"] == 1


def test_main_counts_nothing_when_printing_fails(tmp_path, monkeypatch):
    root, data = _upgrade(tmp_path)

    class _BrokenStdout:
        def write(self, *a, **k):
            raise OSError("closed pipe")

        def flush(self):
            pass

    monkeypatch.setattr(whats_new.sys, "stdout", _BrokenStdout())
    monkeypatch.delenv("VIBE_WHATS_NEW", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", root)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", data)

    assert whats_new.main(argv=[]) == 0
    assert not (Path(data) / whats_new.PENDING_FILENAME).exists()
    assert _read_marker(Path(data)) == "0.29.0"


def test_main_records_one_showing_after_printing(tmp_path, monkeypatch, capsys):
    root, data = _upgrade(tmp_path)
    monkeypatch.delenv("VIBE_WHATS_NEW", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", root)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", data)

    assert whats_new.main(argv=[]) == 0

    assert "New thing happened." in capsys.readouterr().out
    assert _read_pending(Path(data)) == {"version": "0.30.0", "shown": 1}


@pytest.mark.parametrize(
    "pending",
    [
        "not json",
        "[]",
        '{"version": "0.29.5", "shown": 4}',
        '{"version": "0.30.0", "shown": "4"}',
        '{"version": "0.30.0", "shown": true}',
        '{"version": "0.30.0", "shown": -3}',
    ],
)
def test_unusable_pending_file_counts_as_zero(tmp_path, pending):
    root, data = _upgrade(tmp_path)
    (Path(data) / whats_new.PENDING_FILENAME).write_text(pending, encoding="utf-8")

    assert whats_new.check(root, data) != ""
    assert _read_pending(Path(data)) == {"version": "0.30.0", "shown": 1}
    assert _read_marker(Path(data)) == "0.29.0"


# ── confirm() ───────────────────────────────────────────────────────────────


def test_confirm_refuses_a_server_running_another_version(tmp_path):
    root, data = _upgrade(tmp_path)
    whats_new.check(root, data)

    recorded, message = whats_new.confirm(root, data, "0.29.0")

    assert recorded is False
    assert message == whats_new.STALE_SERVER.format(running="0.29.0", installed="0.30.0")
    assert _read_marker(Path(data)) == "0.29.0"
    assert _read_pending(Path(data))["shown"] == 1


def test_confirm_refuses_an_unknown_running_version(tmp_path):
    root, data = _upgrade(tmp_path)

    recorded, message = whats_new.confirm(root, data, None)

    assert recorded is False
    assert "running vunknown" in message
    assert _read_marker(Path(data)) == "0.29.0"


def test_confirm_never_moves_the_marker_backwards(tmp_path):
    root, data = _upgrade(tmp_path, seen="0.31.0", installed="0.30.0")

    recorded, message = whats_new.confirm(root, data, "0.30.0")

    assert recorded is False
    assert message == whats_new.NEWER_CONFIRMED.format(seen="0.31.0", installed="0.30.0")
    assert _read_marker(Path(data)) == "0.31.0"


def test_confirm_is_idempotent(tmp_path):
    root, data = _upgrade(tmp_path)

    first = whats_new.confirm(root, data, "0.30.0")
    second = whats_new.confirm(root, data, "0.30.0")

    assert first == second == (True, whats_new.CONFIRMED.format(version="0.30.0"))
    assert _read_marker(Path(data)) == "0.30.0"


@pytest.mark.parametrize("root_set, data_set", [(False, True), (True, False), (False, False)])
def test_confirm_without_plugin_paths_writes_nothing(tmp_path, root_set, data_set):
    root, data = _upgrade(tmp_path)

    result = whats_new.confirm(root if root_set else "", data if data_set else "", "0.30.0")

    assert result == (False, whats_new.NOTHING_TO_CONFIRM)
    assert _read_marker(Path(data)) == "0.29.0"


def test_confirm_with_unreadable_manifest_writes_nothing(tmp_path):
    root, data = _upgrade(tmp_path)
    _write_plugin_json(Path(root), "not-a-version")

    assert whats_new.confirm(root, data, "not-a-version") == (False, whats_new.NOTHING_TO_CONFIRM)
    assert _read_marker(Path(data)) == "0.29.0"


def test_confirm_reports_a_failed_write(tmp_path, monkeypatch):
    root, data = _upgrade(tmp_path)
    monkeypatch.setattr(whats_new, "_write_seen_marker", lambda *a, **k: False)

    assert whats_new.confirm(root, data, "0.30.0") == (False, whats_new.NOT_RECORDED)
    assert _read_marker(Path(data)) == "0.29.0"


def test_confirm_from_an_older_install_keeps_a_newer_versions_count(tmp_path):
    """A server left over from before an update confirms its own (older)
    version: the newer version's showings count must survive."""
    root, data = _upgrade(tmp_path, seen="0.29.0", installed="0.30.0")
    (Path(data) / whats_new.PENDING_FILENAME).write_text(
        json.dumps({"version": "0.31.0", "shown": 3}), encoding="utf-8"
    )

    assert whats_new.confirm(root, data, "0.30.0")[0] is True

    assert _read_marker(Path(data)) == "0.30.0"
    assert _read_pending(Path(data)) == {"version": "0.31.0", "shown": 3}
