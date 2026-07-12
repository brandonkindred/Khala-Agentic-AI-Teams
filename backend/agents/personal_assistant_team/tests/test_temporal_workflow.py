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
    # The specialist result is threaded into response generation under its key,
    # and the profile-update result + context are threaded in too.
    gen_args = arg_log[acts.generate_response_activity]
    assert gen_args[4] == [
        {"agent": "email", "action": "read", "result": {"n": 1}, "success": True}
    ]
    assert gen_args[5] == {"email": {"n": 1}}
    assert gen_args[6] == [{"pref": "x"}]
    assert gen_args[7] == {}


def test_specialist_error_action_leaves_results_empty(monkeypatch):
    # When the specialist activity's own try/except caught a non-LLM handler
    # exception and returned a degraded orchestrator:error action, `results`
    # must NOT be populated with that error under the specialist's key —
    # matching thread-mode's handle_request, which never assigns
    # results[intent.primary] on the equivalent exception path.
    arg_log: dict = {}
    _script(
        monkeypatch,
        {
            acts.classify_intent_activity: {"primary": "email", "confidence": 0.9, "entities": {}},
            acts.handle_email_activity: {
                "agent": "orchestrator",
                "action": "error",
                "result": {"error": "backend down"},
                "success": False,
            },
            acts.check_profile_updates_activity: [],
            acts.generate_response_activity: {"message": "ok"},
            acts.finalize_success_activity: None,
        },
        arg_log=arg_log,
    )

    _run()

    gen_args = arg_log[acts.generate_response_activity]
    assert gen_args[5] == {}


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
    # The specialist and profile-updates activities are scheduled concurrently
    # (they share no data dependency), so check_profile_updates_activity still
    # runs even though the specialist reports cancellation — its own cancel
    # guard makes that a cheap no-op — and the workflow must still short-
    # circuit on the specialist's cancellation once both complete.
    calls: list = []
    _script(
        monkeypatch,
        {
            acts.classify_intent_activity: {"primary": "email", "confidence": 1.0, "entities": {}},
            acts.handle_email_activity: {"cancelled": True},
            acts.check_profile_updates_activity: [],
        },
        calls=calls,
    )

    result = _run()

    assert result == {"cancelled": True}
    assert acts.classify_intent_activity in calls
    assert acts.handle_email_activity in calls
    assert acts.check_profile_updates_activity in calls
    assert acts.generate_response_activity not in calls


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
    # The broadened `except Exception` catches any workflow-step failure, not
    # just a Temporal ActivityError — including a plain bug in workflow-body
    # code, since ActivityError itself is just an Exception subclass.
    calls: list = []

    def _raise(_args):
        raise RuntimeError("classify blew up")

    _script(
        monkeypatch,
        {
            acts.classify_intent_activity: _raise,
            acts.fail_job_activity: None,
        },
        calls=calls,
    )

    with pytest.raises(RuntimeError, match="classify blew up"):
        _run()

    assert calls == [acts.classify_intent_activity, acts.fail_job_activity]


def test_native_cancellation_propagates_without_failing_job(monkeypatch):
    # Native Temporal-level workflow cancellation (temporalio's CancelledError,
    # which — unlike asyncio.CancelledError — subclasses Exception) must
    # propagate untouched: it is NOT an application failure, so
    # fail_job_activity must never be called for it.
    from temporalio.exceptions import CancelledError

    calls: list = []

    def _raise(_args):
        raise CancelledError("workflow cancelled")

    _script(
        monkeypatch,
        {acts.classify_intent_activity: _raise},
        calls=calls,
    )

    with pytest.raises(CancelledError):
        _run()

    assert calls == [acts.classify_intent_activity]


def test_activity_error_wrapping_cancellation_propagates_without_failing_job(monkeypatch):
    # A cancelled *activity* is what workflow.execute_activity actually raises
    # in production: a temporalio ActivityError whose `.cause` is a
    # CancelledError, NOT a bare CancelledError. A plain `except CancelledError`
    # misses this entirely and would misroute it into fail_job_activity; only
    # `is_cancelled_exception` (isinstance-or-unwrapped-cause) catches it.
    from temporalio.exceptions import ActivityError, CancelledError

    calls: list = []

    def _raise(_args):
        err = ActivityError(
            "activity cancelled",
            scheduled_event_id=1,
            started_event_id=2,
            identity="test",
            activity_type="pa_classify_intent",
            activity_id="1",
            retry_state=None,
        )
        err.__cause__ = CancelledError("cancelled")
        raise err

    _script(
        monkeypatch,
        {acts.classify_intent_activity: _raise},
        calls=calls,
    )

    with pytest.raises(ActivityError):
        _run()

    # fail_job_activity must NOT have run — this is a cancellation, not an
    # application failure.
    assert calls == [acts.classify_intent_activity]


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


def test_error_message_falls_back_for_exception_without_cause_attribute():
    # A plain workflow-body bug (not an ActivityError) has no `.cause`
    # attribute at all — getattr's default must handle that gracefully.
    assert wf._error_message(RuntimeError("plain bug")) == "plain bug"


def test_error_message_never_raises_on_broken_str():
    # A third-party exception with a broken __str__ must not blow up
    # _error_message itself — it runs inside an except handler, where a
    # second exception would mask the one actually being reported.
    class _Unprintable(Exception):
        def __str__(self):
            raise RuntimeError("cannot stringify")

    assert wf._error_message(_Unprintable()) == "_Unprintable (error message unavailable)"


def test_error_message_never_raises_on_broken_cause_str():
    class _BrokenCause:
        def __str__(self):
            raise RuntimeError("cannot stringify cause")

    class _Err(Exception):
        def __init__(self, cause):
            super().__init__("outer")
            self.cause = cause

    assert wf._error_message(_Err(_BrokenCause())) == "_BrokenCause (error message unavailable)"
