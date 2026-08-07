"""Unit tests for the Agent Studio Temporal activities.

Each activity delegates to the process-wide ``AgentStudioService`` (patched here to a
``Mock``) and re-shapes the service's ``ValueError``/``LookupError`` as a typed,
non-retryable ``ApplicationError`` while letting any other exception propagate. The
activities are plain ``@activity.defn`` functions with no ``temporalio.activity``
runtime calls, so they are exercised by calling them directly.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from temporalio.exceptions import ApplicationError

from agent_team_studio.agent_studio.models import AgentDefinition, ConversationStateResponse
from agent_team_studio.agent_studio.registration import build_studio_agent_manifest
from agent_team_studio.agent_studio.service import AgentStudioService
from agent_team_studio.agent_studio.temporal.workflows import (
    clone_from_registry_activity,
    save_agent_activity,
    send_message_activity,
    start_conversation_activity,
)


@pytest.fixture()
def service(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """A Mock service installed as the process-wide singleton the activities use."""
    svc = Mock(spec=AgentStudioService)
    monkeypatch.setattr("agent_team_studio.agent_studio.runtime.get_studio_service", lambda: svc)
    return svc


# ── start_conversation ──────────────────────────────────────────────────────────


def test_start_conversation_activity_returns_state_dict(service: Mock) -> None:
    resp = ConversationStateResponse(conversation_id="c1", mode="new", definition=AgentDefinition())
    service.start_conversation.return_value = resp

    out = start_conversation_activity("new", None, "hi")

    assert out == resp.model_dump()
    assert ConversationStateResponse.model_validate(out) == resp
    service.start_conversation.assert_called_once_with("new", None, "hi")


def test_start_conversation_activity_translates_value_error(service: Mock) -> None:
    service.start_conversation.side_effect = ValueError("bad mode")
    with pytest.raises(ApplicationError) as ei:
        start_conversation_activity("refine", None, None)
    assert ei.value.type == "ValueError"
    assert ei.value.non_retryable is True


def test_start_conversation_activity_translates_lookup_error(service: Mock) -> None:
    service.start_conversation.side_effect = LookupError("no such source")
    with pytest.raises(ApplicationError) as ei:
        start_conversation_activity("refine", "missing", None)
    assert ei.value.type == "LookupError"


def test_start_conversation_activity_propagates_unknown_error(service: Mock) -> None:
    service.start_conversation.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        start_conversation_activity("new", None, None)


# ── send_message ─────────────────────────────────────────────────────────────────


def test_send_message_activity_returns_state_dict(service: Mock) -> None:
    resp = ConversationStateResponse(conversation_id="c9", mode="new", definition=AgentDefinition())
    service.send_message.return_value = resp

    out = send_message_activity("c9", "make a planner")

    assert ConversationStateResponse.model_validate(out) == resp
    service.send_message.assert_called_once_with("c9", "make a planner")


def test_send_message_activity_translates_lookup_error(service: Mock) -> None:
    service.send_message.side_effect = LookupError("unknown conversation")
    with pytest.raises(ApplicationError) as ei:
        send_message_activity("nope", "hi")
    assert ei.value.type == "LookupError"
    assert ei.value.non_retryable is True


# ── clone_from_registry ──────────────────────────────────────────────────────────


def test_clone_from_registry_activity_returns_definition_dict(service: Mock) -> None:
    draft = AgentDefinition(
        name="Planner.copy", role="r", mode="refine", cloned_from="blogging.planner"
    )
    service.clone_from_registry.return_value = draft

    out = clone_from_registry_activity("blogging.planner")

    assert AgentDefinition.model_validate(out) == draft
    service.clone_from_registry.assert_called_once_with("blogging.planner")


def test_clone_from_registry_activity_translates_lookup_error(service: Mock) -> None:
    service.clone_from_registry.side_effect = LookupError("unknown agent")
    with pytest.raises(ApplicationError) as ei:
        clone_from_registry_activity("missing")
    assert ei.value.type == "LookupError"


# ── save_agent ───────────────────────────────────────────────────────────────────


def test_save_agent_activity_returns_manifest_and_created(service: Mock) -> None:
    definition = AgentDefinition(name="Saver", role="Saves things")
    manifest = build_studio_agent_manifest(definition)
    service.save_agent.return_value = (manifest, True)

    out = save_agent_activity(definition.model_dump())

    assert out == {"manifest": manifest.model_dump(), "created": True}
    # The dict is rebuilt into an AgentDefinition before the service call.
    passed = service.save_agent.call_args.args[0]
    assert isinstance(passed, AgentDefinition)
    assert passed.name == "Saver"


def test_save_agent_activity_translates_value_error(service: Mock) -> None:
    service.save_agent.side_effect = ValueError("not ready")
    with pytest.raises(ApplicationError) as ei:
        save_agent_activity(AgentDefinition(name="X", role="Y").model_dump())
    assert ei.value.type == "ValueError"
    assert ei.value.non_retryable is True


# ── subclass markers ─────────────────────────────────────────────────────────────


def test_activity_maps_valueerror_subclass_to_base_marker(service: Mock) -> None:
    """A ``ValueError`` *subclass* maps to the base ``"ValueError"`` marker so the
    dispatch layer still translates it to 400 (not a 500 on an unrecognized name)."""

    class _CustomBadInput(ValueError):
        pass

    service.start_conversation.side_effect = _CustomBadInput("bad")
    with pytest.raises(ApplicationError) as ei:
        start_conversation_activity("new", None, None)
    assert ei.value.type == "ValueError"


def test_activity_maps_lookuperror_subclass_to_base_marker(service: Mock) -> None:
    """A ``LookupError`` subclass (``KeyError``) maps to the base ``"LookupError"``
    marker so the dispatch layer still translates it to 404."""
    service.send_message.side_effect = KeyError("missing")
    with pytest.raises(ApplicationError) as ei:
        send_message_activity("c", "hi")
    assert ei.value.type == "LookupError"
