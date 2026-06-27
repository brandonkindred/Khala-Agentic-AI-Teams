"""Unit tests for :mod:`agent_studio.models`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_studio.models import (
    AgentDefinition,
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
