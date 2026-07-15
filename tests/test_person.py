"""WP-TC5 + WP-TC6: person node type + from_agent provenance tests.

Covers the acceptance criteria from
scratchpad/scope-identity-layer.md (WP-TC5 + WP-TC6):
- register/update/get/list person tools; one-node-per-email invariant
- self-registration uses server-resolved email; explicit-email registration
  is recorded in the audit trail (recorded_by/from_agent), not enforced
- update appends exact profile_history entries ({changed, by} asserted)
- reports_to: self/cycle rejection tested; dangling reports_to legal + flagged
- from_agent stamped on cognition_record/cognition_add_task/cognition_store_document/
  cognition_register_person/cognition_update_person (default true, explicit false
  honored); missing key surfaces as unknown (None), never coerced to false
- person searchable via cognition_search(node_type="person"); update re-embeds
- PERSON in _INERT_TYPES lives in test_deterministic_edges.py (TestPersonInertGate)
- solo prime output is byte-identical whether or not person nodes exist
"""

from __future__ import annotations

from vibe_cognition.cognition.models import SENIORITY_LEVELS
from vibe_cognition.cognition.prime import PrimeConfig, generate_prime
from vibe_cognition.tools.cognition_tools import register_cognition_tools

# ── cognition_register_person ───────────────────────────────────────────────


def test_register_person_self_uses_server_resolved_email(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    """Omitting `email` self-registers using the server-resolved git identity's
    email (impersonation-resistant) -- not any client-supplied value."""
    monkeypatch.setattr(
        "vibe_cognition.tools.cognition_tools.resolve_git_identity",
        lambda repo: {"name": "Vorpid", "email": "Vorpid@Example.com"},
    )
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="Vorpid", role="implementer", seniority="mid",
    )
    assert "error" not in result, result
    assert result["type"] == "person"
    assert result["summary"] == "Vorpid — implementer"
    person = result["metadata"]["person"]
    assert person["email"] == "vorpid@example.com"  # casefolded
    assert person["name"] == "Vorpid"
    assert person["role"] == "implementer"
    assert person["seniority"] == "mid"
    assert person["reports_to_email"] == ""
    assert result["metadata"]["recorded_by"] == {"name": "Vorpid", "email": "Vorpid@Example.com"}
    assert result["metadata"]["from_agent"] is True
    assert result["metadata"]["profile_history"] == []
    assert result["already_registered"] is False


def test_register_person_explicit_email_registers_someone_else(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    """An explicit `email` registers a THIRD PARTY -- allowed, trust-based; who did
    it is recorded via recorded_by, not enforced against the explicit email."""
    monkeypatch.setattr(
        "vibe_cognition.tools.cognition_tools.resolve_git_identity",
        lambda repo: {"name": "Vince", "email": "vince@example.com"},
    )
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="Colton Dyck", role="owner", seniority="owner",
        email="Colton.Dyck@AcrylicCode.com",
    )
    assert "error" not in result, result
    assert result["metadata"]["person"]["email"] == "colton.dyck@acryliccode.com"
    # recorded_by names the ACTUAL caller (Vince), not the registered person.
    assert result["metadata"]["recorded_by"]["email"] == "vince@example.com"


def test_register_person_blank_explicit_email_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="   ",
    )
    assert "error" in result


def test_register_person_no_resolvable_email_errors(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    """No explicit email AND an unresolvable git identity -> clean error (WP-P13n
    empty-email edge case), never a person node with a blank identity key."""
    monkeypatch.setattr(
        "vibe_cognition.tools.cognition_tools.resolve_git_identity",
        lambda repo: {"name": "unknown", "email": ""},
    )
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid",
    )
    assert "error" in result


def test_register_person_invalid_seniority_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="ultra-senior", email="x@example.com",
    )
    assert "error" in result
    for level in SENIORITY_LEVELS:
        assert level in result["error"]


def test_register_person_seniority_casefolded(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="SENIOR", email="x@example.com",
    )
    assert "error" not in result, result
    assert result["metadata"]["person"]["seniority"] == "senior"


def test_register_person_one_node_per_email(build_lc, make_ctx, mock_mcp, tmp_path):
    """Re-registering an existing (casefolded) email returns the EXISTING node
    with already_registered=True -- never a silent duplicate, never a lost caller."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    first = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="Dupe@Example.com",
    )
    second = mock_mcp.tools["cognition_register_person"](
        ctx, name="Someone Else", role="different role", seniority="senior",
        email="dupe@example.com",  # same email, different case
    )
    assert second["id"] == first["id"]
    assert second["already_registered"] is True
    # The existing profile was NOT silently overwritten by the second call's fields.
    assert second["metadata"]["person"]["name"] == "X"


def test_register_person_self_reporting_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to_email="x@example.com",
    )
    assert "error" in result


def test_register_person_dangling_reports_to_is_legal(build_lc, make_ctx, mock_mcp, tmp_path):
    """A reports_to_email with no backing person node is LEGAL (a manager may
    register later) -- surfaced as reports_to_registered=False, not an error."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
        reports_to_email="nobody@example.com",
    )
    assert "error" not in result, result
    assert result["metadata"]["person"]["reports_to_email"] == "nobody@example.com"
    assert result["reports_to_registered"] is False


def test_register_person_registered_reports_to_flagged_true(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    manager = mock_mcp.tools["cognition_register_person"](
        ctx, name="Manager", role="lead", seniority="senior", email="mgr@example.com",
    )
    report = mock_mcp.tools["cognition_register_person"](
        ctx, name="Report", role="ic", seniority="mid", email="ic@example.com",
        reports_to_email="mgr@example.com",
    )
    assert report["reports_to_registered"] is True
    assert manager["id"] != report["id"]


def test_register_person_has_no_created_by_parameter(build_lc, make_ctx, mock_mcp, tmp_path):
    """Mirrors cognition_add_task's contract: no client-settable creator identity param."""
    import inspect
    register_cognition_tools(mock_mcp)
    params = set(inspect.signature(mock_mcp.tools["cognition_register_person"]).parameters)
    assert "created_by" not in params
    assert "recorded_by" not in params


# ── cognition_record must reject node_type="person" ─────────────────────────


def test_cognition_record_rejects_person(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_record"](
        ctx, node_type="person", summary="s", detail="d", context="c", author="client",
    )
    assert "error" in result
    assert "cognition_register_person" in result["error"]


# ── cognition_update_person ──────────────────────────────────────────────────


def test_update_person_appends_exact_profile_history_entry(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vibe_cognition.tools.cognition_tools.resolve_git_identity",
        lambda repo: {"name": "Vince", "email": "vince@example.com"},
    )
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    p = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="junior dev", seniority="junior", email="x@example.com",
    )

    updated = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="x@example.com", role="senior dev", seniority="senior",
    )
    assert "error" not in updated, updated
    history = updated["metadata"]["profile_history"]
    assert len(history) == 1
    entry = history[0]
    assert entry["changed"] == {
        "role": {"from": "junior dev", "to": "senior dev"},
        "seniority": {"from": "junior", "to": "senior"},
    }
    assert entry["by"] == {"name": "Vince", "email": "vince@example.com"}
    assert "at" in entry
    # summary regenerated because role changed
    assert updated["summary"] == "X — senior dev"
    assert updated["id"] == p["id"]


def test_update_person_by_node_id_also_works(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    p = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
    )
    updated = mock_mcp.tools["cognition_update_person"](ctx, email_or_id=p["id"], detail="new bio")
    assert "error" not in updated, updated
    assert updated["detail"] == "new bio"


def test_update_person_summary_unchanged_when_only_detail_changes(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
    )
    updated = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="x@example.com", detail="new bio",
    )
    assert updated["summary"] == "X — r"


def test_update_person_reports_to_clear_with_empty_string(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="Mgr", role="lead", seniority="senior", email="mgr@example.com",
    )
    mock_mcp.tools["cognition_register_person"](
        ctx, name="IC", role="ic", seniority="mid", email="ic@example.com",
        reports_to_email="mgr@example.com",
    )
    cleared = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="ic@example.com", reports_to_email="",
    )
    assert "error" not in cleared, cleared
    assert cleared["metadata"]["person"]["reports_to_email"] == ""
    assert cleared["reports_to_registered"] is False


def test_update_person_reports_to_omitted_leaves_unchanged(build_lc, make_ctx, mock_mcp, tmp_path):
    """None (omitted) means no change -- distinct from "" (clear)."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="Mgr", role="lead", seniority="senior", email="mgr@example.com",
    )
    mock_mcp.tools["cognition_register_person"](
        ctx, name="IC", role="ic", seniority="mid", email="ic@example.com",
        reports_to_email="mgr@example.com",
    )
    updated = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="ic@example.com", role="ic2",
    )
    assert updated["metadata"]["person"]["reports_to_email"] == "mgr@example.com"


def test_update_person_self_reporting_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
    )
    result = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="x@example.com", reports_to_email="x@example.com",
    )
    assert "error" in result


def test_update_person_cycle_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    """A -> B -> A must be rejected. Fails-before: no cycle guard would silently
    create an infinite reports_to loop."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="A", role="r", seniority="mid", email="a@example.com",
    )
    mock_mcp.tools["cognition_register_person"](
        ctx, name="B", role="r", seniority="mid", email="b@example.com",
        reports_to_email="a@example.com",
    )
    # A -> B would close the loop (A already indirectly under B via B -> A).
    result = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="a@example.com", reports_to_email="b@example.com",
    )
    assert "error" in result


def test_update_person_dangling_reports_to_still_legal(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
    )
    result = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="x@example.com", reports_to_email="ghost@example.com",
    )
    assert "error" not in result, result
    assert result["reports_to_registered"] is False


def test_update_person_invalid_seniority_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
    )
    result = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="x@example.com", seniority="P0",
    )
    assert "error" in result


def test_update_person_no_fields_errors(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
    )
    result = mock_mcp.tools["cognition_update_person"](ctx, email_or_id="x@example.com")
    assert "error" in result


def test_update_person_not_found_errors(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="nobody@example.com", role="new",
    )
    assert "error" in result


# ── cognition_get_person / cognition_list_people ─────────────────────────────


def test_get_person_by_email_and_id(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    p = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
    )
    by_email = mock_mcp.tools["cognition_get_person"](ctx, email_or_id="X@Example.com")
    by_id = mock_mcp.tools["cognition_get_person"](ctx, email_or_id=p["id"])
    assert by_email["id"] == p["id"] == by_id["id"]
    assert "profile_history" in by_email["metadata"]


def test_get_person_not_found(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_get_person"](ctx, email_or_id="nobody@example.com")
    assert "error" in result


def test_list_people_roster_sorted_by_name(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="Zed", role="r", seniority="mid", email="zed@example.com",
    )
    mock_mcp.tools["cognition_register_person"](
        ctx, name="Amy", role="r", seniority="mid", email="amy@example.com",
    )
    roster = mock_mcp.tools["cognition_list_people"](ctx)
    assert roster["count"] == 2
    names = [p["name"] for p in roster["people"]]
    assert names == ["Amy", "Zed"]
    row = roster["people"][0]
    assert set(row) == {
        "id", "email", "name", "role", "seniority", "reports_to_email",
        "reports_to_registered",
    }


# ── person searchability + re-embed ──────────────────────────────────────────


def test_person_searchable_by_node_type(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="Alpha Person", role="engineer alpha", seniority="mid",
        email="alpha@example.com",
    )
    result = mock_mcp.tools["cognition_search"](ctx, query="alpha", node_type="person")
    assert result["count"] == 1
    assert result["results"][0]["node_type"] == "person"


def test_update_person_reembeds_changed_summary(build_lc, make_ctx, mock_mcp, tmp_path):
    """The _TextKeyedGen fake embedder returns a distinct vector per marker word
    ("alpha"/"beta") -- an update that changes role from one marker to the other
    must move the stored Chroma metadata's summary text (proving a real re-embed,
    not a stale vector)."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)
    chroma = lc["cognition_embedding_storage"]

    mock_mcp.tools["cognition_register_person"](
        ctx, name="P", role="alpha role", seniority="mid", email="p@example.com",
    )
    assert chroma.count_documents(filter={"summary": "P — alpha role"}) == 1

    updated = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="p@example.com", role="beta role",
    )
    assert updated["reembed"] == "done"
    assert chroma.count_documents(filter={"summary": "P — beta role"}) == 1
    assert chroma.count_documents(filter={"summary": "P — alpha role"}) == 0


# ── from_agent (WP-TC6) ───────────────────────────────────────────────────────


def test_register_person_from_agent_defaults_true(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com",
    )
    assert result["metadata"]["from_agent"] is True


def test_register_person_from_agent_explicit_false_honored(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com", from_agent=False,
    )
    assert result["metadata"]["from_agent"] is False


def test_update_person_from_agent_overwrites_node_level_stamp(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    mock_mcp.tools["cognition_register_person"](
        ctx, name="X", role="r", seniority="mid", email="x@example.com", from_agent=True,
    )
    updated = mock_mcp.tools["cognition_update_person"](
        ctx, email_or_id="x@example.com", role="r2", from_agent=False,
    )
    assert updated["metadata"]["from_agent"] is False


def test_cognition_record_from_agent_defaults_true_and_false_honored(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    default = mock_mcp.tools["cognition_record"](
        ctx, node_type="decision", summary="s1", detail="d", context="c", author="a",
    )
    explicit = mock_mcp.tools["cognition_record"](
        ctx, node_type="decision", summary="s2", detail="d", context="c", author="a",
        from_agent=False,
    )
    storage = lc["cognition_storage"]
    assert storage.get_node(default["id"])["metadata"]["from_agent"] is True
    assert storage.get_node(explicit["id"])["metadata"]["from_agent"] is False


def test_cognition_add_task_from_agent_defaults_true_and_false_honored(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    default = mock_mcp.tools["cognition_add_task"](ctx, summary="t1", detail="d", context="c")
    explicit = mock_mcp.tools["cognition_add_task"](
        ctx, summary="t2", detail="d", context="c", from_agent=False,
    )
    assert default["metadata"]["from_agent"] is True
    assert explicit["metadata"]["from_agent"] is False

    rows = {t["id"]: t for t in mock_mcp.tools["cognition_list_tasks"](ctx)["tasks"]}
    assert rows[default["id"]]["from_agent"] is True
    assert rows[explicit["id"]]["from_agent"] is False


def test_cognition_store_document_from_agent_defaults_true_and_false_honored(
    build_lc, make_ctx, mock_mcp, tmp_path
):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage = lc["cognition_storage"]

    default = mock_mcp.tools["cognition_store_document"](
        ctx, title="doc1", document_text="hello", context="c", author="a",
        content_text="hello world",
    )
    explicit = mock_mcp.tools["cognition_store_document"](
        ctx, title="doc2", document_text="hello", context="c", author="a",
        content_text="a different document body", from_agent=False,
    )
    assert storage.get_node(default["node_id"])["metadata"]["from_agent"] is True
    assert storage.get_node(explicit["node_id"])["metadata"]["from_agent"] is False


def test_search_result_from_agent_missing_key_is_none_not_false(build_lc, make_ctx, mock_mcp, tmp_path):
    """A node embedded before from_agent existed has NO 'from_agent' key in Chroma
    metadata -- the search result must surface None ("unknown"), never coerce it
    to False (which would misrepresent unknown provenance as "known agent-written")."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)
    chroma = lc["cognition_embedding_storage"]

    # Simulate a pre-TC6 vector: upsert directly with no from_agent key.
    storage = lc["cognition_storage"]
    from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
    node = CognitionNode(
        id="legacy1", type=CognitionNodeType.DECISION, summary="legacy alpha decision",
        detail="d", context=[], references=[], timestamp="2026-01-01T00:00:00+00:00",
        author="a", metadata={},  # no recorded_by/from_agent -- pre-existing node
    )
    storage.add_node(node)
    chroma.upsert_embedding(
        "legacy1", lc["embedding_generator"].generate("decision: legacy alpha decision\nd"),
        {"entity_type": "decision", "summary": "legacy alpha decision"},
    )

    result = mock_mcp.tools["cognition_search"](ctx, query="alpha", node_type="decision")
    assert result["count"] == 1
    assert result["results"][0]["from_agent"] is None


# ── prime.py: NO changes in this WP -- person nodes must not appear ─────────


def test_solo_prime_byte_identical_with_and_without_person_nodes(tmp_path):
    """Person nodes are invisible to prime.py (onboarding surfacing is WP-TC7,
    out of scope here) -- the solo digest is byte-identical whether or not
    person nodes exist in the graph."""
    from vibe_cognition.cognition import CognitionStorage
    from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType

    def _make_storage(path):
        storage = CognitionStorage(path / ".cognition")
        storage.add_node(CognitionNode(
            id="dec1", type=CognitionNodeType.DECISION, summary="a decision",
            detail="d", context=[], references=[], timestamp="2026-01-01T00:00:00+00:00",
            author="a",
        ))
        return storage

    baseline = _make_storage(tmp_path / "baseline")
    baseline_out = generate_prime(baseline, PrimeConfig())

    with_person = _make_storage(tmp_path / "with_person")
    with_person.add_node(CognitionNode(
        id="p1", type=CognitionNodeType.PERSON, summary="A — role",
        detail="", context=[], references=[], timestamp="2026-01-01T00:00:00+00:00",
        author="A",
        metadata={"person": {"email": "a@example.com", "name": "A", "role": "role",
                              "seniority": "mid", "reports_to_email": ""},
                  "profile_history": [], "from_agent": True},
    ))
    with_person_out = generate_prime(with_person, PrimeConfig())

    assert baseline_out == with_person_out
    assert "person" not in with_person_out.lower()
