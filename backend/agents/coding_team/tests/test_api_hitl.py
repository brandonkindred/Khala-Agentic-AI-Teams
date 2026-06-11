"""Tests for the coding-team API human-in-the-loop surface: /status pending questions, the
/run/{job_id}/answers endpoint, /run/{job_id}/resume, and the GitHub issue-comment formatter."""

from __future__ import annotations

from typing import Any, Dict, List

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


def test_answers_400_blank_answer(monkeypatch):
    # An answer with neither a selected option nor free text is not a decision and must be rejected,
    # not recorded as an empty answer that spuriously 'covers' the open question.
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    r = client.post("/run/j1/answers", json={"answers": [{"question_id": "q1"}]})
    assert r.status_code == 400
    assert "no option selected" in r.json()["detail"].lower()


def test_answers_400_whitespace_only_text(monkeypatch):
    # A whitespace-only free-text answer is not a decision and must be rejected, not recorded as a
    # blank answer that spuriously 'covers' the question.
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    r = client.post(
        "/run/j1/answers",
        json={
            "answers": [{"question_id": "q1", "selected_option_id": "other", "other_text": "   "}]
        },
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


def test_status_surfaces_current_activity_and_timestamps(monkeypatch):
    """/status round-trips the sub-agent activity dict and the activity/heartbeat
    timestamps the UI uses for the sub-progress bar and stall warning."""
    monkeypatch.setattr(
        api,
        "get_job",
        lambda jid: _job(
            status="running",
            waiting_for_answers=False,
            pending_questions=[],
            current_activity={
                "agent": "tech_lead_review",
                "step": "waiting_retry",
                "detail": "attempt 1/3 failed; retrying in 4s",
                "fraction": 0.37,
            },
            last_activity_at="2026-06-10T12:00:00+00:00",
            updated_at="2026-06-10T12:00:01+00:00",
            last_heartbeat_at="2026-06-10T12:00:02+00:00",
        ),
    )
    r = client.get("/status/j1")
    assert r.status_code == 200
    body = r.json()
    assert body["current_activity"]["agent"] == "tech_lead_review"
    assert body["current_activity"]["fraction"] == 0.37
    assert body["last_activity_at"] == "2026-06-10T12:00:00+00:00"
    assert body["updated_at"] == "2026-06-10T12:00:01+00:00"
    assert body["last_heartbeat_at"] == "2026-06-10T12:00:02+00:00"


def test_status_activity_fields_default_to_none(monkeypatch):
    """Older job records without the new fields validate cleanly with None values,
    and a malformed (non-dict) current_activity is coerced to None."""
    monkeypatch.setattr(api, "get_job", lambda jid: _job(current_activity="garbage"))
    r = client.get("/status/j1")
    assert r.status_code == 200
    body = r.json()
    assert body["current_activity"] is None
    assert body["last_activity_at"] is None
    assert body["updated_at"] is None
    assert body["last_heartbeat_at"] is None


def test_resume_clears_stale_current_activity(monkeypatch):
    """Resume wipes the dead attempt's current_activity (its finally clears never
    ran) so the UI cannot render a frozen mid-review sub-bar through the resumed run."""
    job = _job(
        status="waiting_for_user",
        plan_input={"requirements_title": "T"},
        repo_path="/tmp/repo",
        current_activity={"agent": "code_review", "step": "reviewing", "fraction": 0.4},
    )
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)

    class _SyncThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setattr(api, "run_coding_team_orchestrator", lambda *a, **k: None)
    updates: List[Dict[str, Any]] = []
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: updates.append(kw))

    r = client.post("/run/j1/resume")
    assert r.status_code == 200
    clears = [kw for kw in updates if kw.get("current_activity", "absent") is None]
    assert clears, "resume must clear the stale current_activity"


# --------------------------------------------------------------------------- /status progress + server_time


def test_status_surfaces_progress_and_server_time(monkeypatch):
    """/status exposes the job-level progress band and the server clock the UI
    computes staleness against."""
    from datetime import datetime

    monkeypatch.setattr(api, "get_job", lambda jid: _job(status="running", progress=47))
    r = client.get("/status/j1")
    assert r.status_code == 200
    body = r.json()
    assert body["progress"] == 47
    assert body["server_time"] is not None
    datetime.fromisoformat(body["server_time"])


def test_status_progress_coercion_clamps_garbage(monkeypatch):
    """Garbage progress degrades to None; out-of-range values clamp to [0, 100]."""
    monkeypatch.setattr(api, "get_job", lambda jid: _job(progress="not-a-number"))
    assert client.get("/status/j1").json()["progress"] is None

    monkeypatch.setattr(api, "get_job", lambda jid: _job(progress=250))
    assert client.get("/status/j1").json()["progress"] == 100


def test_resume_releases_claim_when_activity_clear_fails(monkeypatch):
    """A job-service outage during resume's current_activity wipe must not leak the
    run-thread claim: the failed /resume surfaces the error, and a later /resume
    (service recovered) can still spawn the orchestrator instead of being told
    'Job already running' forever."""
    from fastapi.testclient import TestClient

    job = _job(
        status="waiting_for_user",
        plan_input={"requirements_title": "T"},
        repo_path="/tmp/repo",
    )
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)

    class _SyncThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    started = {"n": 0}
    monkeypatch.setattr(
        api,
        "run_coding_team_orchestrator",
        lambda *a, **k: started.__setitem__("n", started["n"] + 1),
    )

    store = {"down": True}

    def flaky_update(jid, **kw):
        if store["down"]:
            raise RuntimeError("job service unreachable")

    monkeypatch.setattr(api, "update_job", flaky_update)

    local_client = TestClient(api.app, raise_server_exceptions=False)
    first = local_client.post("/run/j1/resume")
    assert first.status_code == 500
    assert started["n"] == 0

    store["down"] = False
    second = local_client.post("/run/j1/resume")
    assert second.status_code == 200
    assert second.json()["message"] == "Job resumed."
    assert started["n"] == 1, "claim must have been released so the retry can spawn"
