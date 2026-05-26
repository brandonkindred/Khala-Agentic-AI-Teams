"""Tests for blogging API artifact endpoints.

Backed by an in-memory FakeJobServiceClient — no Postgres or live job service
required.
"""

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_blogging_root = Path(__file__).resolve().parent.parent
if str(_blogging_root) not in sys.path:
    sys.path.insert(0, str(_blogging_root))

_spec = importlib.util.spec_from_file_location(
    "blogging_api_main",
    _blogging_root / "api" / "main.py",
)
_api_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api_main)
app = _api_main.app


@pytest.fixture(autouse=True)
def _patched_blog_client(monkeypatch, fake_job_client):
    from shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "_client", lambda *a, **kw: fake_job_client)
    return fake_job_client


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def artifacts_dir(tmp_path: Path) -> Path:
    d = tmp_path / "artifacts"
    d.mkdir()
    (d / "final.md").write_text("# Final draft\n\nHello world.")
    (d / "outline.md").write_text("# Outline\n\n1. Intro\n2. Body")
    (d / "compliance_report.json").write_text('{"status": "pass"}')
    return d


def test_health_includes_brand_spec_configured(client: TestClient) -> None:
    """GET /health returns brand_spec_configured when the blogging package has a substantive brand spec file."""
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "ok"
    assert "brand_spec_configured" in data
    assert isinstance(data["brand_spec_configured"], bool)


def test_list_job_artifacts_404_when_job_missing(client: TestClient) -> None:
    """GET /job/{id}/artifacts returns 404 when job_id does not exist."""
    r = client.get(f"/job/{uuid.uuid4()}/artifacts")
    assert r.status_code == 404


def test_list_job_artifacts_404_when_no_work_dir(client: TestClient) -> None:
    """GET /job/{id}/artifacts returns 404 when job exists but has no work_dir."""
    from shared.blog_job_store import create_blog_job

    job_id = str(uuid.uuid4())
    create_blog_job(job_id, "Brief")
    r = client.get(f"/job/{job_id}/artifacts")
    assert r.status_code == 404
    detail = r.json().get("detail", "").lower()
    assert "artifact" in detail or "work_dir" in detail or "no " in detail


def test_list_job_artifacts_200_when_artifacts_exist(
    client: TestClient, artifacts_dir: Path
) -> None:
    """GET /job/{id}/artifacts returns 200 with list of existing artifact names."""
    from shared.blog_job_store import create_blog_job, update_blog_job

    job_id = str(uuid.uuid4())
    create_blog_job(job_id, "Brief")
    update_blog_job(job_id, work_dir=str(artifacts_dir))
    r = client.get(f"/job/{job_id}/artifacts")
    assert r.status_code == 200
    data = r.json()
    assert "artifacts" in data
    artifacts = data["artifacts"]
    assert isinstance(artifacts, list)
    names = [a["name"] for a in artifacts]
    assert "final.md" in names
    assert "outline.md" in names
    assert "compliance_report.json" in names
    final_meta = next((a for a in artifacts if a["name"] == "final.md"), None)
    assert final_meta is not None
    assert "producer_phase" in final_meta or "producer_agent" in final_meta


def test_get_job_artifact_content_404_invalid_name(client: TestClient, artifacts_dir: Path) -> None:
    """GET /job/{id}/artifacts/{name} returns 404 when artifact_name is not in ARTIFACT_NAMES."""
    from shared.blog_job_store import create_blog_job, update_blog_job

    job_id = str(uuid.uuid4())
    create_blog_job(job_id, "Brief")
    update_blog_job(job_id, work_dir=str(artifacts_dir))
    r = client.get(f"/job/{job_id}/artifacts/../etc/passwd")
    assert r.status_code == 404
    r2 = client.get(f"/job/{job_id}/artifacts/unknown_file.txt")
    assert r2.status_code == 404


def test_get_job_artifact_content_200(client: TestClient, artifacts_dir: Path) -> None:
    """GET /job/{id}/artifacts/{name} returns 200 with { name, content } for valid artifact."""
    from shared.blog_job_store import create_blog_job, update_blog_job

    job_id = str(uuid.uuid4())
    create_blog_job(job_id, "Brief")
    update_blog_job(job_id, work_dir=str(artifacts_dir))
    r = client.get(f"/job/{job_id}/artifacts/final.md")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "final.md"
    assert "Final draft" in data["content"]
    assert "Hello world" in data["content"]

    r2 = client.get(f"/job/{job_id}/artifacts/compliance_report.json")
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["name"] == "compliance_report.json"
    assert data2["content"] == {"status": "pass"}


def test_get_job_artifact_download_returns_attachment(
    client: TestClient, artifacts_dir: Path
) -> None:
    """GET /job/{id}/artifacts/{name}?download=true returns Content-Disposition attachment with filename."""
    from shared.blog_job_store import create_blog_job, update_blog_job

    job_id = str(uuid.uuid4())
    create_blog_job(job_id, "Brief")
    update_blog_job(job_id, work_dir=str(artifacts_dir))
    r = client.get(f"/job/{job_id}/artifacts/final.md", params={"download": True})
    assert r.status_code == 200
    assert "content-disposition" in r.headers
    assert "attachment" in r.headers["content-disposition"].lower()
    assert "final.md" in r.headers["content-disposition"]


def test_approve_job_400_when_not_terminal(client: TestClient) -> None:
    """POST /job/{id}/approve returns 400 when job status is not completed or needs_human_review."""
    from shared.blog_job_store import create_blog_job

    job_id = str(uuid.uuid4())
    create_blog_job(job_id, "Brief")
    r = client.post(f"/job/{job_id}/approve")
    assert r.status_code == 400


def test_approve_job_200_and_includes_approved_at(client: TestClient) -> None:
    """POST /job/{id}/approve returns 200 and response includes approved_at when job is completed."""
    from shared.blog_job_store import create_blog_job, update_blog_job

    job_id = str(uuid.uuid4())
    create_blog_job(job_id, "Brief")
    update_blog_job(job_id, status="completed")
    r = client.post(f"/job/{job_id}/approve")
    assert r.status_code == 200
    data = r.json()
    assert "approved_at" in data
    assert data["approved_at"]


def test_unapprove_job_200(client: TestClient) -> None:
    """POST /job/{id}/unapprove returns 200 and clears approved_at."""
    from shared.blog_job_store import approve_blog_job, create_blog_job, update_blog_job

    job_id = str(uuid.uuid4())
    create_blog_job(job_id, "Brief")
    update_blog_job(job_id, status="completed")
    approve_blog_job(job_id)
    r = client.post(f"/job/{job_id}/unapprove")
    assert r.status_code == 200
    data = r.json()
    assert data.get("approved_at") is None
