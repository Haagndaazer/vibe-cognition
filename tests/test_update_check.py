"""Tests for the session-start "new version available" nudge (WP-Nudge-1).

Fetch layer is ALWAYS mocked here (urlopen patched) -- these tests never
touch the network, matching the project's no-live-network test convention.
"""

import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from vibe_cognition import update_check


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b"", read_exc: Exception | None = None):
        self.status = status
        self._body = body
        self._read_exc = read_exc

    def read(self) -> bytes:
        if self._read_exc is not None:
            raise self._read_exc
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _ReadRaises:
    """Marker: urlopen() itself succeeds (status 200), but resp.read() raises
    -- distinct from an exception raised by urlopen() itself (a plain
    Exception instance in the responses dict, below)."""

    def __init__(self, exc: Exception):
        self.exc = exc


def _fake_urlopen(responses: dict):
    """Build a fake urlopen(req, timeout=...) that dispatches by URL substring.
    `responses` maps a URL substring -> (status, json-able body), an
    exception instance to raise from urlopen() itself, or a _ReadRaises
    wrapping an exception to raise from resp.read()."""

    def _urlopen(req, timeout=None):  # noqa: ARG001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for substring, outcome in responses.items():
            if substring in url:
                if isinstance(outcome, Exception):
                    raise outcome
                if isinstance(outcome, _ReadRaises):
                    return _FakeResponse(200, read_exc=outcome.exc)
                status, body = outcome
                return _FakeResponse(status, json.dumps(body).encode("utf-8"))
        raise AssertionError(f"unexpected URL: {url}")

    return _urlopen


MARKETPLACE_OK = {"plugins": [{"name": "vibe-cognition", "source": {"sha": "deadbeef"}}]}


def _write_installed_plugin_json(plugin_root: Path, version: str) -> None:
    d = plugin_root / ".claude-plugin"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.json").write_text(json.dumps({"version": version}), encoding="utf-8")


# ── version parsing / comparison ────────────────────────────────────────────


def test_parse_version_basic():
    assert update_check.parse_version("0.28.0") == (0, 28, 0)


@pytest.mark.parametrize(
    "junk",
    [
        "", None, "abc", "1.2.a", "1..2", "v1.2.3",
        "1.-1.0",  # negative part -- int()-parsable but not all-digits
        "1. 2.0",  # whitespace-padded part
        " 1.2.0",  # leading whitespace
        "1.2.0 ",  # trailing whitespace
    ],
)
def test_parse_version_unparsable_returns_none(junk):
    assert update_check.parse_version(junk) is None


def test_version_gt_simple_newer():
    assert update_check.version_gt("0.29.0", "0.28.0") is True


def test_version_gt_pin_rollback_no_nudge():
    """remote < installed (a pin rollback) must never nudge."""
    assert update_check.version_gt("0.27.0", "0.28.0") is False


def test_version_gt_equal_no_nudge():
    assert update_check.version_gt("0.28.0", "0.28.0") is False


def test_version_gt_unequal_length_padding_treats_as_equal():
    """"1.2" and "1.2.0" are the same version once zero-padded."""
    assert update_check.version_gt("1.2.0", "1.2") is False
    assert update_check.version_gt("1.2", "1.2.0") is False


def test_version_gt_unequal_length_still_compares_numerically():
    """1.10 > 1.9.5 numerically, not lexicographically."""
    assert update_check.version_gt("1.10.0", "1.9.5") is True


def test_version_gt_unparsable_either_side_treated_equal():
    assert update_check.version_gt("not-a-version", "0.28.0") is False
    assert update_check.version_gt("0.29.0", "not-a-version") is False
    assert update_check.version_gt(None, "0.28.0") is False


def test_version_gt_negative_part_treated_as_unparsable():
    """Reviewer-confirmed regression: int("-1") succeeds, so before the
    all-digits guard, version_gt("1.-1.0", "0.28.0") was spuriously True."""
    assert update_check.version_gt("1.-1.0", "0.28.0") is False


# ── marketplace-name derivation ─────────────────────────────────────────────


def test_derive_marketplace_name_from_cache_layout(tmp_path):
    plugin_root = tmp_path / "cache" / "coltondyck" / "vibe-cognition" / "0.28.0"
    plugin_root.mkdir(parents=True)
    assert update_check._derive_marketplace_name(str(plugin_root)) == "coltondyck"


def test_derive_marketplace_name_fallback_on_shallow_path():
    # A bare relative path has too few parents to resolve a grandparent
    # meaningfully -- Path('x').parent.parent == Path('.'), name == "".
    assert update_check._derive_marketplace_name("x") == ""


def test_derive_marketplace_name_dev_checkout_not_falsely_derived(tmp_path):
    """A dev checkout (CLAUDE_PLUGIN_ROOT pointed at a repo working copy, not
    the plugin cache) must not derive a wrong-but-plausible marketplace name
    from whatever its real parent happens to be named. E.g. a checkout at
    .../Documents/Projects/vibe-cognition has parent "Projects" (not
    "vibe-cognition") -- without the cache-layout guard (parent.name must
    literally equal "vibe-cognition"), this would have returned "Documents"
    and produced a bogus-but-plausible-looking CTA."""
    plugin_root = tmp_path / "Documents" / "Projects" / "vibe-cognition"
    plugin_root.mkdir(parents=True)

    assert update_check._derive_marketplace_name(str(plugin_root)) == ""
    assert update_check._format_cta("") == "/plugin update vibe-cognition"


def test_format_cta_with_marketplace():
    assert update_check._format_cta("coltondyck") == "/plugin update vibe-cognition@coltondyck"


def test_format_cta_fallback_without_marketplace():
    assert update_check._format_cta("") == "/plugin update vibe-cognition"


# ── end-to-end check() ──────────────────────────────────────────────────────


def test_check_nudges_on_newer_remote_exact_text(tmp_path, monkeypatch):
    plugin_root = tmp_path / "cache" / "coltondyck" / "vibe-cognition" / "0.28.0"
    _write_installed_plugin_json(plugin_root, "0.28.0")
    plugin_data = tmp_path / "plugin_data"

    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        _fake_urlopen({
            "marketplace.json": (200, MARKETPLACE_OK),
            "vibe-cognition/deadbeef": (200, {"version": "0.29.0"}),
        }),
    )

    note = update_check.check(str(plugin_root), str(plugin_data))

    assert note == (
        "vibe-cognition v0.29.0 is available (you have v0.28.0). To update: "
        "run /plugin update vibe-cognition@coltondyck, then restart Claude "
        "Code. Updating is always your call - this notice is informational "
        "only. Disable it with VIBE_UPDATE_NUDGE=off."
    )
    assert note.isascii(), "nudge text must be pure ASCII -- a Windows pipe mangles non-ASCII bytes"


def test_check_no_nudge_when_versions_equal(tmp_path, monkeypatch):
    plugin_root = tmp_path / "cache" / "coltondyck" / "vibe-cognition" / "0.28.0"
    _write_installed_plugin_json(plugin_root, "0.28.0")
    plugin_data = tmp_path / "plugin_data"

    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        _fake_urlopen({
            "marketplace.json": (200, MARKETPLACE_OK),
            "vibe-cognition/deadbeef": (200, {"version": "0.28.0"}),
        }),
    )

    assert update_check.check(str(plugin_root), str(plugin_data)) == ""


def test_check_no_nudge_on_pin_rollback(tmp_path, monkeypatch):
    plugin_root = tmp_path / "cache" / "coltondyck" / "vibe-cognition" / "0.28.0"
    _write_installed_plugin_json(plugin_root, "0.28.0")
    plugin_data = tmp_path / "plugin_data"

    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        _fake_urlopen({
            "marketplace.json": (200, MARKETPLACE_OK),
            "vibe-cognition/deadbeef": (200, {"version": "0.27.0"}),
        }),
    )

    assert update_check.check(str(plugin_root), str(plugin_data)) == ""


@pytest.mark.parametrize(
    "responses",
    [
        {"marketplace.json": TimeoutError()},
        {"marketplace.json": (404, {})},
        {"marketplace.json": (200, {"plugins": []})},  # entry missing
        {"marketplace.json": (200, {"plugins": [{"name": "vibe-cognition", "source": {}}]})},  # no sha
        {"marketplace.json": (200, MARKETPLACE_OK), "vibe-cognition/deadbeef": (200, {})},  # no version
        {"marketplace.json": (200, MARKETPLACE_OK), "vibe-cognition/deadbeef": (200, "not-a-dict")},
        # http.client.HTTPException raised from resp.read() itself -- NOT a
        # URLError/OSError/ValueError subclass; a truncating connection.
        {"marketplace.json": _ReadRaises(http.client.IncompleteRead(partial=b""))},
        {"marketplace.json": _ReadRaises(http.client.BadStatusLine("garbage"))},
    ],
)
def test_check_any_failure_mode_no_nudge_no_crash(tmp_path, monkeypatch, responses):
    plugin_root = tmp_path / "cache" / "coltondyck" / "vibe-cognition" / "0.28.0"
    _write_installed_plugin_json(plugin_root, "0.28.0")
    plugin_data = tmp_path / "plugin_data"

    monkeypatch.setattr(update_check.urllib.request, "urlopen", _fake_urlopen(responses))

    assert update_check.check(str(plugin_root), str(plugin_data)) == ""
    stamp = plugin_data / update_check.STAMP_FILENAME
    assert stamp.exists(), "stamp must be written even on this failure mode"


def test_check_wall_clock_ceiling_treats_hang_as_failure(tmp_path, monkeypatch):
    """A network phase that never returns is treated as failure once the
    wall-clock ceiling elapses -- not a hang forever. Uses a short ceiling
    (0.2s) so the test itself stays fast; the blocked worker thread is
    daemon and simply abandoned (dies with the process)."""
    plugin_root = tmp_path / "cache" / "coltondyck" / "vibe-cognition" / "0.28.0"
    _write_installed_plugin_json(plugin_root, "0.28.0")
    plugin_data = tmp_path / "plugin_data"

    never_set = threading.Event()

    def _hang(timeout):  # noqa: ARG001
        never_set.wait()  # never returns within the test's lifetime
        return None

    monkeypatch.setattr(update_check, "_fetch_marketplace_sha", _hang)

    start = time.monotonic()
    note = update_check.check(str(plugin_root), str(plugin_data), wall_clock_timeout=0.2)
    elapsed = time.monotonic() - start

    assert note == ""
    assert elapsed < 2.0, f"check() did not respect the wall-clock ceiling: {elapsed}s"
    stamp = plugin_data / update_check.STAMP_FILENAME
    assert stamp.exists(), "stamp must still be written when the network phase hangs"
    payload = json.loads(stamp.read_text(encoding="utf-8"))
    assert payload["remote_version"] == ""


def test_check_missing_installed_plugin_json_no_nudge_no_crash(tmp_path, monkeypatch):
    plugin_root = tmp_path / "cache" / "coltondyck" / "vibe-cognition" / "0.28.0"
    plugin_root.mkdir(parents=True)  # no .claude-plugin/plugin.json written
    plugin_data = tmp_path / "plugin_data"

    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        _fake_urlopen({
            "marketplace.json": (200, MARKETPLACE_OK),
            "vibe-cognition/deadbeef": (200, {"version": "0.29.0"}),
        }),
    )

    assert update_check.check(str(plugin_root), str(plugin_data)) == ""


# ── stamp write behavior ─────────────────────────────────────────────────────


def test_check_always_writes_stamp_even_on_failure(tmp_path, monkeypatch):
    plugin_root = tmp_path / "cache" / "coltondyck" / "vibe-cognition" / "0.28.0"
    _write_installed_plugin_json(plugin_root, "0.28.0")
    plugin_data = tmp_path / "plugin_data"

    monkeypatch.setattr(
        update_check.urllib.request, "urlopen", _fake_urlopen({"marketplace.json": TimeoutError()})
    )

    update_check.check(str(plugin_root), str(plugin_data))

    stamp = plugin_data / update_check.STAMP_FILENAME
    assert stamp.exists()
    payload = json.loads(stamp.read_text(encoding="utf-8"))
    assert "checked_at" in payload
    assert payload["remote_version"] == ""


def test_check_stamp_records_remote_version_on_success(tmp_path, monkeypatch):
    plugin_root = tmp_path / "cache" / "coltondyck" / "vibe-cognition" / "0.28.0"
    _write_installed_plugin_json(plugin_root, "0.28.0")
    plugin_data = tmp_path / "plugin_data"

    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        _fake_urlopen({
            "marketplace.json": (200, MARKETPLACE_OK),
            "vibe-cognition/deadbeef": (200, {"version": "0.29.0"}),
        }),
    )

    update_check.check(str(plugin_root), str(plugin_data))

    stamp = plugin_data / update_check.STAMP_FILENAME
    payload = json.loads(stamp.read_text(encoding="utf-8"))
    assert payload["remote_version"] == "0.29.0"


def test_write_stamp_atomic_no_leftover_tmp_file(tmp_path):
    plugin_data = tmp_path / "plugin_data"
    update_check._write_stamp(str(plugin_data), "0.29.0")

    assert (plugin_data / update_check.STAMP_FILENAME).exists()
    assert not (plugin_data / f"{update_check.STAMP_FILENAME}.tmp").exists()


def test_write_stamp_twice_does_not_crash_concurrent_style(tmp_path):
    """Simulates two racing session-starts both writing the stamp -- the
    second write must cleanly replace the first, no torn file."""
    plugin_data = tmp_path / "plugin_data"
    update_check._write_stamp(str(plugin_data), "0.28.0")
    update_check._write_stamp(str(plugin_data), "0.29.0")

    payload = json.loads((plugin_data / update_check.STAMP_FILENAME).read_text(encoding="utf-8"))
    assert payload["remote_version"] == "0.29.0"


# ── main() / kill switch ────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "FALSE", "no", "NO"])
def test_main_respects_kill_switch_never_calls_network(tmp_path, monkeypatch, value):
    plugin_root = tmp_path / "cache" / "coltondyck" / "vibe-cognition" / "0.28.0"
    _write_installed_plugin_json(plugin_root, "0.28.0")
    plugin_data = tmp_path / "plugin_data"

    def _boom(*a, **k):
        raise AssertionError("network must not be reached when the kill switch is on")

    monkeypatch.setattr(update_check.urllib.request, "urlopen", _boom)
    monkeypatch.setenv("VIBE_UPDATE_NUDGE", value)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

    assert update_check.main(argv=[]) == 0
    assert not (plugin_data / update_check.STAMP_FILENAME).exists()


def test_main_missing_env_vars_returns_zero_no_crash(monkeypatch):
    monkeypatch.delenv("VIBE_UPDATE_NUDGE", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

    assert update_check.main(argv=[]) == 0


def test_main_prints_nudge_to_stdout(tmp_path, monkeypatch, capsys):
    plugin_root = tmp_path / "cache" / "coltondyck" / "vibe-cognition" / "0.28.0"
    _write_installed_plugin_json(plugin_root, "0.28.0")
    plugin_data = tmp_path / "plugin_data"

    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        _fake_urlopen({
            "marketplace.json": (200, MARKETPLACE_OK),
            "vibe-cognition/deadbeef": (200, {"version": "0.29.0"}),
        }),
    )
    monkeypatch.delenv("VIBE_UPDATE_NUDGE", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

    assert update_check.main(argv=[]) == 0
    out = capsys.readouterr().out
    assert "vibe-cognition v0.29.0 is available" in out


def test_main_never_crashes_when_check_raises(tmp_path, monkeypatch):
    """main()'s own try/except boundary mechanically enforces the "always
    exits 0" contract: any exception from check() that this module's own
    code failed to anticipate must still degrade to exit 0, never crash the
    SessionStart hook."""
    plugin_root = tmp_path / "cache" / "coltondyck" / "vibe-cognition" / "0.28.0"
    _write_installed_plugin_json(plugin_root, "0.28.0")
    plugin_data = tmp_path / "plugin_data"

    def _boom(*a, **k):
        raise RuntimeError("unanticipated failure")

    monkeypatch.setattr(update_check, "check", _boom)
    monkeypatch.delenv("VIBE_UPDATE_NUDGE", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

    assert update_check.main(argv=[]) == 0


def test_main_stdout_reconfigure_failure_does_not_crash(monkeypatch):
    """If sys.stdout has no reconfigure() (unusual, but the reconfigure call
    is defensive), main() must not crash -- it's wrapped in suppress(Exception)."""

    class _NoReconfigureStdout:
        def write(self, *a, **k):
            pass

        def flush(self):
            pass

    monkeypatch.setattr(update_check.sys, "stdout", _NoReconfigureStdout())
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

    assert update_check.main(argv=[]) == 0
