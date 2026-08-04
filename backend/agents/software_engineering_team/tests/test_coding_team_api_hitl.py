"""Tests for the coding-team API human-in-the-loop surface: /status pending questions, the
/run/{job_id}/answers endpoint, /run/{job_id}/resume, and the GitHub issue-comment formatter."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from software_engineering_team import token_crypto
from software_engineering_team.api import coding_team_main as api

client = TestClient(api.app)


@pytest.fixture(autouse=True)
def _default_resume_claim(monkeypatch):
    """By default let the cross-worker resume claim succeed: these tests use monkeypatched job
    records that don't exist in the in-process job service, so the real claim would always lose.
    Tests that exercise the claim itself override these."""
    monkeypatch.setattr(api, "claim_resume", lambda jid: True)
    monkeypatch.setattr(api, "release_resume_claim", lambda jid: None)


def _set_encryption_key(monkeypatch) -> None:
    """Configure a Fernet key so token_crypto encrypt/decrypt round-trips in-test."""
    monkeypatch.setenv("INTEGRATION_ENCRYPTION_KEY", Fernet.generate_key().decode())


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
        "pending_questions": list(_PENDING),
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


def test_status_surfaces_resume_token(monkeypatch):
    """A client that discovers a Temporal-native pause by polling status (rather than only
    from the original pause notification) must be able to read resume_token from here -- it's
    the only value the client is allowed to echo back on SubmitAnswersRequest."""
    monkeypatch.setattr(api, "get_job", lambda jid: _job(resume_token="j1:tok-1"))
    r = client.get("/status/j1")
    assert r.status_code == 200
    assert r.json()["resume_token"] == "j1:tok-1"


def test_status_resume_token_absent_for_block_mode_pause(monkeypatch):
    monkeypatch.setattr(api, "get_job", lambda jid: _job())  # no resume_token key
    r = client.get("/status/j1")
    assert r.status_code == 200
    assert r.json()["resume_token"] is None


def test_status_skips_non_dict_pending_question(monkeypatch):
    # A corrupted (non-dict) pending_questions entry is skipped rather than 500'ing: the status
    # route now materializes via pending_questions_from_raw, which defensively filters non-dicts.
    monkeypatch.setattr(
        api, "get_job", lambda jid: _job(pending_questions=["oops-not-a-dict", _PENDING[0]])
    )
    r = client.get("/status/j1")
    assert r.status_code == 200
    body = r.json()
    assert len(body["pending_questions"]) == 1
    assert body["pending_questions"][0]["id"] == "q1"


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


def test_answers_500_when_pending_question_missing_id(monkeypatch):
    # A pending question without an "id" is a corrupted job record, not bad client input: the
    # endpoint must surface a controlled 500 with a clear message instead of a bare KeyError.
    bad = [{"question_text": "no id here", "required": True, "options": []}]
    monkeypatch.setattr(api, "get_job", lambda jid: _job(pending_questions=bad))
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "x"}]}
    )
    assert r.status_code == 500
    assert "Corrupted job record" in r.json()["detail"]


def test_answers_400_duplicate_question_id(monkeypatch):
    # Two answers for the same question must be rejected: otherwise the dedup set hides the conflict
    # at validation time and both entries get persisted, letting the orchestrator act on
    # contradictory decisions for one required question.
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    r = client.post(
        "/run/j1/answers",
        json={
            "answers": [
                {"question_id": "q1", "selected_option_id": "strict"},
                {"question_id": "q1", "selected_option_id": "other", "other_text": "lenient"},
            ]
        },
    )
    assert r.status_code == 400
    assert "Duplicate answers" in r.json()["detail"]
    assert "q1" in r.json()["detail"]


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
    # The stored answer must carry the question text so a resume after thread death can match it
    # against re-asked questions (the HITL coverage check matches strictly by text).
    assert stored["answers"][0]["question_text"] == "Allergen strictness default?"


class _SyncThread:
    """Stand-in for threading.Thread that runs the target inline, so spawned work is observable."""

    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class _FakeClient:
    """Minimal stand-in for GitHubClient used by the resume tests: a context
    manager whose get_issue returns a bare ``{"number": n}`` (the resume path only
    reads the number). Shared at module level so the resume tests don't each
    redefine it; a test needing different behaviour defines its own local class,
    which shadows this one within that function."""

    def __init__(self, token=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_issue(self, owner, repo, number):
        return {"number": number}


def test_start_orchestrator_thread_clears_claim_if_registration_fails(monkeypatch):
    """_register_run_thread sits inside the run() try: if it raises, the finally must still release
    the run-thread claim and the job must be marked failed — otherwise the claim wedges in
    _starting_run_jobs and no future resume in this worker can proceed."""
    cleared: list[str] = []
    updates: list[dict] = []
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: updates.append(kw))
    monkeypatch.setattr(api, "_clear_run_thread", lambda jid: cleared.append(jid))

    def boom(jid):
        raise RuntimeError("registry broken")

    monkeypatch.setattr(api, "_register_run_thread", boom)
    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    plan = api.CodingTeamPlanInput.model_validate({"repo_path": "/tmp/repo"})

    # Must not raise out of the spawn helper.
    api._start_orchestrator_thread("j1", "/tmp/repo", plan)

    assert cleared == ["j1"]  # finally ran despite the registration failure
    assert any(u.get("status") == "failed" for u in updates)


def test_answers_dead_thread_auto_resumes(monkeypatch):
    calls = {}
    started = {}
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))
    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        api, "run_coding_team_orchestrator", lambda *a, **k: started.update({"ran": True})
    )
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert started.get("ran") is True
    assert "resuming" in calls["status_text"].lower()


def test_answers_dead_thread_adds_resume_hint_when_unresumable(monkeypatch):
    """No repo_path/plan_input → auto-resume is impossible; fall back to the manual hint."""
    calls = {}
    monkeypatch.setattr(api, "get_job", lambda jid: _job(repo_path=None))
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert "Resume" in calls["status_text"]


def test_answers_dead_thread_claim_store_error_falls_back_to_hint(monkeypatch):
    """A job-store transport error while claiming the resume must not 500 after answers were stored:
    _try_auto_resume honours its 'never raises' contract by swallowing the store error and returning
    False, so the endpoint completes 200 with the manual-resume hint."""
    calls = {}
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))

    def boom(jid):
        raise RuntimeError("store down")

    monkeypatch.setattr(api, "claim_resume", boom)
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert "Resume" in calls["status_text"]


def test_answers_does_not_resume_job_cancelled_after_get(monkeypatch):
    """TOCTOU: if the job is cancelled between the initial get_job and storing answers, the resume
    decision must use the re-read (terminal) record — not the stale pre-cancellation one — and must
    not spawn a new orchestrator that would overwrite the terminal status."""
    waiting = _job()  # initial read: waiting_for_user
    cancelled = _job(status="cancelled")
    reads = iter([waiting, cancelled])  # 1st read waiting (endpoint), 2nd read cancelled (re-read)
    monkeypatch.setattr(api, "get_job", lambda jid: next(reads, cancelled))
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, a: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn an orchestrator for a cancelled job")

    monkeypatch.setattr(api, "_start_orchestrator_thread", _no_spawn)
    monkeypatch.setattr(api, "_start_github_resume_thread", _no_spawn)
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200  # answers stored; no spawn


def test_answers_deferred_claim_schedules_post_ttl_recheck(monkeypatch):
    """Deferring to another worker's resume claim must schedule a recheck past the claim TTL, so a
    winner that dies before advancing the job still gets reclaimed instead of stranding it."""
    scheduled = {}
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, a: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)
    monkeypatch.setattr(api, "_answer_wait_heartbeat_fresh", lambda d: False)
    monkeypatch.setattr(api, "claim_resume", lambda jid: False)  # another worker holds the claim

    def _record(jid, delay=None):
        scheduled.update({"jid": jid, "delay": delay})

    monkeypatch.setattr(api, "_schedule_resume_recheck", _record)
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert scheduled["jid"] == "j1"
    assert scheduled["delay"] == api.RESUME_CLAIM_TTL_S + 5.0


def test_resume_400_when_plan_input_corrupted(monkeypatch):
    """A non-dict plan_input is a corrupted record: resume must reject it with a controlled 400, not
    raise AttributeError off plan_raw.get()."""
    job = _job(status="waiting_for_user", plan_input="not-a-dict")
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    r = client.post("/run/j1/resume")
    assert r.status_code == 400
    assert "corrupted plan_input" in r.json()["detail"]


def test_resume_400_when_plan_input_invalid(monkeypatch):
    """A dict plan_input that fails CodingTeamPlanInput validation (a bad field type, not a missing
    repo_path) must be rejected with a controlled 400 naming the real cause — not the generic
    'no plan_input/repo_path' message reserved for a genuinely missing repo_path, and not an
    uncaught 500 from plan_from_input's ValidationError."""
    job = _job(
        status="waiting_for_user",
        plan_input={"project_overview": "not-a-dict"},  # project_overview must be a dict
    )
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    r = client.post("/run/j1/resume")
    assert r.status_code == 400
    assert "invalid plan_input" in r.json()["detail"]


def test_resume_500_when_claim_store_errors(monkeypatch):
    """A job-store transport error during the resume claim surfaces as a controlled 500 — not a bare
    propagation, and not a misleading 'already running' (no claim was actually taken)."""
    monkeypatch.setattr(api, "get_job", lambda jid: _job(status="waiting_for_user"))
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)

    def boom(jid):
        raise RuntimeError("store down")

    monkeypatch.setattr(api, "claim_resume", boom)
    r = client.post("/run/j1/resume")
    assert r.status_code == 500
    assert "job-store error" in r.json()["detail"]


def test_resume_500_when_post_claim_read_errors(monkeypatch):
    """A job-store transport error during the post-claim re-read surfaces as a controlled 500 and
    releases the resume claim so a later attempt can still succeed."""
    job = _job(status="waiting_for_user", plan_input={"requirements_title": "T"})
    read_count = {"n": 0}

    def _get_job(jid):
        read_count["n"] += 1
        # Read 1 (initial endpoint check) succeeds; read 2 (post-claim re-read) fails.
        if read_count["n"] == 1:
            return job
        raise RuntimeError("store error during post-claim read")

    monkeypatch.setattr(api, "get_job", _get_job)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    release_calls: List[str] = []
    monkeypatch.setattr(api, "release_resume_claim", lambda jid: release_calls.append(jid))
    r = client.post("/run/j1/resume")
    assert r.status_code == 500
    assert "job state" in r.json()["detail"]
    assert release_calls, "claim must be released on post-claim store error"


def test_resume_noop_when_thread_claim_lost(monkeypatch):
    """/resume must report 'already running' (not spawn) when the shared resume claim is won but
    the local run-thread claim is lost to a racing spawn already under way in this process."""
    job = _job(status="waiting_for_user", plan_input={"requirements_title": "T"})
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "_claim_run_thread", lambda jid: False)
    release_calls: List[str] = []
    monkeypatch.setattr(api, "release_resume_claim", lambda jid: release_calls.append(jid))

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn when the local run-thread claim is lost")

    monkeypatch.setattr(api.threading, "Thread", _no_spawn)
    r = client.post("/run/j1/resume")
    assert r.status_code == 200
    assert "already running" in r.json()["message"]
    assert release_calls, "shared claim must be released when the local claim is lost"


def test_resume_raises_on_unhandled_spawn_result(monkeypatch):
    """An unrecognized ResumeSpawnResult from _claim_and_spawn_resume must fail loudly (the
    exhaustiveness guard), not be silently treated as a successful resume."""
    job = _job(status="waiting_for_user", plan_input={"requirements_title": "T"})
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(
        api, "_claim_and_spawn_resume", lambda *a, **k: ("bogus-outcome", None, None)
    )
    local_client = TestClient(api.app, raise_server_exceptions=False)
    r = local_client.post("/run/j1/resume")
    assert r.status_code == 500


def test_answers_dead_thread_claim_lost_counts_as_resuming(monkeypatch):
    """A lost thread claim means someone else is starting the orchestrator — not a failure."""
    calls = {}
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))
    monkeypatch.setattr(api, "_claim_run_thread", lambda jid: False)
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert "resuming" in calls["status_text"].lower()


def test_answers_dead_thread_fresh_heartbeat_skips_spawn(monkeypatch):
    """A fresh answer-wait heartbeat means a live wait loop exists in another worker process —
    spawning here would double-drive the job, so auto-resume must not start a thread."""
    from datetime import datetime, timezone

    calls = {}
    job = _job(answer_wait_heartbeat_at=datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn a thread while a wait loop heartbeats")

    monkeypatch.setattr(api.threading, "Thread", _no_spawn)
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert "resuming" in calls["status_text"].lower()


def test_answers_dead_thread_stale_heartbeat_resumes(monkeypatch):
    calls = {}
    started = {}
    job = _job(answer_wait_heartbeat_at="2020-01-01T00:00:00+00:00")
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))
    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        api, "run_coding_team_orchestrator", lambda *a, **k: started.update({"ran": True})
    )
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert started.get("ran") is True


def test_answers_dead_thread_github_job_resumes_through_hook_path(monkeypatch):
    """GitHub-issue jobs must resume through the hook path so publication (PR, comments) survives."""
    calls = {}
    hook_calls = {}

    ctx = {"owner": "acme", "repo": "widgets", "issue_number": 42, "remote": "origin"}
    monkeypatch.setattr(api, "get_job", lambda jid: _job(github_context=ctx))
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))
    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setattr(api, "GitHubClient", _FakeClient)
    monkeypatch.setattr(
        api,
        "_run_with_github_hooks",
        lambda job_id, request, plan, issue, token: hook_calls.update(
            {"job_id": job_id, "owner": request.owner, "issue": issue, "token": token}
        ),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert hook_calls["job_id"] == "j1"
    assert hook_calls["owner"] == "acme"
    assert hook_calls["token"] == "tok-123"
    assert "resuming" in calls["status_text"].lower()


def test_answers_dead_thread_github_job_without_token_falls_back_to_hint(monkeypatch):
    calls = {}
    ctx = {"owner": "acme", "repo": "widgets", "issue_number": 42}
    monkeypatch.setattr(api, "get_job", lambda jid: _job(github_context=ctx))
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def _no_spawn(*a, **k):
        raise AssertionError("must not resume a GitHub job hook-less")

    monkeypatch.setattr(api.threading, "Thread", _no_spawn)
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert "Resume" in calls["status_text"]


def test_resume_400_when_terminal(monkeypatch):
    """A finished run must never be silently re-executed."""
    for status in ("completed", "completed_with_failures", "failed", "cancelled"):
        monkeypatch.setattr(api, "get_job", lambda jid, s=status: _job(status=s))
        r = client.post("/run/j1/resume")
        assert r.status_code == 400
        assert "cannot be resumed" in r.json()["detail"]


def test_resume_github_job_uses_hook_path(monkeypatch):
    """GitHub-issue jobs resume through the hook path so publication survives."""
    hook_calls = {}

    ctx = {"owner": "acme", "repo": "widgets", "issue_number": 42}
    job = _job(
        status="waiting_for_user",
        plan_input={"requirements_title": "T"},
        github_context=ctx,
    )
    updates: List[Dict[str, Any]] = []
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: updates.append(kw))
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setattr(api, "GitHubClient", _FakeClient)
    monkeypatch.setattr(
        api,
        "_run_with_github_hooks",
        lambda job_id, request, plan, issue, token: hook_calls.update(
            {"job_id": job_id, "owner": request.owner, "token": token}
        ),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    r = client.post("/run/j1/resume")
    assert r.status_code == 200
    assert r.json()["message"] == "Job resumed."
    assert hook_calls == {"job_id": "j1", "owner": "acme", "token": "tok-123"}
    # The resume must wipe any stale current_activity left by the dead attempt so the UI does not
    # render a frozen sub-bar through the resumed run's early phases.
    assert {"current_activity": None} in updates


def test_resume_github_job_propagates_cleanup_flag(monkeypatch):
    """A resumed GitHub-issue job must reproduce the fresh run's checkout-cleanup decision,
    read back from the persisted github_context — otherwise a resume that completes cleanly
    leaks the ephemeral per-issue checkout a fresh completion would have removed."""
    hook_calls = {}

    ctx = {
        "owner": "acme",
        "repo": "widgets",
        "issue_number": 42,
        "cleanup_checkout_on_success": True,
    }
    job = _job(
        status="waiting_for_user",
        plan_input={"requirements_title": "T"},
        github_context=ctx,
    )
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setattr(api, "GitHubClient", _FakeClient)
    monkeypatch.setattr(
        api,
        "_run_with_github_hooks",
        lambda job_id, request, plan, issue, token: hook_calls.update(
            {"cleanup": request.cleanup_checkout_on_success}
        ),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    r = client.post("/run/j1/resume")
    assert r.status_code == 200
    assert hook_calls == {"cleanup": True}


def test_resume_github_job_cleanup_flag_defaults_false_when_absent(monkeypatch):
    """A job persisted before the cleanup flag existed carries no such key in github_context;
    the resume must then default to False (the safe no-cleanup default) rather than delete."""
    hook_calls = {}

    ctx = {"owner": "acme", "repo": "widgets", "issue_number": 42}  # no cleanup key
    job = _job(
        status="waiting_for_user",
        plan_input={"requirements_title": "T"},
        github_context=ctx,
    )
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setattr(api, "GitHubClient", _FakeClient)
    monkeypatch.setattr(
        api,
        "_run_with_github_hooks",
        lambda job_id, request, plan, issue, token: hook_calls.update(
            {"cleanup": request.cleanup_checkout_on_success}
        ),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    r = client.post("/run/j1/resume")
    assert r.status_code == 200
    assert hook_calls == {"cleanup": False}


def test_resume_github_job_cleanup_flag_nonbool_fails_safe(monkeypatch):
    """A non-bool persisted value (e.g. a string from a future serialization change)
    must fail safe to no-cleanup — `bool("False")` would be True and wrongly delete."""
    hook_calls = {}

    ctx = {
        "owner": "acme",
        "repo": "widgets",
        "issue_number": 42,
        "cleanup_checkout_on_success": "False",  # string, not bool
    }
    job = _job(
        status="waiting_for_user",
        plan_input={"requirements_title": "T"},
        github_context=ctx,
    )
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setattr(api, "GitHubClient", _FakeClient)
    monkeypatch.setattr(
        api,
        "_run_with_github_hooks",
        lambda job_id, request, plan, issue, token: hook_calls.update(
            {"cleanup": request.cleanup_checkout_on_success}
        ),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    r = client.post("/run/j1/resume")
    assert r.status_code == 200
    assert hook_calls == {"cleanup": False}


def test_resume_github_job_uses_persisted_token_without_env(monkeypatch):
    """The standard deployment has no GITHUB_TOKEN env: resume must use the token persisted on the
    job record at creation, not require the env var."""
    hook_calls = {}

    _set_encryption_key(monkeypatch)
    ctx = {"owner": "acme", "repo": "widgets", "issue_number": 42}
    job = _job(
        status="waiting_for_user",
        plan_input={"requirements_title": "T"},
        github_context=ctx,
        github_token_encrypted=token_crypto.encrypt_token("persisted-pat"),
    )
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setattr(api, "GitHubClient", _FakeClient)
    monkeypatch.setattr(
        api,
        "_run_with_github_hooks",
        lambda job_id, request, plan, issue, token: hook_calls.update({"token": token}),
    )
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    r = client.post("/run/j1/resume")
    assert r.status_code == 200
    assert hook_calls["token"] == "persisted-pat"


def test_auto_resume_github_job_uses_persisted_token_without_env(monkeypatch):
    """Answer-submit auto-resume of a dead GitHub job also uses the persisted token."""
    hook_calls = {}

    _set_encryption_key(monkeypatch)
    ctx = {"owner": "acme", "repo": "widgets", "issue_number": 42}
    job = _job(
        github_context=ctx, github_token_encrypted=token_crypto.encrypt_token("persisted-pat")
    )
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)
    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setattr(api, "GitHubClient", _FakeClient)
    monkeypatch.setattr(
        api,
        "_run_with_github_hooks",
        lambda job_id, request, plan, issue, token: hook_calls.update({"token": token}),
    )
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert hook_calls["token"] == "persisted-pat"


def test_resume_github_job_400_without_token(monkeypatch):
    ctx = {"owner": "acme", "repo": "widgets", "issue_number": 42}
    job = _job(status="waiting_for_user", github_context=ctx)  # no github_token persisted
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    r = client.post("/run/j1/resume")
    assert r.status_code == 400
    assert "GITHUB_TOKEN" in r.json()["detail"]


def test_persisted_token_is_encrypted_not_plaintext(monkeypatch):
    """Only opaque ciphertext is persisted — a usable PAT is never written to the job record (which
    the generic GET /api/jobs/{team} route echoes verbatim)."""
    _set_encryption_key(monkeypatch)
    enc = token_crypto.encrypt_token("super-secret-pat")
    assert enc and enc != "super-secret-pat"
    assert token_crypto.decrypt_token(enc) == "super-secret-pat"

    # And the ciphertext field is not surfaced by the coding-team response models either.
    ctx = {"owner": "acme", "repo": "widgets", "issue_number": 42}
    job = _job(github_context=ctx, github_token_encrypted=enc)
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "list_jobs", lambda **kw: [job])
    assert "super-secret-pat" not in client.get("/status/j1").text
    assert enc not in client.get("/status/j1").text
    assert "super-secret-pat" not in client.get("/jobs").text
    assert enc not in client.get("/jobs").text
    # Belt-and-braces beyond the substring scan: the token fields must be structurally absent from
    # the parsed JSON, so a differently-encoded value can't slip through either.
    status_body = client.get("/status/j1").json()
    assert "github_token_encrypted" not in status_body
    assert "github_token" not in status_body
    for row in client.get("/jobs").json():
        assert "github_token_encrypted" not in row
        assert "github_token" not in row


def test_resume_400_when_running_and_not_locally_alive(monkeypatch):
    """Multi-worker safety: a genuinely running job whose thread lives in another worker has no
    fresh heartbeat (heartbeats only happen during a pause). Resume must refuse rather than start
    a second orchestrator on the same checkout."""

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn a second orchestrator for a running job")

    job = _job(status="running", waiting_for_answers=False)  # no answer_wait_heartbeat_at → stale
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api.threading, "Thread", _no_spawn)
    r = client.post("/run/j1/resume")
    assert r.status_code == 400
    assert "only a paused" in r.json()["detail"].lower()


def test_resume_running_job_alive_locally_is_noop(monkeypatch):
    """A running job whose thread IS alive in this worker is a no-op, not a 400 — the status guard
    sits after the liveness no-op so a self-targeted resume still reports 'already running'."""
    job = _job(status="running", waiting_for_answers=False)
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: True)
    r = client.post("/run/j1/resume")
    assert r.status_code == 200
    assert "already running" in r.json()["message"]


def test_auto_resume_refuses_terminal_job():
    assert api._try_auto_resume("j1", _job(status="completed")) is False
    assert api._try_auto_resume("j1", _job(status="cancelled")) is False


def test_auto_resume_refuses_non_paused_job():
    """Defensive invariant: _try_auto_resume only restarts a paused (waiting_for_user) job."""
    assert api._try_auto_resume("j1", _job(status="running", waiting_for_answers=False)) is False
    assert api._try_auto_resume("j1", _job(status="pending", waiting_for_answers=False)) is False


def test_auto_resume_coerces_non_dict_plan_input(monkeypatch):
    """A non-dict plan_input must be coerced to {} (not raise off .get()) so auto-resume can still
    use the job's own top-level repo_path field."""
    started = {}
    monkeypatch.setattr(api, "get_job", lambda jid: _job(plan_input="not-a-dict"))
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)
    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        api, "run_coding_team_orchestrator", lambda *a, **k: started.update({"ran": True})
    )
    result = api._try_auto_resume("j1", _job(plan_input="not-a-dict", repo_path="/tmp/repo"))
    assert result is True
    assert started.get("ran") is True


def test_auto_resume_skips_spawn_when_another_worker_holds_claim(monkeypatch):
    """Cross-worker safety: if the shared-store claim is already held (another worker is resuming),
    this worker must NOT spawn a second orchestrator on the same checkout."""

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn while another worker holds the resume claim")

    monkeypatch.setattr(api, "claim_resume", lambda jid: False)  # another worker won
    monkeypatch.setattr(api.threading, "Thread", _no_spawn)
    result = api._try_auto_resume("j1", _job(plan_input={"requirements_title": "T"}))
    assert result is True  # someone is resuming; not a failure


def test_resume_409ish_noop_when_another_worker_holds_claim(monkeypatch):
    """/resume on a paused job whose cross-worker claim another worker already holds is a no-op."""

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn while another worker holds the resume claim")

    job = _job(status="waiting_for_user", plan_input={"requirements_title": "T"})
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "claim_resume", lambda jid: False)
    monkeypatch.setattr(api.threading, "Thread", _no_spawn)
    r = client.post("/run/j1/resume")
    assert r.status_code == 200
    assert "already running" in r.json()["message"]


def test_auto_resume_releases_claim_on_spawn_failure(monkeypatch):
    """A failed spawn after winning the shared claim must release it so a retry can win."""
    released = {}
    monkeypatch.setattr(api, "claim_resume", lambda jid: True)
    monkeypatch.setattr(api, "release_resume_claim", lambda jid: released.setdefault("yes", True))
    monkeypatch.setattr(api, "_claim_run_thread", lambda jid: True)

    def _boom(*a, **k):
        raise RuntimeError("no threads")

    monkeypatch.setattr(api.threading, "Thread", _boom)
    result = api._try_auto_resume("j1", _job(plan_input={"requirements_title": "T"}))
    assert result is False
    assert released.get("yes") is True


def test_auto_resume_aborts_post_claim_when_job_vanishes(monkeypatch):
    """TOCTOU: if the post-claim re-read finds no job record at all (not just a status change),
    the spawn must still be aborted and the claim released — matching the HTTP /resume route's
    handling of the same case rather than falling through to spawn against unknown state."""
    released: List[str] = []
    monkeypatch.setattr(api, "claim_resume", lambda jid: True)
    monkeypatch.setattr(api, "release_resume_claim", lambda jid: released.append(jid))
    monkeypatch.setattr(api, "get_job", lambda jid: None)

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn for a job that vanished after claiming")

    monkeypatch.setattr(api.threading, "Thread", _no_spawn)
    result = api._try_auto_resume("j1", _job(plan_input={"requirements_title": "T"}))
    assert result is False
    assert released, "claim must be released when the post-claim re-read finds no job record"


def test_auto_resume_raises_on_unhandled_spawn_result(monkeypatch):
    """An unrecognized ResumeSpawnResult from _claim_and_spawn_resume must fail loudly (the
    exhaustiveness guard), not be silently treated as a successful auto-resume. _try_auto_resume
    calls _claim_and_spawn_resume as a same-module reference, so the patch target is the
    orchestration module itself, not the main/api re-export."""
    from software_engineering_team.api import orchestration

    monkeypatch.setattr(
        orchestration, "_claim_and_spawn_resume", lambda *a, **k: ("bogus-outcome", None, None)
    )
    with pytest.raises(RuntimeError, match="Unhandled ResumeSpawnResult"):
        api._try_auto_resume("j1", _job(plan_input={"requirements_title": "T"}))


class _ImmediateTimer:
    """Stand-in for threading.Timer that fires the callback synchronously on start()."""

    def __init__(self, interval, fn):
        self._fn = fn
        self.daemon = False

    def start(self):
        self._fn()


def test_fresh_heartbeat_schedules_recheck_that_resumes_a_dead_loop(monkeypatch):
    """The orphaned-heartbeat window: a worker that died right after its last beat must still
    get its run resumed once the staleness window passes."""
    from datetime import datetime, timezone

    started = {}
    fresh = _job(answer_wait_heartbeat_at=datetime.now(timezone.utc).isoformat())
    # After the (simulated) delay, the heartbeat is stale and the job still paused.
    stale = _job(
        waiting_for_answers=False,
        answer_wait_heartbeat_at="2020-01-01T00:00:00+00:00",
    )
    calls = {"n": 0}

    def get_job(jid):
        calls["n"] += 1
        return fresh if calls["n"] == 1 else stale

    monkeypatch.setattr(api, "get_job", get_job)
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)
    monkeypatch.setattr(api.threading, "Timer", _ImmediateTimer)
    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        api, "run_coding_team_orchestrator", lambda *a, **k: started.update({"ran": True})
    )
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert started.get("ran") is True


def test_recheck_writes_hint_when_resume_impossible(monkeypatch):
    """If the dead loop's job can't be auto-resumed at recheck time, the manual hint is restored."""
    from datetime import datetime, timezone

    writes = {}
    fresh = _job(answer_wait_heartbeat_at=datetime.now(timezone.utc).isoformat())
    # Stale at recheck time, and unresumable: no repo_path/plan to restart from.
    stale = _job(
        repo_path=None,
        waiting_for_answers=False,
        answer_wait_heartbeat_at="2020-01-01T00:00:00+00:00",
    )
    calls = {"n": 0}

    def get_job(jid):
        calls["n"] += 1
        return fresh if calls["n"] == 1 else stale

    monkeypatch.setattr(api, "get_job", get_job)
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: writes.update(kw))
    monkeypatch.setattr(api.threading, "Timer", _ImmediateTimer)
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert "Resume" in writes["status_text"]


def test_recheck_noop_when_job_moved_on(monkeypatch):
    """No spawn when the deferred-to wait loop really did consume the answers."""
    from datetime import datetime, timezone

    fresh = _job(answer_wait_heartbeat_at=datetime.now(timezone.utc).isoformat())
    resumed = _job(status="running", waiting_for_answers=False)
    calls = {"n": 0}

    def get_job(jid):
        calls["n"] += 1
        return fresh if calls["n"] == 1 else resumed

    def _no_spawn(*a, **k):
        raise AssertionError("recheck must not spawn once the job moved on")

    monkeypatch.setattr(api, "get_job", get_job)
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)
    monkeypatch.setattr(api.threading, "Timer", _ImmediateTimer)
    monkeypatch.setattr(api.threading, "Thread", _no_spawn)
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200


def test_recheck_noop_when_heartbeat_fresh_again(monkeypatch):
    """A still-fresh heartbeat at recheck time means the loop is genuinely alive elsewhere."""
    from datetime import datetime, timezone

    fresh = _job(answer_wait_heartbeat_at=datetime.now(timezone.utc).isoformat())

    def _no_spawn(*a, **k):
        raise AssertionError("recheck must not spawn while the heartbeat stays fresh")

    monkeypatch.setattr(api, "get_job", lambda jid: fresh)
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)
    monkeypatch.setattr(api.threading, "Timer", _ImmediateTimer)
    monkeypatch.setattr(api.threading, "Thread", _no_spawn)
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200


def test_resume_refuses_when_heartbeat_fresh(monkeypatch):
    from datetime import datetime, timezone

    job = _job(
        status="waiting_for_user",
        answer_wait_heartbeat_at=datetime.now(timezone.utc).isoformat(),
    )
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    r = client.post("/run/j1/resume")
    assert r.status_code == 200
    assert "already running" in r.json()["message"]


def test_answer_wait_heartbeat_fresh_handles_garbage():
    assert api._answer_wait_heartbeat_fresh({}) is False
    assert api._answer_wait_heartbeat_fresh({"answer_wait_heartbeat_at": "not-a-date"}) is False
    assert api._answer_wait_heartbeat_fresh({"answer_wait_heartbeat_at": ""}) is False
    # Naive timestamps are treated as UTC rather than rejected.
    from datetime import datetime, timezone

    naive_now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    assert api._answer_wait_heartbeat_fresh({"answer_wait_heartbeat_at": naive_now}) is True


def test_answer_wait_heartbeat_future_is_not_fresh():
    """A future-dated heartbeat (clock skew / corruption) must not block resume until that time —
    it is treated as stale, not fresh."""
    from datetime import datetime, timedelta, timezone

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert api._answer_wait_heartbeat_fresh({"answer_wait_heartbeat_at": future}) is False
    # And a normal recent stamp is still fresh.
    recent = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    assert api._answer_wait_heartbeat_fresh({"answer_wait_heartbeat_at": recent}) is True


def test_answers_dead_thread_invalid_plan_falls_back_to_hint(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        api, "get_job", lambda jid: _job(plan_input={"requirements_title": {"not": "a string"}})
    )
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert "Resume" in calls["status_text"]


def test_resumed_orchestrator_failure_marks_job_failed(monkeypatch):
    calls = {}
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))
    monkeypatch.setattr(api.threading, "Thread", _SyncThread)

    def _boom(*a, **k):
        raise RuntimeError("orchestrator crashed")

    monkeypatch.setattr(api, "run_coding_team_orchestrator", _boom)
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert calls["status"] == "failed"
    assert "orchestrator crashed" in calls["error"]
    # The crashed thread must release its registration so a manual /resume stays possible.
    assert api._is_run_thread_alive("j1") is False


def test_github_resume_issue_fetch_failure_marks_job_failed(monkeypatch):
    calls = {}

    class _FailingClient:
        def __init__(self, token=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_issue(self, owner, repo, number):
            raise RuntimeError("github down")

    ctx = {"owner": "acme", "repo": "widgets", "issue_number": 42}
    monkeypatch.setattr(api, "get_job", lambda jid: _job(github_context=ctx))
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))
    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setattr(api, "GitHubClient", _FailingClient)
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert calls["status"] == "failed"
    assert "github down" in calls["error"]


def test_github_resume_spawn_failure_falls_back_to_hint(monkeypatch):
    calls = {}
    ctx = {"owner": "acme", "repo": "widgets", "issue_number": 42}
    monkeypatch.setattr(api, "get_job", lambda jid: _job(github_context=ctx))
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")

    def _boom(*a, **k):
        raise RuntimeError("no threads")

    monkeypatch.setattr(api.threading, "Thread", _boom)
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert "Resume" in calls["status_text"]
    assert "j1" not in api._starting_run_jobs


def test_answers_dead_thread_spawn_failure_falls_back_to_hint(monkeypatch):
    calls = {}
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, answers: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))

    def _boom(*a, **k):
        raise RuntimeError("no threads")

    monkeypatch.setattr(api.threading, "Thread", _boom)
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert "Resume" in calls["status_text"]
    # The failed spawn must release the claim so a later manual /resume can succeed.
    assert "j1" not in api._starting_run_jobs


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


# --------------------------------------------------------------------------- /status agents roster


def test_status_surfaces_agent_roster(monkeypatch):
    """/status derives the per-agent roster (Tech Lead + implementation workers) from the
    persisted stack_specs / agent_task_map / task graph / current_activity so the UI can show
    which agent is working and each agent's status."""
    monkeypatch.setattr(
        api,
        "get_job",
        lambda jid: _job(
            status="running",
            phase="coding",
            waiting_for_answers=False,
            pending_questions=[],
            stack_specs=[
                {"name": "frontend", "tools_services": ["Angular"]},
                {"name": "backend", "tools_services": ["Java"]},
            ],
            agent_task_map={"frontend": "t1", "backend": "t2"},
            task_graph_snapshot=[
                {"id": "t1", "title": "Build UI", "status": "in_progress"},
                {"id": "t2", "title": "API", "status": "in_review"},
            ],
            current_activity={
                "agent": "tech_lead_review",
                "step": "parsing",
                "fraction": 0.5,
                "task_id": "t2",
            },
        ),
    )
    body = client.get("/status/j1").json()
    agents = {a["agent_id"]: a for a in body["agents"]}
    assert body["agents"][0]["role"] == "tech_lead"  # Tech Lead is always first
    assert agents["tech_lead"]["status"] == "reviewing"
    # The tech_lead_review overlay lands on the Tech Lead, not the engineer whose task it is.
    assert agents["tech_lead"]["current_step"] == "parsing"
    assert agents["tech_lead"]["activity_fraction"] == 0.5
    assert agents["frontend"]["status"] == "working"
    assert agents["frontend"]["current_task_title"] == "Build UI"
    assert agents["backend"]["status"] == "in_review"
    assert agents["backend"]["current_step"] is None


def test_status_agents_defaults_to_tech_lead_only_without_stacks(monkeypatch):
    """A record with no stack_specs (old/SE-pipeline) still yields a one-entry roster — the
    Tech Lead — rather than an empty list, so the panel always has something to show."""
    monkeypatch.setattr(api, "get_job", lambda jid: _job(status="running", phase="task_graph"))
    agents = client.get("/status/j1").json()["agents"]
    assert [a["agent_id"] for a in agents] == ["tech_lead"]
    assert agents[0]["status"] == "planning"


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


# --------------------------------------------------------------------------- clock-skew tolerance


def test_heartbeat_fresh_tolerates_small_forward_clock_skew():
    """A heartbeat stamped slightly in the future (NTP drift on a faster-clocked worker) must
    still be treated as fresh, so a live wait loop in that worker doesn't appear dead to the
    checking worker and prompt a spurious second orchestrator spawn."""
    from datetime import datetime, timedelta, timezone

    skewed = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    assert api._answer_wait_heartbeat_fresh({"answer_wait_heartbeat_at": skewed}) is True


def test_heartbeat_implausibly_future_stamp_is_not_fresh():
    """A stamp far in the future (more than _HEARTBEAT_CLOCK_SKEW_TOLERANCE_S ahead) is
    corruption or severe misconfiguration — it must NOT block resume indefinitely."""
    from datetime import datetime, timedelta, timezone

    far_future = (
        datetime.now(timezone.utc) + timedelta(seconds=api._HEARTBEAT_CLOCK_SKEW_TOLERANCE_S + 30)
    ).isoformat()
    assert api._answer_wait_heartbeat_fresh({"answer_wait_heartbeat_at": far_future}) is False


# --------------------------------------------------------------------------- post-claim terminal check


def test_auto_resume_aborts_post_claim_if_job_becomes_terminal(monkeypatch):
    """TOCTOU: cancellation that occurs after the resume claim is acquired but before the
    orchestrator is spawned must abort the spawn. The post-claim get_job re-read detects the
    terminal state and the claim is released so a later attempt can succeed."""
    waiting = _job()
    cancelled = _job(status="cancelled")
    read_count = {"n": 0}

    def _get_job(jid):
        read_count["n"] += 1
        # Reads 1 (endpoint) and 2 (TOCTOU reread after store) → waiting.
        # Read 3 (post-claim re-read inside _try_auto_resume) → cancelled.
        return waiting if read_count["n"] < 3 else cancelled

    monkeypatch.setattr(api, "get_job", _get_job)
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, a: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)
    release_calls: List[str] = []
    monkeypatch.setattr(api, "release_resume_claim", lambda jid: release_calls.append(jid))

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn for a job that became terminal after claiming")

    monkeypatch.setattr(api, "_start_orchestrator_thread", _no_spawn)
    monkeypatch.setattr(api, "_start_github_resume_thread", _no_spawn)

    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert release_calls, "claim must be released when the post-claim terminal check fires"


def test_auto_resume_aborts_post_claim_if_job_becomes_running(monkeypatch):
    """TOCTOU: if a concurrent worker advances the job from waiting_for_user to running between
    claim and post-claim re-read, the spawn must be aborted and the claim released. 'running' is
    non-terminal but non-waiting, so the post-claim status check must cover it — not just terminal
    states (which the earlier is_terminal check already handled)."""
    waiting = _job()
    running = _job(status="running", waiting_for_answers=False)
    read_count = {"n": 0}

    def _get_job(jid):
        read_count["n"] += 1
        # Reads 1 (endpoint) and 2 (TOCTOU reread after store) → waiting.
        # Read 3 (post-claim re-read inside _try_auto_resume) → running.
        return waiting if read_count["n"] < 3 else running

    monkeypatch.setattr(api, "get_job", _get_job)
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, a: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)
    release_calls: List[str] = []
    monkeypatch.setattr(api, "release_resume_claim", lambda jid: release_calls.append(jid))

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn for a job that became 'running' after claiming")

    monkeypatch.setattr(api, "_start_orchestrator_thread", _no_spawn)
    monkeypatch.setattr(api, "_start_github_resume_thread", _no_spawn)

    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert release_calls, "claim must be released when post-claim check finds job is 'running'"


def test_auto_resume_post_claim_store_error_releases_claim_and_falls_back_to_hint(monkeypatch):
    """A job-store transport error during the post-claim re-read in _try_auto_resume must release
    the claim immediately (so the TTL window is recovered) and return False so the endpoint falls
    back to the manual-resume hint — never leaves the claim wedged, never raises, never spawns."""
    waiting = _job()
    read_count = {"n": 0}

    def _get_job(jid):
        read_count["n"] += 1
        # Reads 1 (endpoint initial) and 2 (TOCTOU re-read after store) → waiting.
        # Read 3 (post-claim re-read inside _try_auto_resume) → store error.
        # Read 4+ (get_status at end of submit_pending_answers) → waiting (not part of test).
        if read_count["n"] == 3:
            raise RuntimeError("store error during post-claim read")
        return waiting

    monkeypatch.setattr(api, "get_job", _get_job)
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, a: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    calls: dict = {}
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: calls.update(kw))
    release_calls: List[str] = []
    monkeypatch.setattr(api, "release_resume_claim", lambda jid: release_calls.append(jid))

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn when post-claim store read fails")

    monkeypatch.setattr(api, "_start_orchestrator_thread", _no_spawn)
    monkeypatch.setattr(api, "_start_github_resume_thread", _no_spawn)

    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert release_calls, "claim must be released on post-claim store error"
    assert "Resume" in calls.get("status_text", ""), "must fall back to the manual-resume hint"


def test_resume_post_claim_abort_when_job_advances_to_running(monkeypatch):
    """TOCTOU on /resume: if the job status advances from waiting_for_user to running between
    claim_resume and the post-claim re-read, the endpoint must release the claim and return
    'already running' rather than spawning a second orchestrator for a job already in flight."""
    waiting = _job(status="waiting_for_user", plan_input={"requirements_title": "T"})
    running = _job(status="running", waiting_for_answers=False)
    read_count = {"n": 0}

    def _get_job(jid):
        read_count["n"] += 1
        # Read 1 (initial endpoint check): waiting. Read 2 (post-claim re-read): running.
        return waiting if read_count["n"] == 1 else running

    monkeypatch.setattr(api, "get_job", _get_job)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: None)
    release_calls: List[str] = []
    monkeypatch.setattr(api, "release_resume_claim", lambda jid: release_calls.append(jid))

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn when post-claim re-read finds job is 'running'")

    monkeypatch.setattr(api.threading, "Thread", _no_spawn)

    r = client.post("/run/j1/resume")
    assert r.status_code == 200
    assert "already running" in r.json()["message"]
    assert release_calls, "claim must be released when job is no longer waiting after claiming"


# --------------------------------------------------------------------------- GitHub resume status advance


def test_github_resume_thread_advances_status_before_io(monkeypatch):
    """The GitHub-resume thread must update the job to status='running' BEFORE the GitHub issue
    fetch. The cross-worker claim TTL is 60s; a slow issue fetch or branch prep could outlast it
    and let another worker steal the claim. Advancing the status out of waiting_for_user first
    closes that window: _try_auto_resume and resume_job only proceed for waiting_for_user jobs."""
    events: List[Any] = []
    ctx = {"owner": "acme", "repo": "widgets", "issue_number": 42, "remote": "origin"}

    monkeypatch.setattr(api, "get_job", lambda jid: _job(github_context=ctx))
    monkeypatch.setattr(api, "store_submit_answers", lambda jid, a: None)
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: False)
    monkeypatch.setattr(api, "update_job", lambda jid, **kw: events.append(("update", dict(kw))))

    class _FakeClient:
        def __init__(self, token=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_issue(self, owner, repo, number):
            events.append(("get_issue",))
            return {"number": number}

    monkeypatch.setattr(api, "GitHubClient", _FakeClient)
    monkeypatch.setattr(api, "_run_with_github_hooks", lambda *a, **k: None)

    class _SyncThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(api.threading, "Thread", _SyncThread)
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")

    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200

    running_update_idx = next(
        (i for i, e in enumerate(events) if e[0] == "update" and e[1].get("status") == "running"),
        None,
    )
    io_idx = next((i for i, e in enumerate(events) if e[0] == "get_issue"), None)
    assert running_update_idx is not None, "thread must update status to 'running'"
    assert io_idx is not None, "thread must call get_issue"
    assert running_update_idx < io_idx, "status must advance to 'running' before GitHub I/O"


# --------------------------------------------------------------------------- /answers: Temporal-native pause


def test_answers_temporal_native_signals_workflow_and_appends_without_clearing(monkeypatch):
    """A pause published under pause_strategy="return" carries a resume_token on the job
    record. Submitting answers for it must append-only store them (never clear the pause
    envelope -- that's the orchestrator's own re-entry check's job) and signal
    CodingTeamWorkflow directly, instead of the thread-liveness/auto-resume dance that only
    applies to a block-mode pause."""
    from software_engineering_team.api.routes import coding_team_hitl as hitl_route

    job = _job(resume_token="j1:tok-1")
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    appended: Dict[str, Any] = {}
    monkeypatch.setattr(
        api,
        "store_append_submitted_answers",
        lambda jid, answers: appended.update(job_id=jid, answers=answers),
    )
    signaled: Dict[str, Any] = {}
    monkeypatch.setattr(
        hitl_route,
        "signal_workflow_sync",
        lambda workflow_id, signal, payload: signaled.update(
            workflow_id=workflow_id, signal=signal, payload=payload
        ),
    )

    def _must_not_run(*_a, **_k):  # pragma: no cover - block-mode-only paths
        raise AssertionError("block-mode path must not run for a Temporal-native pause")

    monkeypatch.setattr(api, "store_submit_answers", _must_not_run)
    monkeypatch.setattr(api, "_is_run_thread_alive", _must_not_run)

    r = client.post(
        "/run/j1/answers",
        json={
            "answers": [{"question_id": "q1", "selected_option_id": "strict"}],
            "resume_token": "j1:tok-1",
        },
    )

    assert r.status_code == 200
    assert appended["job_id"] == "j1"
    assert appended["answers"][0]["question_id"] == "q1"
    assert signaled["workflow_id"] == "coding_team-j1"
    assert signaled["signal"] == "submit_answers"
    assert signaled["payload"]["resume_token"] == "j1:tok-1"
    assert signaled["payload"]["answers"] == appended["answers"]


def test_answers_temporal_native_rejects_stale_resume_token(monkeypatch):
    """A client echoing a resume_token that doesn't match the job's currently persisted one
    (stale, or answering an already-resolved pause) must get a 409, not a false-confidence 200
    while the workflow silently drops the mismatched signal -- contract doc §3."""
    from software_engineering_team.api.routes import coding_team_hitl as hitl_route

    job = _job(resume_token="j1:tok-current")
    monkeypatch.setattr(api, "get_job", lambda jid: job)

    def _must_not_run(*_a, **_k):  # pragma: no cover
        raise AssertionError("must not append/signal/store on a resume_token mismatch")

    monkeypatch.setattr(api, "store_append_submitted_answers", _must_not_run)
    monkeypatch.setattr(hitl_route, "signal_workflow_sync", _must_not_run)
    monkeypatch.setattr(api, "store_submit_answers", _must_not_run)

    r = client.post(
        "/run/j1/answers",
        json={
            "answers": [{"question_id": "q1", "selected_option_id": "strict"}],
            "resume_token": "j1:tok-stale",
        },
    )

    assert r.status_code == 409


def test_answers_temporal_native_rejects_missing_resume_token(monkeypatch):
    """A client that omits resume_token entirely for a Temporal-native pause is treated the
    same as a mismatch -- a legitimate client always has one, from the pause notification or a
    status poll."""
    from software_engineering_team.api.routes import coding_team_hitl as hitl_route

    job = _job(resume_token="j1:tok-current")
    monkeypatch.setattr(api, "get_job", lambda jid: job)

    def _must_not_run(*_a, **_k):  # pragma: no cover
        raise AssertionError("must not append/signal/store when resume_token is missing")

    monkeypatch.setattr(api, "store_append_submitted_answers", _must_not_run)
    monkeypatch.setattr(hitl_route, "signal_workflow_sync", _must_not_run)
    monkeypatch.setattr(api, "store_submit_answers", _must_not_run)

    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )

    assert r.status_code == 409


def test_answers_thread_mode_unaffected_when_no_resume_token(monkeypatch):
    """A job with no resume_token (block-mode / GitHub-hook pause) keeps today's exact
    behavior -- the new Temporal-signal branch must never be reached."""
    from software_engineering_team.api.routes import coding_team_hitl as hitl_route

    job = _job()  # no resume_token key at all
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    stored: Dict[str, Any] = {}
    monkeypatch.setattr(
        api, "store_submit_answers", lambda jid, answers: stored.update(answers=answers)
    )
    monkeypatch.setattr(api, "_is_run_thread_alive", lambda jid: True)

    def _no_signal(*_a, **_k):  # pragma: no cover
        raise AssertionError("must not signal the workflow for a block-mode pause")

    monkeypatch.setattr(hitl_route, "signal_workflow_sync", _no_signal)
    monkeypatch.setattr(api, "store_append_submitted_answers", _no_signal)

    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )

    assert r.status_code == 200
    assert stored["answers"][0]["question_id"] == "q1"
