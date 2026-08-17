"""Authoring CRUD must not dispatch to the 1-activity Studio Temporal workflows.

Those workflows are demoted: they are not required for start-conversation,
send-message, clone, or save. Even when a Temporal cluster is configured and an
in-process ``agent_studio`` worker is ready, CRUD must call
``AgentStudioService`` in-process and must never start a workflow.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import agent_platform.studio.temporal.dispatch as dispatch
from agent_platform.studio.models import AgentDefinition, ConversationStateResponse
from agent_platform.studio.registration import build_studio_agent_manifest


@pytest.fixture(autouse=True)
def _temporal_and_worker_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the conditions that used to select the Temporal CRUD path."""
    monkeypatch.setattr("shared.temporal.client.is_temporal_enabled", lambda: True)
    monkeypatch.setattr(
        "shared.temporal.worker.is_team_worker_ready", lambda team: team == "agent_studio"
    )


def test_start_conversation_uses_service_when_temporal_ready(service: Mock) -> None:
    resp = ConversationStateResponse(conversation_id="c1", mode="new", definition=AgentDefinition())
    service.start_conversation.return_value = resp

    out = dispatch.start_conversation("new", None, "hi")

    assert out is resp
    service.start_conversation.assert_called_once_with("new", None, "hi")


def test_send_message_uses_service_when_temporal_ready(service: Mock) -> None:
    resp = ConversationStateResponse(conversation_id="c9", mode="new", definition=AgentDefinition())
    service.send_message.return_value = resp

    out = dispatch.send_message("c9", "make a planner")

    assert out is resp
    service.send_message.assert_called_once_with("c9", "make a planner")


def test_clone_from_registry_uses_service_when_temporal_ready(service: Mock) -> None:
    draft = AgentDefinition(
        name="Planner.copy", role="r", mode="refine", cloned_from="blogging.planner"
    )
    service.clone_from_registry.return_value = draft

    out = dispatch.clone_from_registry("blogging.planner")

    assert out is draft
    service.clone_from_registry.assert_called_once_with("blogging.planner")


def test_save_agent_uses_service_when_temporal_ready(service: Mock) -> None:
    definition = AgentDefinition(name="Saver", role="Saves things")
    manifest = build_studio_agent_manifest(definition)
    service.save_agent.return_value = (manifest, True)

    out_manifest, created = dispatch.save_agent(definition)

    assert out_manifest is manifest
    assert created is True
    service.save_agent.assert_called_once_with(definition)
