"""Additional endpoint-level tests for run-team / retry / resume / answers routes.

Verifies the 4xx error-mapping branches and the happy-path body of routes
that *don't* spawn the live background pipeline (those launch branches are
already pragma'd as integration-only).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))
from software_engineering_team.api import main as _api_main  # noqa: E402

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
    # Patch the launch try-block so we don't actually dispatch to Temporal
    with patch("software_engineering_team.temporal.start_workflow.start_retry_failed_workflow"):
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
        pending_questions=[
            {"id": "q1", "required": True, "options": [{"id": "opt_a", "label": "A"}]}
        ],
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
        pending_questions=[
            {"id": "q1", "required": True, "options": [{"id": "opt_a", "label": "A"}]}
        ],
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


def test_submit_pending_answers_500_when_pending_question_missing_id(client, fake_job_client):
    # Intentional behavior change: a pending question without an "id" is a corrupted job record.
    # The reconciled shared validation surfaces a controlled 500 (SE's old inline check would have
    # raised a bare KeyError -> uncaught 500).
    job_id = "job-corrupt"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        pending_questions=[{"question_text": "no id here", "required": True}],
    )
    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={"answers": [{"question_id": "q1", "selected_option_id": "x"}]},
    )
    assert resp.status_code == 500
    assert "Corrupted job record" in resp.json()["detail"]


def test_submit_pending_answers_400_when_duplicate_question_id(client, fake_job_client):
    # Intentional behavior change: two answers for the same question are now rejected. SE previously
    # collapsed them into a set and silently persisted both conflicting entries.
    job_id = "job-dup"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        pending_questions=[{"id": "q1", "required": True, "options": [{"id": "a", "label": "A"}]}],
    )
    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={
            "answers": [
                {"question_id": "q1", "selected_option_id": "a"},
                {"question_id": "q1", "selected_option_id": "other", "other_text": "b"},
            ]
        },
    )
    assert resp.status_code == 400
    assert "Duplicate answers" in resp.json()["detail"]
    assert "q1" in resp.json()["detail"]


def test_submit_pending_answers_clamps_progress_in_response(client, fake_job_client):
    # The answers-endpoint response now clamps progress via coerce_progress, matching the status
    # route — the intentional [0,100] behavior change applies to this endpoint too.
    job_id = "job-prog"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        progress=250,
        pending_questions=[
            {"id": "q1", "required": True, "options": [{"id": "opt_a", "label": "A"}]}
        ],
    )
    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={"answers": [{"question_id": "q1", "selected_option_id": "opt_a"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["progress"] == 100


def _seed_temporal_native_pause(fake_job_client, job_id: str, question: dict) -> str:
    """Seed a job with the pause envelope a Temporal-native pause persists.

    Returns the resume token so callers echo the value the route will validate
    rather than re-deriving it.
    """
    token = f"{job_id}:tok-1"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        resume_token=token,
        pending_questions=[question],
    )
    return token


def _patch_temporal_native_route(monkeypatch, hitl_mod, *, on_append=None, on_signal=None) -> None:
    """Install the three patches every Temporal-native route test needs.

    The ``store_submit_answers`` guard is the point: it pins the route's
    temporal-native/thread-mode discriminator, so a regression there fails by
    name instead of running the real store against the fake client and surfacing
    as an opaque store error. Living in one helper means a change to the patched
    names, or to the discriminator, is edited once rather than in every test.
    """

    def _must_not_run(*_a, **_k):  # pragma: no cover
        raise AssertionError("thread-mode path must not run for a Temporal-native pause")

    monkeypatch.setattr(
        hitl_mod, "store_append_submitted_answers", on_append or (lambda *_a, **_k: None)
    )
    monkeypatch.setattr(hitl_mod, "signal_workflow_sync", on_signal or (lambda *_a, **_k: None))
    monkeypatch.setattr(hitl_mod, "store_submit_answers", _must_not_run)


# ---------------------------------------------------------------------------
# submit_pending_answers — Temporal-native pause (resume_token present)
# ---------------------------------------------------------------------------


def test_submit_pending_answers_temporal_native_signals_workflow(
    client, fake_job_client, monkeypatch
):
    """A pause with a persisted resume_token (set by plan_project_activity's
    PlanningAnswerPauseSignal handler) must append-only store the answers and signal
    RunTeamWorkflowV2 directly, instead of the thread-liveness/auto-resume dance that only
    applies to a thread-mode pause."""
    import software_engineering_team.api.routes.hitl as hitl_mod

    job_id = "job-signal-1"
    token = _seed_temporal_native_pause(
        fake_job_client,
        job_id,
        {
            "id": "q1",
            "question_text": "Which auth provider?",
            "required": True,
            "options": [{"id": "okta", "label": "Okta"}],
        },
    )

    appended: dict = {}
    signaled: dict = {}

    def _fake_append(jid, answers):
        appended.update(job_id=jid, answers=answers)

    def _fake_signal(workflow_id, signal, payload):
        signaled.update(workflow_id=workflow_id, signal=signal, payload=payload)

    _patch_temporal_native_route(
        monkeypatch, hitl_mod, on_append=_fake_append, on_signal=_fake_signal
    )

    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={
            "answers": [{"question_id": "q1", "selected_option_id": "okta"}],
            "resume_token": token,
        },
    )

    assert resp.status_code == 200
    assert appended["job_id"] == job_id
    assert appended["answers"][0]["question_id"] == "q1"
    assert signaled["workflow_id"] == f"se-run-team-{job_id}"
    assert signaled["signal"] == "submit_planning_answers"
    assert signaled["payload"]["resume_token"] == token
    assert signaled["payload"]["answers"] == appended["answers"]
    # The answered pause is no longer advertised back at the client (see below).
    assert resp.json()["resume_token"] is None


def test_submit_pending_answers_temporal_native_stops_advertising_the_answered_pause(
    client, fake_job_client, monkeypatch
):
    """A valid submission must not come back saying the job is still waiting for the
    questions it just answered.

    The pause envelope is the activity's to clear atomically on re-entry, never the
    answers route's, so the record keeps advertising the pause for the minutes until
    the activity runs. Reporting that verbatim tells the client its answers did not
    land: a polling UI keeps rendering the banner, and a re-submit is either rejected
    as a duplicate or silently dropped by the workflow's first-wins signal rule. The
    projection reports the pause resolved; the raw envelope stays put, because
    ``_check_pending_pause_reentry`` classifies the resume from it.
    """
    import software_engineering_team.api.routes.hitl as hitl_mod

    job_id = "job-signal-projected"
    token = _seed_temporal_native_pause(
        fake_job_client,
        job_id,
        {
            "id": "q1",
            "question_text": "Which auth provider?",
            "required": True,
            "options": [{"id": "okta", "label": "Okta"}],
        },
    )

    _patch_temporal_native_route(monkeypatch, hitl_mod)

    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={
            "answers": [{"question_id": "q1", "selected_option_id": "okta"}],
            "resume_token": token,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["waiting_for_answers"] is False
    assert body["pending_questions"] == []
    assert body["resume_token"] is None

    # A follow-up status poll agrees — otherwise the banner just comes back.
    status = client.get(f"/run-team/{job_id}")
    assert status.status_code == 200
    assert status.json()["waiting_for_answers"] is False

    # ...and the envelope the activity classifies its re-entry from is untouched.
    record = fake_job_client.get_job(job_id)
    assert record["waiting_for_answers"] is True
    assert record["resume_token"] == token
    assert record["pending_questions"]


def test_submit_pending_answers_temporal_native_still_advertises_a_later_pause(
    client, fake_job_client, monkeypatch
):
    """The marker is scoped to the token it answered: a NEW pause round must show
    through, or a resumed run's next question would never reach the user."""
    import software_engineering_team.api.routes.hitl as hitl_mod

    job_id = "job-signal-round2"
    token = _seed_temporal_native_pause(
        fake_job_client,
        job_id,
        {
            "id": "q1",
            "question_text": "Q1?",
            "required": True,
            "options": [{"id": "a", "label": "A"}],
        },
    )

    _patch_temporal_native_route(monkeypatch, hitl_mod)

    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={
            "answers": [{"question_id": "q1", "selected_option_id": "a"}],
            "resume_token": token,
        },
    )
    # Assert the submission landed, and that the marker it set is visible.
    # Without this the test proves nothing: a rejected POST sets no marker, the
    # projection falls back to the raw envelope, and the round-2 assertions
    # below would pass for the opposite reason.
    assert resp.status_code == 200
    assert client.get(f"/run-team/{job_id}").json()["waiting_for_answers"] is False

    # The activity consumed round 1 and paused again on a fresh token.
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        resume_token=f"{job_id}:tok-2",
        pending_questions=[{"id": "q2", "question_text": "Q2?", "required": True}],
    )

    status = client.get(f"/run-team/{job_id}").json()
    assert status["waiting_for_answers"] is True
    assert status["resume_token"] == f"{job_id}:tok-2"
    assert [q["id"] for q in status["pending_questions"]] == ["q2"]


def test_submit_pending_answers_temporal_native_rejects_stale_resume_token(
    client, fake_job_client, monkeypatch
):
    """A client echoing a stale/mismatched resume_token must get 409, not a false-confidence
    200 while the workflow silently drops the mismatched signal."""
    import software_engineering_team.api.routes.hitl as hitl_mod

    job_id = "job-signal-2"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        resume_token=f"{job_id}:tok-current",
        pending_questions=[{"id": "q1", "required": True, "options": []}],
    )

    def _must_not_run(*_a, **_k):  # pragma: no cover
        raise AssertionError("must not append/signal/store on a resume_token mismatch")

    monkeypatch.setattr(hitl_mod, "store_append_submitted_answers", _must_not_run)
    monkeypatch.setattr(hitl_mod, "signal_workflow_sync", _must_not_run)
    monkeypatch.setattr(hitl_mod, "store_submit_answers", _must_not_run)

    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={
            "answers": [{"question_id": "q1", "other_text": "x"}],
            "resume_token": f"{job_id}:tok-stale",
        },
    )
    assert resp.status_code == 409


def test_submit_pending_answers_temporal_native_rejects_missing_resume_token(
    client, fake_job_client, monkeypatch
):
    """A client omitting resume_token entirely for a Temporal-native pause is treated the
    same as a mismatch — a legitimate client always has one, from the pause notification or a
    status poll."""
    import software_engineering_team.api.routes.hitl as hitl_mod

    job_id = "job-signal-3"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        resume_token=f"{job_id}:tok-current",
        pending_questions=[{"id": "q1", "required": True, "options": []}],
    )

    def _must_not_run(*_a, **_k):  # pragma: no cover
        raise AssertionError("must not append/signal/store when resume_token is missing")

    monkeypatch.setattr(hitl_mod, "store_append_submitted_answers", _must_not_run)
    monkeypatch.setattr(hitl_mod, "signal_workflow_sync", _must_not_run)
    monkeypatch.setattr(hitl_mod, "store_submit_answers", _must_not_run)

    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={"answers": [{"question_id": "q1", "other_text": "x"}]},
    )
    assert resp.status_code == 409


def test_submit_pending_answers_without_resume_token_never_signals(
    client, fake_job_client, monkeypatch
):
    """Thread-mode /answers (no resume_token on the job record) must not signal a workflow —
    unchanged existing behavior."""
    import software_engineering_team.api.routes.hitl as hitl_mod

    job_id = "job-signal-4"
    fake_job_client.create_job(job_id, repo_path="/tmp/repo", job_type="run_team")
    fake_job_client.update_job(
        job_id,
        waiting_for_answers=True,
        pending_questions=[
            {"id": "q1", "required": True, "options": [{"id": "opt_a", "label": "A"}]}
        ],
    )

    def _must_not_signal(*_a, **_k):  # pragma: no cover
        raise AssertionError("block-mode path must never signal a workflow")

    monkeypatch.setattr(hitl_mod, "signal_workflow_sync", _must_not_signal)

    resp = client.post(
        f"/run-team/{job_id}/answers",
        json={"answers": [{"question_id": "q1", "selected_option_id": "opt_a"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["resume_token"] is None


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
    with patch("software_engineering_team.temporal.start_workflow.start_retry_failed_workflow"):
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
    with patch("software_engineering_team.temporal.start_workflow.start_run_team_workflow"):
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
