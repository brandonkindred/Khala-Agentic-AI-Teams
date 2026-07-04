"""Tests for the ``POST /plan`` Temporal-vs-thread dispatch branch.

With ``TEMPORAL_ADDRESS`` unset ``is_temporal_enabled()`` is False, so the
existing ``test_api.py`` cases already cover the thread path end-to-end. These
tests cover the Temporal branch (patched enabled) and the dispatch-failure
handling, without needing a running Temporal server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_agents_dir))

from fastapi.testclient import TestClient  # noqa: E402

from road_trip_planning_team import pipeline as rtp_pipeline  # noqa: E402
from road_trip_planning_team.api import main as api_main  # noqa: E402


@pytest.fixture
def client():
    with TestClient(api_main.app) as c:
        yield c


def _sample_body() -> Dict[str, Any]:
    return {
        "trip": {
            "start_location": "San Francisco, CA",
            "required_stops": ["Yosemite"],
            "end_location": "Los Angeles, CA",
            "travelers": [{"name": "Alice", "age_group": "adult", "interests": ["hiking"]}],
            "trip_duration_days": 3,
            "budget_level": "moderate",
            "vehicle_type": "car",
            "preferences": [],
        }
    }


def test_plan_dispatches_to_temporal_when_enabled(client, monkeypatch):
    # The dispatch helper imports both names lazily from their live modules.
    # Patch via string paths so the patch targets whatever module object
    # sys.modules currently holds.
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)

    captured: dict = {}
    monkeypatch.setattr(
        "road_trip_planning_team.temporal.start_workflow.start_road_trip_workflow",
        lambda job_id, request: captured.update(job_id=job_id, request=request),
    )

    def _no_thread(*_a, **_k):  # pragma: no cover - asserts the thread path is skipped
        raise AssertionError("thread path must not run when Temporal is enabled")

    monkeypatch.setattr(api_main.threading, "Thread", _no_thread)

    response = client.post("/plan", json=_sample_body())

    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    assert captured["job_id"] == job_id
    assert captured["request"]["trip"]["start_location"] == "San Francisco, CA"


def test_plan_marks_job_failed_when_dispatch_raises(client, monkeypatch):
    """A dispatch failure (e.g. Temporal worker client never connected) must
    leave the job in a terminal FAILED state, not orphaned in PENDING."""
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)

    def _boom(job_id, request):
        raise RuntimeError("worker client not available")

    monkeypatch.setattr(
        "road_trip_planning_team.temporal.start_workflow.start_road_trip_workflow", _boom
    )

    response = client.post("/plan", json=_sample_body())

    assert response.status_code == 500
    assert "Failed to start road trip planning run" in response.json().get("detail", "")


def test_dispatch_helper_returns_thread_label_when_disabled(monkeypatch):
    """Direct unit check of the helper's thread fallback and its label."""
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: False)

    started: dict = {}

    class _FakeThread:
        def __init__(self, *, target, args, daemon):
            started["target"] = target
            started["args"] = args
            started["daemon"] = daemon

        def start(self):
            started["started"] = True

    monkeypatch.setattr(api_main.threading, "Thread", _FakeThread)

    from road_trip_planning_team.models import PlanTripRequest

    body = PlanTripRequest(**_sample_body())
    label = api_main._dispatch_plan_run("job-thread", body)

    assert label == "thread"
    assert started["started"] is True
    assert started["daemon"] is True
    assert started["target"] is rtp_pipeline.run_plan_background
    assert started["args"] == ("job-thread", body)
