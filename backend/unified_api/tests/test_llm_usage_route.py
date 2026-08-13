"""Tests for GET /api/llm-usage telemetry and circuit-breaker health routes."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_service.usage_store import QUERY_FAILED_KEY
from unified_api.routes.llm_usage import router as llm_usage_router

app = FastAPI()
app.include_router(llm_usage_router)
client = TestClient(app)


def test_usage_summary_defaults_to_24h() -> None:
    with patch("unified_api.routes.llm_usage.resolve_storage_status", return_value="unconfigured"):
        resp = client.get("/api/llm-usage/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["team"] == "all"
    assert data["window"] == "24h"
    assert data["window_hours"] == 24.0
    assert data["storage_status"] == "unconfigured"
    assert data["storage_available"] is False
    assert "by_model" in data


def test_usage_summary_accepts_preset_windows() -> None:
    with patch("unified_api.routes.llm_usage.resolve_storage_status", return_value="unconfigured"):
        for window, hours in (("7d", 168.0), ("30d", 720.0), ("all", 0.0)):
            resp = client.get("/api/llm-usage/", params={"window": window, "team": "blogging"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["window"] == window
            assert data["window_hours"] == hours
            assert data["team"] == "blogging"


def test_usage_summary_rejects_unknown_window() -> None:
    resp = client.get("/api/llm-usage/", params={"window": "1h"})
    assert resp.status_code == 422


def test_usage_summary_rejects_overflowing_numeric_window() -> None:
    """Finite hours that overflow timedelta/datetime must 422, not 500."""
    resp = client.get("/api/llm-usage/", params={"window": "1e308"})
    assert resp.status_code == 422
    recent = client.get("/api/llm-usage/recent", params={"window": "1e308"})
    assert recent.status_code == 422


def test_usage_summary_accepts_numeric_hour_window() -> None:
    """Existing clients send window=1.0 (hours), not a preset id."""
    with patch("unified_api.routes.llm_usage.resolve_storage_status", return_value="unconfigured"):
        resp = client.get("/api/llm-usage/", params={"window": "1.0"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["window"] == "1.0"
    assert data["window_hours"] == 1.0


def test_recent_query_failure_returns_503() -> None:
    """A failed recent query must not look like a genuine empty history."""
    with (
        patch("unified_api.routes.llm_usage.is_postgres_enabled", return_value=True),
        patch("unified_api.routes.llm_usage.fetch_recent", return_value=None),
    ):
        resp = client.get("/api/llm-usage/recent")
    assert resp.status_code == 503


def test_recent_defaults_to_unbounded_window() -> None:
    """Omitting window must not silently drop calls older than 24h."""
    with (
        patch("unified_api.routes.llm_usage.is_postgres_enabled", return_value=True),
        patch("unified_api.routes.llm_usage.fetch_recent", return_value=[]) as fetch,
    ):
        resp = client.get("/api/llm-usage/recent")
    assert resp.status_code == 200
    fetch.assert_called_once()
    assert fetch.call_args.kwargs["window"] == "all"


def test_recent_postgres_path_returns_rows() -> None:
    rows = [
        {
            "timestamp": 1.0,
            "team": "blogging",
            "agent_key": "writer",
            "model": "m",
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "status": "success",
        }
    ]
    with (
        patch("unified_api.routes.llm_usage.is_postgres_enabled", return_value=True),
        patch("unified_api.routes.llm_usage.fetch_recent", return_value=rows),
    ):
        resp = client.get("/api/llm-usage/recent")
    assert resp.status_code == 200
    assert resp.json() == rows


def test_recent_calls_returns_list() -> None:
    with patch("unified_api.routes.llm_usage.resolve_storage_status", return_value="unconfigured"):
        resp = client.get("/api/llm-usage/recent")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_recent_calls_rejects_limit_out_of_range() -> None:
    resp = client.get("/api/llm-usage/recent", params={"limit": 0})
    assert resp.status_code == 422


def test_postgres_path_does_not_read_ring_buffer() -> None:
    from llm_service.telemetry import clear_call_log, record_llm_call

    clear_call_log()
    record_llm_call(team="blogging", agent_key="writer", model="ring", total_tokens=99)
    store_summary = {
        "team": "all",
        "window": "24h",
        "window_hours": 24.0,
        "total_calls": 1,
        "total_prompt_tokens": 1,
        "total_completion_tokens": 1,
        "total_tokens": 2,
        "avg_latency_ms": 0.0,
        "error_count": 0,
        "by_agent": {},
        "by_model": {"pg-model": {"calls": 1, "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
    }
    with (
        patch("unified_api.routes.llm_usage.is_postgres_enabled", return_value=True),
        patch("unified_api.routes.llm_usage.resolve_storage_status", return_value="available"),
        patch("unified_api.routes.llm_usage.fetch_summary", return_value=store_summary),
        patch("unified_api.routes.llm_usage.fetch_recent", return_value=[]),
    ):
        resp = client.get("/api/llm-usage/")
    data = resp.json()
    assert data["storage_available"] is True
    assert "pg-model" in data["by_model"]
    assert "ring" not in data["by_model"]


def test_usage_query_failure_reports_storage_unreachable() -> None:
    """SELECT 1 succeeding must not hide a failed llm_call_records query."""
    failed = {
        "team": "all",
        "window": "24h",
        "window_hours": 24.0,
        "total_calls": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_tokens": 0,
        "avg_latency_ms": 0.0,
        "error_count": 0,
        "by_agent": {},
        "by_model": {},
        QUERY_FAILED_KEY: True,
    }
    with (
        patch("unified_api.routes.llm_usage.is_postgres_enabled", return_value=True),
        patch("unified_api.routes.llm_usage.resolve_storage_status", return_value="available"),
        patch("unified_api.routes.llm_usage.fetch_summary", return_value=failed),
    ):
        resp = client.get("/api/llm-usage/")
    data = resp.json()
    assert resp.status_code == 200
    assert data["storage_status"] == "unreachable"
    assert data["storage_available"] is False
    assert data["total_calls"] == 0
    assert QUERY_FAILED_KEY not in data


def test_proxy_health_reports_circuit_breaker_states() -> None:
    from unified_api.team_proxy import circuit_breaker

    circuit_breaker.record_failure("_test_llm_usage_route_team")
    try:
        resp = client.get("/api/llm-usage/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "circuit_breakers" in data
        assert "_test_llm_usage_route_team" in data["circuit_breakers"]
    finally:
        circuit_breaker._circuits.pop("_test_llm_usage_route_team", None)
