"""More coverage for api/main.py — focuses on routes not covered by test_api.py."""

from __future__ import annotations

import importlib.util
import io
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))
_spec = importlib.util.spec_from_file_location(
    "software_engineering_api_main_more",
    _team_dir / "api" / "main.py",
)
_api_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api_main)
app = _api_main.app


@pytest.fixture(autouse=True)
def _autouse_patched_job_store(patched_job_store):
    return patched_job_store


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# /run-team/upload
# ---------------------------------------------------------------------------


def test_run_team_upload_oversized(client: TestClient):
    """A >5MB file is rejected with 413."""
    big = b"x" * (5 * 1024 * 1024 + 100)
    r = client.post(
        "/run-team/upload",
        data={"project_name": "Foo"},
        files={"spec_file": ("spec.md", io.BytesIO(big), "text/markdown")},
    )
    assert r.status_code == 413


def test_run_team_upload_non_utf8(client: TestClient):
    """Non-UTF-8 file is rejected with 422."""
    bad = b"\xff\xfe not utf8"
    r = client.post(
        "/run-team/upload",
        data={"project_name": "Foo"},
        files={"spec_file": ("spec.md", io.BytesIO(bad), "text/markdown")},
    )
    assert r.status_code == 422


def test_run_team_upload_empty_project_name(client: TestClient):
    r = client.post(
        "/run-team/upload",
        data={"project_name": ""},
        files={"spec_file": ("spec.md", io.BytesIO(b"# x"), "text/markdown")},
    )
    assert r.status_code == 422  # min_length=1


# ---------------------------------------------------------------------------
# /run-team/{job_id}/retry-failed
# ---------------------------------------------------------------------------


def test_retry_failed_unknown_job(client: TestClient):
    r = client.post(f"/run-team/{uuid.uuid4()}/retry-failed")
    assert r.status_code == 404


def test_retry_failed_when_running(client: TestClient, tmp_path: Path):
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(tmp_path), job_type="run_team")
    update_job(job_id, status="running")
    r = client.post(f"/run-team/{job_id}/retry-failed")
    assert r.status_code == 409


def test_retry_failed_no_failed_tasks(client: TestClient, tmp_path: Path):
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(tmp_path), job_type="run_team")
    update_job(job_id, status="completed")
    r = client.post(f"/run-team/{job_id}/retry-failed")
    assert r.status_code in (200, 400)


# ---------------------------------------------------------------------------
# /run-team/{job_id}/cancel
# ---------------------------------------------------------------------------


def test_cancel_unknown_job(client: TestClient):
    r = client.post(f"/run-team/{uuid.uuid4()}/cancel")
    assert r.status_code == 404


def test_cancel_running_job(client: TestClient, tmp_path: Path):
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(tmp_path), job_type="run_team")
    update_job(job_id, status="running")
    r = client.post(f"/run-team/{job_id}/cancel")
    assert r.status_code == 200


def test_cancel_terminal_job(client: TestClient, tmp_path: Path):
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(tmp_path), job_type="run_team")
    update_job(job_id, status="completed")
    r = client.post(f"/run-team/{job_id}/cancel")
    assert r.status_code == 400


def test_cancel_already_complete_job_is_terminal(client: TestClient, tmp_path: Path):
    """already_complete is a terminal success (the coding team's "work already done") — cancelling it
    must be refused like completed, not flip a finished job to cancelled."""
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(tmp_path), job_type="run_team")
    update_job(job_id, status="already_complete")
    r = client.post(f"/run-team/{job_id}/cancel")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /run-team/{job_id}/answers
# ---------------------------------------------------------------------------


def test_submit_answers_unknown_job(client: TestClient):
    r = client.post(f"/run-team/{uuid.uuid4()}/answers", json={"answers": []})
    assert r.status_code == 404


def test_submit_answers_not_waiting(client: TestClient, tmp_path: Path):
    from software_engineering_team.shared.job_store import create_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(tmp_path), job_type="run_team")
    r = client.post(f"/run-team/{job_id}/answers", json={"answers": []})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /execution endpoints
# ---------------------------------------------------------------------------


def test_execution_tasks(client: TestClient):
    r = client.get("/execution/tasks")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


# ---------------------------------------------------------------------------
# /architect/design
# ---------------------------------------------------------------------------


def test_architect_design_invalid_spec(client: TestClient):
    """Empty spec or bad request handled gracefully."""
    r = client.post("/architect/design", json={"spec_content": ""})
    # Either 400 or 200 with no architecture
    assert r.status_code in (200, 400, 422)


# ---------------------------------------------------------------------------
# /frontend-code-v2 endpoints
# ---------------------------------------------------------------------------


def test_run_frontend_code_v2_invalid_path(client: TestClient):
    r = client.post(
        "/frontend-code-v2/run",
        json={
            "task": {"id": "t1", "title": "Button", "description": "build a button"},
            "repo_path": "/path/that/does/not/exist",
        },
    )
    assert r.status_code == 400


def test_frontend_code_v2_status_unknown(client: TestClient):
    r = client.get(f"/frontend-code-v2/status/{uuid.uuid4()}")
    assert r.status_code == 404


def test_frontend_code_v2_status_existing(client: TestClient, tmp_path: Path):
    from software_engineering_team.shared.job_store import create_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(tmp_path), job_type="frontend_code_v2")
    r = client.get(f"/frontend-code-v2/status/{job_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == job_id


# ---------------------------------------------------------------------------
# /backend-code-v2 endpoints
# ---------------------------------------------------------------------------


def test_run_backend_code_v2_invalid_path(client: TestClient):
    r = client.post(
        "/backend-code-v2/run",
        json={
            "task": {"id": "t1", "title": "API", "description": "build an api"},
            "repo_path": "/no/such/path",
        },
    )
    assert r.status_code == 400


def test_backend_code_v2_status_unknown(client: TestClient):
    r = client.get(f"/backend-code-v2/status/{uuid.uuid4()}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /product-analysis endpoints
# ---------------------------------------------------------------------------


def test_product_analysis_status_unknown(client: TestClient):
    r = client.get(f"/product-analysis/status/{uuid.uuid4()}")
    assert r.status_code == 404


def test_product_analysis_jobs_list(client: TestClient):
    r = client.get("/product-analysis/jobs")
    assert r.status_code == 200
    assert "jobs" in r.json()


def test_product_analysis_submit_answers_unknown(client: TestClient):
    r = client.post(
        f"/product-analysis/{uuid.uuid4()}/answers",
        json={"answers": []},
    )
    assert r.status_code == 404


def test_product_analysis_submit_answers_wrong_type(client: TestClient, tmp_path: Path):
    """If job_type is not 'product_analysis' -> 400."""
    from software_engineering_team.shared.job_store import create_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(tmp_path), job_type="run_team")
    r = client.post(f"/product-analysis/{job_id}/answers", json={"answers": []})
    assert r.status_code == 400


def test_product_analysis_submit_answers_not_waiting(client: TestClient, tmp_path: Path):
    from software_engineering_team.shared.job_store import create_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(tmp_path), job_type="product_analysis")
    r = client.post(f"/product-analysis/{job_id}/answers", json={"answers": []})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /logs endpoint
# ---------------------------------------------------------------------------


def test_logs_disabled_by_default(client: TestClient, monkeypatch):
    monkeypatch.delenv("ENABLE_LOG_API", raising=False)
    r = client.get("/logs?service=sw_api")
    assert r.status_code == 404


def test_logs_enabled_unknown_service(client: TestClient, monkeypatch):
    monkeypatch.setenv("ENABLE_LOG_API", "1")
    r = client.get("/logs?service=nope")
    # 400 unknown service if dir exists, or 503 if no dir
    assert r.status_code in (400, 503)


def test_logs_enabled_no_log_dir(client: TestClient, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ENABLE_LOG_API", "1")
    # Force a directory that doesn't exist
    monkeypatch.setattr(_api_main, "SUPERVISOR_LOG_DIR", tmp_path / "missing")
    r = client.get("/logs?service=sw_api")
    assert r.status_code == 503


def test_logs_enabled_with_log_files(client: TestClient, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ENABLE_LOG_API", "1")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "sw_api.log").write_text("line 1\nline 2\nline 3")
    monkeypatch.setattr(_api_main, "SUPERVISOR_LOG_DIR", log_dir)
    r = client.get("/logs?service=sw_api&lines=10")
    assert r.status_code == 200
    assert "line 1" in r.text


def test_logs_enabled_all_services(client: TestClient, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ENABLE_LOG_API", "1")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "sw_api.log").write_text("sw log")
    (log_dir / "blogging_api.log").write_text("blog log")
    monkeypatch.setattr(_api_main, "SUPERVISOR_LOG_DIR", log_dir)
    r = client.get("/logs?service=all")
    assert r.status_code == 200


def test_logs_enabled_stderr_flag(client: TestClient, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ENABLE_LOG_API", "1")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "sw_api.log").write_text("normal")
    (log_dir / "sw_api_err.log").write_text("error log")
    monkeypatch.setattr(_api_main, "SUPERVISOR_LOG_DIR", log_dir)
    r = client.get("/logs?service=sw_api&stderr=true")
    assert r.status_code == 200
    assert "error log" in r.text


def test_logs_enabled_no_files_found(client: TestClient, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ENABLE_LOG_API", "1")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(_api_main, "SUPERVISOR_LOG_DIR", log_dir)
    r = client.get("/logs?service=sw_api")
    assert r.status_code == 200
    assert "no log files" in r.text


# ---------------------------------------------------------------------------
# Restart and other status-paths
# ---------------------------------------------------------------------------


def test_run_team_jobs_endpoint(client: TestClient):
    r = client.get("/run-team/jobs?running_only=false")
    assert r.status_code == 200
    body = r.json()
    assert "jobs" in body


def test_get_job_status_includes_failed_tasks(client: TestClient, tmp_path: Path):
    from software_engineering_team.shared.job_store import create_job, update_job

    job_id = str(uuid.uuid4())
    create_job(job_id, str(tmp_path), job_type="run_team")
    update_job(
        job_id,
        failed_tasks=[
            {"task_id": "t1", "title": "Failed", "reason": "lint error"},
            "not a dict — ignored",
        ],
    )
    r = client.get(f"/run-team/{job_id}")
    assert r.status_code == 200
    body = r.json()
    assert len(body.get("failed_tasks", [])) == 1
    assert body["failed_tasks"][0]["task_id"] == "t1"
