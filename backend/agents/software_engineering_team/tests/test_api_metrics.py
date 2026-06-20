"""Tests for the SE DORA metrics endpoint (GET /dora)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from software_engineering_team.api.main import app


def _client() -> TestClient:
    # No `with` block → app lifespan is not entered, keeping this a unit test
    # (no Temporal/Postgres/observer startup side effects).
    return TestClient(app)


def test_metrics_dora_default_window() -> None:
    resp = _client().get("/dora")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_days"] == 30.0
    # Without Postgres the metrics are all zero but the shape is complete.
    for key in (
        "deployment_count",
        "deployment_frequency_per_day",
        "change_failure_rate",
        "merged_count",
        "total_cost_usd",
        "cost_by_job",
        "computed_at",
    ):
        assert key in body


def test_metrics_dora_window_is_clamped() -> None:
    high = _client().get("/dora", params={"window_days": 10_000}).json()
    assert high["window_days"] == 365.0
    low = _client().get("/dora", params={"window_days": 0}).json()
    assert low["window_days"] == 1.0
