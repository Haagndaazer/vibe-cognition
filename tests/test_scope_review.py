"""Curation scope review (docs/wp-scope-review-plan.md)."""

import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import append_legacy
from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.models import CognitionEdge, CognitionEdgeType
from vibe_cognition.cognition.prime import PrimeConfig, generate_prime
from vibe_cognition.tools.cognition_tools import _flag_constraint_scope, register_cognition_tools

ALICE = ("Alice", "alice@corp.example")
BOB = ("Bob", "bob@corp.example")
_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, mock_mcp, build_lc, make_ctx, graph_identity):
    register_cognition_tools(mock_mcp)
    lc = build_lc(tmp_path, embeddings_ready=True)
    ctx = make_ctx(lc)
    graph_identity.acting_as(*ALICE)

    class Env:
        storage: CognitionStorage = lc["cognition_storage"]

        def tool(self, name: str, **kwargs: Any) -> dict[str, Any]:
            return mock_mcp.tools[name](ctx, **kwargs)

        def token(self) -> str:
            return self.tool("cognition_begin_curation")["curation_token"]

        def constraint(self, summary: str, scope: str = "personal") -> str:
            return self.tool(
                "cognition_record", node_type="constraint", summary=summary, detail="d",
                context="c", author="x", scope=scope,
            )["id"]

        def flag(self, node_id: str, suggested: str = "project", reason: str = "names an API limit",
                 token: str | None = "auto") -> dict[str, Any]:
            return self.tool(
                "cognition_flag_constraint_scope", node_id=node_id, suggested_scope=suggested,
                reason=reason, curation_token=self.token() if token == "auto" else token,
            )

        def act_as(self, who: tuple[str, str]) -> None:
            graph_identity.acting_as(*who)

    return Env()


def test_refused_without_a_curation_token(env):
    nid = env.constraint("Roads API needs a license")
    result = env.flag(nid, token=None)
    assert "curation token" in result["error"]
    assert "scope_review" not in env.storage.get_node(nid)


def test_flag_is_its_own_attribute_and_leaves_metadata_alone(env):
    nid = env.constraint("Roads API needs a license")
    before = env.storage.get_node(nid)["metadata"]
    result = env.flag(nid)
    assert result == {
        "flagged": True, "node_id": nid, "current_scope": "personal",
        "suggested_scope": "project", "owner_is_you": True,
    }
    node = env.storage.get_node(nid)
    assert node["metadata"] == before
    assert node["scope_review"]["suggested"] == "project"
    assert node["scope_review"]["reason"] == "names an API limit"
    assert node["scope_review"]["curation_session"].startswith("cur-")


@pytest.mark.parametrize("case", ["decision", "same_scope", "bad_scope", "blank_reason", "missing"])
def test_refusals_write_nothing(env, case):
    if case == "decision":
        nid = env.tool("cognition_record", node_type="decision", summary="s", detail="d",
                       context="c", author="x")["id"]
        result = env.flag(nid)
    elif case == "same_scope":
        nid = env.constraint("already project", scope="project")
        result = env.flag(nid, suggested="project")
    elif case == "bad_scope":
        nid = env.constraint("x")
        result = env.flag(nid, suggested="team")
    elif case == "blank_reason":
        nid = env.constraint("x")
        result = env.flag(nid, reason="   ")
    else:
        nid = "nope"
        result = env.flag(nid)
    assert "error" in result
    node = env.storage.get_node(nid)
    assert node is None or "scope_review" not in node


def test_unowned_and_superseded_constraints_are_refused(env):
    append_legacy(env.storage.cognition_dir, "add_node", {
        "id": "legacy-c", "type": "constraint", "summary": "old", "detail": "d", "context": [],
        "references": [], "severity": None, "timestamp": "2026-01-01T00:00:00+00:00",
        "author": "a", "metadata": {},
    })
    assert "no recorded owner" in env.flag("legacy-c", suggested="personal")["error"]

    old = env.constraint("old rule", scope="project")
    new = env.constraint("new rule", scope="project")
    env.storage.add_edge(CognitionEdge(
        from_id=new, to_id=old, edge_type=CognitionEdgeType.SUPERSEDES,
        timestamp="2026-09-17T00:00:00+00:00",
    ))
    assert "superseded" in env.flag(old, suggested="personal")["error"]


def test_a_teammates_personal_constraint_cannot_be_flagged(env):
    nid = env.constraint("Alice never edits prefabs")
    env.act_as(BOB)
    assert "error" in env.flag(nid, suggested="project")
    env.act_as(ALICE)
    assert "scope_review" not in env.storage.get_node(nid)


def test_the_suspect_cap_and_batch_size_stay_pinned():
    step = (_REPO / "agents" / "curate-orchestrator.md").read_text(encoding="utf-8")
    scope = step.split("## Step 3b: Scope Review")[1].split("## Step 4")[0]
    assert "batches of 5-10" in scope
    assert "10 or more have been examined" in scope and "40%" in scope
    assert "{{model" not in scope
    assert "curate-scope-analyzer" in scope and "OTHER than" in scope
    conflict = step.split("## Step 3: Conflict Pass")[1].split("## Step 3b")[0]
    assert "15 or more" in conflict and "20%" in conflict


def test_owner_ruling_clears_the_flag(env):
    keep = env.constraint("keep me personal")
    env.flag(keep)
    ruled = env.tool("cognition_update_node", node_id=keep, scope="personal")
    assert ruled["metadata"]["scope"] == "personal"
    assert ruled["scope_review"] is None

    move = env.constraint("move me")
    env.flag(move)
    ruled = env.tool("cognition_update_node", node_id=move, scope="project")
    assert ruled["metadata"]["scope"] == "project"
    assert ruled["scope_review"] is None


def test_only_the_owner_sees_or_clears_a_flag(env):
    nid = env.constraint("Alice reviews all renderer changes", scope="project")
    env.act_as(BOB)
    assert env.flag(nid, suggested="personal", reason="one person's authorship")["owner_is_you"] is False

    assert "scope_review" not in env.storage.get_node(nid)
    assert all("scope_review" not in n for n in env.storage.snapshot()["nodes"] if n["id"] == nid)
    assert all("scope_review" not in n for n in env.storage.get_all_nodes() if n["id"] == nid)
    assert "error" in env.tool("cognition_update_node", node_id=nid, scope="project")
    assert "Constraints to Review" not in generate_prime(env.storage, PrimeConfig(), current_email=BOB[1])

    env.act_as(ALICE)
    assert env.storage.get_node(nid)["scope_review"]["suggested"] == "personal"
    assert CognitionStorage(env.storage.cognition_dir, show_all_scopes=True).get_node(nid)["scope_review"]


def test_prime_lists_the_owners_flags_until_ruled(env):
    nid = env.constraint("Google Roads API needs an Asset Tracking license")
    env.flag(nid, reason="a license limit any teammate would hit")
    out = generate_prime(env.storage, PrimeConfig(), current_email=ALICE[1])
    section = out.split("## Constraints to Review")[1].split("\n## ")[0]
    assert "Do not stop work" in section
    assert f"- {nid} [personal -> project]" in section
    assert "a license limit any teammate would hit" in section

    env.tool("cognition_update_node", node_id=nid, scope="project")
    assert "Constraints to Review" not in generate_prime(env.storage, PrimeConfig(), current_email=ALICE[1])


def test_a_flag_from_a_stale_checkout_never_reverts_a_scope_change(tmp_path, graph_identity):
    """Bob's curator flags Alice's constraint before her scope change reaches his
    checkout; once the journals meet, her change and his flag must both stand."""
    alice_dir = tmp_path / "alice" / ".cognition"
    bob_dir = tmp_path / "bob" / ".cognition"
    alice_dir.mkdir(parents=True)

    graph_identity.acting_as(*ALICE)
    alice = CognitionStorage(alice_dir)
    from vibe_cognition.cognition.models import CognitionNode, CognitionNodeType
    alice.add_node(CognitionNode(
        id="c1", type=CognitionNodeType.CONSTRAINT, summary="Alice authors prefabs", detail="d",
        context=[], references=[], timestamp="2026-09-17T00:00:00+00:00", author="Alice",
        metadata={"recorded_by": {"name": "Alice", "email": ALICE[1]}, "scope": "project"},
    ))
    shutil.copytree(alice_dir / "journal", bob_dir / "journal")

    alice.update_node("c1", metadata={**alice.get_node("c1")["metadata"], "scope": "personal"})

    graph_identity.acting_as(*BOB)
    bob = CognitionStorage(bob_dir)
    assert _flag_constraint_scope(bob, "c1", "personal", "one person's authorship", "cur-x")["flagged"]

    for shard in (alice_dir / "journal").glob("*.jsonl"):
        shutil.copyfile(shard, bob_dir / "journal" / shard.name)
    merged = CognitionStorage(bob_dir, show_all_scopes=True).get_node("c1")
    assert merged["metadata"]["scope"] == "personal"
    assert merged["scope_review"]["suggested"] == "personal"


def test_orchestrator_pins_the_scope_analyzer_to_the_mid_model():
    orchestrator = (_REPO / "agents" / "curate-orchestrator.md").read_text(encoding="utf-8")
    assert "vibe-cognition:curate-scope-analyzer" in orchestrator
    assert "cognition_flag_constraint_scope" in orchestrator.split("\n---", 1)[0]
    step = orchestrator.split("## Step 3b: Scope Review")[1].split("## Step 4")[0]
    assert 'model: "sonnet"' in step
    analyzer = (_REPO / "agents" / "curate-scope-analyzer.md").read_text(encoding="utf-8")
    assert '"quote"' in analyzer and "model: sonnet" in analyzer


def test_analyzers_are_told_never_to_use_teammate_comms():
    """Codex gate 2026-09-17: an analyzer registered itself and mailed its proposals
    to four addresses, one outside the curation run."""
    for name in ("curate-edge-analyzer", "curate-conflict-analyzer",
                 "curate-cluster-analyzer", "curate-scope-analyzer"):
        text = (_REPO / "agents" / f"{name}.md").read_text(encoding="utf-8")
        assert "Never register with teammate-comms" in text, name
        assert "never message another agent" in text, name
        assert "task's final answer" in text, name


def test_orchestrator_handles_the_codex_thread_limit_refusal():
    """Codex-only prose: check the Codex render, where the harness block survives."""
    text = (_REPO / "adapters" / "codex" / "skills" / "vibe-curate" / "references"
            / "curate-orchestrator.md").read_text(encoding="utf-8")
    assert "agent thread limit reached" in text
    assert "retry that spawn ONCE" in text
