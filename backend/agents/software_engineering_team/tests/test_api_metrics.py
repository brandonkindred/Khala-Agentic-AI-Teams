"""Tests for the SE DORA metrics endpoint (GET /dora)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from software_engineering_team.api.main import app


def _client() -> TestClient:
    # No `with` block → app lifespan is not entered, keeping this a unit test
    # (no Temporal/Postgres/observer startup side effects).
    return TestClient(app)


def test_metrics_dora_default_window() -> None:
    """GET /dora defaults to a 30-day window and returns the full metrics shape."""
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
    """window_days is clamped to the [1, 365] range."""
    high = _client().get("/dora", params={"window_days": 10_000}).json()
    assert high["window_days"] == 365.0
    low = _client().get("/dora", params={"window_days": 0}).json()
    assert low["window_days"] == 1.0


def test_metrics_dora_falls_back_to_zeroed_shape_on_compute_failure(monkeypatch) -> None:
    """A failure inside compute_dora degrades to a 200 with the full zeroed shape,
    not a 500 — and the literal fallback stays in sync with DoraMetrics' fields."""
    from software_engineering_team.metrics import dora as dora_mod

    def _boom(_window: float):
        raise RuntimeError("postgres exploded")

    monkeypatch.setattr(dora_mod, "compute_dora", _boom)
    resp = _client().get("/dora", params={"window_days": 14})
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_days"] == 14.0
    # The fallback keys must match DoraMetrics exactly (guards against schema drift).
    expected_keys = set(dora_mod.DoraMetrics(window_days=14.0, computed_at="x").to_dict().keys())
    assert set(body.keys()) == expected_keys
    # All metric values are zeroed / null (the "no data" state).
    assert body["deployment_count"] == 0
    assert body["total_cost_usd"] == 0.0
    assert body["cost_by_job"] == {}
    assert body["lead_time_seconds_median"] is None
    assert body["mttr_seconds_median"] is None
