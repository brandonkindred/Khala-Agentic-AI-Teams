"""Tests for the generated-agent manifest builder (cognition core stamping)."""

from __future__ import annotations

import pytest

from agent_registry.models import AgentManifest, CognitionSpec
from agent_team_studio.agentic_team_provisioning.manifest_generation import (
    build_agent_manifest,
    default_cognition_block,
    manifest_agent_id,
    register_team_manifests,
)
from agent_team_studio.agentic_team_provisioning.models import AgenticTeamAgent


def _thin(team_id: str, agent_name: str) -> AgenticTeamAgent:
    return AgenticTeamAgent(
        agent_name=agent_name,
        manifest_id=manifest_agent_id(team_id, agent_name),
    )


def test_default_cognition_block_is_batteries_included():
    block = default_cognition_block()
    assert isinstance(block, CognitionSpec)
    assert block.rule_packs == ["default_guardrails"]
    assert block.memory.retention_days_events == 90
    assert block.tools == []
    assert block.requires_idempotency_key is False
    assert block.knowledge_graph.enabled is True
    assert block.knowledge_graph.ingest_events is True
    assert block.knowledge_graph.ingest_summaries is True
    assert block.knowledge_graph.ground_rule_proposals is True


def test_build_agent_manifest_validates_and_stamps_cognition():
    manifest = build_agent_manifest("team-uuid-123", "Triage Agent", summary="Classifies tickets by urgency")

    assert isinstance(manifest, AgentManifest)
    assert manifest.team == "agentic_team_provisioning"
    assert manifest.name == "Triage Agent"
    assert manifest.summary == "Classifies tickets by urgency"
    assert manifest.source.entrypoint.endswith("agent_builder:invoke_generated_agent")
    assert manifest.source.anatomy_ref is not None
    assert manifest.inputs is not None
    assert (
        manifest.inputs.schema_ref
        == "agent_team_studio.agentic_team_provisioning.models:GeneratedAgentInvokeInput"
    )
    assert manifest.outputs is not None
    assert manifest.cognition is not None
    assert manifest.cognition.rule_packs == ["default_guardrails"]

    # Round-trips cleanly through the registry model (proves it is fully valid).
    revalidated = AgentManifest.model_validate(manifest.model_dump(mode="json"))
    assert revalidated.cognition is not None
    assert revalidated.cognition.rule_packs == ["default_guardrails"]


def test_build_agent_manifest_summary_falls_back_without_role():
    manifest = build_agent_manifest("t", "Nameless", summary="")
    assert manifest.summary == "Generated agent Nameless"


def test_build_agent_manifest_includes_skill_tags():
    manifest = build_agent_manifest(
        "t", "Writer", summary="Writes", skill_tags=["seo", " ", "seo", "copy"]
    )
    assert manifest.tags[:2] == ["generated", "agentic_team_provisioning"]
    assert "seo" in manifest.tags
    assert "copy" in manifest.tags
    assert manifest.tags.count("seo") == 1


def test_build_agent_manifest_rejects_empty_team_id():
    # Explicit ValueError (not assert) so the precondition survives ``python -O``.
    with pytest.raises(ValueError, match="team_id must be non-empty"):
        build_agent_manifest("", "Triage")


def test_build_agent_manifest_rejects_empty_agent_name():
    with pytest.raises(ValueError, match="agent_name must be non-empty"):
        build_agent_manifest("team-1", "")


def test_register_team_manifests_rejects_empty_team_id():
    with pytest.raises(ValueError, match="team_id must be non-empty"):
        register_team_manifests("", [])


def test_build_agent_manifest_id_stable_and_unique():
    id_a1 = build_agent_manifest("team-1", "Router Agent", summary="r").id
    id_a2 = build_agent_manifest("team-1", "Router Agent", summary="r").id
    id_b = build_agent_manifest("team-1", "Resolution Agent", summary="r").id

    assert id_a1 == id_a2  # stable for the same (team_id, agent_name)
    assert id_a1 != id_b  # distinct agents → distinct ids
    assert id_a1.startswith("agentic_team_provisioning.")


def test_manifest_id_disambiguates_normalized_slug_clashes():
    # Distinct roster names that normalize to the same slug must stay distinct.
    id_1 = build_agent_manifest("t", "QA Agent", summary="r").id
    id_2 = build_agent_manifest("t", "qa-agent", summary="r").id
    assert id_1 != id_2
    # The id helper is the single source of truth used by the builder.
    assert id_1 == manifest_agent_id("t", "QA Agent")
    # Same long prefix beyond 40 chars also stays distinct.
    long_a = "X" * 50 + "alpha"
    long_b = "X" * 50 + "beta"
    assert manifest_agent_id("t", long_a) != manifest_agent_id("t", long_b)


def test_manifest_schema_refs_resolve_to_real_models():
    # The manifest's inputs/outputs schema_refs must point at importable Pydantic
    # models so the registry can resolve them lazily.
    import importlib

    from pydantic import BaseModel

    manifest = build_agent_manifest("t", "A", summary="r")
    for ref in (manifest.inputs.schema_ref, manifest.outputs.schema_ref):
        module_path, cls_name = ref.split(":")
        model = getattr(importlib.import_module(module_path), cls_name)
        assert issubclass(model, BaseModel)


def test_free_text_tools_are_not_mapped_into_cognition():
    manifest = build_agent_manifest("t", "Tooled", summary="r")
    assert manifest.cognition is not None
    assert manifest.cognition.tools == []


def test_register_team_manifests_installs_into_registry(monkeypatch: pytest.MonkeyPatch):
    import agent_registry
    from agent_registry.loader import AgentRegistry

    reg = AgentRegistry([], {})
    monkeypatch.setattr(agent_registry, "get_registry", lambda: reg)

    team_id = "team-1"
    agents = [_thin(team_id, "A"), _thin(team_id, "B")]
    result = register_team_manifests(
        team_id,
        agents,
        summaries={"A": "r1", "B": "r2"},
    )

    assert result.registered is True
    assert len(result.manifests) == 2
    summaries = {m.name: m.summary for m in result.manifests}
    assert summaries == {"A": "r1", "B": "r2"}
    for m in result.manifests:
        assert reg.get(m.id) is m
        assert reg.get(m.id).cognition.rule_packs == ["default_guardrails"]


def test_register_team_manifests_uses_existing_registry_summary(monkeypatch: pytest.MonkeyPatch):
    import agent_registry
    from agent_registry.loader import AgentRegistry

    reg = AgentRegistry([], {})
    monkeypatch.setattr(agent_registry, "get_registry", lambda: reg)

    team_id = "team-1"
    prior = build_agent_manifest(team_id, "A", summary="kept summary")
    reg.register(prior)

    result = register_team_manifests(team_id, [_thin(team_id, "A")])

    assert result.manifests[0].summary == "kept summary"


def test_register_team_manifests_preserves_existing_skill_tags(monkeypatch: pytest.MonkeyPatch):
    """Omitting skill_tags (startup path) must not wipe previously stamped skill tags."""
    import agent_registry
    from agent_registry.loader import AgentRegistry

    reg = AgentRegistry([], {})
    monkeypatch.setattr(agent_registry, "get_registry", lambda: reg)

    team_id = "team-1"
    prior = build_agent_manifest(
        team_id, "A", summary="kept summary", skill_tags=["seo", "copy"]
    )
    reg.register(prior)

    result = register_team_manifests(team_id, [_thin(team_id, "A")])

    assert result.manifests[0].summary == "kept summary"
    assert "seo" in result.manifests[0].tags
    assert "copy" in result.manifests[0].tags
    assert reg.get(result.manifests[0].id).tags == result.manifests[0].tags


def test_register_team_manifests_explicit_skill_tags_replace(monkeypatch: pytest.MonkeyPatch):
    """When skill_tags is provided for an agent, those values replace prior skill tags."""
    import agent_registry
    from agent_registry.loader import AgentRegistry

    reg = AgentRegistry([], {})
    monkeypatch.setattr(agent_registry, "get_registry", lambda: reg)

    team_id = "team-1"
    prior = build_agent_manifest(team_id, "A", summary="s", skill_tags=["old-skill"])
    reg.register(prior)

    result = register_team_manifests(
        team_id, [_thin(team_id, "A")], skill_tags={"A": ["new-skill"]}
    )

    assert "new-skill" in result.manifests[0].tags
    assert "old-skill" not in result.manifests[0].tags


def test_register_team_manifests_replaces_stale_roster(monkeypatch: pytest.MonkeyPatch):
    import agent_registry
    from agent_registry.loader import AgentRegistry

    reg = AgentRegistry([], {})
    monkeypatch.setattr(agent_registry, "get_registry", lambda: reg)

    team_id = "team-1"
    first = register_team_manifests(
        team_id,
        [_thin(team_id, "A"), _thin(team_id, "B")],
        summaries={"A": "r", "B": "r"},
    )
    stale_id = next(m.id for m in first.manifests if m.name == "B")

    # New roster drops B and renames to C → B's manifest must be unregistered.
    register_team_manifests(
        team_id,
        [_thin(team_id, "A"), _thin(team_id, "C")],
        summaries={"A": "r", "C": "r"},
    )
    assert reg.get(stale_id) is None
    names = {m.name for m in reg.all()}
    assert names == {"A", "C"}


def test_register_team_manifests_scopes_removal_to_team_and_generated(
    monkeypatch: pytest.MonkeyPatch,
):
    import agent_registry
    from agent_registry.loader import AgentRegistry
    from agent_registry.models import AgentManifest, SourceInfo

    reg = AgentRegistry([], {})
    monkeypatch.setattr(agent_registry, "get_registry", lambda: reg)

    # A hand-authored manifest for another team is never touched by team-1's replace.
    other = AgentManifest(
        id="blogging.planner",
        team="blogging",
        name="Planner",
        summary="s",
        source=SourceInfo(entrypoint="m:f"),
    )
    reg.register(other)

    team_id = "team-1"
    register_team_manifests(team_id, [_thin(team_id, "A")], summaries={"A": "r"})
    register_team_manifests(team_id, [_thin(team_id, "A")], summaries={"A": "r"})
    assert reg.get("blogging.planner") is other


def test_team_prefix_is_injective_for_shared_slug():
    from agent_team_studio.agentic_team_provisioning.manifest_generation import team_id_prefix

    # Two team ids that share their first 12 normalized chars must not share a
    # cleanup prefix (the full team id is hashed into it).
    a = "team-aaaaaaaaaa-1"
    b = "team-aaaaaaaaaa-2"
    assert team_id_prefix(a) != team_id_prefix(b)


def test_register_team_manifests_isolates_teams_with_shared_slug(monkeypatch: pytest.MonkeyPatch):
    import agent_registry
    from agent_registry.loader import AgentRegistry

    reg = AgentRegistry([], {})
    monkeypatch.setattr(agent_registry, "get_registry", lambda: reg)

    team_a = "team-aaaaaaaaaa-1"
    team_b = "team-aaaaaaaaaa-2"
    a_result = register_team_manifests(team_a, [_thin(team_a, "A")], summaries={"A": "r"})
    # Registering team B (same 12-char slug) must not unregister team A's manifest.
    register_team_manifests(team_b, [_thin(team_b, "B")], summaries={"B": "r"})
    assert reg.get(a_result.manifests[0].id) is not None


def test_register_team_manifests_propagates_registry_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    import agent_registry

    def _boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(agent_registry, "get_registry", _boom)

    # Registry failures propagate so chat-save can roll back the roster write.
    with pytest.raises(RuntimeError, match="registry unavailable"):
        register_team_manifests("team-1", [_thin("team-1", "A")])


def test_register_team_manifests_propagates_atomic_replace_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    """A failed atomic replace leaves the prior roster manifests untouched."""
    import agent_registry
    from agent_registry.loader import AgentRegistry

    reg = AgentRegistry([], {})
    monkeypatch.setattr(agent_registry, "get_registry", lambda: reg)

    team_id = "team-1"
    first = register_team_manifests(
        team_id,
        [_thin(team_id, "A")],
        summaries={"A": "prior-A"},
    )
    prior_a_id = first.manifests[0].id

    def _boom(upserts, delete_ids, *, conn=None):
        raise RuntimeError("replace boom")

    monkeypatch.setattr(reg, "replace_dynamic_manifests", _boom)

    with pytest.raises(RuntimeError, match="replace boom"):
        register_team_manifests(
            team_id,
            [_thin(team_id, "B"), _thin(team_id, "C")],
            summaries={"B": "renamed", "C": "new"},
        )

    assert reg.get(prior_a_id) is not None
    assert reg.get(prior_a_id).summary == "prior-A"
    assert {m.name for m in reg.all()} == {"A"}
