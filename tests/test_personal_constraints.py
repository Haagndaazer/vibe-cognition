"""Personal vs project constraints (docs/wp-personal-constraints-plan.md)."""

from typing import Any

import pytest

from tests.conftest import append_legacy
from vibe_cognition.cognition import CognitionStorage
from vibe_cognition.cognition.identity import identity_write_path
from vibe_cognition.cognition.models import CognitionEdge, CognitionEdgeType
from vibe_cognition.cognition.prime import PrimeConfig, generate_prime
from vibe_cognition.tools.cognition_tools import register_cognition_tools

ALICE = ("Alice", "alice@corp.example")
BOB = ("Bob", "bob@corp.example")


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

        def record(self, summary: str, node_type: str = "constraint", **kwargs: Any) -> dict[str, Any]:
            return self.tool(
                "cognition_record", node_type=node_type, summary=summary, detail="d",
                context="c", author="x", **kwargs,
            )

        def act_as(self, who: tuple[str, str]) -> None:
            graph_identity.acting_as(*who)

    return Env()


def test_constraint_defaults_to_personal(env):
    result = env.record("I author all prefab changes myself")
    assert result["scope"] == "personal"
    assert env.storage.get_node(result["id"])["metadata"]["scope"] == "personal"


def test_explicit_project_scope_is_stored(env):
    result = env.record("URP and HDRP cannot coexist", scope="project")
    assert result["scope"] == "project"
    assert env.storage.get_node(result["id"])["metadata"]["scope"] == "project"


def test_scope_rejected_on_other_types_and_bad_values(env):
    assert "error" in env.record("a decision", node_type="decision", scope="personal")
    assert "error" in env.record("bad scope", scope="team")
    assert "scope" not in env.record("a decision", node_type="decision")


def test_teammate_cannot_find_personal_constraint(env):
    personal = env.record("Never touch my prefabs", references="issue:NS-1")["id"]
    project = env.record("Google Roads API needs a license", scope="project", references="issue:NS-1")["id"]
    episode = env.record("prefab work", node_type="episode", references="issue:NS-1")["id"]

    env.act_as(BOB)
    s = env.storage
    assert s.get_node(personal) is None
    assert not s.has_node(personal)
    assert s.get_node(project) is not None
    ids = {n["id"] for n in s.get_all_nodes()}
    assert personal not in ids and project in ids
    assert personal not in s.find_nodes_by_ref("issue:NS-1")
    assert personal not in {sid for sid, _ in s.get_predecessors(episode)}
    assert personal not in {n["id"] for n in s.get_uncurated_nodes()}
    assert personal not in {n["id"] for n in s.snapshot()["nodes"]}
    assert all(personal not in (u, v) for u, v, _, _ in s.snapshot()["edges"])

    search = env.tool("cognition_search", query="Never touch my prefabs")
    assert personal not in {r["id"] for r in search["results"]}
    assert "error" in env.tool("cognition_get_node", node_id=personal)
    neighbors = env.tool("cognition_get_neighbors", node_id=episode)
    assert personal not in str(neighbors)
    assert "error" in env.tool("cognition_update_node", node_id=personal, summary="hijack")
    assert not s.add_edge(CognitionEdge(
        from_id=episode, to_id=personal, edge_type=CognitionEdgeType.RELATES_TO,
        timestamp="2026-09-15T00:00:00+00:00",
    ))
    assert not s.remove_node(personal)

    env.act_as(ALICE)
    assert s.get_node(personal) is not None
    assert personal in {sid for sid, _ in s.get_predecessors(episode)}


def test_statistics_count_only_visible_nodes(env):
    env.record("mine only")
    env.record("everyone", scope="project")
    alice_nodes = env.storage.get_statistics()["constraint"]
    env.act_as(BOB)
    assert env.storage.get_statistics()["constraint"] == alice_nodes - 1


def test_search_hit_carries_scope(env):
    env.record("Always ask before committing")
    hits = env.tool("cognition_search", query="Always ask before committing")["results"]
    assert hits[0]["scope"] == "personal"


def test_unconfirmed_checkout_sees_no_personal_constraints(env):
    personal = env.record("private preference")["id"]
    identity_write_path(env.storage.cognition_dir).unlink()
    assert env.storage.get_node(personal) is None


def test_legacy_constraint_stays_project_and_cannot_become_personal(env):
    append_legacy(env.storage.cognition_dir, "add_node", {
        "id": "legacy-c", "type": "constraint", "summary": "old rule", "detail": "d",
        "context": [], "references": [], "severity": None,
        "timestamp": "2026-01-01T00:00:00+00:00", "author": "someone", "metadata": {},
    })
    env.act_as(BOB)
    assert env.storage.get_node("legacy-c") is not None
    result = env.tool("cognition_update_node", node_id="legacy-c", scope="personal")
    assert "error" in result
    assert env.storage.get_node("legacy-c").get("metadata", {}).get("scope") is None


def test_only_owner_changes_scope(env):
    rule = env.record("shared rule", scope="project")["id"]
    env.act_as(BOB)
    assert "error" in env.tool("cognition_update_node", node_id=rule, scope="personal")

    env.act_as(ALICE)
    changed = env.tool("cognition_update_node", node_id=rule, scope="personal")
    assert changed["metadata"]["scope"] == "personal"
    env.act_as(BOB)
    assert env.storage.get_node(rule) is None

    env.act_as(ALICE)
    env.tool("cognition_update_node", node_id=rule, scope="project")
    env.act_as(BOB)
    assert env.storage.get_node(rule) is not None


def test_scope_survives_replay_in_a_fresh_process(env):
    personal = env.record("replayed preference")["id"]
    fresh = CognitionStorage(env.storage.cognition_dir)
    assert fresh.get_node(personal)["metadata"]["scope"] == "personal"
    env.act_as(BOB)
    assert CognitionStorage(env.storage.cognition_dir).get_node(personal) is None


def test_prime_shows_personal_constraints_only_to_owner(env):
    env.record("Alice prefers tabs", severity="high")
    env.record("Build must pass on CI", scope="project", severity="high")
    alice = generate_prime(env.storage, PrimeConfig(), current_email=ALICE[1])
    assert "## Your Personal Constraints\n- [constraint] Alice prefers tabs" in alice
    active = alice.split("## Active Constraints")[1].split("##")[0]
    assert "Build must pass on CI" in active and "Alice prefers tabs" not in active

    env.act_as(BOB)
    bob = generate_prime(env.storage, PrimeConfig(), current_email=BOB[1])
    assert "Alice prefers tabs" not in bob
    assert "Your Personal Constraints" not in bob
    assert "Build must pass on CI" in bob


def test_show_all_scopes_is_for_maintenance_only(env):
    personal = env.record("remap must still find me")["id"]
    env.act_as(BOB)
    assert CognitionStorage(env.storage.cognition_dir).get_node(personal) is None
    assert CognitionStorage(env.storage.cognition_dir, show_all_scopes=True).get_node(personal) is not None
