"""Tests for ai_systems_team API endpoints.

Drives the team API through ``TestClient``. The autouse fixture in
``tests/conftest.py`` swaps ``ai_systems_team.shared.job_store._client`` for an
in-memory ``FakeJobServiceClient``, so any endpoint path that reaches the
job_store helpers stays off the network. A few tests additionally patch the
``api.main`` module-level imports of those helpers to assert specific return
values; both layers cooperate.
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

from ai_systems_team.api.main import app

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


def test_build_status_reports_completed_phases_from_blueprint():
    """Mid-run progress: completed_phases comes from the checkpointed blueprint."""
    data = {
        "status": "running",
        "project_name": "proj",
        "current_phase": "architecture",
        "progress": 35,
        "completed_phases": [],  # top-level field stays empty; must not win
        "blueprint": {
            "project_name": "proj",
            "completed_phases": ["spec_intake", "architecture"],
        },
    }
    with patch("ai_systems_team.api.main.get_job", return_value=data):
        resp = client.get("/build/status/j1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["completed_phases"] == ["spec_intake", "architecture"]
    assert body["current_phase"] == "architecture"


def test_build_status_no_blueprint_reports_empty_completed_phases():
    """Before the first phase checkpoints a blueprint, completed_phases is empty."""
    data = {
        "status": "running",
        "project_name": "proj",
        "progress": 5,
        "blueprint": None,
    }
    with patch("ai_systems_team.api.main.get_job", return_value=data):
        resp = client.get("/build/status/j2")
    assert resp.status_code == 200
    assert resp.json()["completed_phases"] == []


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


def test_maybe_dispatch_temporal_dispatches_when_enabled(monkeypatch):
    """Enabled + importable: the helper starts the workflow and returns True."""
    import ai_systems_team.temporal.client as temporal_client
    import ai_systems_team.temporal.start_workflow as start_workflow
    from ai_systems_team.api.main import _maybe_dispatch_temporal

    captured: dict = {}
    monkeypatch.setattr(temporal_client, "is_temporal_enabled", lambda: True)
    monkeypatch.setattr(
        start_workflow,
        "start_build_workflow",
        lambda job_id, project_name, spec_path, constraints, output_dir: captured.update(
            job_id=job_id,
            project_name=project_name,
            spec_path=spec_path,
            constraints=constraints,
            output_dir=output_dir,
        ),
    )

    assert _maybe_dispatch_temporal("j1", "proj", "/spec.md", {"k": "v"}, "/out") is True
    assert captured == {
        "job_id": "j1",
        "project_name": "proj",
        "spec_path": "/spec.md",
        "constraints": {"k": "v"},
        "output_dir": "/out",
    }


def test_maybe_dispatch_temporal_returns_false_when_disabled(monkeypatch):
    """Temporal disabled: no dispatch, returns False."""
    import ai_systems_team.temporal.client as temporal_client
    import ai_systems_team.temporal.start_workflow as start_workflow
    from ai_systems_team.api.main import _maybe_dispatch_temporal

    called = {"n": 0}
    monkeypatch.setattr(temporal_client, "is_temporal_enabled", lambda: False)
    monkeypatch.setattr(
        start_workflow,
        "start_build_workflow",
        lambda *a, **kw: called.__setitem__("n", called["n"] + 1),
    )

    assert _maybe_dispatch_temporal("j2", "proj", "/spec.md", {}, None) is False
    assert called["n"] == 0


def test_maybe_dispatch_temporal_returns_false_on_import_error(monkeypatch):
    """Temporal package not importable: swallowed, returns False (no exception)."""
    import sys

    from ai_systems_team.api.main import _maybe_dispatch_temporal

    monkeypatch.setitem(sys.modules, "ai_systems_team.temporal.client", None)

    assert _maybe_dispatch_temporal("j3", "proj", "/spec.md", {}, None) is False


def test_start_build_dispatches_to_temporal():
    """When Temporal accepts the job, start_build responds without spawning a thread."""
    with (
        patch("ai_systems_team.api.main.create_job"),
        patch(
            "ai_systems_team.api.main._maybe_dispatch_temporal", return_value=True
        ) as mock_dispatch,
        patch("threading.Thread") as mock_thread,
    ):
        resp = client.post(
            "/build",
            json={"project_name": "test_proj", "spec_path": "/tmp/spec.md"},
        )
    assert resp.status_code == 200
    assert "Temporal" in resp.json()["message"]
    mock_dispatch.assert_called_once()
    mock_thread.assert_not_called()


def test_resume_build_job_dispatches_to_temporal():
    """When Temporal accepts the resume, resume_build_job responds without a thread."""
    job_data = {
        "status": "failed",
        "project_name": "proj",
        "spec_path": "/tmp/spec.md",
        "constraints": {},
        "output_dir": None,
        "blueprint": None,
    }
    with (
        patch("ai_systems_team.api.main.get_job", return_value=job_data),
        patch("ai_systems_team.api.main.update_job"),
        patch(
            "ai_systems_team.api.main._maybe_dispatch_temporal", return_value=True
        ) as mock_dispatch,
        patch("threading.Thread") as mock_thread,
    ):
        resp = client.post("/build/job/j-resume/resume")
    assert resp.status_code == 200
    assert "Temporal" in resp.json()["message"]
    mock_dispatch.assert_called_once()
    mock_thread.assert_not_called()


def test_restart_build_job_dispatches_to_temporal():
    """When Temporal accepts the restart, restart_build_job responds without a thread."""
    job_data = {
        "status": "completed",
        "project_name": "proj",
        "spec_path": "/tmp/spec.md",
        "constraints": {},
        "output_dir": None,
    }
    with (
        patch("ai_systems_team.api.main.get_job", return_value=job_data),
        patch("ai_systems_team.api.main.store_reset_job"),
        patch(
            "ai_systems_team.api.main._maybe_dispatch_temporal", return_value=True
        ) as mock_dispatch,
        patch("threading.Thread") as mock_thread,
    ):
        resp = client.post("/build/job/j-restart/restart")
    assert resp.status_code == 200
    assert "Temporal" in resp.json()["message"]
    mock_dispatch.assert_called_once()
    mock_thread.assert_not_called()


def test_get_blueprint_not_found():
    resp = client.get("/blueprints/nonexistent")
    assert resp.status_code == 404


def test_get_blueprint_falls_back_to_job_store():
    """A Temporal build (not in the in-memory cache) is served from the job store."""
    jobs = [
        {
            "status": "completed",
            "project_name": "temporal_proj",
            "blueprint": {"project_name": "temporal_proj", "version": "1.0.0"},
        }
    ]
    with patch("ai_systems_team.api.main.list_jobs", return_value=jobs):
        resp = client.get("/blueprints/temporal_proj")
    assert resp.status_code == 200
    assert resp.json()["project_name"] == "temporal_proj"


def test_get_blueprint_job_store_no_match_still_404():
    """A completed job for a different project name doesn't satisfy the lookup."""
    jobs = [
        {"status": "completed", "project_name": "other", "blueprint": {"project_name": "other"}}
    ]
    with patch("ai_systems_team.api.main.list_jobs", return_value=jobs):
        resp = client.get("/blueprints/temporal_proj_missing")
    assert resp.status_code == 404


def test_list_blueprints_includes_job_store_completed():
    """/blueprints unions the in-memory cache with completed jobs in the job store."""
    jobs = [
        {
            "status": "completed",
            "project_name": "ts_proj",
            "blueprint": {"project_name": "ts_proj"},
        },
        {"status": "running", "project_name": "not_done", "blueprint": None},
    ]
    with patch("ai_systems_team.api.main.list_jobs", return_value=jobs):
        resp = client.get("/blueprints")
    assert resp.status_code == 200
    names = resp.json()["blueprints"]
    assert "ts_proj" in names
    assert "not_done" not in names
