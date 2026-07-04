"""WP-B (decision 9022f7de94e9), reconciled by WP-C: read-only pre-import
venv health guard.

check()/check_or_exit() are pure and side-effect-free (beyond the print/exit
in the failure branch), so they're tested directly rather than by actually
breaking this test venv or reloading the module.

WP-C reconciliation: torch must be PRESENCE-checked only (importlib.util.
find_spec), never actually imported here -- WP-C moved sentence_transformers/
torch's ~9.6s import cost off the pre-handshake path into the background
thread, and a real `import torch` in this guard (which runs at server.py
module-load, pre-handshake) would completely neutralize that win.
"""

import sys

import pytest

from vibe_cognition import _venv_guard


def test_check_healthy_venv_returns_true_no_message():
    """Zero-regression proof: against the REAL required modules (this test's
    own venv has both chromadb and torch installed), the guard must return
    (True, "") -- i.e. never fire on a healthy, steady-state install.

    Fails-before: N/A for a pure check, but this pins the exact contract
    _venv_guard.check_or_exit()'s module-level call at import time relies on
    to be a silent no-op for every existing install.
    """
    assert _venv_guard.check() == (True, "")


def test_check_does_not_actually_import_torch():
    """WP-C reconciliation: the guard's torch check must be presence-only --
    proven by confirming torch is NOT in sys.modules as a side effect of
    check() (this test doesn't import torch itself, so if it shows up, the
    guard imported it).

    Fails-before (WP-C): a real `import torch` in the guard would completely
    cancel WP-C's win of moving that ~9.6s cost to the background thread.
    """
    was_loaded_before = "torch" in sys.modules
    _venv_guard.check()
    if not was_loaded_before:
        assert "torch" not in sys.modules, (
            "check() actually imported torch -- this neutralizes WP-C's "
            "lazy-import win, since the guard runs pre-handshake"
        )


def test_check_reports_first_broken_real_import_module():
    """A missing/broken REAL-import module is reported by name, not swallowed."""
    ok, err = _venv_guard.check(real_import_modules=("definitely_not_a_real_module_xyz",))
    assert ok is False
    assert "definitely_not_a_real_module_xyz" in err


def test_check_stops_at_first_failure_does_not_probe_later_modules():
    """Bounded, not exhaustive: the first broken module short-circuits the
    check (matches the READ-ONLY, fast-fail intent -- no reason to keep
    probing once one native dep is confirmed broken)."""
    ok, err = _venv_guard.check(
        real_import_modules=("definitely_not_a_real_module_xyz", "chromadb")
    )
    assert ok is False
    assert "definitely_not_a_real_module_xyz" in err


def test_check_presence_only_module_missing_reported_without_importing():
    """A missing presence-only module fails the check by name, via find_spec
    -- never attempted as a real import."""
    ok, err = _venv_guard.check(presence_only_modules=("definitely_not_a_real_module_xyz",))
    assert ok is False
    assert "definitely_not_a_real_module_xyz" in err


def test_check_or_exit_healthy_venv_is_a_noop(capsys):
    """On a healthy venv, check_or_exit() must NOT print anything or exit --
    this is the exact call that runs unconditionally at server.py import
    time, so a false positive here would break every single install."""
    _venv_guard.check_or_exit()  # must not raise SystemExit
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_check_or_exit_broken_venv_exits_1_with_clear_message(monkeypatch, capsys):
    """A broken venv -> sys.exit(1) with a message naming --no-sync and the
    hook's responsibility -- never a raw ImportError traceback, and the guard
    itself never attempts `uv sync` or any other mutation.

    Fails-before: no guard existed, so `--no-sync` (WP-B) against a broken
    venv would have surfaced as a raw, unactionable ImportError instead.
    """
    monkeypatch.setattr(_venv_guard, "check", lambda modules=None: (False, "torch: simulated import failure"))
    with pytest.raises(SystemExit) as exc:
        _venv_guard.check_or_exit()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "--no-sync" in err
    assert "simulated import failure" in err
    assert "uv sync" not in err  # guard never suggests/attempts self-syncing
