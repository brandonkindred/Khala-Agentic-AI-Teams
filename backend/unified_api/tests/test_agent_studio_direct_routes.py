"""Thin HTTP smoke tests for Agent Studio authoring CRUD.

Routes call ``AgentStudioService`` in-process. Does not duplicate the fuller
cases in ``test_agent_studio_routes.py``.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_platform.studio import router
from agent_platform.studio.assistant import AgentDesignerAgent
from agent_platform.studio.service import AgentStudioService
from agent_platform.studio.store import AgentStudioConversationStore
from agent_platform.studio.testing import FakeRegistry, seed_manifest

_DRAFT_REPLY = """\
Drafted it.

```agent
{"name": "my.agent", "role": "Does a thing", "tools": ["web.search"]}
```

```suggestions
["Add an input?"]
```
"""


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
    """Factory: TestClient whose dispatch uses the in-process service path."""

    def _factory(service: object, *, raise_server_exceptions: bool = True) -> TestClient:
        monkeypatch.setattr("agent_platform.studio.runtime.get_studio_service", lambda: service)
        app = FastAPI()
        app.include_router(router)
        return TestClient(app, raise_server_exceptions=raise_server_exceptions)

    return _factory


@pytest.fixture()
def client(registry: FakeRegistry, make_client) -> TestClient:
    """Default client: a service whose assistant always returns ``_DRAFT_REPLY``."""
    return make_client(_service(_DRAFT_REPLY, registry))


def test_start_new_conversation(client: TestClient) -> None:
    resp = client.post("/api/agent-studio/conversations", json={"mode": "new"})
    assert resp.status_code == 200
    assert resp.json()["conversation_id"]


def test_send_message_after_start(client: TestClient) -> None:
    started = client.post("/api/agent-studio/conversations", json={"mode": "new"}).json()
    cid = started["conversation_id"]
    resp = client.post(
        f"/api/agent-studio/conversations/{cid}/messages",
        json={"message": "make a planner"},
    )
    assert resp.status_code == 200
    assert resp.json()["definition"]["name"] == "my.agent"


def test_clone_from_registry_endpoint(client: TestClient) -> None:
    resp = client.post("/api/agent-studio/agents/from-registry/blogging.planner")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "refine"


def test_save_agent_registers(client: TestClient) -> None:
    resp = client.post(
        "/api/agent-studio/agents",
        json={"name": "Saver", "role": "Saves things"},
    )
    assert resp.status_code == 200
    assert resp.json()["created"] is True


def test_start_refine_requires_source_is_400(client: TestClient) -> None:
    resp = client.post("/api/agent-studio/conversations", json={"mode": "refine"})
    assert resp.status_code == 400


def test_start_refine_unknown_source_is_404(client: TestClient) -> None:
    resp = client.post(
        "/api/agent-studio/conversations",
        json={"mode": "refine", "source_agent_id": "missing"},
    )
    assert resp.status_code == 404


def test_send_message_unknown_conversation_is_404(client: TestClient) -> None:
    resp = client.post(
        "/api/agent-studio/conversations/nope/messages",
        json={"message": "hi"},
    )
    assert resp.status_code == 404


def test_send_message_value_error_is_400(registry: FakeRegistry, make_client) -> None:
    service = Mock(spec=AgentStudioService)
    service.send_message.side_effect = ValueError("bad input")
    client = make_client(service)
    resp = client.post("/api/agent-studio/conversations/x/messages", json={"message": "hi"})
    assert resp.status_code == 400


def test_clone_from_registry_unknown_is_404(client: TestClient) -> None:
    resp = client.post("/api/agent-studio/agents/from-registry/missing")
    assert resp.status_code == 404


def test_save_agent_not_ready_is_400(client: TestClient) -> None:
    resp = client.post("/api/agent-studio/agents", json={"name": "OnlyName"})
    assert resp.status_code == 400


def test_save_agent_unexpected_error_is_500(registry: FakeRegistry, make_client) -> None:
    service = Mock(spec=AgentStudioService)
    service.save_agent.side_effect = RuntimeError("registry exploded")
    client = make_client(service, raise_server_exceptions=False)
    resp = client.post(
        "/api/agent-studio/agents",
        json={"name": "Boom", "role": "Explodes"},
    )
    assert resp.status_code == 500
