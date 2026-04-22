"""API-level tests for the /api/agents router.

These tests isolate from the on-disk manifest set by monkeypatching the
registry singleton to a fixture-built instance.
"""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_registry import loader
from agent_registry.loader import AgentRegistry
from unified_api.routes.agents import router as agents_router


def _write(dir_: Path, team: str, filename: str, body: str) -> None:
    d = dir_ / team / "agent_console" / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(dedent(body).lstrip(), encoding="utf-8")


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    _write(
        tmp_path,
        "blogging",
        "planner.yaml",
        """
        schema_version: 1
        id: blogging.planner
        team: blogging
        name: Planner
        summary: Plans posts
        tags: [planning]
        inputs:
          schema_ref: agent_registry.models:AgentSummary
        source:
          entrypoint: x:y
        """,
    )
    _write(
        tmp_path,
        "branding",
        "a.yaml",
        """
        schema_version: 1
        id: branding.auditor
        team: branding
        name: Auditor
        summary: Audits brand
        source:
          entrypoint: x:y
        """,
    )
    # Replace the cached singleton with one that scans the tmp dir.
    loader.get_registry.cache_clear()
    rebuilt = AgentRegistry.load(tmp_path)
    loader.get_registry.cache_clear()
    original = loader.get_registry
    loader.get_registry = lambda: rebuilt  # type: ignore[assignment]

    # Rebind the agents router's reference as well so it picks the patched fn.
    import unified_api.routes.agents as agents_route_mod

    agents_route_mod.get_registry = lambda: rebuilt  # type: ignore[assignment]

    app = FastAPI()
    app.include_router(agents_router)
    try:
        yield TestClient(app)
    finally:
        loader.get_registry = original  # type: ignore[assignment]
        agents_route_mod.get_registry = original  # type: ignore[assignment]
        loader.get_registry.cache_clear()


def test_list_agents(client: TestClient) -> None:
    resp = client.get("/api/agents")
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()}
    assert ids == {"blogging.planner", "branding.auditor"}


def test_list_agents_filters(client: TestClient) -> None:
    resp = client.get("/api/agents", params={"team": "blogging"})
    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()] == ["blogging.planner"]

    resp = client.get("/api/agents", params={"q": "audits"})
    assert [item["id"] for item in resp.json()] == ["branding.auditor"]


def test_list_teams(client: TestClient) -> None:
    resp = client.get("/api/agents/teams")
    assert resp.status_code == 200
    teams = {t["team"]: t["agent_count"] for t in resp.json()}
    assert teams == {"blogging": 1, "branding": 1}


def test_get_agent_detail(client: TestClient) -> None:
    resp = client.get("/api/agents/blogging.planner")
    assert resp.status_code == 200
    body = resp.json()
    assert body["manifest"]["id"] == "blogging.planner"
    assert body["manifest"]["name"] == "Planner"


def test_get_agent_unknown_is_404(client: TestClient) -> None:
    resp = client.get("/api/agents/does.not.exist")
    assert resp.status_code == 404


def test_schema_input_resolves_when_ref_exists(client: TestClient) -> None:
    resp = client.get("/api/agents/blogging.planner/schema/input")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "object"
    assert "id" in body["properties"]


def test_schema_input_404_when_missing_ref(client: TestClient) -> None:
    resp = client.get("/api/agents/branding.auditor/schema/input")
    assert resp.status_code == 404


def test_schema_output_404_when_missing_ref(client: TestClient) -> None:
    resp = client.get("/api/agents/blogging.planner/schema/output")
    assert resp.status_code == 404


def test_invoke_oversized_body_returns_413_without_acquiring_sandbox(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for issue #256: the payload cap must fire before any sandbox work."""
    import unified_api.routes.agents as agents_route_mod

    async def _fail_acquire(agent_id: str):  # pragma: no cover — must not run
        raise AssertionError(f"acquire({agent_id!r}) must not be called on oversized body")

    monkeypatch.setattr(agents_route_mod, "acquire", _fail_acquire)
    monkeypatch.setenv("AGENT_INVOKE_MAX_PAYLOAD_BYTES", "1024")

    payload = "x" * 4096
    resp = client.post(
        "/api/agents/blogging.planner/invoke",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413
