"""Tests for the coding-team API human-in-the-loop surface: /status pending questions, the
/run/{job_id}/answers endpoint, /run/{job_id}/resume, and the GitHub issue-comment formatter."""

from __future__ import annotations

from typing import Any, Dict

from fastapi.testclient import TestClient

from coding_team.api import main as api

client = TestClient(api.app)

_PENDING = [
    {
        "id": "q1",
        "question_text": "Allergen strictness default?",
        "context": "affects safety",
        "options": [{"id": "strict", "label": "Strict", "is_default": True}],
        "required": True,
        "source": "engineer:backend",
    }
]


def _job(**over: Any) -> Dict[str, Any]:
    base = {
        "job_id": "j1",
        "status": "waiting_for_user",
        "phase": "paused",
        "repo_path": "/tmp/repo",
        "waiting_for_answers": True,
        "pending_questions": _PENDING,
        "task_graph_snapshot": [],
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- /status


def test_status_surfaces_pending_questions(monkeypatch):
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    r = client.get("/status/j1")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "waiting_for_user"
    assert body["waiting_for_answers"] is True
    assert body["pending_questions"][0]["id"] == "q1"
    assert body["pending_questions"][0]["options"][0]["is_default"] is True


# --------------------------------------------------------------------------- /run/{id}/answers


def test_answers_404_when_missing(monkeypatch):
    monkeypatch.setattr(api, "get_job", lambda jid: None)
    assert client.post("/run/j1/answers", json={"answers": []}).status_code == 404


def test_answers_400_when_not_waiting(monkeypatch):
    monkeypatch.setattr(api, "get_job", lambda jid: _job(waiting_for_answers=False))
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 400
    assert "not waiting" in r.json()["detail"]


def test_answers_400_when_no_pending(monkeypatch):
    monkeypatch.setattr(api, "get_job", lambda jid: _job(pending_questions=[]))
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 400
    assert "No pending questions" in r.json()["detail"]


def test_answers_400_missing_required(monkeypatch):
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    r = client.post("/run/j1/answers", json={"answers": []})
    assert r.status_code == 400
    assert "Missing answers" in r.json()["detail"]


def test_answers_400_unknown_id(monkeypatch):
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    r = client.post(
        "/run/j1/answers",
        json={
            "answers": [
                {"question_id": "q1", "selected_option_id": "strict"},
                {"question_id": "ghost", "selected_option_id": "x"},
            ]
        },
    )
    assert r.status_code == 400
    assert "Unknown question" in r.json()["detail"]


def test_answers_400_other_without_text(monkeypatch):
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "other"}]}
    )
    assert r.status_code == 400
    assert "no text" in r.json()["detail"]


def test_answers_400_unknown_option_id(monkeypatch):
    # A non-'other' option id the question never offered must be rejected, not threaded through as
    # the literal user decision.
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "bogus"}]}
    )
    assert r.status_code == 400
    assert "unknown option" in r.json()["detail"].lower()


def test_answers_accepts_other_with_text(monkeypatch):
    stored = {}
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    monkeypatch.setattr(
        api, "store_submit_answers", lambda jid, answers: stored.update({"a": answers})
    )
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: True)
    r = client.post(
        "/run/j1/answers",
        json={
            "answers": [
                {"question_id": "q1", "selected_option_id": "other", "other_text": "use mTLS"}
            ]
        },
    )
    assert r.status_code == 200
    assert stored["a"][0]["other_text"] == "use mTLS"


def test_claim_run_thread_is_exclusive():
    api._active_run_threads.pop("claim-job", None)
    api._starting_run_jobs.discard("claim-job")
    assert api._claim_run_thread("claim-job") is True  # first claim wins
    assert api._claim_run_thread("claim-job") is False  # second rejected while 'starting'
    api._starting_run_jobs.discard("claim-job")  # cleanup


def test_answers_success_stores_and_returns_status(monkeypatch):
    stored = {}
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    monkeypatch.setattr(
        api, "store_submit_answers", lambda jid, answers: stored.update({"answers": answers})
    )
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: True)
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert stored["answers"][0]["question_id"] == "q1"


def test_answers_dead_thread_adds_resume_hint(monkeypatch):
    calls = {}
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert "Resume" in calls["status_text"]


# --------------------------------------------------------------------------- /run/{id}/resume


def test_resume_404(monkeypatch):
    monkeypatch.setattr(api, "get_job", lambda jid: None)
    assert client.post("/run/j1/resume").status_code == 404


def test_resume_noop_when_thread_alive(monkeypatch):
    monkeypatch.setattr(api, "get_job", lambda jid: _job(status="running"))
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: True)
    r = client.post("/run/j1/resume")
    assert r.status_code == 200
    assert "already running" in r.json()["message"]


def test_resume_400_no_plan(monkeypatch):
    monkeypatch.setattr(api, "get_job", lambda jid: {"job_id": "j1", "status": "waiting_for_user"})
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    assert client.post("/run/j1/resume").status_code == 400


def test_resume_spawns_orchestrator(monkeypatch):
    job = _job(
        status="waiting_for_user", plan_input={"requirements_title": "T"}, repo_path="/tmp/repo"
    )
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    started = {}

    # Run the thread target synchronously so the orchestrator call is observable.
    class _SyncThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        api, "run_coding_team_orchestrator", lambda *a, **k: started.update({"ran": True})
    )
    monkeypatch.setattr(api, "update_job", lambda *a, **k: None)

    r = client.post("/run/j1/resume")
    assert r.status_code == 200
    assert r.json()["message"] == "Job resumed."
    assert started.get("ran") is True


# --------------------------------------------------------------------------- GitHub comment formatter


def test_format_questions_comment():
    out = api._format_questions_comment(_PENDING, "job-123")
    assert "paused for a decision" in out
    assert "Allergen strictness default?" in out
    assert "`q1`" in out
    assert "`strict`" in out
    assert "/run/job-123/answers" in out
