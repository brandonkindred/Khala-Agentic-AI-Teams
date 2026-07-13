"""Tests for ``PersonalAssistantOrchestrator._dispatch_intent`` and its two
public callers, ``run`` (direct/library usage) and ``handle_request`` (the
thread-mode job-dispatch path).

Regression coverage for extracting the previously-duplicated intent if/elif
chain out of both methods into one shared ``_dispatch_intent`` helper.
"""

from __future__ import annotations

import pytest

from llm_service import LLMNotConfiguredError

from ..models import AssistantRequest
from ..orchestrator.agent import PersonalAssistantOrchestrator
from ..orchestrator.models import AgentAction, Intent, OrchestratorRequest, OrchestratorResponse


class _StubLLM:
    def complete(self, prompt, **kwargs):
        return ""

    def complete_json(self, prompt, **kwargs):
        return {}

    def get_max_context_tokens(self) -> int:
        return 4096


def _orchestrator() -> PersonalAssistantOrchestrator:
    return PersonalAssistantOrchestrator(_StubLLM())


_BRANCHES = [
    ("email", "_handle_email", "email"),
    ("calendar", "_handle_calendar", "calendar"),
    ("tasks", "_handle_tasks", "tasks"),
    ("deals", "_handle_deals", "deals"),
    ("reservations", "_handle_reservations", "reservations"),
    ("documentation", "_handle_documentation", "documentation"),
    ("profile", "_handle_profile", "profile"),
    ("general", "_handle_general", "general"),
    ("something_unknown", "_handle_general", "general"),  # fallback
]


@pytest.mark.parametrize("primary,handler_name,result_key", _BRANCHES)
def test_dispatch_intent_routes_to_correct_handler(monkeypatch, primary, handler_name, result_key):
    orch = _orchestrator()
    request = OrchestratorRequest(user_id="u1", message="do it")
    intent = Intent(primary=primary, confidence=0.9)
    action = AgentAction(agent=primary, action="ran", result={"ok": True})
    monkeypatch.setattr(orch, handler_name, lambda req, intent, _a=action: _a)

    actions, results = orch._dispatch_intent(request, intent)

    assert actions == [action]
    assert results == {result_key: {"ok": True}}


def test_dispatch_intent_calls_on_specialist_start_with_status_text(monkeypatch):
    orch = _orchestrator()
    request = OrchestratorRequest(user_id="u1", message="check my inbox")
    intent = Intent(primary="email", confidence=0.9)
    monkeypatch.setattr(
        orch, "_handle_email", lambda req, intent: AgentAction(agent="email", action="ran")
    )

    calls = []
    orch._dispatch_intent(request, intent, on_specialist_start=calls.append)

    assert calls == ["Handling email request..."]


def test_dispatch_intent_reraises_llm_not_configured(monkeypatch):
    orch = _orchestrator()
    request = OrchestratorRequest(user_id="u1", message="check my inbox")
    intent = Intent(primary="email", confidence=0.9)

    def _boom(req, intent):
        raise LLMNotConfiguredError("no provider configured")

    monkeypatch.setattr(orch, "_handle_email", _boom)

    with pytest.raises(LLMNotConfiguredError):
        orch._dispatch_intent(request, intent)


def test_dispatch_intent_backend_error_becomes_degraded_action_with_empty_results(monkeypatch):
    orch = _orchestrator()
    request = OrchestratorRequest(user_id="u1", message="check my inbox")
    intent = Intent(primary="email", confidence=0.9)

    def _boom(req, intent):
        raise RuntimeError("calendar backend down")

    monkeypatch.setattr(orch, "_handle_email", _boom)

    actions, results = orch._dispatch_intent(request, intent)

    assert len(actions) == 1
    assert actions[0].agent == "orchestrator"
    assert actions[0].action == "error"
    assert actions[0].success is False
    assert "calendar backend down" in actions[0].result["error"]
    # No result populated under the intent's key on a caught handler error —
    # matches the Temporal activity path's equivalent contract.
    assert results == {}


def test_run_dispatches_and_builds_response(monkeypatch):
    orch = _orchestrator()
    monkeypatch.setattr(
        orch, "classify_intent", lambda message: Intent(primary="email", confidence=0.9)
    )
    monkeypatch.setattr(
        orch,
        "_handle_email",
        lambda req, intent: AgentAction(agent="email", action="read", result={"n": 1}),
    )
    monkeypatch.setattr(orch, "_check_for_profile_updates", lambda req: [{"pref": "x"}])
    monkeypatch.setattr(
        orch,
        "_generate_response",
        lambda req, intent, actions, results: OrchestratorResponse(
            message="done", intent=intent, actions_taken=["email:read"], data=results
        ),
    )

    response = orch.run(OrchestratorRequest(user_id="u1", message="read my inbox"))

    assert response.message == "done"
    assert response.data == {"email": {"n": 1}}
    assert response.profile_updates == [{"pref": "x"}]


def test_run_propagates_llm_not_configured(monkeypatch):
    orch = _orchestrator()
    monkeypatch.setattr(
        orch, "classify_intent", lambda message: Intent(primary="email", confidence=0.9)
    )

    def _boom(req, intent):
        raise LLMNotConfiguredError("no provider configured")

    monkeypatch.setattr(orch, "_handle_email", _boom)

    with pytest.raises(LLMNotConfiguredError):
        orch.run(OrchestratorRequest(user_id="u1", message="read my inbox"))


def test_handle_request_cancelled_before_classify_intent():
    orch = _orchestrator()
    request = AssistantRequest(request_id="job-1", user_id="u1", message="hi")

    response = orch.handle_request(request, job_updater=lambda **_kw: False)

    assert response.message == "Request was cancelled."
    assert response.actions_taken == ["cancelled"]


def test_handle_request_cancelled_after_classify_intent(monkeypatch):
    orch = _orchestrator()
    monkeypatch.setattr(
        orch, "classify_intent", lambda message: Intent(primary="email", confidence=0.9)
    )
    dispatch_calls = []
    monkeypatch.setattr(orch, "_handle_email", lambda req, intent: dispatch_calls.append(1))
    request = AssistantRequest(request_id="job-1", user_id="u1", message="hi")

    calls = {"n": 0}

    def _job_updater(**_kw):
        calls["n"] += 1
        return (
            calls["n"] < 2
        )  # True on the 1st (pre-classify) call, False on the 2nd (post-classify)

    response = orch.handle_request(request, job_updater=_job_updater)

    assert response.message == "Request was cancelled."
    # Must have cancelled AFTER classify_intent ran but BEFORE dispatching to
    # the specialist — distinguishing this from the "before classify_intent"
    # cancellation test.
    assert calls["n"] == 2
    assert dispatch_calls == []


def test_handle_request_dispatches_with_progress_updates(monkeypatch):
    orch = _orchestrator()
    monkeypatch.setattr(
        orch, "classify_intent", lambda message: Intent(primary="email", confidence=0.9)
    )
    monkeypatch.setattr(
        orch,
        "_handle_email",
        lambda req, intent: AgentAction(agent="email", action="read", result={"n": 1}),
    )
    monkeypatch.setattr(orch, "_check_for_profile_updates", lambda req: [])
    monkeypatch.setattr(
        orch,
        "_generate_response",
        lambda req, intent, actions, results: OrchestratorResponse(
            message="done", intent=intent, actions_taken=["email:read"], data=results
        ),
    )
    request = AssistantRequest(request_id="job-1", user_id="u1", message="check my inbox")
    progress_texts = []

    def _job_updater(status_text=None, progress=None, request_type=None):
        if status_text is not None:
            progress_texts.append(status_text)
        return True

    response = orch.handle_request(request, job_updater=_job_updater)

    assert response.message == "done"
    assert response.data == {"email": {"n": 1}}
    assert "Handling email request..." in progress_texts
    assert "Request completed" in progress_texts


def test_handle_request_backend_error_becomes_degraded_response(monkeypatch):
    orch = _orchestrator()
    monkeypatch.setattr(
        orch, "classify_intent", lambda message: Intent(primary="email", confidence=0.9)
    )

    def _boom(req, intent):
        raise RuntimeError("backend down")

    monkeypatch.setattr(orch, "_handle_email", _boom)
    monkeypatch.setattr(orch, "_check_for_profile_updates", lambda req: [])
    captured = {}

    def _capture_generate_response(req, intent, actions, results):
        captured["actions"] = actions
        captured["results"] = results
        return OrchestratorResponse(message="ok", intent=intent, actions_taken=[], data=results)

    monkeypatch.setattr(orch, "_generate_response", _capture_generate_response)
    request = AssistantRequest(request_id="job-1", user_id="u1", message="check my inbox")

    orch.handle_request(request)

    assert captured["actions"][0].agent == "orchestrator"
    assert captured["actions"][0].action == "error"
    assert captured["results"] == {}


def test_handle_request_propagates_llm_not_configured(monkeypatch):
    orch = _orchestrator()
    monkeypatch.setattr(
        orch, "classify_intent", lambda message: Intent(primary="email", confidence=0.9)
    )

    def _boom(req, intent):
        raise LLMNotConfiguredError("no provider configured")

    monkeypatch.setattr(orch, "_handle_email", _boom)
    request = AssistantRequest(request_id="job-1", user_id="u1", message="check my inbox")

    with pytest.raises(LLMNotConfiguredError):
        orch.handle_request(request)
