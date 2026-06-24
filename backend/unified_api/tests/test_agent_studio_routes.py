"""Hermetic route-level tests for the Agent Studio Stage-1 endpoints.

A fresh ``FastAPI`` app mounts only the agent-studio router, and the route
module's module-level ``_service`` is swapped for one wired to a scripted
assistant + fake registry, so no live LLM, Postgres, or process-wide registry
is touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_registry.models import AgentManifest, CognitionSpec, SourceInfo
from agent_studio.assistant import AgentDesignerAgent
from agent_studio.service import AgentStudioService
from agent_studio.store import AgentStudioConversationStore

_DRAFT_REPLY = """\
Drafted it.

```agent
{"name": "my.agent", "role": "Does a thing", "tools": ["web.search"]}
```

```suggestions
["Add an input?"]
```
"""


class _FakeRegistry:
    def __init__(self) -> None:
        self.registered: dict[str, AgentManifest] = {}
        self._seed: dict[str, AgentManifest] = {}

    def seed(self, manifest: AgentManifest) -> None:
        self._seed[manifest.id] = manifest

    def get(self, agent_id: str) -> AgentManifest | None:
        return self._seed.get(agent_id) or self.registered.get(agent_id)

    def register(self, manifest: AgentManifest) -> None:
        self.registered[manifest.id] = manifest


def _seed_manifest() -> AgentManifest:
    return AgentManifest(
        id="blogging.planner",
        team="blogging",
        name="Planner",
        summary="Plans blog outlines",
        tags=["content"],
        cognition=CognitionSpec(rule_packs=["default_guardrails"], tools=["web.search"]),
        source=SourceInfo(entrypoint="x:run"),
    )


@pytest.fixture()
def registry() -> _FakeRegistry:
    reg = _FakeRegistry()
    reg.seed(_seed_manifest())
    return reg


@pytest.fixture()
def client(registry: _FakeRegistry) -> TestClient:
    import unified_api.routes.agent_studio as routes_mod

    original = routes_mod._service
    routes_mod._service = AgentStudioService(
        assistant=AgentDesignerAgent(complete=lambda _s, _p: _DRAFT_REPLY),
        store=AgentStudioConversationStore(),
        registry_getter=lambda: registry,
    )
    app = FastAPI()
    app.include_router(routes_mod.router)
    try:
        yield TestClient(app)
    finally:
        routes_mod._service = original


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


def test_clone_from_registry_endpoint(client: TestClient) -> None:
    resp = client.post("/api/agent-studio/agents/from-registry/blogging.planner")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "refine"
    assert body["cloned_from"] == "blogging.planner"


def test_clone_from_registry_unknown_is_404(client: TestClient) -> None:
    resp = client.post("/api/agent-studio/agents/from-registry/missing")
    assert resp.status_code == 404


def test_save_agent_registers(client: TestClient, registry: _FakeRegistry) -> None:
    resp = client.post(
        "/api/agent-studio/agents",
        json={"name": "Saver", "role": "Saves things", "tags": ["util"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] in registry.registered
    assert body["manifest"]["team"] == "agent_studio"


def test_save_agent_not_ready_is_400(client: TestClient) -> None:
    resp = client.post("/api/agent-studio/agents", json={"name": "OnlyName"})
    assert resp.status_code == 400
