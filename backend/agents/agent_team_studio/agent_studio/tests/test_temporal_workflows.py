"""Unit tests for the Agent Studio workflow classes.

Each workflow ``run`` is a thin deterministic wrapper that executes its single
activity with a single-attempt retry policy. We patch ``workflow.execute_activity``
with an async stand-in and drive ``run`` via ``asyncio.run`` — no Temporal worker or
sandbox needed.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_team_studio.agent_studio.models import AgentDefinition, ConversationStateResponse
from agent_team_studio.agent_studio.temporal import workflows as wf


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch ``workflow.execute_activity`` to capture its call and echo the args."""
    cap: dict = {}

    async def _fake_execute_activity(activity, *, args, start_to_close_timeout, retry_policy):
        cap.update(
            activity=activity,
            args=list(args),
            timeout=start_to_close_timeout,
            retry=retry_policy,
        )
        return {"echo": list(args)}

    monkeypatch.setattr(wf.workflow, "execute_activity", _fake_execute_activity)
    return cap


def test_start_conversation_workflow_delegates(captured: dict) -> None:
    result = asyncio.run(wf.StartConversationWorkflow().run("new", None, "hi"))
    assert result == {"echo": ["new", None, "hi"]}
    assert captured["activity"] is wf.start_conversation_activity
    assert captured["args"] == ["new", None, "hi"]
    assert captured["retry"].maximum_attempts == 1


def test_send_message_workflow_delegates(captured: dict) -> None:
    result = asyncio.run(wf.SendMessageWorkflow().run("c1", "hello"))
    assert result == {"echo": ["c1", "hello"]}
    assert captured["activity"] is wf.send_message_activity
    assert captured["retry"].maximum_attempts == 1


def test_clone_from_registry_workflow_delegates(captured: dict) -> None:
    result = asyncio.run(wf.CloneFromRegistryWorkflow().run("blogging.planner"))
    assert result == {"echo": ["blogging.planner"]}
    assert captured["activity"] is wf.clone_from_registry_activity
    assert captured["retry"].maximum_attempts == 1


def test_save_agent_workflow_delegates(captured: dict) -> None:
    payload = AgentDefinition(name="X", role="Y").model_dump()
    result = asyncio.run(wf.SaveAgentWorkflow().run(payload))
    assert result == {"echo": [payload]}
    assert captured["activity"] is wf.save_agent_activity
    assert captured["retry"].maximum_attempts == 1


def test_response_models_round_trip_across_activity_boundary() -> None:
    """The dicts activities return must rebuild into the exact response models."""
    resp = ConversationStateResponse(
        conversation_id="c", mode="new", definition=AgentDefinition(name="n", role="r")
    )
    assert ConversationStateResponse.model_validate(resp.model_dump()) == resp

    defn = AgentDefinition(name="n", role="r", mode="refine", cloned_from="s")
    assert AgentDefinition.model_validate(defn.model_dump()) == defn
