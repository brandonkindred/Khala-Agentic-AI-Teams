"""Unit tests for the shared manifest build/clone/project helpers."""

from __future__ import annotations

import pytest

from agent_platform.registry.models import AgentManifest, AgentStateSpec, CognitionSpec, SourceInfo
from shared.manifests import (
    AGENT_ANATOMY_REF,
    GENERATED_AGENT_ENTRYPOINT,
    GENERATED_AGENT_INPUT_REF,
    GENERATED_AGENT_OUTPUT_REF,
    build_manifest,
    clone_manifest,
    default_cognition_block,
    io_schema,
    project_manifest,
)


def _source() -> SourceInfo:
    return SourceInfo(entrypoint="pkg.module:run", anatomy_ref="docs/ANATOMY.md")


def _manifest(**overrides) -> AgentManifest:
    base = dict(
        id="team.agent-abc123",
        team="team",
        name="Agent",
        summary="Does things",
        source=_source(),
    )
    base.update(overrides)
    return AgentManifest(**base)


# -- io_schema -----------------------------------------------------------------


def test_io_schema_prefers_inline_when_present() -> None:
    schema = io_schema(
        {"type": "object"},
        schema_ref="pkg.module:Input",
        ref_description="ref",
        inline_description="inline",
    )
    assert schema.inline_schema == {"type": "object"}
    assert schema.schema_ref is None
    assert schema.description == "inline"


def test_io_schema_round_trips_empty_inline_schema() -> None:
    schema = io_schema(
        {},
        schema_ref="pkg.module:Input",
        ref_description="ref",
        inline_description="inline",
    )
    assert schema.inline_schema == {}
    assert schema.schema_ref is None


def test_io_schema_falls_back_to_ref_when_inline_omitted() -> None:
    schema = io_schema(
        None,
        schema_ref="pkg.module:Input",
        ref_description="ref",
        inline_description="inline",
    )
    assert schema.schema_ref == "pkg.module:Input"
    assert schema.inline_schema is None
    assert schema.description == "ref"


def test_io_schema_rejects_empty_schema_ref() -> None:
    with pytest.raises(ValueError):
        io_schema(None, schema_ref="", ref_description="ref", inline_description="inline")


# -- build_manifest --------------------------------------------------------------


def test_build_manifest_returns_validated_manifest_with_defaults() -> None:
    manifest = build_manifest(
        id="team.agent-abc123",
        team="team",
        name="Agent",
        summary="Does things",
        source=_source(),
    )
    assert manifest.id == "team.agent-abc123"
    assert manifest.team == "team"
    assert manifest.name == "Agent"
    assert manifest.summary == "Does things"
    assert manifest.tags == []
    assert manifest.states == []
    assert manifest.source.entrypoint == "pkg.module:run"


def test_build_manifest_carries_all_supplied_fields() -> None:
    cognition = CognitionSpec(tools=["search"])
    states = [AgentStateSpec(key="executing", label="Executing", system_prompt="go")]
    manifest = build_manifest(
        id="team.agent-abc123",
        team="team",
        name="Agent",
        summary="Does things",
        source=_source(),
        description="A description",
        tags=["a", "b"],
        cognition=cognition,
        states=states,
    )
    assert manifest.description == "A description"
    assert manifest.tags == ["a", "b"]
    assert manifest.cognition is not None
    assert manifest.cognition.tools == ["search"]
    assert manifest.states == states


@pytest.mark.parametrize(
    "field",
    ["id", "team", "name", "summary"],
)
def test_build_manifest_rejects_empty_required_field(field: str) -> None:
    kwargs = dict(id="team.agent", team="team", name="Agent", summary="Summary", source=_source())
    kwargs[field] = ""
    with pytest.raises(ValueError):
        build_manifest(**kwargs)


def test_build_manifest_result_is_json_safe_and_equal_to_a_direct_construction() -> None:
    manifest = build_manifest(
        id="team.agent-abc123",
        team="team",
        name="Agent",
        summary="Does things",
        source=_source(),
    )
    assert manifest == AgentManifest.model_validate(manifest.model_dump(mode="json"))


def test_build_manifest_stamps_required_generated_fields() -> None:
    manifest = build_manifest(
        id="agentic_team_provisioning.example-agent",
        team="agentic_team_provisioning",
        name="Triage Agent",
        summary="Classifies tickets",
        source=SourceInfo(entrypoint=GENERATED_AGENT_ENTRYPOINT, anatomy_ref=AGENT_ANATOMY_REF),
        cognition=default_cognition_block(),
        inputs=io_schema(
            None,
            schema_ref=GENERATED_AGENT_INPUT_REF,
            ref_description="in",
            inline_description="i",
        ),
        outputs=io_schema(
            None,
            schema_ref=GENERATED_AGENT_OUTPUT_REF,
            ref_description="out",
            inline_description="i",
        ),
    )
    assert manifest.team == "agentic_team_provisioning"
    assert manifest.source.entrypoint == (
        "agent_team_studio.agentic_team_provisioning.runtime.agent_builder:invoke_generated_agent"
    )
    assert manifest.source.anatomy_ref == ("backend/agents/agent_team_studio/agent_provisioning_team/AGENT_ANATOMY.md")
    assert manifest.inputs is not None
    assert manifest.inputs.schema_ref == (
        "agent_team_studio.agentic_team_provisioning.models:GeneratedAgentInvokeInput"
    )
    assert manifest.outputs is not None
    assert manifest.outputs.schema_ref == (
        "agent_team_studio.agentic_team_provisioning.models:GeneratedAgentInvokeOutput"
    )
    assert manifest.cognition is not None
    assert manifest.cognition.rule_packs == ["default_guardrails"]
    assert manifest.cognition.memory.retention_days_events == 90
    assert manifest.cognition.tools == []
    assert manifest.cognition.requires_idempotency_key is False
    assert manifest.cognition.knowledge_graph.enabled is True


def test_build_manifest_stamps_required_studio_fields() -> None:
    cognition = default_cognition_block().model_copy(update={"tools": ["web.search"]})
    manifest = build_manifest(
        id="agent_studio.planner",
        team="agent_studio",
        name="Planner",
        summary="Plans things",
        source=SourceInfo(entrypoint=GENERATED_AGENT_ENTRYPOINT, anatomy_ref=AGENT_ANATOMY_REF),
        cognition=cognition,
        inputs=io_schema(
            None,
            schema_ref=GENERATED_AGENT_INPUT_REF,
            ref_description="in",
            inline_description="i",
        ),
        outputs=io_schema(
            None,
            schema_ref=GENERATED_AGENT_OUTPUT_REF,
            ref_description="out",
            inline_description="i",
        ),
    )
    assert manifest.team == "agent_studio"
    assert manifest.source.entrypoint == (
        "agent_team_studio.agentic_team_provisioning.runtime.agent_builder:invoke_generated_agent"
    )
    assert manifest.source.anatomy_ref == ("backend/agents/agent_team_studio/agent_provisioning_team/AGENT_ANATOMY.md")
    assert manifest.cognition is not None
    assert manifest.cognition.tools == ["web.search"]
    assert manifest.cognition.rule_packs == ["default_guardrails"]
    assert manifest.cognition.memory.retention_days_events == 90
    assert manifest.cognition.requires_idempotency_key is False
    assert manifest.cognition.knowledge_graph.enabled is True


# -- clone_manifest --------------------------------------------------------------


def test_clone_manifest_returns_new_object_with_overrides_applied() -> None:
    original = _manifest(tags=["a"])
    cloned = clone_manifest(original, name="Agent Two", tags=["a", "b"])
    assert cloned is not original
    assert cloned.name == "Agent Two"
    assert cloned.tags == ["a", "b"]


def test_clone_manifest_does_not_mutate_source() -> None:
    original = _manifest(tags=["a"])
    clone_manifest(original, name="Agent Two")
    assert original.name == "Agent"
    assert original.tags == ["a"]


def test_clone_manifest_with_no_overrides_is_equal_to_source() -> None:
    original = _manifest()
    cloned = clone_manifest(original)
    assert cloned == original
    assert cloned is not original


# -- project_manifest --------------------------------------------------------------


def test_project_manifest_extracts_author_facing_fields() -> None:
    manifest = _manifest(
        description="desc",
        tags=["real", "generated"],
        cognition=CognitionSpec(tools=["search"]),
        states=[AgentStateSpec(key="executing", label="Executing", system_prompt="go")],
    )
    projected = project_manifest(manifest)
    assert projected == {
        "name": "Agent",
        "summary": "Does things",
        "description": "desc",
        "tags": ["real", "generated"],
        "tools": ["search"],
        "input_schema": None,
        "output_schema": None,
        "states": [{"key": "executing", "label": "Executing", "system_prompt": "go"}],
    }


def test_project_manifest_strips_marker_tags() -> None:
    manifest = _manifest(tags=["real", "generated", "studio"])
    projected = project_manifest(manifest, strip_tags=frozenset({"generated", "studio"}))
    assert projected["tags"] == ["real"]


def test_project_manifest_defaults_tools_to_empty_list_when_cognition_absent() -> None:
    manifest = _manifest(cognition=None)
    assert project_manifest(manifest)["tools"] == []


def test_project_manifest_extracts_inline_schemas() -> None:
    manifest = _manifest(
        inputs=io_schema({"type": "object"}, schema_ref="x:Y", ref_description="r", inline_description="i"),
        outputs=io_schema({}, schema_ref="x:Z", ref_description="r", inline_description="i"),
    )
    projected = project_manifest(manifest)
    assert projected["input_schema"] == {"type": "object"}
    assert projected["output_schema"] == {}


def test_project_manifest_does_not_mutate_source() -> None:
    manifest = _manifest(tags=["real"])
    project_manifest(manifest, strip_tags=frozenset({"real"}))
    assert manifest.tags == ["real"]
