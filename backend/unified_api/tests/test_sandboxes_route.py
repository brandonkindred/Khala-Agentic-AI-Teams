"""Route-level tests for /api/agents/sandboxes/*."""

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

from agent_sandbox.config import TeamSandboxConfig
from agent_sandbox.manager import SandboxManager
from agent_sandbox.models import SandboxStatus


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    # Build a fresh manager pointed at tmp_path with a single team.
    cfg = {
        "blogging": TeamSandboxConfig(
            team="blogging",
            service_name="blogging-sandbox",
            container_name="khala-sandbox-blogging",
            default_host_port=8200,
            port_env_var="BLOGGING_SANDBOX_PORT",
        )
    }
    mgr = SandboxManager(
        compose_file=tmp_path / "compose.yml",
        state_file=tmp_path / "state.json",
        configs=cfg,
    )
    # Patch compose + health so no Docker is actually touched.
    monkeypatch.setattr("agent_sandbox.manager.compose_mod.up_detached", AsyncMock())
    monkeypatch.setattr("agent_sandbox.manager.compose_mod.is_running", AsyncMock(return_value=False))
    monkeypatch.setattr("agent_sandbox.manager.compose_mod.stop_and_remove", AsyncMock())
    monkeypatch.setattr(SandboxManager, "_wait_healthy", AsyncMock())

    # Swap the module-level singleton.
    import agent_sandbox.manager as mgr_mod
    import unified_api.routes.sandboxes as routes_mod

    mgr_mod.get_manager.cache_clear()
    routes_mod.get_manager = lambda: mgr  # type: ignore[assignment]

    from unified_api.routes.sandboxes import router as sandboxes_router

    app = FastAPI()
    app.include_router(sandboxes_router)
    return TestClient(app)


def test_status_cold_for_unwarmed_team(client: TestClient) -> None:
    resp = client.get("/api/agents/sandboxes/blogging")
    assert resp.status_code == 200
    body = resp.json()
    assert body["team"] == "blogging"
    assert body["status"] == SandboxStatus.COLD


def test_status_404_for_unknown_team(client: TestClient) -> None:
    resp = client.get("/api/agents/sandboxes/no_such_team")
    assert resp.status_code == 404


def test_ensure_warm_then_list_then_teardown(client: TestClient) -> None:
    # Warm
    resp = client.post("/api/agents/sandboxes/blogging")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == SandboxStatus.WARM

    # List shows it
    resp = client.get("/api/agents/sandboxes")
    assert resp.status_code == 200
    assert {h["team"] for h in resp.json()} == {"blogging"}

    # Teardown clears it
    resp = client.delete("/api/agents/sandboxes/blogging")
    assert resp.status_code == 200
    resp = client.get("/api/agents/sandboxes")
    assert resp.json() == []


def test_ensure_warm_404_for_unknown_team(client: TestClient) -> None:
    resp = client.post("/api/agents/sandboxes/no_such_team")
    assert resp.status_code == 404
