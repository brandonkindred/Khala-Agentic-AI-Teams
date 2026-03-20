"""Job service HTTP tests with repository methods mocked (no Postgres required)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("job_service.db.ensure_schema", lambda: None)
    monkeypatch.setattr("job_service.repository.health_check", lambda: True)
    monkeypatch.setattr("job_service.repository.mark_stale_failed", lambda **_: [])

    from job_service.main import app

    return TestClient(app, raise_server_exceptions=True)


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_get_job_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("job_service.repository.get_job", lambda t, j: None)
    r = client.get("/v1/jobs/acme/j1")
    assert r.status_code == 404


def test_get_job_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "job_service.repository.get_job",
        lambda t, j: {"job_id": j, "team": t, "status": "pending", "_version": 3},
    )
    r = client.get("/v1/jobs/acme/j1")
    assert r.status_code == 200
    assert r.json()["job_id"] == "j1"
    assert r.headers.get("X-Job-Version") == "3"


def test_put_create(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "job_service.repository.put_job",
        lambda team, job_id, payload, expected_version=None, unconditional=False: (True, 1),
    )
    r = client.put("/v1/jobs/acme/j1", json={"job_id": "j1", "status": "pending"})
    assert r.status_code == 200
    assert r.json()["version"] == 1


def test_put_conflict(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "job_service.repository.put_job",
        lambda team, job_id, payload, expected_version=None, unconditional=False: (False, -1),
    )
    r = client.put("/v1/jobs/acme/j1", json={"job_id": "j1"}, headers={"If-Match": "2"})
    assert r.status_code == 409


def test_heartbeat(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "job_service.repository.heartbeat", lambda t, j, expected_version=None: (True, 4)
    )
    r = client.post("/v1/jobs/acme/j1/heartbeat")
    assert r.status_code == 200
    assert r.json()["version"] == 4


def test_api_key_rejects_when_set(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_SERVICE_API_KEY", "secret")
    monkeypatch.setattr("job_service.settings.get_api_key", lambda: "secret")
    r = client.get("/v1/jobs/acme/j1")
    assert r.status_code == 401
