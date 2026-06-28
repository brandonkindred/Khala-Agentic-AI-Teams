"""Unit tests for :mod:`agent_studio.registration`."""

from __future__ import annotations

import pytest

from agent_registry.models import AgentManifest, AgentStateSpec, CognitionSpec, IOSchema, SourceInfo
from agent_studio.agent_states import STATE_ORDER
from agent_studio.models import AgentDefinition, AgentState
from agent_studio.registration import (
    STUDIO_TEAM,
    build_studio_agent_manifest,
    clone_from_manifest,
    studio_agent_id,
)


def test_studio_agent_id_is_stable_and_slugged() -> None:
    a = studio_agent_id("My Cool Agent")
    b = studio_agent_id("My Cool Agent")
    assert a == b
    assert a.startswith(f"{STUDIO_TEAM}.my-cool-agent-")


def test_studio_agent_id_falls_back_to_agent_slug() -> None:
    # All-symbol name slugs to empty -> "agent".
    assert studio_agent_id("!!!").startswith(f"{STUDIO_TEAM}.agent-")


def test_studio_agent_id_rejects_blank() -> None:
    # Explicit raise (not assert) so it survives ``python -O``.
    with pytest.raises(ValueError):
        studio_agent_id("   ")


def test_build_manifest_reuses_generated_runtime() -> None:
    definition = AgentDefinition(
        name="Planner",
        role="Plans things",
        description="desc",
        tags=["content", "seo"],
        tools=["web.search"],
    )
    manifest = build_studio_agent_manifest(definition)

    assert manifest.team == STUDIO_TEAM
    assert manifest.id == studio_agent_id("Planner")
    assert manifest.name == "Planner"
    assert manifest.summary == "Plans things"
    assert "studio" in manifest.tags and "content" in manifest.tags
    assert "agentic_team_provisioning" in manifest.source.entrypoint
    assert manifest.cognition is not None
    assert manifest.cognition.rule_packs == ["default_guardrails"]
    assert manifest.cognition.tools == ["web.search"]


def test_build_manifest_persists_seeded_states() -> None:
    # The definition's three operating states ride along onto the manifest.
    manifest = build_studio_agent_manifest(AgentDefinition(name="Planner", role="r"))
    assert [s.key for s in manifest.states] == list(STATE_ORDER)
    assert all(s.system_prompt.strip() for s in manifest.states)


def test_build_manifest_persists_edited_state_prompt() -> None:
    definition = AgentDefinition(name="Planner", role="r")
    definition.states[0].system_prompt = "EDITED plan prompt"
    manifest = build_studio_agent_manifest(definition)
    assert manifest.states[0].key == "planning"
    assert manifest.states[0].system_prompt == "EDITED plan prompt"


def test_build_manifest_summary_fallback_when_no_role() -> None:
    manifest = build_studio_agent_manifest(AgentDefinition(name="Solo"))
    assert manifest.summary == "Studio agent Solo"


def test_build_manifest_rejects_blank_name() -> None:
    with pytest.raises(ValueError):
        build_studio_agent_manifest(AgentDefinition(name="  "))


def _manifest(**overrides) -> AgentManifest:
    base = dict(
        id="blogging.planner",
        team="blogging",
        name="Planner",
        summary="Plans blog outlines",
        description="A planner",
        tags=["content", "studio", "generated"],
        cognition=CognitionSpec(rule_packs=["default_guardrails"], tools=["web.search"]),
        inputs=IOSchema(schema_ref="x:In"),
        outputs=IOSchema(schema_ref="x:Out"),
        source=SourceInfo(entrypoint="x:run"),
    )
    base.update(overrides)
    return AgentManifest(**base)


def test_clone_from_manifest_produces_refine_draft() -> None:
    manifest = _manifest()
    draft = clone_from_manifest(manifest)

    assert draft.mode == "refine"
    assert draft.cloned_from == "blogging.planner"
    assert draft.name == "Planner.copy"
    assert draft.role == "Plans blog outlines"
    assert draft.description == "A planner"
    assert draft.tools == ["web.search"]
    # Plumbing tags are stripped; real tags survive.
    assert draft.tags == ["content"]


def test_clone_from_manifest_handles_no_cognition() -> None:
    draft = clone_from_manifest(_manifest(cognition=None, tags=[]))
    assert draft.tools == []
    assert draft.tags == []


def test_clone_round_trips_persisted_states() -> None:
    # A manifest that carries states clones them back verbatim into the refine draft.
    states = [
        AgentStateSpec(key="planning", label="Planning", system_prompt="EDITED plan"),
        AgentStateSpec(key="executing", label="Executing", system_prompt="exec"),
        AgentStateSpec(key="researching", label="Researching", system_prompt="research"),
    ]
    draft = clone_from_manifest(_manifest(states=states))
    assert [s.key for s in draft.states] == ["planning", "executing", "researching"]
    assert draft.states[0].system_prompt == "EDITED plan"
    assert all(isinstance(s, AgentState) for s in draft.states)


def test_clone_filters_unsupported_state_keys_without_raising() -> None:
    # AgentStateSpec.key is a permissive str; a manifest carrying a non-canonical
    # key must not 500 the clone — the unsupported key is dropped and the canonical
    # set is backfilled, while a supported edited key survives.
    states = [
        AgentStateSpec(key="planning", label="Planning", system_prompt="EDIT"),
        AgentStateSpec(key="deploying", label="Deploying", system_prompt="bad"),
    ]
    draft = clone_from_manifest(_manifest(states=states))
    assert [s.key for s in draft.states] == list(STATE_ORDER)
    assert draft.states[0].system_prompt == "EDIT"
    # The dropped key's slot is filled from defaults, not left missing.
    assert all(s.system_prompt.strip() for s in draft.states)


def test_clone_backfills_default_states_for_legacy_manifest() -> None:
    # A pre-feature manifest (no states) still yields the three seeded defaults.
    draft = clone_from_manifest(_manifest())  # _manifest() has no states -> []
    assert [s.key for s in draft.states] == list(STATE_ORDER)
    assert all(s.system_prompt.strip() for s in draft.states)


def test_clone_does_not_mutate_source() -> None:
    manifest = _manifest()
    before = manifest.model_dump()
    clone_from_manifest(manifest)
    assert manifest.model_dump() == before


def test_clone_of_already_copy_name_does_not_double_suffix() -> None:
    # Cloning an already-cloned name must not produce "X.copy.copy".
    draft = clone_from_manifest(_manifest(name="Planner.copy"))
    assert draft.name == "Planner.copy"
