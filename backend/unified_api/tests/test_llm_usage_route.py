"""Tests for GET /api/llm-usage telemetry and circuit-breaker health routes."""

from __future__ import annotations

import sys
from pathlib import Path

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unified_api.routes.llm_usage import router as llm_usage_router

app = FastAPI()
app.include_router(llm_usage_router)
client = TestClient(app)


def test_usage_summary_returns_200_with_defaults() -> None:
    """GET /api/llm-usage/ returns a summary dict for the default all-teams/24h window."""
    resp = client.get("/api/llm-usage/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["team"] == "all"
    assert data["window_hours"] == 24.0


def test_usage_summary_accepts_team_and_window_query_params() -> None:
    """GET /api/llm-usage/ narrows the summary by the team and window query params."""
    resp = client.get("/api/llm-usage/", params={"team": "blogging", "window": 1.0})
    assert resp.status_code == 200
    data = resp.json()
    assert data["team"] == "blogging"
    assert data["window_hours"] == 1.0


def test_recent_calls_returns_empty_list_by_default() -> None:
    """GET /api/llm-usage/recent returns a list (empty when no calls have been logged)."""
    resp = client.get("/api/llm-usage/recent")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_recent_calls_accepts_team_and_limit_query_params() -> None:
    """GET /api/llm-usage/recent accepts team and limit filters without erroring."""
    resp = client.get("/api/llm-usage/recent", params={"team": "blogging", "limit": 5})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_recent_calls_rejects_limit_out_of_range() -> None:
    """GET /api/llm-usage/recent rejects a limit outside the declared 1..1000 bounds."""
    resp = client.get("/api/llm-usage/recent", params={"limit": 0})
    assert resp.status_code == 422


def test_proxy_health_reports_circuit_breaker_states() -> None:
    """GET /api/llm-usage/health returns the shared circuit breaker's state snapshot."""
    from unified_api.team_proxy import circuit_breaker

    circuit_breaker.record_failure("_test_llm_usage_route_team")
    try:
        resp = client.get("/api/llm-usage/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "circuit_breakers" in data
        assert data["circuit_breakers"] == circuit_breaker.get_all_states()
        assert "_test_llm_usage_route_team" in data["circuit_breakers"]
    finally:
        circuit_breaker._circuits.pop("_test_llm_usage_route_team", None)
