"""Unit tests for the decomposed personal-assistant Temporal activities.

Each ``@activity.defn`` is exercised directly as a plain function (no Temporal
harness), with ``core.get_orchestrator`` and the PA job store faked. This is the
house style for activity tests across the codebase.
"""

from __future__ import annotations

import sys
import types

import pytest

from personal_assistant_team.orchestrator.models import (
    AgentAction,
    Intent,
    OrchestratorResponse,
)
from personal_assistant_team.temporal import activities as acts


class _FakeOrchestrator:
    """Minimal orchestrator stand-in recording the calls the activities make."""

    def __init__(self) -> None:
        self.calls: list = []

    def classify_intent(self, message: str) -> Intent:
        self.calls.append(("classify", message))
        return Intent(primary="email", confidence=0.9, entities={"x": 1})

    def _check_for_profile_updates(self, request) -> list:
        self.calls.append(("profile_updates", request.user_id, request.context))
        return [{"pref": "coffee"}]

    def _generate_response(self, request, intent, actions, results) -> OrchestratorResponse:
        self.calls.append(("generate", request.user_id, intent.primary, results, request.context))
        return OrchestratorResponse(
            message="all done",
            intent=intent,
            actions_taken=[f"{a.agent}:{a.action}" for a in actions],
            data=results,
            follow_up_suggestions=["what next?"],
        )

    def __getattr__(self, name):
        # Serves the eight ``_handle_*`` specialist methods generically.
        if name.startswith("_handle_"):
            agent = name.replace("_handle_", "")

            def handler(request, intent, _agent=agent):
                self.calls.append((_agent, request.user_id, intent.primary))
                return AgentAction(agent=_agent, action="ran", result={"ok": True})

            return handler
        raise AttributeError(name)


@pytest.fixture
def orchestrator(monkeypatch) -> _FakeOrchestrator:
    fake = _FakeOrchestrator()
    monkeypatch.setattr("personal_assistant_team.core.get_orchestrator", lambda: fake)
    return fake


@pytest.fixture
def job_client(monkeypatch):
    from job_service_client_fake import FakeJobServiceClient

    fake = FakeJobServiceClient(team="personal_assistant_team")
    monkeypatch.setattr("personal_assistant_team.shared.pa_job_store._client", lambda *a, **k: fake)
    return fake


def _new_job(job_client, job_id="job-1", status="running"):
    job_client.create_job(job_id, status=status, user_id="u1", request_type="assistant")


def _intent(primary="email"):
    return Intent(primary=primary, confidence=0.9, entities={"x": 1}).model_dump()


# --------------------------------------------------------------------------- #
# classify
# --------------------------------------------------------------------------- #


def test_classify_intent_activity(orchestrator, job_client):
    _new_job(job_client, status="pending")
    result = acts.classify_intent_activity("job-1", "check my inbox")

    assert result["primary"] == "email"
    job = job_client.get_job("job-1")
    assert job["status"] == "running"
    assert job["progress"] == 15
    assert job["request_type"] == "email"
    assert job["status_text"] == "Processing email request..."
    assert ("classify", "check my inbox") in orchestrator.calls


def test_classify_intent_activity_cancelled_before_running_stamp(orchestrator, job_client):
    # A job cancelled before the first activity must NOT be clobbered back to
    # RUNNING: the guard runs before the status stamp.
    _new_job(job_client, status="cancelled")
    assert acts.classify_intent_activity("job-1", "check my inbox") == {"cancelled": True}
    assert ("classify", "check my inbox") not in orchestrator.calls
    # Status is left untouched (not stamped to RUNNING).
    assert job_client.get_job("job-1")["status"] == "cancelled"


# --------------------------------------------------------------------------- #
# specialists
# --------------------------------------------------------------------------- #


_SPECIALISTS = [
    (acts.handle_email_activity, "email", "Handling email request..."),
    (acts.handle_calendar_activity, "calendar", "Checking your calendar..."),
    (acts.handle_tasks_activity, "tasks", "Managing your tasks..."),
    (acts.handle_deals_activity, "deals", "Searching for deals..."),
    (acts.handle_reservations_activity, "reservations", "Processing reservation request..."),
    (acts.handle_documentation_activity, "documentation", "Generating documentation..."),
    (acts.handle_profile_activity, "profile", "Updating your profile..."),
    (acts.handle_general_activity, "general", "Processing your request..."),
]


@pytest.mark.parametrize("activity_fn,agent,status_text", _SPECIALISTS)
def test_specialist_activity(orchestrator, job_client, activity_fn, agent, status_text):
    _new_job(job_client)
    result = activity_fn("job-1", "u1", "do the thing", {}, _intent(agent))

    assert result["agent"] == agent
    assert result["success"] is True
    job = job_client.get_job("job-1")
    assert job["progress"] == 30
    assert job["status_text"] == status_text


def test_specialist_activity_cancelled_short_circuits(orchestrator, job_client):
    _new_job(job_client, status="cancelled")
    result = acts.handle_email_activity("job-1", "u1", "read inbox", {}, _intent("email"))

    assert result == {"cancelled": True}
    # No specialist call and no progress write happened.
    assert orchestrator.calls == []
    assert job_client.get_job("job-1")["status"] == "cancelled"


def test_specialist_activity_backend_error_becomes_error_action(job_client, monkeypatch):
    # A non-LLM specialist failure yields a degraded orchestrator:error action so
    # the job still completes, matching the thread path — not a failed job.
    class _Boom:
        def _handle_email(self, request, intent):
            raise RuntimeError("calendar backend down")

    monkeypatch.setattr("personal_assistant_team.core.get_orchestrator", lambda: _Boom())
    _new_job(job_client)

    result = acts.handle_email_activity("job-1", "u1", "read inbox", {}, _intent("email"))

    assert result["agent"] == "orchestrator"
    assert result["action"] == "error"
    assert result["success"] is False
    assert "calendar backend down" in result["result"]["error"]


def test_specialist_activity_reraises_llm_not_configured(job_client, monkeypatch):
    from llm_service import LLMNotConfiguredError

    class _NoLLM:
        def _handle_email(self, request, intent):
            raise LLMNotConfiguredError("no provider configured")

    monkeypatch.setattr("personal_assistant_team.core.get_orchestrator", lambda: _NoLLM())
    _new_job(job_client)

    with pytest.raises(LLMNotConfiguredError):
        acts.handle_email_activity("job-1", "u1", "read inbox", {}, _intent("email"))


def test_specialist_activity_malformed_intent_raises_loudly(orchestrator, job_client):
    # Intent(**intent) reconstruction is a contract check, not specialist work:
    # a malformed intent dict (missing the required `primary` field) must fail
    # loudly, NOT be caught and downgraded into a degraded orchestrator:error
    # action the way a genuine handler exception is.
    import pydantic

    _new_job(job_client)

    with pytest.raises(pydantic.ValidationError):
        acts.handle_email_activity("job-1", "u1", "read inbox", {}, {"confidence": 0.9})

    # The handler itself must never have been invoked.
    assert orchestrator.calls == []


# --------------------------------------------------------------------------- #
# profile-updates / response / finalize / fail
# --------------------------------------------------------------------------- #


def test_check_profile_updates_activity(orchestrator, job_client):
    _new_job(job_client)
    result = acts.check_profile_updates_activity("job-1", "u1", "I love oat milk", {"tz": "UTC"})

    assert result == [{"pref": "coffee"}]
    assert job_client.get_job("job-1")["progress"] == 70
    # The caller-supplied context is threaded through to the orchestrator
    # request, matching every other activity in this module (not hardcoded
    # to {}).
    assert ("profile_updates", "u1", {"tz": "UTC"}) in orchestrator.calls


def test_check_profile_updates_activity_defaults_context_to_empty_dict(orchestrator, job_client):
    _new_job(job_client)
    acts.check_profile_updates_activity("job-1", "u1", "I love oat milk")

    assert ("profile_updates", "u1", {}) in orchestrator.calls


def test_check_profile_updates_activity_cancelled(orchestrator, job_client):
    # A cancel after the specialist step must skip the extraction LLM call and
    # the profile mutation, and signal cancellation to the workflow (None).
    _new_job(job_client, status="cancelled")
    assert acts.check_profile_updates_activity("job-1", "u1", "I love oat milk") is None
    assert orchestrator.calls == []
    assert job_client.get_job("job-1").get("progress") != 70


def test_generate_response_activity(orchestrator, job_client):
    _new_job(job_client)
    action = AgentAction(agent="email", action="read", result={"ok": True}).model_dump()
    result = acts.generate_response_activity(
        "job-1",
        "u1",
        "read inbox",
        _intent("email"),
        [action],
        {"email": {"ok": True}},
        [{"pref": "coffee"}],
        {"tz": "UTC"},
    )

    assert result["message"] == "all done"
    assert result["actions_taken"] == ["email:read"]
    assert result["follow_up_suggestions"] == ["what next?"]
    # profile_updates from the (concurrent-in-time, sequential-in-code)
    # profile-update step is merged into the response — matches the thread
    # path's `response.profile_updates = profile_updates` assignment.
    assert result["profile_updates"] == [{"pref": "coffee"}]
    assert ("generate", "u1", "email", {"email": {"ok": True}}, {"tz": "UTC"}) in orchestrator.calls
    job = job_client.get_job("job-1")
    # The intermediate "Request completed" write (matching the thread path's
    # _update call right after _generate_response returns) lands at the end
    # of this activity, ahead of finalize_success_activity's final write.
    assert job["progress"] == 100
    assert job["status_text"] == "Request completed"


def test_generate_response_activity_defaults_profile_updates_to_empty_list(
    orchestrator, job_client
):
    _new_job(job_client)
    action = AgentAction(agent="email", action="read", result={"ok": True}).model_dump()
    result = acts.generate_response_activity(
        "job-1", "u1", "read inbox", _intent("email"), [action], {"email": {"ok": True}}
    )

    assert result["profile_updates"] == []


def test_generate_response_activity_cancelled(orchestrator, job_client):
    # A cancel before response generation skips the response LLM call and signals
    # cancellation so the workflow skips finalize.
    _new_job(job_client, status="cancelled")
    result = acts.generate_response_activity("job-1", "u1", "read inbox", _intent("email"), [], {})
    assert result == {"cancelled": True}
    assert orchestrator.calls == []


def test_finalize_success_activity(job_client, monkeypatch):
    _new_job(job_client)
    notified = {}
    monkeypatch.setattr(
        acts, "_notify_slack", lambda user_id, message, resp: notified.update(u=user_id)
    )
    response = {
        "message": "all done",
        "actions_taken": ["email:read"],
        "data": {"email": {"ok": True}},
        "follow_up_suggestions": ["what next?"],
    }
    acts.finalize_success_activity("job-1", response, "u1", "read inbox")

    job = job_client.get_job("job-1")
    assert job["status"] == "completed"
    assert job["progress"] == 100
    assert job["status_text"] == "Request completed successfully"
    stored = job["response"]
    assert stored["request_id"] == "job-1"
    assert stored["message"] == "all done"
    assert stored["actions_taken"] == ["email:read"]
    assert stored["follow_up_suggestions"] == ["what next?"]
    assert notified == {"u": "u1"}


def test_finalize_success_activity_defaults_missing_fields(job_client, monkeypatch):
    _new_job(job_client)
    monkeypatch.setattr(acts, "_notify_slack", lambda *a, **k: None)
    acts.finalize_success_activity("job-1", {}, "u1", "hi")

    stored = job_client.get_job("job-1")["response"]
    assert stored["message"] == "I've processed your request."
    assert stored["actions_taken"] == []


def test_finalize_success_activity_skips_cancelled_job(job_client, monkeypatch):
    # A job cancelled after the last cancel-checked step must not be completed.
    _new_job(job_client, status="cancelled")
    notified = {"called": False}
    monkeypatch.setattr(acts, "_notify_slack", lambda *a, **k: notified.update(called=True))

    acts.finalize_success_activity("job-1", {"message": "done"}, "u1", "hi")

    job = job_client.get_job("job-1")
    assert job["status"] == "cancelled"
    assert job.get("response") is None
    assert notified["called"] is False


def test_finalize_success_activity_notifies_only_once_on_retry(job_client, monkeypatch):
    # A retry (e.g. worker crash before Temporal recorded completion) must not
    # re-send the non-idempotent Slack notification.
    _new_job(job_client)
    notify_count = {"n": 0}
    monkeypatch.setattr(
        acts, "_notify_slack", lambda *a, **k: notify_count.update(n=notify_count["n"] + 1)
    )
    response = {"message": "done", "actions_taken": [], "data": {}, "follow_up_suggestions": []}

    acts.finalize_success_activity("job-1", response, "u1", "hi")
    acts.finalize_success_activity("job-1", response, "u1", "hi")  # retry

    assert job_client.get_job("job-1")["status"] == "completed"
    assert notify_count["n"] == 1


def test_fail_job_activity(job_client):
    _new_job(job_client)
    acts.fail_job_activity("job-1", "boom")

    job = job_client.get_job("job-1")
    assert job["status"] == "failed"
    assert job["error"] == "boom"
    assert job["status_text"] == "Error: boom"


def test_fail_job_activity_does_not_overwrite_cancelled(job_client):
    # A user cancellation takes precedence over a downstream error.
    _new_job(job_client, status="cancelled")
    acts.fail_job_activity("job-1", "boom")

    job = job_client.get_job("job-1")
    assert job["status"] == "cancelled"
    assert job.get("error") is None


def test_run_assistant_activity_legacy_delegates_to_thread_runner(monkeypatch):
    # The retained legacy activity runs the monolithic thread-path job runner.
    import personal_assistant_team.api.main as api_main

    captured = {}
    monkeypatch.setattr(
        api_main,
        "_run_assistant_job",
        lambda job_id, user_id, message, context: captured.update(
            job_id=job_id, user_id=user_id, message=message, context=context
        ),
    )

    acts.run_assistant_activity("job-9", "u9", "legacy", {"k": "v"})

    assert captured == {
        "job_id": "job-9",
        "user_id": "u9",
        "message": "legacy",
        "context": {"k": "v"},
    }


def test_run_assistant_activity_legacy_reraises_on_failure(monkeypatch):
    import personal_assistant_team.api.main as api_main

    def _boom(*_a, **_k):
        raise RuntimeError("legacy blew up")

    monkeypatch.setattr(api_main, "_run_assistant_job", _boom)

    with pytest.raises(RuntimeError, match="legacy blew up"):
        acts.run_assistant_activity("job-9", "u9", "legacy", None)


# --------------------------------------------------------------------------- #
# slack notification helper
# --------------------------------------------------------------------------- #


def _install_fake_notifier(monkeypatch, fn):
    module = types.ModuleType("unified_api.slack_notifier")
    module.notify_pa_response = fn
    monkeypatch.setitem(sys.modules, "unified_api.slack_notifier", module)


def test_notify_slack_delivers(monkeypatch):
    captured = {}
    _install_fake_notifier(monkeypatch, lambda *a: captured.setdefault("args", a))
    resp = types.SimpleNamespace(message="m", actions_taken=["a"], follow_up_suggestions=["s"])

    acts._notify_slack("u1", "msg", resp)

    assert captured["args"] == ("u1", "msg", "m", ["a"], ["s"])


def test_notify_slack_swallows_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "unified_api.slack_notifier", None)
    resp = types.SimpleNamespace(message="m", actions_taken=[], follow_up_suggestions=[])
    # Must not raise even though the notifier can't be imported.
    acts._notify_slack("u1", "msg", resp)


def test_notify_slack_swallows_delivery_error(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("slack down")

    _install_fake_notifier(monkeypatch, _boom)
    resp = types.SimpleNamespace(message="m", actions_taken=[], follow_up_suggestions=[])
    # Delivery failure is best-effort — swallowed, never fails the job.
    acts._notify_slack("u1", "msg", resp)
