"""Hermetic route-level tests for the Agent Studio Stage-1 endpoints.

Agent Studio is Temporal-only: each handler dispatches its operation as a workflow →
activity. These tests exercise the full router → dispatch → activity → service →
response path **in-process, without a Temporal cluster**, by:

  * patching ``agent_team_studio.agent_studio.runtime.get_studio_service`` so the activities delegate to
    a scripted assistant + fake registry (no live LLM / Postgres), and
  * patching ``agent_team_studio.agent_studio.temporal.dispatch.execute_workflow_sync`` with an inline
    stand-in that runs the workflow's single activity directly and reproduces
    Temporal's exception wrapping, so the dispatch layer's ``ValueError`` → 400 /
    ``LookupError`` → 404 translation is genuinely exercised.

``backend/conftest.py`` already puts ``agents/`` on ``sys.path``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError

import agent_team_studio.agent_studio.temporal.dispatch as dispatch_mod
from agent_team_studio.agent_studio.assistant import AgentDesignerAgent
from agent_team_studio.agent_studio.service import AgentStudioService
from agent_team_studio.agent_studio.store import AgentStudioConversationStore
from agent_team_studio.agent_studio.temporal.workflows import (
    CloneFromRegistryWorkflow,
    SaveAgentWorkflow,
    SendMessageWorkflow,
    StartConversationWorkflow,
    clone_from_registry_activity,
    save_agent_activity,
    send_message_activity,
    start_conversation_activity,
)
from agent_team_studio.agent_studio.testing import FakeRegistry, seed_manifest
from unified_api.routes.agent_studio import router

_DRAFT_REPLY = """\
Drafted it.

```agent
{"name": "my.agent", "role": "Does a thing", "tools": ["web.search"]}
```

```suggestions
["Add an input?"]
```
"""

# Map each workflow's run method to the single activity it executes, so the inline
# stand-in can run that activity directly (mirroring the workflow's one-activity body).
_WF_TO_ACTIVITY = {
    StartConversationWorkflow.run: start_conversation_activity,
    SendMessageWorkflow.run: send_message_activity,
    CloneFromRegistryWorkflow.run: clone_from_registry_activity,
    SaveAgentWorkflow.run: save_agent_activity,
}


def _inline_execute(workflow_run: Any, *args: Any, workflow_id: str, task_queue: str, **_: Any) -> Any:
    """In-process stand-in for ``execute_workflow_sync``.

    Runs the workflow's single activity directly and reproduces Temporal's failure
    wrapping: a typed ``ApplicationError`` (the activity's translated contract error)
    is preserved as the ``WorkflowFailureError`` cause; any other activity exception
    is wrapped as ``ApplicationError(type=<class name>)`` exactly as Temporal would.
    """
    activity_fn = _WF_TO_ACTIVITY[workflow_run]
    try:
        return activity_fn(*args)
    except ApplicationError as exc:
        raise WorkflowFailureError(cause=exc) from exc
    except Exception as exc:
        raise WorkflowFailureError(cause=ApplicationError(str(exc), type=type(exc).__name__)) from exc


def _service(reply: str, registry: FakeRegistry) -> AgentStudioService:
    """A service wired to a scripted single-reply assistant + the given registry."""
    return AgentStudioService(
        assistant=AgentDesignerAgent(complete=lambda _s, _p: reply),
        store=AgentStudioConversationStore(),
        registry_getter=lambda: registry,
    )


@pytest.fixture()
def registry() -> FakeRegistry:
    """A fake registry pre-seeded with a clonable ``blogging.planner`` source."""
    reg = FakeRegistry()
    reg.seed(seed_manifest())
    return reg


@pytest.fixture()
def make_client(monkeypatch: pytest.MonkeyPatch):
    """Factory: a TestClient whose Temporal dispatch runs activities in-process.

    Installs the two patches (runtime singleton + inline execute) via ``monkeypatch``
    so they auto-revert after the test. Each call builds a fresh app so there is no
    cross-test router leakage.
    """

    def _factory(service: object, *, raise_server_exceptions: bool = True) -> TestClient:
        monkeypatch.setattr("agent_team_studio.agent_studio.runtime.get_studio_service", lambda: service)
        monkeypatch.setattr(dispatch_mod, "execute_workflow_sync", _inline_execute)
        app = FastAPI()
        app.include_router(router)
        return TestClient(app, raise_server_exceptions=raise_server_exceptions)

    return _factory


@pytest.fixture()
def client(registry: FakeRegistry, make_client) -> TestClient:
    """Default client: a service whose assistant always returns ``_DRAFT_REPLY``."""
    return make_client(_service(_DRAFT_REPLY, registry))


def test_start_new_conversation(client: TestClient) -> None:
    """Starting a new conversation returns 200 with a greeting + readiness hints."""
    resp = client.post("/api/agent-studio/conversations", json={"mode": "new"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "new"
    assert body["readiness"] == ["name", "role"]
    assert body["conversation_id"]
    assert len(body["messages"]) == 1


def test_start_with_initial_message_drafts(client: TestClient) -> None:
    """An initial message runs a turn so the draft + suggestions come back immediately."""
    resp = client.post(
        "/api/agent-studio/conversations",
        json={"mode": "new", "initial_message": "Build a planner"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["definition"]["name"] == "my.agent"
    assert body["readiness"] == []
    assert body["suggested_questions"] == ["Add an input?"]


def test_start_refine_requires_source_is_400(client: TestClient) -> None:
    """Refine mode without a source agent is a 400."""
    resp = client.post("/api/agent-studio/conversations", json={"mode": "refine"})
    assert resp.status_code == 400


def test_start_new_with_source_is_400(client: TestClient) -> None:
    """New mode must not carry a source agent — 400."""
    resp = client.post(
        "/api/agent-studio/conversations",
        json={"mode": "new", "source_agent_id": "blogging.planner"},
    )
    assert resp.status_code == 400


def test_start_invalid_mode_is_422(client: TestClient) -> None:
    """An out-of-enum mode is rejected by Pydantic as 422."""
    resp = client.post("/api/agent-studio/conversations", json={"mode": "bogus"})
    assert resp.status_code == 422


def test_start_refine_unknown_source_is_404(client: TestClient) -> None:
    """Refine against an unregistered source agent is a 404."""
    resp = client.post(
        "/api/agent-studio/conversations",
        json={"mode": "refine", "source_agent_id": "missing"},
    )
    assert resp.status_code == 404


def test_start_refine_clones_seed(client: TestClient) -> None:
    """Refine pre-seeds the definition from a clone of the source manifest."""
    resp = client.post(
        "/api/agent-studio/conversations",
        json={"mode": "refine", "source_agent_id": "blogging.planner"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["definition"]["mode"] == "refine"
    assert body["definition"]["cloned_from"] == "blogging.planner"
    assert body["definition"]["name"] == "Planner.copy"


def test_send_message_updates_definition(client: TestClient) -> None:
    """A message turn applies the assistant's drafted definition."""
    started = client.post("/api/agent-studio/conversations", json={"mode": "new"}).json()
    cid = started["conversation_id"]
    resp = client.post(
        f"/api/agent-studio/conversations/{cid}/messages",
        json={"message": "make a planner"},
    )
    assert resp.status_code == 200
    assert resp.json()["definition"]["name"] == "my.agent"


def test_send_message_cannot_rewrite_server_owned_fields(registry: FakeRegistry, make_client) -> None:
    """Spec: the model can't rewrite server-owned ``mode``/``cloned_from``.

    Start a refine conversation, then have the assistant emit an ``agent`` block that
    tries to flip ``mode`` to ``new`` and overwrite ``cloned_from`` — the response
    definition must keep ``refine`` and the original source id.
    """
    reply = 'Updated.\n\n```agent\n{"name": "x", "role": "r", "mode": "new", "cloned_from": "other"}\n```'
    client = make_client(_service(reply, registry))

    started = client.post(
        "/api/agent-studio/conversations",
        json={"mode": "refine", "source_agent_id": "blogging.planner"},
    ).json()
    cid = started["conversation_id"]
    body = client.post(
        f"/api/agent-studio/conversations/{cid}/messages",
        json={"message": "rename it"},
    ).json()
    assert body["definition"]["name"] == "x"  # editable field applied
    assert body["definition"]["mode"] == "refine"  # server-owned, preserved
    assert body["definition"]["cloned_from"] == "blogging.planner"  # server-owned, preserved


def test_send_message_unknown_conversation_is_404(client: TestClient) -> None:
    """Messaging an unknown conversation id is a 404."""
    resp = client.post(
        "/api/agent-studio/conversations/nope/messages",
        json={"message": "hi"},
    )
    assert resp.status_code == 404


def test_send_message_rejects_empty_body(client: TestClient) -> None:
    """An empty message body is rejected by Pydantic as 422."""
    started = client.post("/api/agent-studio/conversations", json={"mode": "new"}).json()
    cid = started["conversation_id"]
    resp = client.post(f"/api/agent-studio/conversations/{cid}/messages", json={"message": ""})
    assert resp.status_code == 422


def test_send_message_value_error_is_400(registry: FakeRegistry, make_client) -> None:
    """A service ``ValueError`` maps to 400 (not an unhandled 500) through Temporal."""
    service = Mock(spec=AgentStudioService)
    service.send_message.side_effect = ValueError("bad input")
    client = make_client(service)
    resp = client.post("/api/agent-studio/conversations/x/messages", json={"message": "hi"})
    assert resp.status_code == 400


def test_clone_from_registry_endpoint(client: TestClient) -> None:
    """Cloning a registered agent returns a refine-mode draft."""
    resp = client.post("/api/agent-studio/agents/from-registry/blogging.planner")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "refine"
    assert body["cloned_from"] == "blogging.planner"


def test_clone_from_registry_unknown_is_404(client: TestClient) -> None:
    """Cloning an unknown agent id is a 404."""
    resp = client.post("/api/agent-studio/agents/from-registry/missing")
    assert resp.status_code == 404


def test_save_agent_registers(client: TestClient, registry: FakeRegistry) -> None:
    """Saving a ready definition registers it and reports ``created`` true."""
    resp = client.post(
        "/api/agent-studio/agents",
        json={"name": "Saver", "role": "Saves things", "tags": ["util"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] in registry.registered
    assert body["manifest"]["team"] == "agent_studio"
    assert body["created"] is True


def test_save_agent_same_name_reports_not_created(client: TestClient, registry: FakeRegistry) -> None:
    """Re-saving the same name updates in place and reports ``created`` false.

    The second save must actually overwrite the registered manifest, not just report
    ``created=False`` — assert the registry now holds the edited role (manifest
    ``summary`` mirrors ``definition.role``).
    """
    payload = {"name": "Dup", "role": "Does a thing"}
    first = client.post("/api/agent-studio/agents", json=payload).json()
    second = client.post("/api/agent-studio/agents", json={**payload, "role": "Edited"}).json()
    assert first["created"] is True
    assert second["created"] is False
    assert first["agent_id"] == second["agent_id"]
    # The in-place update is real: the live manifest reflects the new role.
    assert registry.registered[second["agent_id"]].summary == "Edited"


def test_save_agent_persists_edited_states(client: TestClient) -> None:
    """A save carrying edited operating states persists them onto the manifest."""
    resp = client.post(
        "/api/agent-studio/agents",
        json={
            "name": "Stateful",
            "role": "Has states",
            "states": [
                {"key": "planning", "label": "Planning", "system_prompt": "EDITED plan"},
                {"key": "executing", "label": "Executing", "system_prompt": "exec"},
                {"key": "researching", "label": "Researching", "system_prompt": "research"},
            ],
        },
    )
    assert resp.status_code == 200
    states = resp.json()["manifest"]["states"]
    assert [s["key"] for s in states] == ["planning", "executing", "researching"]
    assert states[0]["system_prompt"] == "EDITED plan"


def test_save_agent_seeds_states_when_omitted(client: TestClient) -> None:
    """A thin client that omits states still saves an agent with the three seeds."""
    resp = client.post("/api/agent-studio/agents", json={"name": "Seeded", "role": "r"})
    assert resp.status_code == 200
    states = resp.json()["manifest"]["states"]
    assert [s["key"] for s in states] == ["planning", "executing", "researching"]


def test_save_agent_normalizes_partial_states(client: TestClient) -> None:
    """An explicit partial/empty states list is normalized to all three on save."""
    resp = client.post(
        "/api/agent-studio/agents",
        json={
            "name": "Partial",
            "role": "r",
            "states": [{"key": "planning", "label": "Planning", "system_prompt": "EDITED"}],
        },
    )
    assert resp.status_code == 200
    states = resp.json()["manifest"]["states"]
    assert [s["key"] for s in states] == ["planning", "executing", "researching"]
    assert states[0]["system_prompt"] == "EDITED"


def test_clone_from_registry_returns_states(client: TestClient) -> None:
    """A cloned draft carries the three operating states (back-filled for legacy)."""
    resp = client.post("/api/agent-studio/agents/from-registry/blogging.planner")
    assert resp.status_code == 200
    states = resp.json()["states"]
    assert [s["key"] for s in states] == ["planning", "executing", "researching"]


def test_save_agent_not_ready_is_400(client: TestClient) -> None:
    """Saving a definition missing required fields is a 400."""
    resp = client.post("/api/agent-studio/agents", json={"name": "OnlyName"})
    assert resp.status_code == 400


def test_save_agent_unexpected_error_is_500(registry: FakeRegistry, make_client) -> None:
    """An unmapped service error surfaces as a 500, not a swallowed success.

    Only ``ValueError``/``LookupError`` markers are translated back to a native
    exception (→ 400/404); any other activity failure re-raises the
    ``WorkflowFailureError``, which the route does not catch → FastAPI's default 500.
    ``raise_server_exceptions=False`` lets the TestClient return that 500.
    """
    service = Mock(spec=AgentStudioService)
    service.save_agent.side_effect = RuntimeError("registry exploded")
    client = make_client(service, raise_server_exceptions=False)
    resp = client.post(
        "/api/agent-studio/agents",
        json={"name": "Boom", "role": "Explodes"},
    )
    assert resp.status_code == 500


def test_bad_typed_agent_block_returns_200_not_500(registry: FakeRegistry, make_client) -> None:
    """A wrong-typed field in the LLM block doesn't 500 — the turn 200s, draft unchanged."""
    bad_reply = 'Sure.\n\n```agent\n{"name": 123}\n```'
    client = make_client(_service(bad_reply, registry))

    started = client.post("/api/agent-studio/conversations", json={"mode": "new"}).json()
    resp = client.post(
        f"/api/agent-studio/conversations/{started['conversation_id']}/messages",
        json={"message": "name it"},
    )
    assert resp.status_code == 200
    assert resp.json()["definition"]["name"] == ""  # unchanged
