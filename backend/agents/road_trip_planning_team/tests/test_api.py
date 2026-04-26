"""API tests for the Road Trip Planning team — async-only job flow.

Hits the team API which calls the real job service.  Marked integration
pending follow-up.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = [pytest.mark.integration]

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_agents_dir))

from fastapi.testclient import TestClient  # noqa: E402

from road_trip_planning_team.api import main as api_main  # noqa: E402
from road_trip_planning_team.models import TripItinerary  # noqa: E402


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    with TestClient(api_main.app) as c:
        yield c


def _sample_body() -> Dict[str, Any]:
    return {
        "trip": {
            "start_location": "San Francisco, CA",
            "required_stops": ["Yosemite"],
            "end_location": "Los Angeles, CA",
            "travelers": [
                {
                    "name": "Alice",
                    "age_group": "adult",
                    "interests": ["hiking"],
                    "needs": [],
                    "notes": "",
                }
            ],
            "trip_duration_days": 3,
            "budget_level": "moderate",
            "travel_start_date": None,
            "vehicle_type": "car",
            "preferences": [],
        }
    }


def _poll(client: TestClient, job_id: str, deadline_s: float = 5.0) -> Dict[str, Any]:
    start = time.time()
    while time.time() - start < deadline_s:
        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 200, r.text
        data = r.json()
        if data.get("status") in {"completed", "failed", "cancelled"}:
            return data
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not reach terminal state in {deadline_s}s")


def test_health(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json().get("team") == "road_trip_planning"


def test_plan_async_submit_returns_job_id(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    canned = TripItinerary(title="Test Trip", overview="ok", total_days=3)
    monkeypatch.setattr(api_main, "_run_pipeline", lambda body: canned)

    r = client.post("/plan", json=_sample_body())
    assert r.status_code == 200, r.text
    data = r.json()
    assert "job_id" in data
    assert data["status"] in {"pending", "running", "completed"}

    final = _poll(client, data["job_id"])
    assert final["status"] == "completed"
    assert final["result"]["title"] == "Test Trip"


def test_plan_requires_start_location(client: TestClient):
    body = _sample_body()
    body["trip"]["start_location"] = ""
    r = client.post("/plan", json=body)
    assert r.status_code == 400
    assert "start_location" in r.json().get("detail", "")


def test_plan_requires_travelers(client: TestClient):
    body = _sample_body()
    body["trip"]["travelers"] = []
    r = client.post("/plan", json=body)
    assert r.status_code == 400
    assert "traveler" in r.json().get("detail", "").lower()


def test_plan_failure_captured_in_job(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    def boom(_body):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(api_main, "_run_pipeline", boom)

    r = client.post("/plan", json=_sample_body())
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    final = _poll(client, job_id)
    assert final["status"] == "failed"
    assert "pipeline exploded" in (final.get("error") or "")


def test_plan_async_route_removed(client: TestClient):
    r = client.post("/plan/async", json=_sample_body())
    assert r.status_code == 404


def test_get_job_404_for_unknown_id(client: TestClient):
    r = client.get("/jobs/does-not-exist")
    assert r.status_code == 404
