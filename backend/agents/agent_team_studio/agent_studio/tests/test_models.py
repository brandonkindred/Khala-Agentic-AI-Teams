"""Unit tests for :mod:`agent_team_studio.agent_studio.models`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_team_studio.agent_studio.agent_states import STATE_ORDER
from agent_team_studio.agent_studio.models import (
    AgentDefinition,
    AgentState,
    SaveAgentRequest,
    SendMessageRequest,
    StartConversationRequest,
)


def test_missing_required_lists_empty_fields_in_order() -> None:
    assert AgentDefinition().missing_required() == ["name", "role"]
    assert AgentDefinition(name="x").missing_required() == ["role"]
    assert AgentDefinition(role="x").missing_required() == ["name"]


def test_missing_required_treats_whitespace_as_empty() -> None:
    assert AgentDefinition(name="  ", role="\t").missing_required() == ["name", "role"]


def test_is_ready_iff_required_present() -> None:
    assert not AgentDefinition().is_ready
    assert not AgentDefinition(name="a").is_ready
    assert AgentDefinition(name="a", role="b").is_ready


def test_definition_defaults() -> None:
    d = AgentDefinition()
    assert d.mode == "new"
    assert d.cloned_from is None
    assert d.tags == [] and d.tools == []
    assert d.input_schema is None and d.output_schema is None


def test_definition_seeds_three_operating_states() -> None:
    # Every fresh definition is born with the three fixed states, in order, each
    # with a non-empty behavioral prompt.
    d = AgentDefinition()
    assert [s.key for s in d.states] == list(STATE_ORDER)
    assert all(s.system_prompt.strip() for s in d.states)
    assert [s.label for s in d.states] == ["Planning", "Executing", "Researching"]


def test_definition_states_are_independent_per_instance() -> None:
    # The default_factory must hand each instance its own list/objects, so editing
    # one draft's state prompt never leaks into another's.
    a = AgentDefinition()
    b = AgentDefinition()
    a.states[0].system_prompt = "MUTATED"
    assert b.states[0].system_prompt != "MUTATED"
    assert a.states is not b.states


def test_agent_state_rejects_unknown_key() -> None:
    # key is a fixed Literal — the model can never invent a fourth state.
    AgentState(key="planning", label="Planning", system_prompt="x")  # valid
    with pytest.raises(ValidationError):
        AgentState(key="bogus", label="Bogus", system_prompt="x")


def test_definition_normalizes_explicit_partial_states() -> None:
    # Supplying a partial/empty/duplicate list must not bypass the fixed key set —
    # the AfterValidator normalizes it to exactly the three states on construction.
    d = AgentDefinition(name="a", role="b", states=[])
    assert [s.key for s in d.states] == list(STATE_ORDER)

    partial = AgentDefinition(
        states=[AgentState(key="executing", label="Executing", system_prompt="EDIT")]
    )
    assert [s.key for s in partial.states] == list(STATE_ORDER)
    # The supplied edit survives in its canonical slot.
    assert partial.states[1].system_prompt == "EDIT"


def test_save_request_normalizes_explicit_partial_states() -> None:
    # The save surface is the one a thin client/LLM hits directly — normalize there
    # too, so build_studio_agent_manifest never persists a partial list.
    req = SaveAgentRequest(name="a", role="b", states=[])
    assert [s.key for s in req.states] == list(STATE_ORDER)
    definition = req.to_definition()
    assert [s.key for s in definition.states] == list(STATE_ORDER)


def test_save_request_seeds_states_and_to_definition_carries_edits() -> None:
    # A save request defaults to the three seeded states...
    assert [s.key for s in SaveAgentRequest().states] == list(STATE_ORDER)
    # ...and an edited set flows through to_definition() unchanged.
    edited = [
        AgentState(key="planning", label="Planning", system_prompt="EDITED plan prompt"),
        AgentState(key="executing", label="Executing", system_prompt="exec"),
        AgentState(key="researching", label="Researching", system_prompt="research"),
    ]
    definition = SaveAgentRequest(name="A", role="r", states=edited).to_definition()
    assert definition.states[0].system_prompt == "EDITED plan prompt"


def test_start_conversation_request_defaults_to_new() -> None:
    req = StartConversationRequest()
    assert req.mode == "new"
    assert req.source_agent_id is None
    assert req.initial_message is None


def test_send_message_request_rejects_empty() -> None:
    assert SendMessageRequest(message="hi").message == "hi"
    with pytest.raises(ValidationError):
        SendMessageRequest(message="")


def test_save_agent_request_to_definition_drops_server_owned_fields() -> None:
    req = SaveAgentRequest(name="Planner", role="Plans", tags=["seo"], tools=["web.search"])
    definition = req.to_definition()
    assert definition.name == "Planner"
    assert definition.role == "Plans"
    assert definition.tags == ["seo"]
    assert definition.tools == ["web.search"]
    # Server-owned provenance fields are not part of the request surface.
    assert definition.mode == "new"
    assert definition.cloned_from is None
    assert "mode" not in SaveAgentRequest.model_fields
    assert "cloned_from" not in SaveAgentRequest.model_fields


def test_start_conversation_request_rejects_invalid_mode() -> None:
    # mode is Literal["new", "refine"]; arbitrary strings must be rejected.
    with pytest.raises(ValidationError):
        StartConversationRequest(mode="bogus")
