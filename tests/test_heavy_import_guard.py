"""WP-Sidecar (P0 endgame) §S-c: runtime sys.modules assertion unit tests."""

from __future__ import annotations

import sys

from vibe_cognition import _heavy_import_guard


def test_find_heavy_modules_returns_empty_when_none_present(monkeypatch):
    monkeypatch.setattr(sys, "modules", {"os": sys.modules["os"], "json": sys.modules["json"]})
    assert _heavy_import_guard.find_heavy_modules_in_sys_modules() == []


def test_find_heavy_modules_detects_a_real_match(monkeypatch):
    fake_modules = dict(sys.modules)
    fake_modules["torch"] = object()
    fake_modules["scipy.interpolate"] = object()
    monkeypatch.setattr(sys, "modules", fake_modules)

    found = _heavy_import_guard.find_heavy_modules_in_sys_modules()

    assert "torch" in found
    assert "scipy.interpolate" in found


def test_find_heavy_modules_does_not_false_positive_on_similar_names(monkeypatch):
    """A module whose name merely CONTAINS a heavy prefix as a substring (not
    a real package/submodule of it) must not trigger -- e.g. a hypothetical
    `torchvision_utils` package is not `torch`.

    Fails-before note: this must start from a CONTROLLED base set, not
    dict(sys.modules) -- scipy (a legitimate transitive dependency of
    something else already imported elsewhere in a shared pytest process)
    can genuinely be present by the time this test runs, which would make a
    dict(sys.modules)-based assertion of found == [] flaky-by-suite-order
    rather than a real false-positive check.
    """
    fake_modules = {
        "os": sys.modules["os"],
        "torchvision_utils": object(),
        "my_transformers_helper": object(),
    }
    monkeypatch.setattr(sys, "modules", fake_modules)

    found = _heavy_import_guard.find_heavy_modules_in_sys_modules()

    assert found == []


def test_check_and_log_writes_to_stderr_on_violation(monkeypatch, capsys):
    fake_modules = dict(sys.modules)
    fake_modules["sentence_transformers"] = object()
    monkeypatch.setattr(sys, "modules", fake_modules)

    _heavy_import_guard.check_and_log("test_moment")

    err = capsys.readouterr().err
    assert "INVARIANT VIOLATION" in err
    assert "test_moment" in err
    assert "sentence_transformers" in err


def test_check_and_log_silent_when_clean(monkeypatch, capsys):
    monkeypatch.setattr(sys, "modules", {"os": sys.modules["os"]})

    _heavy_import_guard.check_and_log("test_moment")

    assert capsys.readouterr().err == ""


def test_check_and_log_never_raises_even_on_violation(monkeypatch):
    fake_modules = dict(sys.modules)
    fake_modules["torch"] = object()
    monkeypatch.setattr(sys, "modules", fake_modules)

    _heavy_import_guard.check_and_log("test_moment")  # must not raise
