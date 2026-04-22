"""Route-level tests for the agent-keyed /api/agents/sandboxes/* endpoints.

Phase 3 (issue #265) switched this router from the legacy per-team
``agent_sandbox`` manager to the agent-keyed ``agent_provisioning_team.sandbox``
lifecycle. Tests mock the docker CLI so no daemon is touched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_provisioning_team import sandbox as sb
from agent_provisioning_team.sandbox import SandboxStatus
from agent_provisioning_team.sandbox import provisioner as provisioner_mod
from agent_provisioning_team.sandbox.lifecycle import Lifecycle


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    # Fresh Lifecycle pointed at tmp_path.
    lc = Lifecycle(state_file=tmp_path / "state.json")
    sb.set_lifecycle_for_testing(lc)

    # Registry resolves agent_id → team without touching the on-disk manifests.
    monkeypatch.setattr(
        "agent_provisioning_team.sandbox.lifecycle._resolve_team",
        lambda agent_id: "blogging"
        if agent_id.startswith("blogging.")
        else (_ for _ in ()).throw(sb.UnknownAgentError(f"No agent manifest for {agent_id!r}")),
    )

    # Mock the docker CLI.
    monkeypatch.setattr(provisioner_mod, "run_container", AsyncMock(return_value="abc123"))
    monkeypatch.setattr(provisioner_mod, "inspect_host_port", AsyncMock(return_value=55123))
    monkeypatch.setattr(provisioner_mod, "is_running", AsyncMock(return_value=True))
    monkeypatch.setattr(provisioner_mod, "stop_container", AsyncMock())
    monkeypatch.setattr(Lifecycle, "_wait_healthy", AsyncMock())

    from unified_api.routes.sandboxes import router as sandboxes_router

    app = FastAPI()
    app.include_router(sandboxes_router)
    try:
        yield TestClient(app)
    finally:
        sb.set_lifecycle_for_testing(None)


def test_status_cold_for_unwarmed_agent(client: TestClient) -> None:
    resp = client.get("/api/agents/sandboxes/blogging.planner")
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "blogging.planner"
    assert body["status"] == SandboxStatus.COLD


def test_status_404_for_unknown_agent(client: TestClient) -> None:
    resp = client.get("/api/agents/sandboxes/ghost.agent")
    assert resp.status_code == 404


def test_warm_then_list_then_teardown(client: TestClient) -> None:
    # Warm (eager acquire).
    resp = client.post("/api/agents/sandboxes/blogging.planner/warm")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == SandboxStatus.WARM
    assert body["agent_id"] == "blogging.planner"
    assert body["url"] == "http://127.0.0.1:55123"

    # List shows it (agent-keyed, not team-keyed).
    resp = client.get("/api/agents/sandboxes")
    assert resp.status_code == 200
    ids = {h["agent_id"] for h in resp.json()}
    assert ids == {"blogging.planner"}

    # Teardown clears it.
    resp = client.delete("/api/agents/sandboxes/blogging.planner")
    assert resp.status_code == 200
    assert resp.json()["status"] == "torn down"
    resp = client.get("/api/agents/sandboxes")
    assert resp.json() == []


def test_warm_404_for_unknown_agent(client: TestClient) -> None:
    resp = client.post("/api/agents/sandboxes/ghost.agent/warm")
    assert resp.status_code == 404


def test_teardown_is_idempotent_for_cold_agent(client: TestClient) -> None:
    # Teardown of a never-warmed sandbox returns 200 (no-op) rather than 404 —
    # the lifecycle's teardown is a silent no-op when state is empty, which
    # keeps the route idempotent for clients retrying cleanup.
    resp = client.delete("/api/agents/sandboxes/blogging.planner")
    assert resp.status_code == 200


def test_status_reconciles_vanished_container(client: TestClient, monkeypatch) -> None:
    """If the container was reaped externally, status() must flip the
    stored state back to COLD rather than keep reporting WARM."""
    # Warm first.
    resp = client.post("/api/agents/sandboxes/blogging.planner/warm")
    assert resp.status_code == 200
    assert resp.json()["status"] == SandboxStatus.WARM

    # Simulate the container vanishing behind our back.
    monkeypatch.setattr(provisioner_mod, "is_running", AsyncMock(return_value=False))

    resp = client.get("/api/agents/sandboxes/blogging.planner")
    assert resp.status_code == 200
    assert resp.json()["status"] == SandboxStatus.COLD


@patch("agent_provisioning_team.sandbox.lifecycle.provisioner_mod")
def test_legacy_team_keyed_urls_are_not_served(_unused, tmp_path) -> None:
    """Regression guard: the old ``POST /api/agents/sandboxes/{team}`` URL
    shape is gone — any CI/UI caller still on the team-keyed path must see
    a 404 rather than silently hitting a different handler."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unified_api.routes.sandboxes import router

    app = FastAPI()
    app.include_router(router)
    legacy_client = TestClient(app)

    resp = legacy_client.post("/api/agents/sandboxes/blogging")
    # The new contract requires the /warm suffix on POST; FastAPI's router
    # returns 405 (Method Not Allowed) because GET still matches the bare
    # {agent_id} path. Either 404 or 405 is an acceptable "legacy URL does
    # not work" signal — the only outcome we must prevent is 200.
    assert resp.status_code in (404, 405)
