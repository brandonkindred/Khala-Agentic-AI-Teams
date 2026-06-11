"""Tests for the generated-agent manifest builder (cognition core stamping)."""

from __future__ import annotations

from agent_registry.models import AgentManifest, CognitionSpec
from agentic_team_provisioning.manifest_generation import (
    build_agent_manifest,
    default_cognition_block,
)
from agentic_team_provisioning.models import AgenticTeamAgent


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
    agent = AgenticTeamAgent(
        agent_name="Triage Agent",
        role="Classifies tickets by urgency",
        skills=["text classification"],
        tools=["Ticket API"],
    )
    manifest = build_agent_manifest("team-uuid-123", agent)

    assert isinstance(manifest, AgentManifest)
    assert manifest.team == "agentic_team_provisioning"
    assert manifest.name == "Triage Agent"
    assert manifest.summary == "Classifies tickets by urgency"
    assert manifest.source.entrypoint.endswith("agent_builder:invoke_generated_agent")
    assert manifest.source.anatomy_ref is not None
    assert manifest.inputs is not None
    assert (
        manifest.inputs.schema_ref == "agentic_team_provisioning.models:GeneratedAgentInvokeInput"
    )
    assert manifest.outputs is not None
    assert manifest.cognition is not None
    assert manifest.cognition.rule_packs == ["default_guardrails"]

    # Round-trips cleanly through the registry model (proves it is fully valid).
    revalidated = AgentManifest.model_validate(manifest.model_dump(mode="json"))
    assert revalidated.cognition is not None
    assert revalidated.cognition.rule_packs == ["default_guardrails"]


def test_build_agent_manifest_summary_falls_back_without_role():
    agent = AgenticTeamAgent(agent_name="Nameless", role="")
    manifest = build_agent_manifest("t", agent)
    assert manifest.summary == "Generated agent Nameless"


def test_build_agent_manifest_id_stable_and_unique():
    a = AgenticTeamAgent(agent_name="Router Agent", role="r")
    b = AgenticTeamAgent(agent_name="Resolution Agent", role="r")

    id_a1 = build_agent_manifest("team-1", a).id
    id_a2 = build_agent_manifest("team-1", a).id
    id_b = build_agent_manifest("team-1", b).id

    assert id_a1 == id_a2  # stable for the same (team_id, agent_name)
    assert id_a1 != id_b  # distinct agents → distinct ids
    assert id_a1.startswith("agentic_team_provisioning.")


def test_manifest_schema_refs_resolve_to_real_models():
    # The manifest's inputs/outputs schema_refs must point at importable Pydantic
    # models so the registry can resolve them lazily.
    import importlib

    from pydantic import BaseModel

    agent = AgenticTeamAgent(agent_name="A", role="r")
    manifest = build_agent_manifest("t", agent)
    for ref in (manifest.inputs.schema_ref, manifest.outputs.schema_ref):
        module_path, cls_name = ref.split(":")
        model = getattr(importlib.import_module(module_path), cls_name)
        assert issubclass(model, BaseModel)


def test_free_text_tools_are_not_mapped_into_cognition():
    agent = AgenticTeamAgent(
        agent_name="Tooled", role="r", tools=["Git", "PostgreSQL", "Slack API"]
    )
    manifest = build_agent_manifest("t", agent)
    assert manifest.cognition is not None
    assert manifest.cognition.tools == []
