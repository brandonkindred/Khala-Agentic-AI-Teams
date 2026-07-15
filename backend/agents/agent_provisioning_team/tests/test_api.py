"""Tests for agent_provisioning_team API endpoints."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from agent_provisioning_team.api import main as api_main
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


def test_start_provision_starts_temporal_workflow():
    """/provision dispatches to the Temporal starter returned by
    _require_provision_starter — there is no thread-executor path."""
    fake_starter = MagicMock()
    with (
        patch("agent_provisioning_team.api.main.create_job"),
        patch.object(api_main, "_require_provision_starter", return_value=fake_starter),
    ):
        resp = client.post("/provision", json={"agent_id": "test-agent-001"})

    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data and len(data["job_id"]) > 0
    fake_starter.assert_called_once()


def test_provision_returns_503_when_temporal_disabled():
    """Provisioning requires Temporal; /provision returns 503 without it."""
    with patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=False):
        resp = client.post("/provision", json={"agent_id": "test-agent-002"})

    assert resp.status_code == 503
    assert "Temporal" in resp.json()["detail"]


def test_lifespan_does_not_fail_running_jobs_on_shutdown(monkeypatch):
    """Rolling-deploy shutdown must not mark Temporal-owned jobs failed.

    Work is durable in Temporal; compensating agents / marking job-store rows
    failed here would race workflows that continue on the worker.
    """
    compensate_calls = []
    mark_failed_calls = []

    monkeypatch.setattr(
        api_main.orchestrator,
        "_compensate",
        lambda agent_id, tool_results: compensate_calls.append(agent_id),
    )
    monkeypatch.setattr(
        api_main,
        "list_jobs",
        lambda running_only=False: [{"agent_id": "stuck-agent-1", "status": "running"}],
    )
    # Symbol removed from api.main — patch job_store module to catch any regression.
    with patch(
        "agent_provisioning_team.shared.job_store.mark_all_running_jobs_failed",
        lambda reason: mark_failed_calls.append(reason),
    ):
        with TestClient(app) as _c:
            pass

    assert compensate_calls == []
    assert mark_failed_calls == []


def test_deprovision_runs_via_temporal_runner():
    """DELETE /environments/{id} dispatches through _require_deprovision_runner."""
    dump = {"agent_id": "nonexistent-agent", "success": False, "details": {}, "error": "not found"}
    fake_runner = MagicMock(return_value=dump)
    with patch.object(api_main, "_require_deprovision_runner", return_value=fake_runner):
        resp = client.delete("/environments/nonexistent-agent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False


def test_deprovision_returns_503_when_temporal_disabled():
    with patch("agent_provisioning_team.temporal.client.is_temporal_enabled", return_value=False):
        resp = client.delete("/environments/some-agent")
    assert resp.status_code == 503
    assert "Temporal" in resp.json()["detail"]


def test_cancel_job_not_found():
    with patch("agent_provisioning_team.api.main.get_job", return_value={}):
        resp = client.post("/provision/job/nonexistent/cancel")
    assert resp.status_code == 404
