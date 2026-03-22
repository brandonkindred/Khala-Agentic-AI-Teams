"""Tests for SOC2 audit API."""

from pathlib import Path

from fastapi.testclient import TestClient

from soc2_compliance_team.api.main import app

client = TestClient(app)


def test_health() -> None:
    """Health endpoint returns ok."""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_run_audit_requires_repo_path() -> None:
    """POST without repo_path or with invalid path returns 400."""
    r = client.post("/soc2-audit/run", json={})
    assert r.status_code == 422  # validation error

    r = client.post(
        "/soc2-audit/run",
        json={"repo_path": "/nonexistent/path/12345"},
    )
    assert r.status_code == 400


def test_run_audit_and_poll(tmp_path: Path) -> None:
    """POST with valid path returns job_id; status endpoint returns 404 for unknown job."""
    (tmp_path / "file.txt").write_text("x")
    r = client.post("/soc2-audit/run", json={"repo_path": str(tmp_path)})
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
    assert data["status"] == "running"

    r = client.get(f"/soc2-audit/status/{data['job_id']}")
    assert r.status_code == 200
    # Job may still be running or already completed depending on timing
    assert r.json()["status"] in ("pending", "running", "completed", "failed")


def test_status_404() -> None:
    """GET status for unknown job_id returns 404."""
    r = client.get("/soc2-audit/status/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
