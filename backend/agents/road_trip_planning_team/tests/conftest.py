"""Test fixtures for the Road Trip Planning team.

Routes the team's ``job_store`` through the shared in-memory fake so the unit
tests (dispatch branch, Temporal activity) exercise the FastAPI app and the
activity end-to-end without the real job service or Postgres. Integration-marked
tests (``test_api.py``) keep using the real in-process job service, so the fake
is not installed for them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _sample_trip_dict() -> dict:
    """Canonical ``PlanTripRequest`` payload shared by the team's tests.

    One source of truth so a change to ``PlanTripRequest``'s required fields is
    edited once, not in every test module.
    """
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
            "trip_duration_days": 2,
            "budget_level": "moderate",
            "travel_start_date": None,
            "vehicle_type": "car",
            "preferences": [],
        }
    }


@pytest.fixture
def sample_trip_body() -> dict:
    """A fresh canonical trip-request dict (safe to mutate within a test)."""
    return _sample_trip_dict()


@pytest.fixture
def sample_plan_request():
    """The canonical trip request as a validated ``PlanTripRequest``."""
    from road_trip_planning_team.models import PlanTripRequest

    return PlanTripRequest(**_sample_trip_dict())


@pytest.fixture(autouse=True)
def _patched_road_trip_job_client(request, monkeypatch, fake_job_client):
    """Route the team's job_store ``_client`` factory through the in-memory fake.

    A no-op for ``@pytest.mark.integration`` tests, which run against the real
    in-process job service. Clears the module-level singleton cache so a real
    client cached at import time can't leak in.
    """
    if request.node.get_closest_marker("integration"):
        return None

    from road_trip_planning_team.shared import job_store as js

    monkeypatch.setattr(js, "_client_instance", None, raising=False)
    monkeypatch.setattr(js, "_client", lambda *a, **kw: fake_job_client)
    return fake_job_client
