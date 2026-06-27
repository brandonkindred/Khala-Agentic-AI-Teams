"""Hermetic route-level tests for the Agent Studio Stage-1 endpoints.

A fresh ``FastAPI`` app mounts only the agent-studio router, and the service is
injected via FastAPI's ``dependency_overrides`` — wired to a scripted assistant +
fake registry, so no live LLM, Postgres, or process-wide registry is touched.
``backend/conftest.py`` already puts ``agents/`` on ``sys.path``; imports below
rely on that rather than manipulating the path here.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_studio.assistant import AgentDesignerAgent
from agent_studio.service import AgentStudioService
from agent_studio.store import AgentStudioConversationStore
from agent_studio.testing import FakeRegistry, seed_manifest

_DRAFT_REPLY = """\
Drafted it.

```agent
{"name": "my.agent", "role": "Does a thing", "tools": ["web.search"]}
```

```suggestions
["Add an input?"]
```
"""


@pytest.fixture()
def registry() -> FakeRegistry:
    reg = FakeRegistry()
    reg.seed(seed_manifest())
    return reg


@pytest.fixture()
def client(registry: FakeRegistry) -> TestClient:
    from unified_api.routes.agent_studio import get_agent_studio_service, router

    service = AgentStudioService(
        assistant=AgentDesignerAgent(complete=lambda _s, _p: _DRAFT_REPLY),
        store=AgentStudioConversationStore(),
        registry_getter=lambda: registry,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_studio_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_start_new_conversation(client: TestClient) -> None:
    resp = client.post("/api/agent-studio/conversations", json={"mode": "new"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "new"
    assert body["readiness"] == ["name", "role"]
    assert body["conversation_id"]
    assert len(body["messages"]) == 1


def test_start_with_initial_message_drafts(client: TestClient) -> None:
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
    resp = client.post("/api/agent-studio/conversations", json={"mode": "refine"})
    assert resp.status_code == 400


def test_start_new_with_source_is_400(client: TestClient) -> None:
    resp = client.post(
        "/api/agent-studio/conversations",
        json={"mode": "new", "source_agent_id": "blogging.planner"},
    )
    assert resp.status_code == 400


def test_start_invalid_mode_is_422(client: TestClient) -> None:
    resp = client.post("/api/agent-studio/conversations", json={"mode": "bogus"})
    assert resp.status_code == 422


def test_start_refine_unknown_source_is_404(client: TestClient) -> None:
    resp = client.post(
        "/api/agent-studio/conversations",
        json={"mode": "refine", "source_agent_id": "missing"},
    )
    assert resp.status_code == 404


def test_start_refine_clones_seed(client: TestClient) -> None:
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
    started = client.post("/api/agent-studio/conversations", json={"mode": "new"}).json()
    cid = started["conversation_id"]
    resp = client.post(
        f"/api/agent-studio/conversations/{cid}/messages",
        json={"message": "make a planner"},
    )
    assert resp.status_code == 200
    assert resp.json()["definition"]["name"] == "my.agent"


def test_send_message_unknown_conversation_is_404(client: TestClient) -> None:
    resp = client.post(
        "/api/agent-studio/conversations/nope/messages",
        json={"message": "hi"},
    )
    assert resp.status_code == 404


def test_send_message_rejects_empty_body(client: TestClient) -> None:
    started = client.post("/api/agent-studio/conversations", json={"mode": "new"}).json()
    cid = started["conversation_id"]
    resp = client.post(f"/api/agent-studio/conversations/{cid}/messages", json={"message": ""})
    assert resp.status_code == 422


def test_send_message_value_error_is_400() -> None:
    # The handler must map a service ValueError to 400 (not let it 500).
    from unified_api.routes.agent_studio import get_agent_studio_service, router

    class _BoomService:
        def send_message(self, *_args):
            raise ValueError("bad input")

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_studio_service] = lambda: _BoomService()
    client = TestClient(app)
    resp = client.post("/api/agent-studio/conversations/x/messages", json={"message": "hi"})
    app.dependency_overrides.clear()
    assert resp.status_code == 400


def test_clone_from_registry_endpoint(client: TestClient) -> None:
    resp = client.post("/api/agent-studio/agents/from-registry/blogging.planner")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "refine"
    assert body["cloned_from"] == "blogging.planner"


def test_clone_from_registry_unknown_is_404(client: TestClient) -> None:
    resp = client.post("/api/agent-studio/agents/from-registry/missing")
    assert resp.status_code == 404


def test_save_agent_registers(client: TestClient, registry: FakeRegistry) -> None:
    resp = client.post(
        "/api/agent-studio/agents",
        json={"name": "Saver", "role": "Saves things", "tags": ["util"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] in registry.registered
    assert body["manifest"]["team"] == "agent_studio"
    assert body["created"] is True


def test_save_agent_same_name_reports_not_created(client: TestClient) -> None:
    payload = {"name": "Dup", "role": "Does a thing"}
    first = client.post("/api/agent-studio/agents", json=payload).json()
    second = client.post("/api/agent-studio/agents", json={**payload, "role": "Edited"}).json()
    assert first["created"] is True
    assert second["created"] is False
    assert first["agent_id"] == second["agent_id"]


def test_save_agent_not_ready_is_400(client: TestClient) -> None:
    resp = client.post("/api/agent-studio/agents", json={"name": "OnlyName"})
    assert resp.status_code == 400


def test_default_dependency_returns_module_service() -> None:
    # The un-overridden dependency resolves the process-wide default service.
    import unified_api.routes.agent_studio as routes_mod

    assert routes_mod.get_agent_studio_service() is routes_mod._service


def test_bad_typed_agent_block_returns_200_not_500(registry: FakeRegistry) -> None:
    # An LLM block with a wrong-typed field must not surface as a 500; the turn
    # succeeds (200) with the definition left unchanged.
    from unified_api.routes.agent_studio import get_agent_studio_service, router

    bad_reply = 'Sure.\n\n```agent\n{"name": 123}\n```'
    service = AgentStudioService(
        assistant=AgentDesignerAgent(complete=lambda _s, _p: bad_reply),
        store=AgentStudioConversationStore(),
        registry_getter=lambda: registry,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_studio_service] = lambda: service
    c = TestClient(app)

    started = c.post("/api/agent-studio/conversations", json={"mode": "new"}).json()
    resp = c.post(
        f"/api/agent-studio/conversations/{started['conversation_id']}/messages",
        json={"message": "name it"},
    )
    assert resp.status_code == 200
    assert resp.json()["definition"]["name"] == ""  # unchanged
    app.dependency_overrides.clear()
