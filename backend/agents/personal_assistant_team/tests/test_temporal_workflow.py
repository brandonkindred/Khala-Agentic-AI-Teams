"""Tests for ``PaAssistantWorkflow`` orchestration.

The workflow is driven directly under ``asyncio.run`` with
``workflow.execute_activity`` monkeypatched to a scripted async stub — no
Temporal server needed (house style, mirrors job_matching's workflow test).
"""

from __future__ import annotations

import asyncio

import pytest

from personal_assistant_team.temporal import activities as acts
from personal_assistant_team.temporal import workflows as wf


def _script(monkeypatch, table, *, calls=None, arg_log=None, patched=True):
    """Install a scripted ``execute_activity`` returning ``table[activity_fn]``.

    ``table`` values may be a plain result or a callable ``(args) -> result``;
    a callable that raises is used to simulate an activity failure. ``patched``
    stubs ``workflow.patched`` (True → decomposed flow, False → legacy branch).
    """

    async def _fake_execute_activity(activity_fn, *, args, **_kwargs):
        if calls is not None:
            calls.append(activity_fn)
        if arg_log is not None:
            arg_log[activity_fn] = args
        if activity_fn not in table:
            raise AssertionError(f"unexpected activity {activity_fn}")
        value = table[activity_fn]
        return value(args) if callable(value) else value

    monkeypatch.setattr(wf.workflow, "execute_activity", _fake_execute_activity)
    monkeypatch.setattr(wf.workflow, "patched", lambda _patch_id: patched)


def _run(job_id="job-1", user_id="u1", message="do it", context=None):
    return asyncio.run(wf.PaAssistantWorkflow().run(job_id, user_id, message, context or {}))


def test_happy_path_email(monkeypatch):
    calls: list = []
    arg_log: dict = {}
    _script(
        monkeypatch,
        {
            acts.classify_intent_activity: {"primary": "email", "confidence": 0.9, "entities": {}},
            acts.handle_email_activity: {
                "agent": "email",
                "action": "read",
                "result": {"n": 1},
                "success": True,
            },
            acts.check_profile_updates_activity: [{"pref": "x"}],
            acts.generate_response_activity: {
                "message": "done",
                "actions_taken": ["email:read"],
                "data": {},
                "follow_up_suggestions": [],
            },
            acts.finalize_success_activity: None,
        },
        calls=calls,
        arg_log=arg_log,
    )

    result = _run(message="read my inbox")

    assert result["message"] == "done"
    assert calls == [
        acts.classify_intent_activity,
        acts.handle_email_activity,
        acts.check_profile_updates_activity,
        acts.generate_response_activity,
        acts.finalize_success_activity,
    ]
    # The specialist result is threaded into response generation under its key.
    gen_args = arg_log[acts.generate_response_activity]
    assert gen_args[4] == [
        {"agent": "email", "action": "read", "result": {"n": 1}, "success": True}
    ]
    assert gen_args[5] == {"email": {"n": 1}}


_BRANCHES = [
    ("email", acts.handle_email_activity),
    ("calendar", acts.handle_calendar_activity),
    ("tasks", acts.handle_tasks_activity),
    ("deals", acts.handle_deals_activity),
    ("reservations", acts.handle_reservations_activity),
    ("documentation", acts.handle_documentation_activity),
    ("profile", acts.handle_profile_activity),
    ("general", acts.handle_general_activity),
    ("something_unknown", acts.handle_general_activity),  # fallback
]


@pytest.mark.parametrize("primary,expected_activity", _BRANCHES)
def test_intent_branching(monkeypatch, primary, expected_activity):
    calls: list = []
    _script(
        monkeypatch,
        {
            acts.classify_intent_activity: {"primary": primary, "confidence": 0.5, "entities": {}},
            expected_activity: {"agent": primary, "action": "ran", "result": {}, "success": True},
            acts.check_profile_updates_activity: [],
            acts.generate_response_activity: {"message": "ok"},
            acts.finalize_success_activity: None,
        },
        calls=calls,
    )

    _run()

    assert calls[1] is expected_activity


def test_cancelled_at_classify(monkeypatch):
    calls: list = []
    _script(
        monkeypatch,
        {acts.classify_intent_activity: {"cancelled": True}},
        calls=calls,
    )

    result = _run()

    assert result == {"cancelled": True}
    assert calls == [acts.classify_intent_activity]


def test_cancelled_at_specialist(monkeypatch):
    calls: list = []
    _script(
        monkeypatch,
        {
            acts.classify_intent_activity: {"primary": "email", "confidence": 1.0, "entities": {}},
            acts.handle_email_activity: {"cancelled": True},
        },
        calls=calls,
    )

    result = _run()

    assert result == {"cancelled": True}
    assert calls == [acts.classify_intent_activity, acts.handle_email_activity]


def test_cancelled_at_profile_updates(monkeypatch):
    # check_profile_updates returns None (cancelled) -> workflow stops before
    # generating a response or finalizing.
    calls: list = []
    _script(
        monkeypatch,
        {
            acts.classify_intent_activity: {"primary": "email", "confidence": 1.0, "entities": {}},
            acts.handle_email_activity: {"agent": "email", "action": "r", "result": {}},
            acts.check_profile_updates_activity: None,
        },
        calls=calls,
    )

    result = _run()

    assert result == {"cancelled": True}
    assert calls == [
        acts.classify_intent_activity,
        acts.handle_email_activity,
        acts.check_profile_updates_activity,
    ]


def test_cancelled_at_generate_response(monkeypatch):
    # generate_response returns the cancelled sentinel -> workflow returns it and
    # skips finalize.
    calls: list = []
    _script(
        monkeypatch,
        {
            acts.classify_intent_activity: {"primary": "email", "confidence": 1.0, "entities": {}},
            acts.handle_email_activity: {"agent": "email", "action": "r", "result": {}},
            acts.check_profile_updates_activity: [],
            acts.generate_response_activity: {"cancelled": True},
        },
        calls=calls,
    )

    result = _run()

    assert result == {"cancelled": True}
    assert calls == [
        acts.classify_intent_activity,
        acts.handle_email_activity,
        acts.check_profile_updates_activity,
        acts.generate_response_activity,
    ]


def test_failure_marks_job_and_reraises(monkeypatch):
    class _FakeActivityError(Exception):
        cause = None

    # Make the workflow's ``except ActivityError`` catch our fake failure.
    monkeypatch.setattr(wf, "ActivityError", _FakeActivityError)

    calls: list = []

    def _raise(_args):
        raise _FakeActivityError("classify blew up")

    _script(
        monkeypatch,
        {
            acts.classify_intent_activity: _raise,
            acts.fail_job_activity: None,
        },
        calls=calls,
    )

    with pytest.raises(_FakeActivityError):
        _run()

    assert calls == [acts.classify_intent_activity, acts.fail_job_activity]


def test_legacy_unpatched_execution_runs_single_activity(monkeypatch):
    """An execution started before the decomposition (workflow.patched False)
    replays the original single ``run_assistant_activity`` and nothing else, so
    old histories stay deterministic."""
    calls: list = []
    _script(
        monkeypatch,
        {acts.run_assistant_activity: None},
        calls=calls,
        patched=False,
    )

    result = _run(message="legacy job")

    assert calls == [acts.run_assistant_activity]
    assert result is None


def test_error_message_prefers_cause():
    class _Err(Exception):
        def __init__(self, msg, cause):
            super().__init__(msg)
            self.cause = cause

    assert wf._error_message(_Err("outer", "inner cause")) == "inner cause"
    assert wf._error_message(_Err("outer", None)) == "outer"
