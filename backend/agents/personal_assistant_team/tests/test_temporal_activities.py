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
        self.calls.append(("profile_updates", request.user_id))
        return [{"pref": "coffee"}]

    def _generate_response(self, request, intent, actions, results) -> OrchestratorResponse:
        self.calls.append(("generate", request.user_id, intent.primary, results))
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


def test_classify_intent_activity_cancelled(orchestrator, job_client, monkeypatch):
    _new_job(job_client)
    # A cancel landing after the RUNNING stamp: force the guard to trip.
    monkeypatch.setattr(
        "personal_assistant_team.shared.pa_job_store.is_job_cancelled", lambda job_id: True
    )
    assert acts.classify_intent_activity("job-1", "check my inbox") == {"cancelled": True}
    assert ("classify", "check my inbox") not in orchestrator.calls


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


# --------------------------------------------------------------------------- #
# profile-updates / response / finalize / fail
# --------------------------------------------------------------------------- #


def test_check_profile_updates_activity(orchestrator, job_client):
    _new_job(job_client)
    result = acts.check_profile_updates_activity("job-1", "u1", "I love oat milk")

    assert result == [{"pref": "coffee"}]
    assert job_client.get_job("job-1")["progress"] == 70


def test_generate_response_activity(orchestrator, job_client):
    _new_job(job_client)
    action = AgentAction(agent="email", action="read", result={"ok": True}).model_dump()
    result = acts.generate_response_activity(
        "job-1", "u1", "read inbox", _intent("email"), [action], {"email": {"ok": True}}
    )

    assert result["message"] == "all done"
    assert result["actions_taken"] == ["email:read"]
    assert result["follow_up_suggestions"] == ["what next?"]
    assert job_client.get_job("job-1")["progress"] == 85


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


def test_fail_job_activity(job_client):
    _new_job(job_client)
    acts.fail_job_activity("job-1", "boom")

    job = job_client.get_job("job-1")
    assert job["status"] == "failed"
    assert job["error"] == "boom"
    assert job["status_text"] == "Error: boom"


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
