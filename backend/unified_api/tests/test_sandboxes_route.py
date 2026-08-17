"""Route-level tests for the agent-keyed /api/agents/sandboxes/* endpoints.

Backed by the ``agent_platform.sandbox`` lifecycle. Tests mock the
docker CLI so no daemon is touched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_platform.sandbox import SandboxStatus, UnknownAgentError
from agent_platform.sandbox import lifecycle as lifecycle_mod
from agent_platform.sandbox import provisioner as provisioner_mod
from agent_platform.sandbox.lifecycle import Lifecycle


async def _fake_resolve_team(agent_id: str) -> str:
    if agent_id.startswith("blogging."):
        return "blogging"
    raise UnknownAgentError(f"No agent manifest for {agent_id!r}")


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    lc = Lifecycle(state_file=tmp_path / "state.json")
    lifecycle_mod.get_lifecycle.cache_clear()
    monkeypatch.setattr(lifecycle_mod, "get_lifecycle", lambda: lc)
    monkeypatch.setattr(lifecycle_mod, "_resolve_team", _fake_resolve_team)
    monkeypatch.setattr(lifecycle_mod, "_check_docker_available", lambda: None)

    monkeypatch.setattr(provisioner_mod, "run_container", AsyncMock(return_value="abc123"))
    monkeypatch.setattr(provisioner_mod, "inspect_host_port", AsyncMock(return_value=55123))
    monkeypatch.setattr(provisioner_mod, "is_running", AsyncMock(return_value=True))
    monkeypatch.setattr(provisioner_mod, "stop_container", AsyncMock())
    monkeypatch.setattr(Lifecycle, "_wait_healthy", AsyncMock())

    from unified_api.routes.sandboxes import router as sandboxes_router

    app = FastAPI()
    app.include_router(sandboxes_router)
    yield TestClient(app)


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


def test_teardown_returns_503_on_docker_error(client: TestClient, monkeypatch) -> None:
    """Regression: delete_sandbox previously had no exception handling at all,
    so a stop_container() failure (e.g. daemon unreachable) surfaced as a raw
    500 instead of a clean 503, unlike warm_sandbox's equivalent mapping."""
    resp = client.post("/api/agents/sandboxes/blogging.planner/warm")
    assert resp.status_code == 200

    monkeypatch.setattr(
        provisioner_mod, "stop_container", AsyncMock(side_effect=provisioner_mod.DockerError("daemon gone"))
    )

    resp = client.delete("/api/agents/sandboxes/blogging.planner")
    assert resp.status_code == 503


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


def test_metrics_returns_pool_counters(client: TestClient) -> None:
    resp = client.get("/api/agents/sandboxes/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resident"] == 0
    assert "by_team" in body
    assert "by_status" in body
    assert "reaper" in body
    assert "boot_ms" in body

    # Resident count reflects a warmed sandbox.
    resp = client.post("/api/agents/sandboxes/blogging.planner/warm")
    assert resp.status_code == 200
    resp = client.get("/api/agents/sandboxes/metrics")
    assert resp.json()["resident"] == 1


def test_warm_and_teardown_dispatch_through_temporal_when_enabled(client: TestClient, monkeypatch) -> None:
    """The route layer (warm_sandbox/delete_sandbox) must actually reach the
    Temporal dispatch branch when it's enabled, not just the in-process
    fallback every other test in this file exercises — sandbox_temporal_enabled()
    is False in the ambient test environment (no TEMPORAL_ADDRESS), so without
    this test the route's `if sandbox_temporal_enabled():` branch in
    dispatch.acquire_sandbox/teardown_sandbox would have no route-level
    coverage at all (the branch itself is unit-tested in test_sandbox_temporal.py,
    but never through the actual FastAPI route)."""
    from agent_platform.sandbox.state import SandboxHandle
    from agent_platform.sandbox.temporal import dispatch as sd

    handle = SandboxHandle(
        agent_id="blogging.planner",
        team="blogging",
        status=SandboxStatus.WARM,
        container_name="sbx-blogging.planner",
        host_port=55123,
    )
    monkeypatch.setattr(sd, "sandbox_temporal_enabled", lambda: True)
    acquire_mock = AsyncMock(return_value=handle)
    teardown_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(sd, "acquire_sandbox_via_temporal", acquire_mock)
    monkeypatch.setattr(sd, "teardown_sandbox_via_temporal", teardown_mock)

    resp = client.post("/api/agents/sandboxes/blogging.planner/warm")
    assert resp.status_code == 200
    assert resp.json()["agent_id"] == "blogging.planner"
    acquire_mock.assert_awaited_once_with("blogging.planner")

    resp = client.delete("/api/agents/sandboxes/blogging.planner")
    assert resp.status_code == 200
    teardown_mock.assert_awaited_once_with("blogging.planner")
