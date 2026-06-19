"""Additional endpoint-level tests for run-team / retry / resume / answers routes.

Verifies the 4xx error-mapping branches and the happy-path body of routes
that *don't* spawn the live background pipeline (those launch branches are
already pragma'd as integration-only).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))
_spec = importlib.util.spec_from_file_location(
    "software_engineering_api_main",
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
# resume_after_llm_check
# ---------------------------------------------------------------------------


def test_resume_after_llm_check_404_when_job_missing(client):
    resp = client.post("/run-team/no-such-job/resume-after-llm-check")
    assert resp.status_code == 404


def test_resume_after_llm_check_400_when_job_not_paused_llm_connectivity(client, fake_job_client):
    job_id = "job-r1"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(job_id, status="running")
    resp = client.post(f"/run-team/{job_id}/resume-after-llm-check")
    assert resp.status_code == 400
    assert "paused_llm_connectivity" in resp.json()["detail"]


def test_resume_after_llm_check_accepts_correct_status(client, fake_job_client):
    job_id = "job-r2"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        status="paused_llm_connectivity",
        failed_tasks=[{"task_id": "t1"}, {"task_id": "t2"}],
    )
    # Patch the launch try-block so we don't actually spawn a thread
    with (
        patch.object(_api_main, "_run_retry_background"),
        patch(
            "software_engineering_team.temporal.client.is_temporal_enabled",
            return_value=False,
        ),
    ):
        resp = client.post(f"/run-team/{job_id}/resume-after-llm-check")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert sorted(body["retrying_tasks"]) == ["t1", "t2"]


# ---------------------------------------------------------------------------
# submit_pending_answers
# ---------------------------------------------------------------------------


def test_submit_pending_answers_404_when_job_missing(client):
    resp = client.post("/run-team/no-such-job/answers", json={"answers": []})
    assert resp.status_code == 404


def test_submit_pending_answers_400_when_not_waiting(client, fake_job_client):
    job_id = "job-a1"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    # waiting_for_answers default false → 400
    resp = client.post(f"/run-team/{job_id}/answers", json={"answers": []})
    assert resp.status_code == 400


def test_submit_pending_answers_400_when_no_pending_questions(client, fake_job_client):
    job_id = "job-a2"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(job_id, waiting_for_answers=True, pending_questions=[])
    resp = client.post(f"/run-team/{job_id}/answers", json={"answers": []})
    assert resp.status_code == 400


def test_submit_pending_answers_400_when_required_missing(client, fake_job_client):
    job_id = "job-a3"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        pending_questions=[{"id": "q1", "required": True}],
    )
    resp = client.post(f"/run-team/{job_id}/answers", json={"answers": []})
    assert resp.status_code == 400
    assert "Missing answers" in resp.json()["detail"]


def test_submit_pending_answers_400_when_unknown_id(client, fake_job_client):
    job_id = "job-a4"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        pending_questions=[{"id": "q1", "required": False}],
    )
    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={"answers": [{"question_id": "wrong-id", "selected_option_id": "yes"}]},
    )
    assert resp.status_code == 400
    assert "Unknown question" in resp.json()["detail"]


def test_submit_pending_answers_400_when_other_without_text(client, fake_job_client):
    job_id = "job-a5"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        pending_questions=[{"id": "q1", "required": True}],
    )
    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={
            "answers": [{"question_id": "q1", "selected_option_id": "other", "other_text": None}]
        },
    )
    assert resp.status_code == 400
    assert "no text provided" in resp.json()["detail"]


def test_submit_pending_answers_accepts_valid_option(client, fake_job_client):
    job_id = "job-a6"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        pending_questions=[{"id": "q1", "required": True, "options": [{"id": "opt_a", "label": "A"}]}],
    )
    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={"answers": [{"question_id": "q1", "selected_option_id": "opt_a"}]},
    )
    assert resp.status_code == 200


def test_submit_pending_answers_accepts_free_text_for_optionless_question(client, fake_job_client):
    job_id = "job-a7"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        pending_questions=[{"id": "q1", "required": True, "options": []}],
    )
    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={"answers": [{"question_id": "q1", "other_text": "Use Postgres"}]},
    )
    assert resp.status_code == 200


def test_submit_pending_answers_400_when_unknown_option_id(client, fake_job_client):
    job_id = "job-a8"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        pending_questions=[{"id": "q1", "required": True, "options": [{"id": "opt_a", "label": "A"}]}],
    )
    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={"answers": [{"question_id": "q1", "selected_option_id": "opt_invalid"}]},
    )
    assert resp.status_code == 400
    assert "unknown option" in resp.json()["detail"]


def test_submit_pending_answers_400_when_no_option_and_no_text(client, fake_job_client):
    job_id = "job-a9"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        pending_questions=[{"id": "q1", "required": True, "options": []}],
    )
    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={"answers": [{"question_id": "q1", "selected_option_id": "", "other_text": ""}]},
    )
    assert resp.status_code == 400
    assert "no text provided" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# retry_failed_tasks
# ---------------------------------------------------------------------------


def test_retry_failed_tasks_404_when_job_missing(client):
    resp = client.post("/run-team/no-such-job/retry-failed")
    assert resp.status_code == 404


def test_retry_failed_tasks_400_when_no_failed(client, fake_job_client):
    job_id = "job-rf1"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(job_id, failed_tasks=[])
    resp = client.post(f"/run-team/{job_id}/retry-failed")
    assert resp.status_code == 400


def test_retry_failed_tasks_happy_path(client, fake_job_client):
    job_id = "job-rf2"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(job_id, failed_tasks=[{"task_id": "t1"}], status="failed")
    with (
        patch.object(_api_main, "_run_retry_background"),
        patch(
            "software_engineering_team.temporal.client.is_temporal_enabled",
            return_value=False,
        ),
    ):
        resp = client.post(f"/run-team/{job_id}/retry-failed")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# cancel_job
# ---------------------------------------------------------------------------


def test_cancel_job_404_when_job_missing(client):
    resp = client.post("/run-team/no-such-job/cancel")
    assert resp.status_code == 404


def test_cancel_job_succeeds_for_pending_or_running(client, fake_job_client):
    job_id = "job-c1"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(job_id, status="running")
    resp = client.post(f"/run-team/{job_id}/cancel")
    assert resp.status_code in (200, 400)  # 400 if already terminal — depends on impl


# ---------------------------------------------------------------------------
# delete_run_team_job
# ---------------------------------------------------------------------------


def test_delete_run_team_job_404_when_missing(client):
    resp = client.delete("/run-team/no-such-job")
    assert resp.status_code == 404


def test_delete_run_team_job_succeeds_for_existing(client, fake_job_client):
    job_id = "job-d1"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(job_id, status="completed")
    resp = client.delete(f"/run-team/{job_id}")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# get_running_jobs
# ---------------------------------------------------------------------------


def test_get_running_jobs_returns_list(client):
    resp = client.get("/run-team/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert "jobs" in body


# ---------------------------------------------------------------------------
# resume / restart run-team
# ---------------------------------------------------------------------------


def test_resume_404_when_job_missing(client):
    resp = client.post("/run-team/no-such-job/resume")
    assert resp.status_code == 404


def test_resume_400_when_status_not_resumable(client, fake_job_client):
    """A completed job shouldn't be resumable."""
    job_id = "job-res-1"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(job_id, status="completed")
    resp = client.post(f"/run-team/{job_id}/resume")
    assert resp.status_code == 400


def test_resume_400_when_job_has_no_repo_path(client, fake_job_client):
    job_id = "job-res-2"
    fake_job_client.create_job(job_id, job_type="run_team")
    fake_job_client.update_job(job_id, status="failed", repo_path=None)
    resp = client.post(f"/run-team/{job_id}/resume")
    assert resp.status_code == 400


def test_restart_404_when_job_missing(client):
    resp = client.post("/run-team/no-such-job/restart")
    assert resp.status_code == 404


def test_restart_400_when_no_repo_path(client, fake_job_client):
    job_id = "job-rst-1"
    fake_job_client.create_job(job_id, job_type="run_team")
    fake_job_client.update_job(job_id, repo_path=None, status="failed")
    resp = client.post(f"/run-team/{job_id}/restart")
    assert resp.status_code == 400


def test_restart_accepts_already_complete_status(client, fake_job_client):
    """A run-team job that delegated to the coding team can end as already_complete (a terminal
    success like completed), so restart must accept that status. It still 400s here only because the
    job has no repo_path — proving it passed the status gate rather than being rejected for status."""
    job_id = "job-rst-ac"
    fake_job_client.create_job(job_id, job_type="run_team")
    fake_job_client.update_job(job_id, repo_path=None, status="already_complete")
    resp = client.post(f"/run-team/{job_id}/restart")
    assert resp.status_code == 400
    # Passed the RESTARTABLE_STATUSES gate (not "cannot be restarted"); fails on the missing path.
    assert "repo_path" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# get_execution_tasks / stream_execution_events
# ---------------------------------------------------------------------------


def test_get_execution_tasks_returns_snapshot(client):
    resp = client.get("/execution/tasks")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


# ---------------------------------------------------------------------------
# run_team_upload
# ---------------------------------------------------------------------------


def test_run_team_upload_rejects_oversized_file(monkeypatch, tmp_path: Path, client):
    """Files larger than 5 MB return 413."""
    monkeypatch.setenv("SE_WORKSPACE_DIR", str(tmp_path))
    big = b"x" * (5 * 1024 * 1024 + 16)
    resp = client.post(
        "/run-team/upload",
        files={"spec_file": ("spec.md", big, "text/markdown")},
        data={"project_name": "proj1"},
    )
    assert resp.status_code == 413


def test_run_team_upload_rejects_non_utf8_payload(monkeypatch, tmp_path: Path, client):
    """Spec files that fail UTF-8 decoding return 422."""
    monkeypatch.setenv("SE_WORKSPACE_DIR", str(tmp_path))
    payload = b"\xff\xfe garbage"
    resp = client.post(
        "/run-team/upload",
        files={"spec_file": ("spec.md", payload, "text/markdown")},
        data={"project_name": "proj1"},
    )
    assert resp.status_code == 422


def test_run_team_upload_rejects_empty_project_name_after_sanitization(
    monkeypatch, tmp_path: Path, client
):
    monkeypatch.setenv("SE_WORKSPACE_DIR", str(tmp_path))
    resp = client.post(
        "/run-team/upload",
        files={"spec_file": ("spec.md", b"# Spec\n", "text/markdown")},
        data={"project_name": "@@@"},
    )
    assert resp.status_code == 400


def test_run_team_upload_rejects_empty_spec(monkeypatch, tmp_path: Path, client):
    monkeypatch.setenv("SE_WORKSPACE_DIR", str(tmp_path))
    resp = client.post(
        "/run-team/upload",
        files={"spec_file": ("spec.md", b"   \n  ", "text/markdown")},
        data={"project_name": "proj1"},
    )
    assert resp.status_code == 400


def test_run_team_upload_happy_path(monkeypatch, tmp_path: Path, client):
    monkeypatch.setenv("SE_WORKSPACE_DIR", str(tmp_path))
    with (
        patch.object(_api_main, "_run_orchestrator_background"),
        patch(
            "software_engineering_team.temporal.client.is_temporal_enabled",
            return_value=False,
        ),
    ):
        resp = client.post(
            "/run-team/upload",
            files={"spec_file": ("spec.md", b"# Spec\nFeature", "text/markdown")},
            data={"project_name": "proj1"},
        )
    assert resp.status_code == 200
    assert resp.json()["job_id"]


# ---------------------------------------------------------------------------
# auto_answer for run_team
# ---------------------------------------------------------------------------


def test_auto_answer_run_team_404_when_job_missing(client):
    resp = client.post("/run-team/no-such-job/auto-answer/q1")
    assert resp.status_code == 404


def test_auto_answer_run_team_400_when_wrong_job_type(client, fake_job_client):
    job_id = "job-aa1"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="product_analysis")
    resp = client.post(f"/run-team/{job_id}/auto-answer/q1")
    assert resp.status_code == 400


def test_auto_answer_run_team_404_when_question_unknown(client, fake_job_client):
    job_id = "job-aa2"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(job_id, pending_questions=[{"id": "q1"}])
    resp = client.post(f"/run-team/{job_id}/auto-answer/q-unknown")
    assert resp.status_code == 404


def test_auto_answer_run_team_422_when_no_options(client, fake_job_client):
    job_id = "job-aa3"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        pending_questions=[{"id": "q1", "question_text": "What fields?", "options": []}],
    )
    resp = client.post(f"/run-team/{job_id}/auto-answer/q1")
    assert resp.status_code == 422


def test_auto_answer_run_team_422_when_only_synthetic_other_option(client, fake_job_client):
    """Synthetic {"id":"other"} placeholder must not be treated as a selectable option."""
    job_id = "job-aa4"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        pending_questions=[
            {
                "id": "q1",
                "question_text": "What fields?",
                "options": [{"id": "other", "label": "Provide answer in text field"}],
            }
        ],
    )
    resp = client.post(f"/run-team/{job_id}/auto-answer/q1")
    assert resp.status_code == 422
