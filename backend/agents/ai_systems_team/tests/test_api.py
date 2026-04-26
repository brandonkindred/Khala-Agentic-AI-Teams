"""Tests for ai_systems_team API endpoints.

Hits the team API which calls the real job service.  Marked integration
pending follow-up to mock the team's ``_client`` factory.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from ai_systems_team.api.main import app

pytestmark = [pytest.mark.integration]

client = TestClient(app)


def test_health_endpoint():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"


def test_root_endpoint():
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert "AI Systems" in data.get("service", "")


def test_list_jobs_empty(tmp_path):
    with patch("ai_systems_team.api.main.list_jobs", return_value=[]):
        resp = client.get("/build/jobs")
    assert resp.status_code == 200
    assert resp.json()["jobs"] == []


def test_list_blueprints_empty():
    resp = client.get("/blueprints")
    assert resp.status_code == 200
    assert resp.json()["blueprints"] == []


def test_get_job_status_not_found():
    with patch("ai_systems_team.api.main.get_job", return_value={}):
        resp = client.get("/build/status/nonexistent-job")
    assert resp.status_code == 404


def test_cancel_missing_job_returns_404():
    with patch("ai_systems_team.api.main.get_job", return_value={}):
        resp = client.post("/build/job/nonexistent/cancel")
    assert resp.status_code == 404


def test_delete_missing_job_returns_404():
    with patch("ai_systems_team.api.main.get_job", return_value={}):
        resp = client.delete("/build/job/nonexistent")
    assert resp.status_code == 404


def test_start_build_returns_job_id():
    with (
        patch("ai_systems_team.api.main.create_job"),
        patch("ai_systems_team.api.main.mark_job_running"),
        patch("threading.Thread") as mock_thread,
    ):
        mock_thread.return_value.start = lambda: None
        resp = client.post(
            "/build",
            json={"project_name": "test_proj", "spec_path": "/tmp/spec.md"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data
    assert len(data["job_id"]) > 0


def test_get_blueprint_not_found():
    resp = client.get("/blueprints/nonexistent")
    assert resp.status_code == 404
