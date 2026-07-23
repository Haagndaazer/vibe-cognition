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
        "(Shown once per update. Full details: the plugin's CHANGELOG.md. "
        "Disable these notices with VIBE_WHATS_NEW=off.)"
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
        whats_new._SEEN_FLOOR_DISPLAY,
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


def test_check_upgrade_path_exact_block_and_marker_advances(tmp_path):
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
        "(Shown once per update. Full details: the plugin's CHANGELOG.md. "
        "Disable these notices with VIBE_WHATS_NEW=off.)"
    )
    assert _read_marker(plugin_data) == "0.30.0"


def test_check_steady_state_after_marker_advanced_shows_nothing(tmp_path):
    """Second call after the marker caught up to installed: no spawn's worth
    of content -- the module itself is also silent (defense in depth on top
    of the bash fast path)."""
    plugin_root = tmp_path / "root"
    plugin_data = tmp_path / "data"
    _write_plugin_json(plugin_root, "0.30.0")
    _write_whats_new_json(plugin_root, {"0.30.0": ["New thing happened."]})
    _seed_marker(plugin_data, "0.29.0")

    whats_new.check(str(plugin_root), str(plugin_data))  # first call advances the marker
    note = whats_new.check(str(plugin_root), str(plugin_data))  # second call: steady state

    assert note == ""
    assert _read_marker(plugin_data) == "0.30.0"


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
    assert _read_marker(plugin_data) == "0.30.0"


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

    assert "old" not in note  # 0.29.0 == seen, excluded by the (seen, installed] range
    assert "new" in note


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
    assert not (plugin_data / f"{whats_new.SEEN_MARKER_FILENAME}.tmp").exists()


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

    monkeypatch.setattr(whats_new, "check", _boom)
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
