"""WP-Task-Node: task first-class node type tests.

Covers the acceptance criteria from docs/wp-task-node-plan.md:
- task is a valid node type created via cognition_add_task (NOT cognition_record)
- created_by is resolved SERVER-SIDE and is not client-overridable (no parameter)
- git-identity fallback chain: git config -> OS user -> "unknown"; never raises
- create -> list -> update status: transition log grows; done drops from default list
- status transition legality (reject unknown / illegal jump; allow reopen)
- explicit parent part_of edge; parent validated (missing / non-task rejected)
- re-parenting: move / detach / cycle-guard / subtree-carry / cluster-edge-untouched
- parent deletion tolerated by list_tasks grouping (stale parent_id, no crash)
- re-embed surfaces status: the NEW status string lands in Chroma metadata + embed text
  (the B1 regression guard — fails against an un-extended _embed_entity_node)
- collision regression: same-summary tasks one tick apart get distinct minted ids
- prime injection: open tasks sorted by priority, done/cancelled excluded, top-N cap
- get_status statistics include the new type
- matcher inertness lives in test_deterministic_edges.py (TestTaskInertGate)
"""

import os
from datetime import UTC, datetime

from vibe_cognition.cognition import (
    CognitionEdge,
    CognitionEdgeType,
    CognitionNode,
    CognitionNodeType,
    CognitionStorage,
    resolve_git_identity,
)
from vibe_cognition.cognition.prime import _format_tasks, generate_prime
from vibe_cognition.tools.cognition_tools import (
    _TASK_TRANSITIONS,
    _embed_entity_node,
    register_cognition_tools,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _task_node(
    storage: CognitionStorage,
    node_id: str,
    summary: str,
    *,
    status: str = "open",
    severity: str = "normal",
    owner: str | None = None,
    parent_id: str | None = None,
    ts: str | None = None,
) -> None:
    """Add a task node straight to storage (for prime/list tests that don't need the tool)."""
    ts = ts or datetime.now(UTC).isoformat()
    who = {"name": "t", "email": ""}
    storage.add_node(CognitionNode(
        id=node_id, type=CognitionNodeType.TASK, summary=summary, detail="d",
        context=[], references=[], severity=severity, timestamp=ts, author="t",
        metadata={
            "status": status, "owner": owner, "parent_id": parent_id,
            "created_by": who,
            "transitions": [{"status": "open", "at": ts, "by": who}],
        },
    ))


def _meta(storage: CognitionStorage, node_id: str) -> dict:
    """Fetch a node's metadata dict, asserting the node exists (keeps pyright happy)."""
    node = storage.get_node(node_id)
    assert node is not None, f"node {node_id} missing"
    return node["metadata"]


def _gitconfig_text(name: str | None = None, email: str | None = None) -> str:
    """Render a minimal git-config body with tab-indented keys (exactly like real git)."""
    lines = ["[user]"]
    if name is not None:
        lines.append(f"\tname = {name}")
    if email is not None:
        lines.append(f"\temail = {email}")
    return "\n".join(lines) + "\n"


# ── git identity resolution (pure file-read; NO subprocess — P0 v0.12.1) ───────
#
# The original shelled `git config`, which hangs forever in the detached/windowless
# MCP server (the git child never closes the stdout pipe, so subprocess's reader-
# thread join never returns and timeout= cannot fire). resolve_git_identity now reads
# the git config FILES directly. These tests isolate the global file via
# GIT_CONFIG_GLOBAL (git's own override) so they never read the dev's real ~/.gitconfig.


def test_resolve_git_identity_reads_global_config(tmp_path, monkeypatch):
    """[user] name+email in the global config file → returned verbatim."""
    gc = tmp_path / "gitconfig"
    gc.write_text(_gitconfig_text(name="Alice Dev", email="alice@example.com"), encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
    ident = resolve_git_identity(tmp_path / "repo")  # repo has no .git → only global applies
    assert ident == {"name": "Alice Dev", "email": "alice@example.com"}


def test_resolve_git_identity_local_overrides_global(tmp_path, monkeypatch):
    """Local .git/config overrides global name; email inherits from global when local omits it.

    Fails-before: a reader that ignored precedence, or wiped email when local lacked it.
    """
    gc = tmp_path / "gitconfig"
    gc.write_text(_gitconfig_text(name="Global Name", email="global@example.com"), encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(_gitconfig_text(name="Local Name"), encoding="utf-8")
    ident = resolve_git_identity(repo)
    assert ident["name"] == "Local Name"
    assert ident["email"] == "global@example.com"


def test_resolve_git_identity_name_unset_falls_back_to_os_user(tmp_path, monkeypatch):
    """No name in any config → OS user (getpass.getuser()); email stays "".

    Fails-before: if the helper hard-required a config or returned "" for name.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(
        "vibe_cognition.cognition.git_identity.getpass.getuser", lambda: "osuser"
    )
    ident = resolve_git_identity(tmp_path / "repo")
    assert ident["name"] == "osuser"
    assert ident["email"] == ""


def test_resolve_git_identity_total_failure_is_unknown(tmp_path, monkeypatch):
    """No config AND getpass raises → name "unknown"; never propagates the error."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "does-not-exist"))

    def _boom():
        raise OSError("no user env")

    monkeypatch.setattr(
        "vibe_cognition.cognition.git_identity.getpass.getuser", _boom
    )
    ident = resolve_git_identity(tmp_path / "repo")
    assert ident["name"] == "unknown"


def test_resolve_git_identity_malformed_config_does_not_raise(tmp_path, monkeypatch):
    """A garbage config file must never raise — fall back cleanly to the OS user."""
    gc = tmp_path / "gitconfig"
    gc.write_text("\x00\x01 not ini [user\nname no-equals\n= = =\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
    monkeypatch.setattr(
        "vibe_cognition.cognition.git_identity.getpass.getuser", lambda: "osuser"
    )
    ident = resolve_git_identity(tmp_path / "repo")
    assert ident["name"] == "osuser"


def test_resolve_git_identity_never_spawns_subprocess(tmp_path, monkeypatch):
    """THE regression pin (P0 v0.12.1): resolution must NEVER shell out — a git subprocess
    in the detached/windowless MCP server hangs forever. Record-and-raise on every spawn
    primitive, then assert NOTHING was spawned and the file value still resolved.

    Fails-before: the old subprocess implementation calls subprocess.run → recorded → fails.
    """
    import subprocess as _sp

    spawned: list = []

    def _spy(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("resolve_git_identity must not spawn a subprocess")

    monkeypatch.setattr(_sp, "run", _spy)
    monkeypatch.setattr(_sp, "Popen", _spy)
    monkeypatch.setattr(_sp, "call", _spy, raising=False)
    monkeypatch.setattr(os, "system", _spy, raising=False)

    gc = tmp_path / "gitconfig"
    gc.write_text(_gitconfig_text(name="No Shell", email="ns@example.com"), encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
    ident = resolve_git_identity(tmp_path / "repo")

    assert spawned == [], f"resolve_git_identity spawned a subprocess: {spawned}"
    assert ident == {"name": "No Shell", "email": "ns@example.com"}


def test_resolve_git_identity_ignores_user_subsection(tmp_path, monkeypatch):
    """A name in a [user "sub"] subsection is NOT the identity — only bare [user] counts.

    Fails-before: a section matcher that keys on the first token would wrongly accept it.
    """
    gc = tmp_path / "gitconfig"
    gc.write_text('[user "alt"]\n\tname = Wrong Person\n\temail = wrong@example.com\n', encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
    monkeypatch.setattr(
        "vibe_cognition.cognition.git_identity.getpass.getuser", lambda: "osuser"
    )
    ident = resolve_git_identity(tmp_path / "repo")
    assert ident["name"] == "osuser"  # subsection ignored → fallback
    assert ident["email"] == ""


def test_resolve_git_identity_strips_quotes_and_inline_comment(tmp_path, monkeypatch):
    """Quoted name → literal contents; unquoted email with a whitespace-preceded comment → trimmed."""
    gc = tmp_path / "gitconfig"
    gc.write_text(
        '[user]\n\tname = "Colton Dyck"\n\temail = me@example.com ; work addr\n', encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
    ident = resolve_git_identity(tmp_path / "repo")
    assert ident == {"name": "Colton Dyck", "email": "me@example.com"}


def test_resolve_git_identity_local_empty_value_overrides_global(tmp_path, monkeypatch):
    """An explicit empty `email =` in local clears the global email (precedence, not truthiness).

    Fails-before: a truthiness merge (`if found.get("email")`) lets the global email bleed through.
    """
    gc = tmp_path / "gitconfig"
    gc.write_text(_gitconfig_text(name="N", email="global@example.com"), encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gc))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text("[user]\n\temail =\n", encoding="utf-8")
    ident = resolve_git_identity(repo)
    assert ident["name"] == "N"     # name still inherited from global
    assert ident["email"] == ""     # local explicit-empty wins over global


def test_resolve_git_identity_home_unset_does_not_raise(tmp_path, monkeypatch):
    """Path.home() raising (no home env) must NOT propagate — fall back to OS user.

    Fails-before: an unguarded Path.home() in the global-path builder crashes the tool.
    """
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)

    def _no_home():
        raise RuntimeError("no home env")

    monkeypatch.setattr("vibe_cognition.cognition.git_identity.Path.home", _no_home)
    monkeypatch.setattr(
        "vibe_cognition.cognition.git_identity.getpass.getuser", lambda: "osuser"
    )
    ident = resolve_git_identity(tmp_path / "repo")  # repo has no .git → only (failed) global
    assert ident["name"] == "osuser"


# ── cognition_add_task ─────────────────────────────────────────────────────────


def test_add_task_seeds_lifecycle_and_server_identity(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    """cognition_add_task: seeds status=open + created_by (server-resolved) + initial transition.

    Fails-before: if the tool didn't seed metadata or trusted a client identity.
    """
    monkeypatch.setattr(
        "vibe_cognition.tools.cognition_tools.resolve_git_identity",
        lambda repo: {"name": "Server Resolved", "email": "srv@x.com"},
    )
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_add_task"](
        ctx, summary="wire the thing", detail="do X then Y", context="thing,wiring",
        priority="high", owner="bob",
    )
    assert "error" not in result, result
    assert result["type"] == "task"
    assert result["severity"] == "high"  # priority IS severity
    meta = result["metadata"]
    assert meta["status"] == "open"
    assert meta["created_by"] == {"name": "Server Resolved", "email": "srv@x.com"}
    assert meta["owner"] == "bob"
    assert len(meta["transitions"]) == 1
    assert meta["transitions"][0]["status"] == "open"
    # author mirrors the server-resolved name (no client author param)
    assert result["author"] == "Server Resolved"


def test_add_task_has_no_created_by_parameter(build_lc, make_ctx, mock_mcp, tmp_path):
    """cognition_add_task has NO created_by param — the client cannot set the creator.

    Fails-before: if a created_by argument existed, a client could spoof attribution.
    """
    import inspect
    register_cognition_tools(mock_mcp)
    params = set(inspect.signature(mock_mcp.tools["cognition_add_task"]).parameters)
    assert "created_by" not in params, "cognition_add_task must not accept a created_by param"


def test_cognition_record_rejects_task(build_lc, make_ctx, mock_mcp, tmp_path):
    """cognition_record(node_type="task") is rejected and names cognition_add_task.

    Fails-before: if cognition_record minted an un-attributed, lifecycle-less task.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    result = mock_mcp.tools["cognition_record"](
        ctx, node_type="task", summary="t", detail="d", context="c", author="client",
    )
    assert "error" in result
    assert "cognition_add_task" in result["error"]


def test_add_task_collision_regression(build_lc, make_ctx, mock_mcp, tmp_path):
    """Two same-summary tasks one tick apart get distinct minted ids (WP-ID)."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    r1 = mock_mcp.tools["cognition_add_task"](ctx, summary="fix the thing", detail="a", context="c")
    r2 = mock_mcp.tools["cognition_add_task"](ctx, summary="fix the thing", detail="b", context="c")
    assert r1["id"] != r2["id"]
    storage: CognitionStorage = lc["cognition_storage"]
    assert storage.get_node(r1["id"]) is not None
    assert storage.get_node(r2["id"]) is not None


# ── parent edge + validation ───────────────────────────────────────────────────


def test_add_task_parent_edge_created(build_lc, make_ctx, mock_mcp, tmp_path):
    """A parent_id creates a child→parent part_of edge tagged source=task-parent."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]

    parent = mock_mcp.tools["cognition_add_task"](ctx, summary="epic", detail="d", context="c")
    child = mock_mcp.tools["cognition_add_task"](
        ctx, summary="subtask", detail="d", context="c", parent_id=parent["id"],
    )
    assert child["metadata"]["parent_id"] == parent["id"]
    succ = storage.get_successors(child["id"], CognitionEdgeType.PART_OF)
    assert len(succ) == 1
    assert succ[0][0] == parent["id"]
    assert succ[0][1].get("source") == "task-parent"


def test_add_task_rejects_missing_parent(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    result = mock_mcp.tools["cognition_add_task"](
        ctx, summary="t", detail="d", context="c", parent_id="nope",
    )
    assert "error" in result and "does not exist" in result["error"]


def test_add_task_rejects_non_task_parent(build_lc, make_ctx, mock_mcp, tmp_path):
    """A parent_id pointing at a non-task node is rejected (no orphan task created)."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]
    # a decision node, not a task
    dec = mock_mcp.tools["cognition_record"](
        ctx, node_type="decision", summary="a decision", detail="d", context="c", author="t",
    )
    result = mock_mcp.tools["cognition_add_task"](
        ctx, summary="t", detail="d", context="c", parent_id=dec["id"],
    )
    assert "error" in result and "not a task" in result["error"]
    # no orphan task left behind
    assert not storage.get_nodes_by_type(CognitionNodeType.TASK)


# ── status transitions + list ──────────────────────────────────────────────────


def test_create_list_update_status_done_flow(build_lc, make_ctx, mock_mcp, tmp_path):
    """create -> list (visible) -> in_progress -> done: transition log grows, done drops."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)

    t = mock_mcp.tools["cognition_add_task"](ctx, summary="task A", detail="d", context="c")
    tid = t["id"]

    listed = mock_mcp.tools["cognition_list_tasks"](ctx)
    assert listed["count"] == 1 and listed["tasks"][0]["id"] == tid

    up = mock_mcp.tools["cognition_update_task"](ctx, node_id=tid, status="in_progress")
    assert up["metadata"]["status"] == "in_progress"
    assert len(up["metadata"]["transitions"]) == 2

    done = mock_mcp.tools["cognition_update_task"](ctx, node_id=tid, status="done")
    assert done["metadata"]["status"] == "done"
    assert len(done["metadata"]["transitions"]) == 3

    # done drops from the default view, returns with include_done
    assert mock_mcp.tools["cognition_list_tasks"](ctx)["count"] == 0
    assert mock_mcp.tools["cognition_list_tasks"](ctx, include_done=True)["count"] == 1


def test_update_task_reopen_allowed(build_lc, make_ctx, mock_mcp, tmp_path):
    """done -> open (reopen) is a legal transition."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    t = mock_mcp.tools["cognition_add_task"](ctx, summary="t", detail="d", context="c")
    mock_mcp.tools["cognition_update_task"](ctx, node_id=t["id"], status="done")
    reopened = mock_mcp.tools["cognition_update_task"](ctx, node_id=t["id"], status="open")
    assert "error" not in reopened
    assert reopened["metadata"]["status"] == "open"


def test_update_task_rejects_unknown_status(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    t = mock_mcp.tools["cognition_add_task"](ctx, summary="t", detail="d", context="c")
    result = mock_mcp.tools["cognition_update_task"](ctx, node_id=t["id"], status="frobnicated")
    assert "error" in result and "Invalid status" in result["error"]


def test_add_task_rejects_invalid_priority(build_lc, make_ctx, mock_mcp, tmp_path):
    """WP-12 (4ae72cafb48c): priority is validated like status, one function
    away -- a typo like "urgent" must be rejected, not silently accepted and
    sorted into the "normal" SEVERITY_ORDER band.

    Fails-before: any string was written into severity unvalidated.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    result = mock_mcp.tools["cognition_add_task"](
        ctx, summary="t", detail="d", context="c", priority="urgent"
    )
    assert "error" in result and "Invalid priority" in result["error"]


def test_add_task_rejects_p0_style_priority(build_lc, make_ctx, mock_mcp, tmp_path):
    """The other example from the task write-up: "P0" must also be rejected."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    result = mock_mcp.tools["cognition_add_task"](
        ctx, summary="t", detail="d", context="c", priority="P0"
    )
    assert "error" in result and "Invalid priority" in result["error"]


def test_update_task_rejects_invalid_priority(build_lc, make_ctx, mock_mcp, tmp_path):
    """Same guard on the update path -- a task's priority can't be corrupted
    to an unfilterable value after creation either."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    t = mock_mcp.tools["cognition_add_task"](ctx, summary="t", detail="d", context="c")
    result = mock_mcp.tools["cognition_update_task"](ctx, node_id=t["id"], priority="urgent")
    assert "error" in result and "Invalid priority" in result["error"]
    # Original priority must be untouched by the rejected update.
    fetched = mock_mcp.tools["cognition_get_node"](ctx, node_id=t["id"])
    assert fetched["severity"] == "normal"


def test_update_task_rejects_illegal_jump(build_lc, make_ctx, mock_mcp, tmp_path):
    """done -> in_progress is not legal (must reopen first); rejected with a clear error."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    t = mock_mcp.tools["cognition_add_task"](ctx, summary="t", detail="d", context="c")
    mock_mcp.tools["cognition_update_task"](ctx, node_id=t["id"], status="done")
    result = mock_mcp.tools["cognition_update_task"](ctx, node_id=t["id"], status="in_progress")
    assert "error" in result and "transition" in result["error"].lower()


def test_transition_table_reopen_is_legal():
    """The shared transition constant allows reopen from both terminal states (locked spec)."""
    assert "open" in _TASK_TRANSITIONS["done"]
    assert "open" in _TASK_TRANSITIONS["cancelled"]


def test_update_task_rejects_non_task(build_lc, make_ctx, mock_mcp, tmp_path):
    """cognition_update_task on a non-task id errors and points at cognition_update_node."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    dec = mock_mcp.tools["cognition_record"](
        ctx, node_type="decision", summary="d", detail="d", context="c", author="t",
    )
    result = mock_mcp.tools["cognition_update_task"](ctx, node_id=dec["id"], status="done")
    assert "error" in result and "cognition_update_node" in result["error"]


def test_update_task_no_fields_errors(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    t = mock_mcp.tools["cognition_add_task"](ctx, summary="t", detail="d", context="c")
    result = mock_mcp.tools["cognition_update_task"](ctx, node_id=t["id"])
    assert "error" in result and "No updatable fields" in result["error"]


def test_update_task_owner_and_narrative(build_lc, make_ctx, mock_mcp, tmp_path):
    """owner/priority/summary edits apply; owner="" clears."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    t = mock_mcp.tools["cognition_add_task"](ctx, summary="t", detail="d", context="c", owner="bob")
    up = mock_mcp.tools["cognition_update_task"](
        ctx, node_id=t["id"], owner="carol", priority="critical", summary="renamed",
    )
    assert up["metadata"]["owner"] == "carol"
    assert up["severity"] == "critical"
    assert up["summary"] == "renamed"
    cleared = mock_mcp.tools["cognition_update_task"](ctx, node_id=t["id"], owner="")
    assert cleared["metadata"]["owner"] is None


def test_update_task_note_recorded_on_transition(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    t = mock_mcp.tools["cognition_add_task"](ctx, summary="t", detail="d", context="c")
    up = mock_mcp.tools["cognition_update_task"](
        ctx, node_id=t["id"], status="blocked", note="waiting on upstream",
    )
    last = up["metadata"]["transitions"][-1]
    assert last["status"] == "blocked" and last["note"] == "waiting on upstream"


def test_update_task_note_without_transition_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    """note without a status change is rejected (not silently dropped); the rejected calls
    mutate nothing, and note WITH a real transition is accepted.

    Fails-before: note only attached inside the status-change branch, so a note passed
    without (or with an unchanged) status vanished silently.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    t = mock_mcp.tools["cognition_add_task"](ctx, summary="t", detail="d", context="c")

    r1 = mock_mcp.tools["cognition_update_task"](ctx, node_id=t["id"], note="orphan note")
    assert "error" in r1 and "note" in r1["error"]
    r2 = mock_mcp.tools["cognition_update_task"](ctx, node_id=t["id"], status="open", note="x")
    assert "error" in r2 and "note" in r2["error"]

    r3 = mock_mcp.tools["cognition_update_task"](ctx, node_id=t["id"], status="in_progress", note="real")
    assert "error" not in r3
    assert r3["metadata"]["transitions"][-1]["note"] == "real"
    # the two rejected calls left no stray transitions (initial open + in_progress == 2)
    assert len(r3["metadata"]["transitions"]) == 2


# ── list filters + tree depth ──────────────────────────────────────────────────


def test_list_tasks_filters_and_priority_sort(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]
    _task_node(storage, "low1", "low task", severity="low")
    _task_node(storage, "crit1", "crit task", severity="critical")
    _task_node(storage, "norm1", "norm task", severity="normal", owner="bob")

    out = mock_mcp.tools["cognition_list_tasks"](ctx)
    ids = [t["id"] for t in out["tasks"]]
    assert ids[0] == "crit1"  # critical sorts first
    assert ids[-1] == "low1"  # low sorts last

    only_bob = mock_mcp.tools["cognition_list_tasks"](ctx, owner="bob")
    assert [t["id"] for t in only_bob["tasks"]] == ["norm1"]

    only_crit = mock_mcp.tools["cognition_list_tasks"](ctx, priority="critical")
    assert [t["id"] for t in only_crit["tasks"]] == ["crit1"]


def test_list_tasks_depth_annotation(build_lc, make_ctx, mock_mcp, tmp_path):
    """Children carry a depth = count of present ancestors; tree reads via depth."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]
    _task_node(storage, "p", "parent")
    _task_node(storage, "c", "child", parent_id="p")
    _task_node(storage, "g", "grand", parent_id="c")

    rows = {t["id"]: t for t in mock_mcp.tools["cognition_list_tasks"](ctx)["tasks"]}
    assert rows["p"]["depth"] == 0
    assert rows["c"]["depth"] == 1
    assert rows["g"]["depth"] == 2


def test_list_tasks_rejects_bad_status(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    result = mock_mcp.tools["cognition_list_tasks"](ctx, status="frob")
    assert "error" in result


def test_list_tasks_explicit_closed_status_filter_returns_them(build_lc, make_ctx, mock_mcp, tmp_path):
    """list_tasks(status='done'/'cancelled') returns those tasks — a closed-status filter
    implies include_done; the default still excludes both.

    Fails-before: the default closed-status exclusion fired BEFORE the status filter, so
    list_tasks(status='done') returned empty (contradicting the docstring).
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]
    _task_node(storage, "o1", "open one", status="open")
    _task_node(storage, "d1", "done one", status="done")
    _task_node(storage, "x1", "cancelled one", status="cancelled")

    assert [t["id"] for t in mock_mcp.tools["cognition_list_tasks"](ctx, status="done")["tasks"]] == ["d1"]
    assert [t["id"] for t in mock_mcp.tools["cognition_list_tasks"](ctx, status="cancelled")["tasks"]] == ["x1"]
    # default (no status) and an explicit open filter both exclude the closed tasks
    assert [t["id"] for t in mock_mcp.tools["cognition_list_tasks"](ctx)["tasks"]] == ["o1"]
    assert [t["id"] for t in mock_mcp.tools["cognition_list_tasks"](ctx, status="open")["tasks"]] == ["o1"]


def test_list_tasks_tolerates_deleted_parent(build_lc, make_ctx, mock_mcp, tmp_path):
    """Deleting a parent leaves a stale metadata.parent_id; list_tasks shows the child
    ungrouped (depth 0) and does not crash (F10)."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]
    _task_node(storage, "p", "parent")
    _task_node(storage, "c", "child", parent_id="p")
    storage.remove_node("p")

    out = mock_mcp.tools["cognition_list_tasks"](ctx)
    rows = {t["id"]: t for t in out["tasks"]}
    assert "c" in rows
    assert rows["c"]["depth"] == 0  # parent gone → shown ungrouped
    assert rows["c"]["parent_id"] == "p"  # stale pointer retained (not an FK)


# ── re-parenting ────────────────────────────────────────────────────────────────


def test_reparent_moves_edge_and_pointer(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]
    p1 = mock_mcp.tools["cognition_add_task"](ctx, summary="p1", detail="d", context="c")
    p2 = mock_mcp.tools["cognition_add_task"](ctx, summary="p2", detail="d", context="c")
    c = mock_mcp.tools["cognition_add_task"](ctx, summary="c", detail="d", context="c", parent_id=p1["id"])

    moved = mock_mcp.tools["cognition_update_task"](ctx, node_id=c["id"], parent_id=p2["id"])
    assert moved["metadata"]["parent_id"] == p2["id"]
    # old edge gone, new edge present
    assert not storage.graph.has_edge(c["id"], p1["id"])
    succ = storage.get_successors(c["id"], CognitionEdgeType.PART_OF)
    assert [s[0] for s in succ] == [p2["id"]]


def test_reparent_detach_to_top_level(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]
    p = mock_mcp.tools["cognition_add_task"](ctx, summary="p", detail="d", context="c")
    c = mock_mcp.tools["cognition_add_task"](ctx, summary="c", detail="d", context="c", parent_id=p["id"])

    detached = mock_mcp.tools["cognition_update_task"](ctx, node_id=c["id"], parent_id="")
    assert detached["metadata"]["parent_id"] is None
    assert not storage.get_successors(c["id"], CognitionEdgeType.PART_OF)


def test_reparent_cycle_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    """Making a task a child of its own descendant is rejected; no edge/pointer change."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]
    p = mock_mcp.tools["cognition_add_task"](ctx, summary="p", detail="d", context="c")
    c = mock_mcp.tools["cognition_add_task"](ctx, summary="c", detail="d", context="c", parent_id=p["id"])

    result = mock_mcp.tools["cognition_update_task"](ctx, node_id=p["id"], parent_id=c["id"])
    assert "error" in result and "cycle" in result["error"].lower()
    # p unchanged
    assert _meta(storage, p["id"])["parent_id"] is None
    assert not storage.graph.has_edge(p["id"], c["id"])


def test_reparent_self_rejected(build_lc, make_ctx, mock_mcp, tmp_path):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    t = mock_mcp.tools["cognition_add_task"](ctx, summary="t", detail="d", context="c")
    result = mock_mcp.tools["cognition_update_task"](ctx, node_id=t["id"], parent_id=t["id"])
    assert "error" in result


def test_reparent_carries_subtree(build_lc, make_ctx, mock_mcp, tmp_path):
    """Moving a task moves only its own edge — its children stay attached (subtree rides along)."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]
    p1 = mock_mcp.tools["cognition_add_task"](ctx, summary="p1", detail="d", context="c")
    p2 = mock_mcp.tools["cognition_add_task"](ctx, summary="p2", detail="d", context="c")
    c = mock_mcp.tools["cognition_add_task"](ctx, summary="c", detail="d", context="c", parent_id=p1["id"])
    g = mock_mcp.tools["cognition_add_task"](ctx, summary="g", detail="d", context="c", parent_id=c["id"])

    mock_mcp.tools["cognition_update_task"](ctx, node_id=c["id"], parent_id=p2["id"])
    # g still under c (untouched)
    assert _meta(storage, g["id"])["parent_id"] == c["id"]
    assert storage.graph.has_edge(g["id"], c["id"])


def test_reparent_leaves_cluster_membership_edge_untouched(build_lc, make_ctx, mock_mcp, tmp_path):
    """A task with BOTH a task-parent edge and a curate cluster part_of edge keeps the
    cluster edge when moved (only the parent edge swaps)."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]
    p1 = mock_mcp.tools["cognition_add_task"](ctx, summary="p1", detail="d", context="c")
    p2 = mock_mcp.tools["cognition_add_task"](ctx, summary="p2", detail="d", context="c")
    c = mock_mcp.tools["cognition_add_task"](ctx, summary="c", detail="d", context="c", parent_id=p1["id"])
    # a /vibe-curate cluster summary node + a cluster-membership part_of edge from the task
    storage.add_node(CognitionNode(
        id="cluster1", type=CognitionNodeType.PATTERN, summary="cluster", detail="d",
        context=[], references=[], timestamp=datetime.now(UTC).isoformat(), author="t",
    ))
    storage.add_edge(CognitionEdge(
        from_id=c["id"], to_id="cluster1", edge_type=CognitionEdgeType.PART_OF,
        timestamp=datetime.now(UTC).isoformat(), source="curate-skill",
    ))

    mock_mcp.tools["cognition_update_task"](ctx, node_id=c["id"], parent_id=p2["id"])
    assert storage.graph.has_edge(c["id"], "cluster1"), "cluster-membership edge was disturbed"
    assert storage.graph.has_edge(c["id"], p2["id"])
    assert not storage.graph.has_edge(c["id"], p1["id"])


def test_reparent_add_edge_failure_leaves_state_intact(build_lc, make_ctx, mock_mcp, tmp_path, monkeypatch):
    """If add_edge fails (new parent vanished cross-process between validation and add),
    the re-parent errors BEFORE removing the old edge or writing parent_id — no orphaned
    pointer with a missing edge.

    Fails-before: the old code removed the old edge + wrote parent_id regardless of
    add_edge's return.
    """
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    storage: CognitionStorage = lc["cognition_storage"]
    p1 = mock_mcp.tools["cognition_add_task"](ctx, summary="p1", detail="d", context="c")
    p2 = mock_mcp.tools["cognition_add_task"](ctx, summary="p2", detail="d", context="c")
    c = mock_mcp.tools["cognition_add_task"](ctx, summary="c", detail="d", context="c", parent_id=p1["id"])

    # Simulate add_edge failing at the moment of the re-parent (node vanished at add time).
    monkeypatch.setattr(storage, "add_edge", lambda edge: False)
    result = mock_mcp.tools["cognition_update_task"](ctx, node_id=c["id"], parent_id=p2["id"])
    assert "error" in result
    # old edge + pointer intact; no new edge
    assert _meta(storage, c["id"])["parent_id"] == p1["id"]
    assert storage.graph.has_edge(c["id"], p1["id"])
    assert not storage.graph.has_edge(c["id"], p2["id"])


# ── re-embed surfaces status (B1 regression guard) ─────────────────────────────


def test_update_status_reembeds_new_status_into_chroma(build_lc, make_ctx, mock_mcp, tmp_path):
    """After cognition_update_task(status=...), the NEW status string lands in Chroma
    metadata (the old one is gone). Fails against an un-extended _embed_entity_node."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)
    chroma = lc["cognition_embedding_storage"]

    t = mock_mcp.tools["cognition_add_task"](ctx, summary="embed me", detail="d", context="c")
    # seeded open status is in the vector metadata
    assert chroma.count_documents(filter={"status": "open"}) == 1

    mock_mcp.tools["cognition_update_task"](ctx, node_id=t["id"], status="in_progress")
    assert chroma.count_documents(filter={"status": "in_progress"}) == 1
    assert chroma.count_documents(filter={"status": "open"}) == 0  # upsert overwrote it


def test_embed_entity_node_extends_text_with_status(fake_generator, tmp_path):
    """_embed_entity_node appends 'status: ...'/'owner: ...' to the embed TEXT for tasks.

    Fails-before: the un-extended embed path emitted only 'type: summary\\ndetail'.
    """
    from vibe_cognition.embeddings import ChromaDBStorage

    seen = {}

    class _RecordingGen:
        def generate(self, text, input_type="document"):
            seen["text"] = text
            return [1.0, 0.0, 0.0]

        def generate_query_embedding(self, text):
            return [1.0, 0.0, 0.0]

    chroma = ChromaDBStorage(
        persist_directory=tmp_path / "chromadb", embedding_model="m", embedding_dimensions=3,
    )
    node = CognitionNode(
        id="tk1", type=CognitionNodeType.TASK, summary="do it", detail="body",
        context=[], references=[], severity="high", timestamp=datetime.now(UTC).isoformat(),
        author="t", metadata={"status": "in_progress", "owner": "bob"},
    )
    _embed_entity_node(chroma, _RecordingGen(), node)  # type: ignore[arg-type]
    assert "status: in_progress" in seen["text"]
    assert "owner: bob" in seen["text"]


def test_embed_entity_node_no_status_for_non_task(fake_generator, tmp_path):
    """Non-task nodes (empty metadata) get NO status/owner in the embed text — no regression."""
    seen = {}

    class _RecordingGen:
        def generate(self, text, input_type="document"):
            seen["text"] = text
            return [0.0, 0.0, 1.0]

        def generate_query_embedding(self, text):
            return [0.0, 0.0, 1.0]

    from vibe_cognition.embeddings import ChromaDBStorage
    chroma = ChromaDBStorage(
        persist_directory=tmp_path / "chromadb", embedding_model="m", embedding_dimensions=3,
    )
    node = CognitionNode(
        id="d1", type=CognitionNodeType.DECISION, summary="s", detail="b",
        context=[], references=[], timestamp=datetime.now(UTC).isoformat(), author="t",
    )
    _embed_entity_node(chroma, _RecordingGen(), node)  # type: ignore[arg-type]
    assert "status:" not in seen["text"]


# ── prime injection ─────────────────────────────────────────────────────────────


def test_prime_injects_open_tasks_sorted(tmp_path):
    """generate_prime: open tasks → '## Open Tasks', critical before low."""
    storage = CognitionStorage(tmp_path / ".cognition")
    _task_node(storage, "low1", "low task", severity="low")
    _task_node(storage, "crit1", "critical task", severity="critical")
    out = generate_prime(storage)
    assert "## Open Tasks" in out
    assert out.index("critical task") < out.index("low task")


def test_prime_excludes_done_and_cancelled(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    _task_node(storage, "o1", "open one", status="open")
    _task_node(storage, "d1", "done one", status="done")
    _task_node(storage, "x1", "cancelled one", status="cancelled")
    out = _format_tasks(storage, cap=5)
    assert "open one" in out
    assert "done one" not in out
    assert "cancelled one" not in out


def test_prime_caps_with_overflow_line(tmp_path):
    """More than the top-N cap → exactly N shown + a single overflow line."""
    storage = CognitionStorage(tmp_path / ".cognition")
    for i in range(12):
        _task_node(storage, f"t{i}", f"task {i}", severity="normal")
    out = _format_tasks(storage, cap=10)
    assert "+2 more open tasks" in out
    assert "cognition_list_tasks" in out


def test_prime_no_tasks_section_when_none(tmp_path):
    storage = CognitionStorage(tmp_path / ".cognition")
    assert _format_tasks(storage, cap=5) == ""


# ── get_status type coverage ────────────────────────────────────────────────────


def test_statistics_include_task_type(build_lc, make_ctx, mock_mcp, tmp_path):
    """get_statistics enumerates the enum, so the new task type is counted for free."""
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path)
    ctx = make_ctx(lc)
    mock_mcp.tools["cognition_add_task"](ctx, summary="t", detail="d", context="c")
    stats = lc["cognition_storage"].get_statistics()
    assert "task" in stats
    assert stats["task"] == 1
