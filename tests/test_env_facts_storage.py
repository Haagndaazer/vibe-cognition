"""WP-EnvFacts-A Stage 1: per-person fact files — storage core.

Covers the brief's (doc:35d9206676e0) storage-layer acceptance:
- slug: casefold-before-encode, +tag distinctness, pinned lowercase hex,
  bounded deterministic long-email form, round-trip stability
- delta-only writes: one line per change, no-op suppression journals NOTHING,
  anti-O(n²) linear file size (fails-before: a whole-map-per-write
  implementation blows the linear bound)
- fold semantics: set/overwrite/delete/per-machine isolation/clear
- machine cap: 11th machine rejected (retryable ValueError naming the remedy),
  existing machines keep writing at cap (fails-before: a never-firing cap
  check fails the rejection assertion)
- fresh project: first write creates .cognition/people/ (append_journal_line
  does NOT create parents)
- catch-up: cross-instance convergence, dir-mtime-gated listdir + stat-gated
  reads (zero reads on a no-change pass, benchmarked at N=50), torn-tail
  parking, single-file truncation re-fold, union-merge both-sides-append
- concurrency: two threads appending different person files interleave safely
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from vibe_cognition.cognition.people_facts import (
    PeopleFactsRegistry,
    email_slug,
)
from vibe_cognition.cognition.storage import CognitionStorage

BY = {"name": "Colton Dyck", "email": "colton@example.com"}


def _store(tmp_path: Path) -> CognitionStorage:
    return CognitionStorage(tmp_path / ".cognition")


def _person_file(tmp_path: Path, email: str) -> Path:
    return tmp_path / ".cognition" / "people" / f"{email_slug(email)}.jsonl"


# ── email_slug ──────────────────────────────────────────────────────────────


def test_slug_casefolds_before_encoding():
    assert email_slug("Alice@X.com") == email_slug("alice@x.com")


def test_slug_plus_tags_stay_distinct():
    assert email_slug("alice+a@x.com") != email_slug("alice+b@x.com")


def test_slug_hex_is_lowercase_and_deterministic():
    slug = email_slug("alice+work@x.com")
    assert slug == email_slug("alice+work@x.com")  # byte-identical across calls
    assert "%2b" in slug and "%40" in slug  # pinned LOWERCASE hex
    assert "%2B" not in slug  # uppercase hex must never appear


def test_slug_long_email_bounded_and_deterministic():
    long_email = ("very." * 20) + "long.address@example-domain.com"
    s1, s2 = email_slug(long_email), email_slug(long_email)
    assert s1 == s2
    assert len(s1) <= 24 + 1 + 12
    # A different long email must not collide.
    assert s1 != email_slug("x" + long_email)


def test_slug_unicode_email_is_encoded_not_stripped():
    slug = email_slug("ülrich@x.com")
    assert "%" in slug
    assert slug == email_slug("ÜLRICH@x.com")  # casefold first


# ── delta-only writes ───────────────────────────────────────────────────────


def test_set_fact_journals_exactly_one_line(tmp_path):
    store = _store(tmp_path)
    r = store.set_env_fact("colton@example.com", "desk", "os", "Windows 11", BY)
    assert r["written"] is True and r["noop"] is False
    lines = _person_file(tmp_path, "colton@example.com").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["action"] == "fact_set"
    assert entry["key"] == "os" and entry["value"] == "Windows 11"
    assert entry["old"] is None and entry["by"] == BY


def test_noop_suppression_journals_nothing(tmp_path):
    store = _store(tmp_path)
    store.set_env_fact("colton@example.com", "desk", "os", "Windows 11", BY)
    path = _person_file(tmp_path, "colton@example.com")
    size_before = path.stat().st_size
    r = store.set_env_fact("colton@example.com", "desk", "os", "Windows 11", BY)
    assert r["written"] is False and r["noop"] is True
    assert path.stat().st_size == size_before  # NOTHING appended


def test_anti_quadratic_file_growth(tmp_path):
    """100 updates to one fact -> 100 delta lines, file size LINEAR.

    Fails-before: an update_node-shaped implementation that journals the whole
    accumulated map each write grows quadratically and blows the per-line bound.
    """
    store = _store(tmp_path)
    for i in range(100):
        store.set_env_fact("colton@example.com", "desk", "counter", i, BY)
    path = _person_file(tmp_path, "colton@example.com")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    # Every line carries exactly one change: bounded size, no accumulation.
    max_line = max(len(ln) for ln in lines)
    min_line = min(len(ln) for ln in lines)
    assert max_line - min_line < 40  # value digits/old field only — no growth
    assert path.stat().st_size < 100 * (max_line + 2) + 1


def test_delete_and_clear_write_delta_lines(tmp_path):
    store = _store(tmp_path)
    store.set_env_fact("colton@example.com", "desk", "os", "Windows 11", BY)
    store.set_env_fact("colton@example.com", "desk", "shell", "pwsh", BY)
    store.set_env_fact("colton@example.com", "laptop", "os", "macOS", BY)

    r = store.delete_env_fact("colton@example.com", "desk", "os", BY)
    assert r["written"] is True and r["old"] == "Windows 11"
    assert store.get_env_facts("colton@example.com")["desk"] == {"shell": "pwsh"}

    r = store.clear_env_facts("colton@example.com", "laptop", BY)
    assert r["cleared_keys"] == ["laptop/os"]  # uniform machine/key shape
    facts = store.get_env_facts("colton@example.com")
    assert "laptop" not in facts and facts["desk"] == {"shell": "pwsh"}

    r = store.clear_env_facts("colton@example.com", None, BY)
    assert r["cleared_keys"] == ["desk/shell"]
    assert store.get_env_facts("colton@example.com") == {}

    # Clearing an already-empty state journals nothing.
    path = _person_file(tmp_path, "colton@example.com")
    size = path.stat().st_size
    r = store.clear_env_facts("colton@example.com", None, BY)
    assert r["noop"] is True and path.stat().st_size == size


def test_none_value_is_a_real_fact_not_a_noop(tmp_path):
    """Regression (Stage 1 verification gate, HIGH): storing a literal None
    for a NEW key on a machine that already has other facts must WRITE, not
    vanish as a mislabeled noop (None-as-missing-sentinel confusion)."""
    store = _store(tmp_path)
    store.set_env_fact("a@x.com", "desk", "os", "linux", BY)
    r = store.set_env_fact("a@x.com", "desk", "new_key", None, BY)
    assert r["written"] is True and r["noop"] is False
    assert "new_key" in store.get_env_facts("a@x.com")["desk"]
    assert store.get_env_facts("a@x.com")["desk"]["new_key"] is None
    # Re-setting the SAME None value IS a noop (real equality, not sentinel).
    r = store.set_env_fact("a@x.com", "desk", "new_key", None, BY)
    assert r["noop"] is True


def test_delete_absent_key_is_noop(tmp_path):
    store = _store(tmp_path)
    r = store.delete_env_fact("colton@example.com", "desk", "nope", BY)
    assert r["noop"] is True
    assert not _person_file(tmp_path, "colton@example.com").exists()


# ── fold semantics ──────────────────────────────────────────────────────────


def test_per_machine_isolation_and_overwrite(tmp_path):
    store = _store(tmp_path)
    store.set_env_fact("colton@example.com", "Desk", "root", r"C:\proj", BY)
    store.set_env_fact("colton@example.com", "laptop", "root", "/home/c/proj", BY)
    store.set_env_fact("colton@example.com", "desk", "root", r"D:\proj", BY)  # overwrite; machine casefolds
    facts = store.get_env_facts("colton@example.com")
    assert facts == {"desk": {"root": r"D:\proj"}, "laptop": {"root": "/home/c/proj"}}


def test_email_casefolded_on_write_and_read(tmp_path):
    store = _store(tmp_path)
    store.set_env_fact("Colton@Example.com", "desk", "os", "w11", BY)
    assert store.get_env_facts("colton@example.com")["desk"]["os"] == "w11"
    assert store.env_fact_emails() == ["colton@example.com"]


def test_blank_inputs_rejected(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.set_env_fact("", "desk", "os", "w11", BY)
    with pytest.raises(ValueError):
        store.set_env_fact("a@b.c", "", "os", "w11", BY)
    with pytest.raises(ValueError):
        store.set_env_fact("a@b.c", "desk", "", "w11", BY)


# ── machine cap ─────────────────────────────────────────────────────────────


def test_machine_cap_rejects_new_machine_and_allows_existing(tmp_path):
    """Fails-before: an off-by-one or never-firing cap check fails the
    pytest.raises assertion; a cap that also blocks EXISTING machines fails
    the follow-up write."""
    store = _store(tmp_path)
    for i in range(10):
        store.set_env_fact("colton@example.com", f"m{i}", "os", "linux", BY)
    with pytest.raises(ValueError, match="machine cap"):
        store.set_env_fact("colton@example.com", "m10", "os", "linux", BY)
    # Existing machines keep writing at cap.
    r = store.set_env_fact("colton@example.com", "m0", "os", "windows", BY)
    assert r["written"] is True
    # Pruning one machine frees a slot.
    store.clear_env_facts("colton@example.com", "m1", BY)
    r = store.set_env_fact("colton@example.com", "m10", "os", "linux", BY)
    assert r["written"] is True


# ── fresh project / directory lifecycle ─────────────────────────────────────


def test_fresh_project_first_write_creates_people_dir(tmp_path):
    store = _store(tmp_path)
    people = tmp_path / ".cognition" / "people"
    assert not people.exists()  # init does NOT create it
    store.set_env_fact("colton@example.com", "desk", "os", "w11", BY)
    assert people.is_dir()
    assert _person_file(tmp_path, "colton@example.com").exists()


# ── catch-up ────────────────────────────────────────────────────────────────


def test_cross_instance_convergence(tmp_path):
    a = _store(tmp_path)
    b = _store(tmp_path)
    a.set_env_fact("colton@example.com", "desk", "os", "w11", BY)
    # b sees a's write on its next synced read (dir mtime changed -> listdir
    # discovers the new file; stat-gate reads it).
    assert b.get_env_facts("colton@example.com") == {"desk": {"os": "w11"}}
    # And appends to the EXISTING file are seen via the per-file stat gate.
    a.set_env_fact("colton@example.com", "desk", "shell", "pwsh", BY)
    assert b.get_env_facts("colton@example.com")["desk"]["shell"] == "pwsh"


@pytest.mark.parametrize("n_people", [50, 100])
def test_no_change_pass_does_zero_reads_and_zero_listdirs(tmp_path, monkeypatch, n_people):
    """The peer-review HIGH, as a test, at both brief-mandated team sizes:
    a no-change catch-up performs NO file reads, NO directory listing, and
    exactly 1 dir stat + 1 stat per known file (stats scale with file count;
    reads never do — the honest cost contract)."""
    cog = tmp_path / ".cognition"
    reg = PeopleFactsRegistry(cog)
    for i in range(n_people):
        reg.set_fact(f"user{i}@example.com", "desk", "os", "linux", BY, True)
    reg.catch_up()  # fold everything; steady state
    reg.catch_up()  # settle mtime bookkeeping so the next pass is truly no-change

    reads: list[str] = []
    listdirs: list[str] = []
    stats: list[str] = []
    real_read_bytes = Path.read_bytes
    real_iterdir = Path.iterdir
    real_stat = Path.stat
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: (reads.append(str(self)), real_read_bytes(self))[1]
    )
    monkeypatch.setattr(
        Path, "iterdir", lambda self: (listdirs.append(str(self)), real_iterdir(self))[1]
    )
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self, **kw: (stats.append(str(self)), real_stat(self, **kw))[1],
    )
    reg.catch_up()  # nothing changed since the fold
    assert reads == []
    assert listdirs == []
    assert len(stats) == 1 + n_people  # dir gate + per-file stat gates, nothing more


def test_torn_tail_parks_then_recovers(tmp_path):
    store = _store(tmp_path)
    store.set_env_fact("colton@example.com", "desk", "os", "w11", BY)
    path = _person_file(tmp_path, "colton@example.com")
    # Simulate a torn append: half a line, no terminator.
    with path.open("ab") as f:
        f.write(b'{"action": "fact_set", "email": "colton@ex')
    assert store.get_env_facts("colton@example.com") == {"desk": {"os": "w11"}}
    # Complete the line; it folds on the next pass.
    rest = json.dumps(
        {"action": "fact_set", "email": "colton@example.com", "machine": "desk",
         "key": "late", "value": 1, "old": None, "at": "", "by": BY, "from_agent": True}
    ).encode("utf-8")
    with path.open("ab") as f:
        f.write(b'ample.com", "machine": "desk", "key": "torn", "value": 0, "old": null}\n')
        f.write(rest + b"\n")
    facts = store.get_env_facts("colton@example.com")["desk"]
    assert facts["torn"] == 0 and facts["late"] == 1


def test_single_file_truncation_refolds_that_file_only(tmp_path):
    store = _store(tmp_path)
    store.set_env_fact("a@x.com", "desk", "os", "w11", BY)
    store.set_env_fact("a@x.com", "desk", "shell", "pwsh", BY)
    store.set_env_fact("b@x.com", "desk", "os", "macos", BY)
    graph_nodes_before = store.graph.number_of_nodes()

    # Truncate a's file down to its first line (rewrite/rotation).
    path = _person_file(tmp_path, "a@x.com")
    first_line = path.read_bytes().split(b"\n")[0] + b"\n"
    path.write_bytes(first_line)

    assert store.get_env_facts("a@x.com") == {"desk": {"os": "w11"}}
    assert store.get_env_facts("b@x.com") == {"desk": {"os": "macos"}}  # untouched
    assert store.graph.number_of_nodes() == graph_nodes_before  # never a graph rehydrate


def test_union_merge_both_sides_survive(tmp_path):
    """merge=union interleave fixture: two divergent single-writer tails merged
    into one file — the prefix-hash detects the rewrite and the re-fold keeps
    BOTH sides' lines."""
    store = _store(tmp_path)
    store.set_env_fact("a@x.com", "desk", "base", 1, BY)
    path = _person_file(tmp_path, "a@x.com")
    base = path.read_bytes()

    def line(key: str, value):
        return json.dumps(
            {"action": "fact_set", "email": "a@x.com", "machine": "desk",
             "key": key, "value": value, "old": None, "at": "", "by": BY,
             "from_agent": True}
        ).encode("utf-8") + b"\n"

    # Simulate the union merge of two clones' divergent appends.
    path.write_bytes(base + line("from_clone_1", 1) + line("from_clone_2", 2))
    facts = store.get_env_facts("a@x.com")["desk"]
    assert facts == {"base": 1, "from_clone_1": 1, "from_clone_2": 2}


def test_malformed_line_skipped_rest_folds(tmp_path):
    store = _store(tmp_path)
    store.set_env_fact("a@x.com", "desk", "ok", 1, BY)
    path = _person_file(tmp_path, "a@x.com")
    with path.open("ab") as f:
        f.write(b"NOT JSON AT ALL\n")
    store.set_env_fact("a@x.com", "desk", "after", 2, BY)
    assert store.get_env_facts("a@x.com")["desk"] == {"ok": 1, "after": 2}


def test_deleted_people_dir_clears_state(tmp_path):
    store = _store(tmp_path)
    store.set_env_fact("a@x.com", "desk", "os", "w11", BY)
    assert store.get_env_facts("a@x.com")
    people = tmp_path / ".cognition" / "people"
    for f in people.iterdir():  # containment gate: no rmtree, even in tests
        f.unlink()
    people.rmdir()
    assert store.get_env_facts("a@x.com") == {}


# ── concurrency ─────────────────────────────────────────────────────────────


def test_concurrent_appends_to_different_person_files(tmp_path):
    store = _store(tmp_path)
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def writer(email: str):
        try:
            barrier.wait(timeout=10)
            for i in range(25):
                store.set_env_fact(email, "desk", f"k{i}", i, BY)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=writer, args=("a@x.com",))
    t2 = threading.Thread(target=writer, args=("b@x.com",))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert not errors
    assert len(store.get_env_facts("a@x.com")["desk"]) == 25
    assert len(store.get_env_facts("b@x.com")["desk"]) == 25
    # Files stayed separate and each holds exactly its own 25 delta lines.
    assert len(_person_file(tmp_path, "a@x.com").read_text(encoding="utf-8").splitlines()) == 25
    assert len(_person_file(tmp_path, "b@x.com").read_text(encoding="utf-8").splitlines()) == 25
