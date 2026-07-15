"""WP-TC14: "Since You Were Gone" digest (per-email last-seen marker + prime
section). Each test names the specific failure mode it guards and, where the
brief flags it fails-before, is written to fail before its fix exists.

Covers: marker read tolerance (_last_seen_for), marker write (_stamp_last_seen
-- lifecycle, generate_prime purity, concurrency), the '## Since You Were
Gone' section (cutoff strictness, own-exclusion, unstamped inclusion, HEAD
filter on constraints only, naive-timestamp tolerance, cap+overflow,
newest-first interleave), placement, and compat (personalize off / single-user
auto byte-identical).
"""

import json
import threading
from datetime import UTC, datetime, timedelta

from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.models import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
)
from vibe_cognition.cognition.prime import (
    LAST_SEEN_FILENAME,
    PrimeConfig,
    _last_seen_for,
    _stamp_last_seen,
    generate_prime,
    main,
)

# ── helpers ───────────────────────────────────────────────────────────────────

ME = {"name": "Alice", "email": "alice@x.com"}
TEAMMATE = {"name": "Bob", "email": "bob@x.com"}


def _add(
    storage: CognitionStorage, node_id: str, ntype: CognitionNodeType, summary: str, *,
    severity: str | None = None, timestamp: str | None = None, metadata: dict | None = None,
) -> None:
    ts = timestamp or datetime.now(UTC).isoformat()
    storage.add_node(CognitionNode(
        id=node_id, type=ntype, summary=summary, detail="d",
        context=[], references=[], severity=severity, timestamp=ts, author="t",
        metadata=metadata or {},
    ))


def _marker_path(cognition_dir):
    return cognition_dir / LAST_SEEN_FILENAME


def _section(result: str, header: str) -> str:
    """Isolate one '## Header\\n...' section's body from the full prime
    output, up to the next '## ' header or end of string. Needed because
    UNSCOPED global sections (Recent Decisions, Recent Incidents, Active
    Constraints, Your Recent Activity) render the SAME underlying nodes with
    no cutoff/exclusion filter of their own -- a whole-`result` substring
    check would false-positive on content correctly excluded from THIS
    section but legitimately present in another (same lesson as WP-TC16's
    test suite)."""
    if header not in result:
        return ""
    start = result.index(header)
    rest = result[start + len(header):]
    next_header = rest.find("\n## ")
    body = rest if next_header == -1 else rest[:next_header]
    return header + body


def _write_marker(cognition_dir, data: dict) -> None:
    cognition_dir.mkdir(parents=True, exist_ok=True)
    _marker_path(cognition_dir).write_text(json.dumps(data), encoding="utf-8")


# ── _last_seen_for: marker read tolerance ───────────────────────────────────


def test_last_seen_for_missing_file_returns_none(tmp_path):
    assert _last_seen_for(tmp_path / ".cognition", ME["email"]) is None


def test_last_seen_for_malformed_json_returns_none(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    _marker_path(cognition_dir).write_text("{not valid json", encoding="utf-8")
    assert _last_seen_for(cognition_dir, ME["email"]) is None


def test_last_seen_for_non_dict_payload_returns_none(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    _marker_path(cognition_dir).write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert _last_seen_for(cognition_dir, ME["email"]) is None


def test_last_seen_for_null_value_returns_none(tmp_path):
    """A null-valued entry ({"email": null}) is treated as missing -> fallback,
    never a crash on the isinstance(value, str) check."""
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    _write_marker(cognition_dir, {ME["email"]: None})
    assert _last_seen_for(cognition_dir, ME["email"]) is None


def test_last_seen_for_present_value_returned(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    _write_marker(cognition_dir, {ME["email"]: "2026-01-01T00:00:00+00:00"})
    assert _last_seen_for(cognition_dir, ME["email"]) == "2026-01-01T00:00:00+00:00"


def test_last_seen_for_other_email_absent_returns_none(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    _write_marker(cognition_dir, {TEAMMATE["email"]: "2026-01-01T00:00:00+00:00"})
    assert _last_seen_for(cognition_dir, ME["email"]) is None


# ── _stamp_last_seen: marker write lifecycle ────────────────────────────────


def test_stamp_last_seen_creates_marker_with_now(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    before = datetime.now(UTC)
    _stamp_last_seen(cognition_dir, ME["email"])
    after = datetime.now(UTC)

    data = json.loads(_marker_path(cognition_dir).read_text(encoding="utf-8"))
    stamped = datetime.fromisoformat(data[ME["email"]])
    assert before <= stamped <= after


def test_stamp_last_seen_casefolds_email(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    _stamp_last_seen(cognition_dir, "Alice@X.COM")
    data = json.loads(_marker_path(cognition_dir).read_text(encoding="utf-8"))
    assert "alice@x.com" in data
    assert "Alice@X.COM" not in data


def test_stamp_last_seen_preserves_other_emails_entries(tmp_path):
    """The per-email ruling: stamping MY entry must not stomp a teammate's
    existing entry on a shared machine."""
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    _write_marker(cognition_dir, {TEAMMATE["email"]: "2026-01-01T00:00:00+00:00"})

    _stamp_last_seen(cognition_dir, ME["email"])

    data = json.loads(_marker_path(cognition_dir).read_text(encoding="utf-8"))
    assert data[TEAMMATE["email"]] == "2026-01-01T00:00:00+00:00"
    assert ME["email"] in data


def test_stamp_last_seen_empty_email_is_noop(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    _stamp_last_seen(cognition_dir, "")
    assert not _marker_path(cognition_dir).exists()


def test_stamp_last_seen_unconditional_overwrite_self_heals_future_marker(tmp_path):
    """A future-dated marker (clock skew / suspended VM) is unconditionally
    overwritten with real now on the next stamp -- self-heal, no crash."""
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    future = (datetime.now(UTC) + timedelta(days=365)).isoformat()
    _write_marker(cognition_dir, {ME["email"]: future})

    _stamp_last_seen(cognition_dir, ME["email"])

    data = json.loads(_marker_path(cognition_dir).read_text(encoding="utf-8"))
    restamped = datetime.fromisoformat(data[ME["email"]])
    assert restamped < datetime.now(UTC) + timedelta(minutes=1)


def test_stamp_last_seen_cleans_up_stray_tmp_from_prior_crash(tmp_path):
    """Gate F1: a process killed between write_text and os.replace on a prior
    stamp leaves last-seen.json.tmp behind. The NEXT successful stamp cleans
    it up as a side effect of its own write_text+os.replace (which truncate
    and then rename over it) -- this test pins that end-to-end outcome; the
    NEXT test below is the real fails-before proof for the entry-unlink
    itself, since a successful write makes the explicit unlink redundant."""
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    stray = cognition_dir / f"{LAST_SEEN_FILENAME}.tmp"
    stray.write_text("torn-from-a-crash", encoding="utf-8")

    _stamp_last_seen(cognition_dir, ME["email"])

    assert not stray.exists()
    data = json.loads(_marker_path(cognition_dir).read_text(encoding="utf-8"))
    assert ME["email"] in data


def test_stamp_last_seen_cleans_up_stray_tmp_even_if_this_stamp_also_fails(tmp_path, monkeypatch):
    """Gate F1: the stray-tmp cleanup must happen on ENTRY (before the write
    attempt), so even if THIS stamp call ALSO fails to write (e.g. a second
    disk hiccup), the earlier crash's stray content does not survive --
    unlike the previous test, a failed write_text here can't overwrite the
    stray as an incidental side effect, so this genuinely isolates the
    entry-unlink. Fails-before: without it, the stray's "torn-from-a-crash"
    bytes are left untouched on disk."""
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    stray = cognition_dir / f"{LAST_SEEN_FILENAME}.tmp"
    stray.write_text("torn-from-a-crash", encoding="utf-8")

    import vibe_cognition.cognition.prime as prime_module

    def _boom(self, *a, **kw):
        raise OSError("simulated disk hiccup")

    monkeypatch.setattr(prime_module.Path, "write_text", _boom)
    _stamp_last_seen(cognition_dir, ME["email"])  # must not raise

    assert not stray.exists()


def test_stamp_last_seen_skips_when_lock_held(tmp_path):
    """Fails-before proof that the lock is actually checked (not merely
    present but ignored): a fresh (non-stale) lock file held by "someone
    else" must cause the stamp to be skipped entirely, not written anyway."""
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    (cognition_dir / f"{LAST_SEEN_FILENAME}.lock").touch()

    _stamp_last_seen(cognition_dir, ME["email"])

    assert not _marker_path(cognition_dir).exists()


def test_stamp_last_seen_write_failure_never_raises(tmp_path, monkeypatch):
    """A read-only-filesystem-style write failure must degrade silently
    (suppress(OSError)) -- the SessionStart hook must never crash on this."""
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()

    import vibe_cognition.cognition.prime as prime_module

    def _boom(self, *a, **kw):
        raise OSError("simulated read-only filesystem")

    monkeypatch.setattr(prime_module.Path, "write_text", _boom)
    _stamp_last_seen(cognition_dir, ME["email"])  # must not raise
    assert not _marker_path(cognition_dir).exists()


def test_stamp_last_seen_concurrent_writers_both_survive_and_preserve_existing(tmp_path):
    """HIGH peer-review: two teammates starting sessions on a shared machine
    concurrently is exactly the per-email ruling's race. Real threads +
    threading.Barrier force both writers' _stamp_last_seen calls to start at
    the SAME wall-clock instant -- NOT a sequential call-then-call check,
    which would pass trivially even with no lock at all. A pre-existing
    THIRD email's entry (seeded before the race) must also survive, proving
    the RMW doesn't silently clobber it via a stale-snapshot write."""
    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    _write_marker(cognition_dir, {"carol@x.com": "2026-01-01T00:00:00+00:00"})

    barrier = threading.Barrier(2)

    def _writer(email):
        barrier.wait(timeout=5)
        _stamp_last_seen(cognition_dir, email)

    t1 = threading.Thread(target=_writer, args=("alice@x.com",))
    t2 = threading.Thread(target=_writer, args=("bob@x.com",))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()

    data = json.loads(_marker_path(cognition_dir).read_text(encoding="utf-8"))
    assert data.get("carol@x.com") == "2026-01-01T00:00:00+00:00", "pre-existing entry must survive a concurrent RMW"
    assert "alice@x.com" in data
    assert "bob@x.com" in data


def test_stamp_last_seen_lock_genuinely_blocks_concurrent_acquire(tmp_path):
    """Deterministic (Event-based, no timing luck) proof that the lock
    primitive _stamp_last_seen relies on is a real mutex: while a holder
    thread provably still holds the lock (confirmed via threading.Event, not
    a sleep guess), a concurrent contender's acquire attempt must fail."""
    from vibe_cognition.cognition.git_hygiene import _acquire_lock, _release_lock

    cognition_dir = tmp_path / ".cognition"
    cognition_dir.mkdir()
    lock_path = cognition_dir / f"{LAST_SEEN_FILENAME}.lock"

    holder_has_lock = threading.Event()
    release_holder = threading.Event()

    def _holder():
        assert _acquire_lock(lock_path)
        holder_has_lock.set()
        release_holder.wait(timeout=5)
        _release_lock(lock_path)

    t = threading.Thread(target=_holder)
    t.start()
    assert holder_has_lock.wait(timeout=5), "holder never acquired the lock"

    contender_acquired = _acquire_lock(lock_path)
    assert contender_acquired is False, "a held lock must block a concurrent acquire attempt"

    release_holder.set()
    t.join(timeout=5)


# ── generate_prime purity ────────────────────────────────────────────────────


def test_generate_prime_never_writes_the_marker(tmp_path):
    """THE invariant: generate_prime stays pure read-only. Bare calls (as the
    dashboard, instructions.py's compact-reinject, and tests all make) must
    never create/update last-seen.json -- only prime.py's own main() does."""
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    _add(storage, "d1", CognitionNodeType.DECISION, "a decision", metadata={"recorded_by": TEAMMATE})

    for _ in range(3):
        generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])

    assert not _marker_path(cognition_dir).exists()


# ── '## Since You Were Gone' section ────────────────────────────────────────


def test_digest_node_newer_than_marker_shows(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    _write_marker(cognition_dir, {ME["email"]: "2026-01-01T00:00:00+00:00"})
    _add(storage, "d1", CognitionNodeType.DECISION, "newer decision",
         timestamp="2026-01-02T00:00:00+00:00", metadata={"recorded_by": TEAMMATE})

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "## Since You Were Gone" in result
    assert "newer decision" in result


def test_digest_node_older_than_marker_absent(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    _write_marker(cognition_dir, {ME["email"]: "2026-01-02T00:00:00+00:00"})
    _add(storage, "d1", CognitionNodeType.DECISION, "older decision",
         timestamp="2026-01-01T00:00:00+00:00", metadata={"recorded_by": TEAMMATE})

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "## Since You Were Gone" not in result


def test_digest_node_exactly_equal_to_marker_absent_fails_before(tmp_path):
    """Fails-before: the cutoff must be STRICTLY greater-than (deliberate
    divergence from _format_incidents' >=) -- a node timestamped exactly at
    the marker is a node the marker's OWN session already saw, not news."""
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    marker_ts = "2026-01-02T00:00:00+00:00"
    _write_marker(cognition_dir, {ME["email"]: marker_ts})
    _add(storage, "d1", CognitionNodeType.DECISION, "exactly-at-marker decision",
         timestamp=marker_ts, metadata={"recorded_by": TEAMMATE})

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "## Since You Were Gone" not in result


def test_digest_excludes_own_stamped_nodes_fails_before(tmp_path):
    """Fails-before: "your own writes are not news to you" -- a node
    _node_email-matching my own casefolded email must never appear, even
    though it is newer than the marker."""
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    _write_marker(cognition_dir, {ME["email"]: "2026-01-01T00:00:00+00:00"})
    _add(storage, "d-mine", CognitionNodeType.DECISION, "my own new decision",
         timestamp="2026-01-02T00:00:00+00:00", metadata={"recorded_by": ME})
    _add(storage, "d-theirs", CognitionNodeType.DECISION, "teammate's new decision",
         timestamp="2026-01-02T00:00:00+00:00", metadata={"recorded_by": TEAMMATE})

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    section = _section(result, "## Since You Were Gone")
    assert "my own new decision" not in section
    assert "teammate's new decision" in section


def test_digest_includes_unstamped_nodes(tmp_path):
    """Deliberate divergence from the rollup's attribution doctrine: an
    unstamped node (_node_email == "") is INCLUDED, not excluded -- an
    awareness view reports content, not people."""
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    _write_marker(cognition_dir, {ME["email"]: "2026-01-01T00:00:00+00:00"})
    _add(storage, "d1", CognitionNodeType.DECISION, "unstamped new decision",
         timestamp="2026-01-02T00:00:00+00:00")

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "unstamped new decision" in result
    assert "(unattributed, 2026-01-02)" in result


def test_digest_constraint_head_filter_superseded_absent_fails_before(tmp_path):
    """Fails-before: a superseded constraint version must not resurface as
    "news" (the exact inflation bug discovery dfd9f59827b1 records)."""
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    _write_marker(cognition_dir, {ME["email"]: "2026-01-01T00:00:00+00:00"})
    _add(storage, "c-old", CognitionNodeType.CONSTRAINT, "old constraint version",
         timestamp="2026-01-02T00:00:00+00:00", metadata={"recorded_by": TEAMMATE})
    _add(storage, "c-new", CognitionNodeType.CONSTRAINT, "new constraint version",
         timestamp="2026-01-03T00:00:00+00:00", metadata={"recorded_by": TEAMMATE})
    storage.add_edge(CognitionEdge(
        from_id="c-new", to_id="c-old", edge_type=CognitionEdgeType.SUPERSEDES,
        timestamp="2026-01-03T00:00:00+00:00",
    ))

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "new constraint version" in result
    assert "old constraint version" not in result


def test_digest_superseded_decision_shows_mirrors_global_section(tmp_path):
    """Decisions are NOT HEAD-filtered here -- mirrors the global Recent
    Decisions section exactly (same TC16 "mirror each type's existing
    semantics" precedent). A superseded decision legitimately still shows."""
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    _write_marker(cognition_dir, {ME["email"]: "2026-01-01T00:00:00+00:00"})
    _add(storage, "d-old", CognitionNodeType.DECISION, "old superseded decision",
         timestamp="2026-01-02T00:00:00+00:00", metadata={"recorded_by": TEAMMATE})
    _add(storage, "d-new", CognitionNodeType.DECISION, "new superseding decision",
         timestamp="2026-01-03T00:00:00+00:00", metadata={"recorded_by": TEAMMATE})
    storage.add_edge(CognitionEdge(
        from_id="d-new", to_id="d-old", edge_type=CognitionEdgeType.SUPERSEDES,
        timestamp="2026-01-03T00:00:00+00:00",
    ))

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "old superseded decision" in result
    assert "new superseding decision" in result


def test_digest_naive_timestamp_node_no_crash_fails_before(tmp_path):
    """Fails-before: the cutoff comparison is pure LEXICOGRAPHIC string
    compare -- no datetime parsing anywhere in this section -- so a naive/
    malformed node timestamp string simply compares (never crashes), the
    TC16-F2 crash class structurally absent here."""
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    _write_marker(cognition_dir, {ME["email"]: "2026-01-01T00:00:00+00:00"})
    _add(storage, "d1", CognitionNodeType.DECISION, "naive timestamp decision",
         timestamp="2026-01-02T00:00:00", metadata={"recorded_by": TEAMMATE})  # no tzinfo

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert isinstance(result, str)  # must not raise


def test_digest_missing_timestamp_key_treated_as_empty_string_no_crash(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    _write_marker(cognition_dir, {ME["email"]: "2026-01-01T00:00:00+00:00"})
    storage.add_node(CognitionNode(
        id="d1", type=CognitionNodeType.DECISION, summary="no timestamp decision",
        detail="d", context=[], references=[], severity=None, timestamp="", author="t",
        metadata={"recorded_by": TEAMMATE},
    ))

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "## Since You Were Gone" not in result  # "" is never > any real cutoff


def test_digest_cap_and_overflow(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    _write_marker(cognition_dir, {ME["email"]: "2026-01-01T00:00:00+00:00"})
    for i in range(4):
        _add(storage, f"d{i}", CognitionNodeType.DECISION, f"news item {i}",
             timestamp=f"2026-01-0{i + 2}T00:00:00+00:00", metadata={"recorded_by": TEAMMATE})

    result = generate_prime(
        storage, PrimeConfig(prime_personalize="on", prime_digest_cap=2), current_email=ME["email"]
    )
    section = _section(result, "## Since You Were Gone")
    shown = sum(section.count(f"news item {i}") for i in range(4))
    assert shown == 2
    assert "+2 more since you were gone" in section


def test_digest_newest_first_interleave_across_types(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    _write_marker(cognition_dir, {ME["email"]: "2026-01-01T00:00:00+00:00"})
    _add(storage, "c1", CognitionNodeType.CONSTRAINT, "middle constraint",
         timestamp="2026-01-02T00:00:00+00:00", metadata={"recorded_by": TEAMMATE})
    _add(storage, "i1", CognitionNodeType.INCIDENT, "newest incident",
         timestamp="2026-01-03T00:00:00+00:00", metadata={"recorded_by": TEAMMATE})
    _add(storage, "d1", CognitionNodeType.DECISION, "oldest decision",
         timestamp="2026-01-01T12:00:00+00:00", metadata={"recorded_by": TEAMMATE})

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    section = _section(result, "## Since You Were Gone")
    i_incident = section.index("newest incident")
    i_constraint = section.index("middle constraint")
    i_decision = section.index("oldest decision")
    assert i_incident < i_constraint < i_decision


# ── fallback window (no marker) ─────────────────────────────────────────────


def test_digest_no_marker_uses_fallback_window_caps_older_node_fails_before(tmp_path):
    """Fails-before: "never a full-history dump" -- with no marker, only
    nodes within prime_digest_fallback_days show; a node OLDER than the
    window is absent, proving it's a capped lookback, not everything ever."""
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    now = datetime.now(UTC)
    _add(storage, "d-recent", CognitionNodeType.DECISION, "within window decision",
         timestamp=(now - timedelta(days=2)).isoformat(), metadata={"recorded_by": TEAMMATE})
    _add(storage, "d-ancient", CognitionNodeType.DECISION, "outside window decision",
         timestamp=(now - timedelta(days=30)).isoformat(), metadata={"recorded_by": TEAMMATE})

    result = generate_prime(
        storage, PrimeConfig(prime_personalize="on", prime_digest_fallback_days=7),
        current_email=ME["email"],
    )
    section = _section(result, "## Since You Were Gone")
    assert "within window decision" in section
    assert "outside window decision" not in section


def test_digest_malformed_marker_json_falls_back_no_crash(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    cognition_dir.mkdir(parents=True, exist_ok=True)
    _marker_path(cognition_dir).write_text("{not valid json at all", encoding="utf-8")
    _add(storage, "d1", CognitionNodeType.DECISION, "recent decision",
         timestamp=datetime.now(UTC).isoformat(), metadata={"recorded_by": TEAMMATE})

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    assert "recent decision" in result  # falls back to the window, doesn't crash or omit


# ── placement ────────────────────────────────────────────────────────────────


def test_digest_placement_after_manager_decisions_before_recent_activity(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    mgr = {"name": "Manny", "email": "mgr@x.com"}
    _add(storage, "p-mgr", CognitionNodeType.PERSON, "Manny — person", metadata={
        "person": {"email": mgr["email"], "name": mgr["name"], "role": "eng",
                    "seniority": "mid", "reports_to_email": ""},
        "profile_history": [], "recorded_by": mgr, "from_agent": False,
    })
    _add(storage, "p-me", CognitionNodeType.PERSON, "Alice — person", metadata={
        "person": {"email": ME["email"], "name": ME["name"], "role": "eng",
                    "seniority": "mid", "reports_to_email": mgr["email"]},
        "profile_history": [], "recorded_by": ME, "from_agent": False,
    })
    _add(storage, "d-mgr", CognitionNodeType.DECISION, "manager's decision",
         timestamp="2026-01-05T00:00:00+00:00", metadata={"recorded_by": mgr})
    _write_marker(cognition_dir, {ME["email"]: "2026-01-01T00:00:00+00:00"})
    _add(storage, "d-news", CognitionNodeType.DECISION, "since-gone decision",
         timestamp="2026-01-06T00:00:00+00:00", metadata={"recorded_by": TEAMMATE})
    _add(storage, "e-mine", CognitionNodeType.EPISODE, "my recent episode",
         timestamp="2026-01-07T00:00:00+00:00", metadata={"recorded_by": ME})

    result = generate_prime(storage, PrimeConfig(prime_personalize="on"), current_email=ME["email"])
    i_mgr_dec = result.index("## Your Manager's Recent Decisions")
    i_gone = result.index("## Since You Were Gone")
    i_activity = result.index("## Your Recent Activity")
    assert i_mgr_dec < i_gone < i_activity


# ── compat ───────────────────────────────────────────────────────────────────


def test_digest_absent_when_personalize_off(tmp_path):
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    _write_marker(cognition_dir, {ME["email"]: "2026-01-01T00:00:00+00:00"})
    _add(storage, "d1", CognitionNodeType.DECISION, "newer decision",
         timestamp="2026-01-02T00:00:00+00:00", metadata={"recorded_by": TEAMMATE})

    result = generate_prime(storage, PrimeConfig(prime_personalize="off"), current_email=ME["email"])
    assert "## Since You Were Gone" not in result


def test_digest_byte_identical_single_user_auto_graph(tmp_path):
    """A single-user graph under prime_personalize='auto' never personalizes
    (TC16/P13n-2 invariant) -- output must be byte-identical whether or not a
    last-seen marker exists."""
    cognition_dir = tmp_path / ".cognition"
    storage = CognitionStorage(cognition_dir)
    _add(storage, "d1", CognitionNodeType.DECISION, "solo decision", metadata={"recorded_by": ME})

    config = PrimeConfig(prime_personalize="auto")
    no_marker = generate_prime(storage, config, current_email=ME["email"])
    _write_marker(cognition_dir, {ME["email"]: "2020-01-01T00:00:00+00:00"})
    with_marker = generate_prime(storage, config, current_email=ME["email"])
    assert no_marker == with_marker
    assert "## Since You Were Gone" not in with_marker


# ── main() integration ───────────────────────────────────────────────────────


def test_main_stamps_marker_after_output_fails_before(tmp_path, monkeypatch):
    """Fails-before: main() must call _stamp_last_seen after producing prime
    output -- a bare generate_prime call (exercised elsewhere) never does."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "d1", CognitionNodeType.DECISION, "seed decision")

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.delenv("VIBE_MIGRATION_NOTE", raising=False)
    monkeypatch.setattr("vibe_cognition.cognition.prime.resolve_git_identity", lambda repo: ME)

    import io
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main(argv=[])

    data = json.loads((tmp_path / ".cognition" / LAST_SEEN_FILENAME).read_text(encoding="utf-8"))
    assert ME["email"] in data


def test_main_empty_graph_does_not_stamp(tmp_path, monkeypatch):
    """main()'s empty-graph early-exit never reaches the stamp call -- there
    is nothing to digest yet, and the None marker self-heals via the
    fallback window on the first real session."""
    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.delenv("VIBE_MIGRATION_NOTE", raising=False)
    monkeypatch.setattr("vibe_cognition.cognition.prime.resolve_git_identity", lambda repo: ME)

    import io
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main(argv=[])  # no .cognition dir at all -> empty branch

    assert not (tmp_path / ".cognition" / LAST_SEEN_FILENAME).exists()


def test_main_unresolvable_identity_does_not_stamp(tmp_path, monkeypatch):
    storage = CognitionStorage(tmp_path / ".cognition")
    _add(storage, "d1", CognitionNodeType.DECISION, "seed decision")

    monkeypatch.setenv("REPO_PATH", str(tmp_path))
    monkeypatch.delenv("VIBE_MIGRATION_NOTE", raising=False)
    monkeypatch.setattr(
        "vibe_cognition.cognition.prime.resolve_git_identity",
        lambda repo: {"name": "unknown", "email": ""},
    )

    import io
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    main(argv=[])

    assert not (tmp_path / ".cognition" / LAST_SEEN_FILENAME).exists()
