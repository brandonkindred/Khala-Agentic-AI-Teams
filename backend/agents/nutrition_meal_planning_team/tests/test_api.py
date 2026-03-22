"""API tests for Nutrition & Meal Planning team (profile, plan, meals, feedback, history)."""

from pathlib import Path

import pytest

# Ensure agents dir is on path
_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_agents_dir))

from fastapi.testclient import TestClient  # noqa: E402

from nutrition_meal_planning_team.api.main import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def temp_cache(tmp_path, monkeypatch):
    """Use temp dir for AGENT_CACHE so profile and meal stores don't persist."""
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    return tmp_path


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json().get("team") == "nutrition_meal_planning"


def test_get_profile_404(client, temp_cache):
    r = client.get("/profile/nonexistent")
    assert r.status_code == 404


def test_put_profile_creates_and_returns(client, temp_cache):
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


def test_get_profile_after_put(client, temp_cache):
    client.put("/profile/c2", json={"household": {"number_of_people": 2, "description": "couple", "ages_if_relevant": []}})
    r = client.get("/profile/c2")
    assert r.status_code == 200
    assert r.json().get("household", {}).get("number_of_people") == 2


def test_post_plan_nutrition_404(client, temp_cache):
    r = client.post("/plan/nutrition", json={"client_id": "nonexistent"})
    assert r.status_code == 404


def test_post_plan_nutrition_success(client, temp_cache):
    client.put("/profile/p1", json={"household": {"number_of_people": 1, "description": "solo", "ages_if_relevant": []}})
    r = client.post("/plan/nutrition", json={"client_id": "p1"})
    assert r.status_code == 200
    data = r.json()
    assert data.get("client_id") == "p1"
    assert "plan" in data


def test_post_plan_meals_404(client, temp_cache):
    r = client.post("/plan/meals", json={"client_id": "nonexistent"})
    assert r.status_code == 404


def test_post_feedback_recorded(client, temp_cache):
    # Create profile and get a meal plan to have a recommendation_id
    client.put("/profile/fb1", json={"household": {"number_of_people": 1, "description": "solo", "ages_if_relevant": []}})
    r_meals = client.post("/plan/meals", json={"client_id": "fb1", "period_days": 1})
    assert r_meals.status_code == 200
    suggestions = r_meals.json().get("suggestions", [])
    if not suggestions:
        pytest.skip("No suggestions returned (LLM may be unavailable)")
    rec_id = suggestions[0].get("recommendation_id")
    r = client.post("/feedback", json={"client_id": "fb1", "recommendation_id": rec_id, "rating": 5, "would_make_again": True})
    assert r.status_code == 200
    assert r.json().get("recorded") is True


def test_get_history_meals_400(client):
    r = client.get("/history/meals")
    assert r.status_code == 400


def test_get_history_meals_empty(client, temp_cache):
    r = client.get("/history/meals?client_id=hist1")
    assert r.status_code == 200
    assert r.json().get("entries") == []


def test_get_job_404(client):
    r = client.get("/jobs/nonexistent-job-id")
    assert r.status_code == 404
