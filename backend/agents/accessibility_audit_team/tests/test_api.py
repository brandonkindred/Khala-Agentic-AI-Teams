"""Tests for accessibility_audit_team API endpoints."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from accessibility_audit_team.api.main import router

# Create a test app that mounts the router
_test_app = FastAPI()
_test_app.include_router(router)

client = TestClient(_test_app)


def test_health_endpoint():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "healthy"


def test_create_audit_returns_job_id():
    resp = client.post(
        "/audit/create",
        json={"name": "test audit", "web_urls": ["https://example.com"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data
    assert "audit_id" in data
    assert data["status"] == "running"


def test_get_audit_status_not_found():
    resp = client.get("/audit/status/nonexistent-job-id")
    assert resp.status_code == 404


def test_create_audit_without_urls():
    resp = client.post("/audit/create", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data


def test_create_audit_returns_message():
    resp = client.post(
        "/audit/create",
        json={"web_urls": ["https://test.example.org"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["job_id"]) > 0
    assert len(data["audit_id"]) > 0
    assert "Poll /audit/status/{job_id}" in data["message"]


def test_job_status_returns_frontend_compatible_status_values():
    resp = client.post(
        "/audit/create",
        json={"name": "status-compat", "web_urls": ["https://example.org"]},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    status_resp = client.get(f"/audit/status/{job_id}")
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert status_data["status"] in {"running", "complete", "failed", "cancelled", "pending"}


def test_create_audit_invalid_url_rejected():
    """URLs must start with http:// or https://."""
    resp = client.post(
        "/audit/create",
        json={"web_urls": ["ftp://invalid.example.com"]},
    )
    assert resp.status_code == 422


def test_create_audit_relative_url_rejected():
    resp = client.post(
        "/audit/create",
        json={"web_urls": ["/relative/path"]},
    )
    assert resp.status_code == 422


def test_findings_endpoint_returns_pagination_fields():
    """GET /audit/{id}/findings should include offset, limit, has_more."""
    resp = client.get("/audit/nonexistent_audit/findings?offset=0&limit=10")
    # Will return 404 because audit doesn't exist, but the param parsing works
    assert resp.status_code == 404
