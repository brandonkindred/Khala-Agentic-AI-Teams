"""API tests for Nutrition & Meal Planning team (profile, plan, meals, feedback, history).

Hits the team API which calls the real job service.  Marked integration
pending follow-up.
"""

import time
from pathlib import Path
from typing import Any, Dict

import pytest

from shared_postgres import is_postgres_enabled

pytestmark = [pytest.mark.integration]

# Ensure agents dir is on path
_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_agents_dir))

from fastapi.testclient import TestClient  # noqa: E402

from nutrition_meal_planning_team.api.main import app  # noqa: E402


def _poll_job(client: TestClient, job_id: str, deadline_s: float = 10.0) -> Dict[str, Any]:
    start = time.time()
    while time.time() - start < deadline_s:
        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 200, r.text
        data = r.json()
        if data.get("status") in {"completed", "failed", "cancelled"}:
            return data
        time.sleep(0.05)
    raise AssertionError(f"Nutrition job {job_id} did not terminate in {deadline_s}s")


@pytest.fixture
def client():
    # Use ``TestClient`` as a context manager so Starlette runs the
    # lifespan (schema registration) before any request hits the app.
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json().get("team") == "nutrition_meal_planning"


@pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="Postgres-backed profile store required (set POSTGRES_HOST).",
)
def test_get_profile_404(client):
    r = client.get("/profile/nonexistent")
    assert r.status_code == 404


@pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="Postgres-backed profile store required (set POSTGRES_HOST).",
)
def test_put_profile_creates_and_returns(client):
    r = client.put(
        "/profile/client1",
        json={
            "household": {"number_of_people": 1, "description": "solo", "ages_if_relevant": []},
            "dietary_needs": ["vegetarian"],
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("client_id") == "client1"
    assert "vegetarian" in data.get("dietary_needs", [])


@pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="Postgres-backed profile store required (set POSTGRES_HOST).",
)
def test_get_profile_after_put(client):
    client.put(
        "/profile/c2",
        json={
            "household": {"number_of_people": 2, "description": "couple", "ages_if_relevant": []}
        },
    )
    r = client.get("/profile/c2")
    assert r.status_code == 200
    assert r.json().get("household", {}).get("number_of_people") == 2


@pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="Postgres-backed profile store required (set POSTGRES_HOST).",
)
def test_post_plan_nutrition_unknown_profile_fails_job(client):
    r = client.post("/plan/nutrition", json={"client_id": "nonexistent"})
    assert r.status_code == 200
    final = _poll_job(client, r.json()["job_id"])
    assert final["status"] == "failed"
    assert final.get("not_found") is True


@pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="Postgres-backed profile store required (set POSTGRES_HOST).",
)
def test_post_plan_nutrition_success(client):
    client.put(
        "/profile/p1",
        json={"household": {"number_of_people": 1, "description": "solo", "ages_if_relevant": []}},
    )
    r = client.post("/plan/nutrition", json={"client_id": "p1"})
    assert r.status_code == 200
    final = _poll_job(client, r.json()["job_id"])
    assert final["status"] == "completed"
    result = final["result"]
    assert result.get("client_id") == "p1"
    assert "plan" in result


@pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="Postgres-backed profile store required (set POSTGRES_HOST).",
)
def test_post_plan_meals_unknown_profile_fails_job(client):
    r = client.post("/plan/meals", json={"client_id": "nonexistent"})
    assert r.status_code == 200
    final = _poll_job(client, r.json()["job_id"])
    assert final["status"] == "failed"
    assert final.get("not_found") is True


@pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="Postgres-backed stores required (set POSTGRES_HOST).",
)
def test_post_feedback_recorded(client):
    # Create profile and get a meal plan to have a recommendation_id
    client.put(
        "/profile/fb1",
        json={"household": {"number_of_people": 1, "description": "solo", "ages_if_relevant": []}},
    )
    r_meals = client.post("/plan/meals", json={"client_id": "fb1", "period_days": 1})
    assert r_meals.status_code == 200
    final = _poll_job(client, r_meals.json()["job_id"])
    if final["status"] != "completed":
        pytest.skip(f"Meal plan job did not complete: {final.get('error')}")
    suggestions = (final.get("result") or {}).get("suggestions", [])
    if not suggestions:
        pytest.skip("No suggestions returned (LLM may be unavailable)")
    rec_id = suggestions[0].get("recommendation_id")
    r = client.post(
        "/feedback",
        json={
            "client_id": "fb1",
            "recommendation_id": rec_id,
            "rating": 5,
            "would_make_again": True,
        },
    )
    assert r.status_code == 200
    assert r.json().get("recorded") is True


def test_get_history_meals_400(client):
    r = client.get("/history/meals")
    assert r.status_code == 400


@pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="Postgres-backed meal feedback store required (set POSTGRES_HOST).",
)
def test_get_history_meals_empty(client):
    r = client.get("/history/meals?client_id=hist1")
    assert r.status_code == 200
    assert r.json().get("entries") == []


def test_get_job_404(client):
    r = client.get("/jobs/nonexistent-job-id")
    assert r.status_code == 404
