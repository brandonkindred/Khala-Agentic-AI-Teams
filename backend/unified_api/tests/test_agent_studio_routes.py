"""Hermetic route-level tests for the Agent Studio Stage-1 endpoints.

A fresh ``FastAPI`` app mounts only the agent-studio router, and the service is
injected via FastAPI's ``dependency_overrides`` — wired to a scripted assistant +
fake registry, so no live LLM, Postgres, or process-wide registry is touched.
``backend/conftest.py`` already puts ``agents/`` on ``sys.path``; imports below
rely on that rather than manipulating the path here.
"""

from __future__ import annotations

from unittest.mock import Mock

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


def _make_client(service: object, *, raise_server_exceptions: bool = True) -> TestClient:
    """Build a TestClient for a fresh app with ``service`` injected as the dependency.

    Each call gets its own ``FastAPI`` instance, so the override is scoped to that
    app and there's no cross-test leakage to clean up. Pass
    ``raise_server_exceptions=False`` to let the app's exception handler turn an
    unhandled error into a 500 response (instead of re-raising into the test) when
    asserting on server-error mapping.
    """
    from unified_api.routes.agent_studio import get_agent_studio_service, router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_studio_service] = lambda: service
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


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
def client(registry: FakeRegistry) -> TestClient:
    """Default client: a service whose assistant always returns ``_DRAFT_REPLY``."""
    return _make_client(_service(_DRAFT_REPLY, registry))


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


def test_send_message_cannot_rewrite_server_owned_fields(registry: FakeRegistry) -> None:
    """Spec: the model can't rewrite server-owned ``mode``/``cloned_from``.

    Start a refine conversation, then have the assistant emit an ``agent`` block
    that tries to flip ``mode`` to ``new`` and overwrite ``cloned_from`` — the
    response definition must keep ``refine`` and the original source id.
    """
    reply = 'Updated.\n\n```agent\n{"name": "x", "role": "r", "mode": "new", "cloned_from": "other"}\n```'
    client = _make_client(_service(reply, registry))

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


def test_send_message_value_error_is_400() -> None:
    """A service ``ValueError`` maps to 400 (not an unhandled 500)."""
    service = Mock(spec=AgentStudioService)
    service.send_message.side_effect = ValueError("bad input")
    client = _make_client(service)
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

    The second save must actually overwrite the registered manifest, not just
    report ``created=False`` — assert the registry now holds the edited role
    (manifest ``summary`` mirrors ``definition.role``).
    """
    payload = {"name": "Dup", "role": "Does a thing"}
    first = client.post("/api/agent-studio/agents", json=payload).json()
    second = client.post("/api/agent-studio/agents", json={**payload, "role": "Edited"}).json()
    assert first["created"] is True
    assert second["created"] is False
    assert first["agent_id"] == second["agent_id"]
    # The in-place update is real: the live manifest reflects the new role.
    assert registry.registered[second["agent_id"]].summary == "Edited"


def test_save_agent_not_ready_is_400(client: TestClient) -> None:
    """Saving a definition missing required fields is a 400."""
    resp = client.post("/api/agent-studio/agents", json={"name": "OnlyName"})
    assert resp.status_code == 400


def test_save_agent_unexpected_error_is_500() -> None:
    """An unmapped service error surfaces as a 500, not a swallowed success.

    Only ``ValueError`` (→400) is caught in the route; any other exception must
    propagate to FastAPI's default 500 handler. ``raise_server_exceptions=False``
    lets the TestClient return that 500 instead of re-raising into the test.
    """
    service = Mock(spec=AgentStudioService)
    service.save_agent.side_effect = RuntimeError("registry exploded")
    client = _make_client(service, raise_server_exceptions=False)
    resp = client.post(
        "/api/agent-studio/agents",
        json={"name": "Boom", "role": "Explodes"},
    )
    assert resp.status_code == 500


def test_default_dependency_returns_module_service() -> None:
    """The un-overridden dependency resolves the process-wide default service.

    Checked directly (rather than via a live request) so the test stays hermetic —
    a real request would invoke the default service's live LLM/registry.
    """
    import unified_api.routes.agent_studio as routes_mod

    assert routes_mod.get_agent_studio_service() is routes_mod._service


def test_bad_typed_agent_block_returns_200_not_500(registry: FakeRegistry) -> None:
    """A wrong-typed field in the LLM block doesn't 500 — the turn 200s, draft unchanged."""
    bad_reply = 'Sure.\n\n```agent\n{"name": 123}\n```'
    client = _make_client(_service(bad_reply, registry))

    started = client.post("/api/agent-studio/conversations", json={"mode": "new"}).json()
    resp = client.post(
        f"/api/agent-studio/conversations/{started['conversation_id']}/messages",
        json={"message": "name it"},
    )
    assert resp.status_code == 200
    assert resp.json()["definition"]["name"] == ""  # unchanged
