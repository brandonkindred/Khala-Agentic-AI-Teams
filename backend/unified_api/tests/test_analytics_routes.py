"""Route-level tests for /api/analytics (team scorecard + signal history)."""

from __future__ import annotations

import sys
from pathlib import Path

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import unified_api.routes.analytics as routes_mod


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(routes_mod.router)
    return TestClient(app)


def test_team_scorecard_returns_dict_for_unknown_team(client: TestClient) -> None:
    resp = client.get("/api/analytics/team/some_team/scorecard")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


def test_team_scorecard_accepts_window_override(client: TestClient) -> None:
    resp = client.get("/api/analytics/team/some_team/scorecard", params={"window": 1.5})
    assert resp.status_code == 200


def test_signal_history_defaults(client: TestClient) -> None:
    resp = client.get("/api/analytics/signals")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_signal_history_filters_by_team_and_valid_signal_type(client: TestClient) -> None:
    resp = client.get(
        "/api/analytics/signals",
        params={"team": "software_engineering", "signal_type": "code_review.passed", "window": 12, "limit": 5},
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_signal_history_ignores_invalid_signal_type(client: TestClient) -> None:
    """An unrecognized signal_type value is silently ignored (falls back to no filter)."""
    resp = client.get("/api/analytics/signals", params={"signal_type": "not_a_real_signal_type"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
