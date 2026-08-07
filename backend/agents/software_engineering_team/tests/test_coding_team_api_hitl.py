"""Tests for the coding-team API human-in-the-loop surface: /status pending questions, the
/run/{job_id}/answers endpoint, /run/{job_id}/resume, and the GitHub issue-comment formatter."""

from __future__ import annotations

from typing import Any, Dict

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from software_engineering_team import token_crypto
from software_engineering_team.api import coding_team_main as api

client = TestClient(api.app)


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
    r = client.post(
        "/run/j1/answers", json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]}
    )
    assert r.status_code == 200
    assert stored["answers"][0]["question_id"] == "q1"
    assert stored["answers"][0]["question_text"] == "Allergen strictness default?"


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


# --------------------------------------------------------------------------- /run/{id}/resume


def test_resume_404(monkeypatch):
    monkeypatch.setattr(api, "get_job", lambda jid: None)
    assert client.post("/run/j1/resume").status_code == 404


def test_resume_400_when_terminal(monkeypatch):
    """A finished run must never be silently re-executed."""
    for status in ("completed", "completed_with_failures", "failed", "cancelled"):
        monkeypatch.setattr(api, "get_job", lambda jid, s=status: _job(status=s))
        r = client.post("/run/j1/resume")
        assert r.status_code == 400
        assert "cannot be resumed" in r.json()["detail"]


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


def test_answers_without_resume_token_stores_only_no_auto_resume(monkeypatch):
    """Block-mode /answers stores answers and must not signal the workflow."""
    from software_engineering_team.api.routes import coding_team_hitl as hitl_route

    job = _job()  # no resume_token
    monkeypatch.setattr(api, "get_job", lambda jid: job)
    stored: Dict[str, Any] = {}
    monkeypatch.setattr(
        api, "store_submit_answers", lambda jid, answers: stored.update(answers=answers)
    )

    def _must_not(*_a, **_k):  # pragma: no cover
        raise AssertionError("must not signal for block-mode answers")

    monkeypatch.setattr(hitl_route, "signal_workflow_sync", _must_not)
    monkeypatch.setattr(api, "store_append_submitted_answers", _must_not)

    r = client.post(
        "/run/j1/answers",
        json={"answers": [{"question_id": "q1", "selected_option_id": "strict"}]},
    )
    assert r.status_code == 200
    assert stored["answers"][0]["question_id"] == "q1"


# --------------------------------------------------------------------------- /resume: Temporal-native pause


def test_resume_400_when_no_resume_token(monkeypatch):
    """Without resume_token, /resume must not claim/spawn — Temporal-native only."""
    from software_engineering_team.api.routes import coding_team_hitl as hitl_route

    monkeypatch.setattr(api, "get_job", lambda jid: _job(status="waiting_for_user"))
    monkeypatch.setattr(
        hitl_route,
        "signal_workflow_sync",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not signal")),
    )

    r = client.post("/run/j1/resume")
    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    assert "resume_token" in detail or "temporal" in detail


def test_resume_temporal_native_signals_workflow(monkeypatch):
    """A Temporal-native pause (resume_token on the job) must wake CodingTeamWorkflow
    via submit_answers."""
    from software_engineering_team.api.routes import coding_team_hitl as hitl_route

    answers = [{"question_id": "q1", "selected_option_id": "strict"}]
    job = _job(
        resume_token="j1:tok-1",
        submitted_answers=answers,
        status="waiting_for_user",
    )
    monkeypatch.setattr(api, "get_job", lambda jid: job)

    signaled: Dict[str, Any] = {}
    monkeypatch.setattr(
        hitl_route,
        "signal_workflow_sync",
        lambda workflow_id, signal, payload: signaled.update(
            workflow_id=workflow_id, signal=signal, payload=payload
        ),
    )

    r = client.post("/run/j1/resume")

    assert r.status_code == 200
    assert r.json()["message"] == "Job resumed."
    assert r.json()["status"] == "running"
    assert signaled["workflow_id"] == "coding_team-j1"
    assert signaled["signal"] == "submit_answers"
    assert signaled["payload"] == {"resume_token": "j1:tok-1", "answers": answers}


def test_resume_temporal_native_signals_empty_answers_when_none_stored(monkeypatch):
    """If submitted_answers is missing, the signal still carries answers: []."""
    from software_engineering_team.api.routes import coding_team_hitl as hitl_route

    job = _job(resume_token="j1:tok-2", status="waiting_for_user")
    # _job has no submitted_answers key
    monkeypatch.setattr(api, "get_job", lambda jid: job)

    signaled: Dict[str, Any] = {}
    monkeypatch.setattr(
        hitl_route,
        "signal_workflow_sync",
        lambda workflow_id, signal, payload: signaled.update(
            workflow_id=workflow_id, signal=signal, payload=payload
        ),
    )

    r = client.post("/run/j1/resume")

    assert r.status_code == 200
    assert signaled["payload"] == {"resume_token": "j1:tok-2", "answers": []}


def test_resume_400_when_terminal_even_with_resume_token(monkeypatch):
    """Terminal check runs before the Temporal branch; resume_token must not change the 400."""
    from software_engineering_team.api.routes import coding_team_hitl as hitl_route

    def _must_not_signal(*_a, **_k):  # pragma: no cover
        raise AssertionError("must not signal a terminal job")

    monkeypatch.setattr(hitl_route, "signal_workflow_sync", _must_not_signal)

    for status in ("completed", "completed_with_failures", "failed", "cancelled"):
        monkeypatch.setattr(
            api, "get_job", lambda jid, s=status: _job(status=s, resume_token="j1:tok-x")
        )
        r = client.post("/run/j1/resume")
        assert r.status_code == 400
        assert "cannot be resumed" in r.json()["detail"]
