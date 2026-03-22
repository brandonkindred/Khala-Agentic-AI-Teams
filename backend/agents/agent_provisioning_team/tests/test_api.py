"""Tests for agent_provisioning_team API endpoints."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_provisioning_team.api.main import app

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
    assert "service" in data


def test_list_jobs_empty():
    with patch("agent_provisioning_team.api.main.list_jobs", return_value=[]):
        resp = client.get("/provision/jobs")
    assert resp.status_code == 200
    assert resp.json()["jobs"] == []


def test_get_status_not_found():
    with patch("agent_provisioning_team.api.main.get_job", return_value={}):
        resp = client.get("/provision/status/nonexistent-job")
    assert resp.status_code == 404


def test_start_provision_returns_job_id():
    with (
        patch("agent_provisioning_team.api.main.create_job"),
        patch("threading.Thread") as mock_thread,
    ):
        mock_thread.return_value.start = lambda: None
        resp = client.post(
            "/provision",
            json={"agent_id": "test-agent-001"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data
    assert len(data["job_id"]) > 0


def test_deprovision_runs_via_orchestrator():
    from agent_provisioning_team.models import DeprovisionResponse
    mock_resp = DeprovisionResponse(agent_id="nonexistent-agent", success=False, error="not found")
    with patch("agent_provisioning_team.api.main.orchestrator") as mock_orch:
        mock_orch.deprovision.return_value = mock_resp
        resp = client.delete("/environments/nonexistent-agent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False


def test_cancel_job_not_found():
    with patch("agent_provisioning_team.api.main.get_job", return_value={}):
        resp = client.post("/provision/job/nonexistent/cancel")
    assert resp.status_code == 404
