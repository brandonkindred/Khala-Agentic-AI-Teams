"""Unit tests for CodingTeamWorkflow's submit_answers signal + wait_condition skeleton.

Drives ``.run()`` directly as a plain object (no Temporal server, no
``pytest.mark.integration``), patching ``temporalio.workflow.execute_activity``
and ``temporalio.workflow.wait_condition`` in place -- the same lightweight
pattern ``agentic_team_provisioning/tests/test_temporal_activity.py`` uses for
``AgenticPipelineWorkflow``.

``run_pipeline_activity`` does not emit ``{"outcome": "paused", ...}`` yet --
that activity-side pause detection is separate, not-yet-implemented work (see
``system_design/hitl_pause_resume_contract.md``). These tests fake the
activity's *return value* directly so the signal / wait_condition / re-invoke
SHAPE -- including resume_token validation and acknowledged_resume_token --
can be proven now, independent of that future change. Buffering an early
signal (before a pause is active) and applying answers into
``request["plan_input"]`` are deferred to the sibling reconciliation-loop
issue (#3988) and are not covered here.
"""

from __future__ import annotations

import asyncio

import pytest

from software_engineering_team.api.coding_team_models import RunRequest
from software_engineering_team.temporal.coding_team_workflow import CodingTeamWorkflow


def _patch_execute(monkeypatch: pytest.MonkeyPatch, results: list) -> tuple[list, list]:
    """Patch workflow.execute_activity to return successive ``results`` per call.

    Returns ``(calls, snapshots)``: ``calls`` records each ``(fn, request)`` pair
    by reference (for identity assertions -- proving the SAME request object is
    reused across the loop), while ``snapshots`` records a ``dict`` copy of
    ``request`` taken at the moment of each call (for content assertions that
    must not be affected by mutations the workflow makes to that same object on
    a later iteration).
    """
    calls: list = []
    snapshots: list = []
    results_iter = iter(results)

    async def _fake_exec(fn, request, **_kw):
        calls.append((fn, request))
        snapshots.append(dict(request))
        return next(results_iter)

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)
    return calls, snapshots


def test_run_returns_immediately_when_not_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Today's real activity result (no 'outcome' key) returns on the first
    iteration without ever touching wait_condition -- the pause loop is dead
    code until activity-side pause detection lands."""
    workflow_obj = CodingTeamWorkflow()
    calls, _ = _patch_execute(monkeypatch, [{"job_id": "j1", "status": "completed"}])

    async def _no_wait(*_a, **_kw):  # pragma: no cover - must not be called
        raise AssertionError("wait_condition must not be called when not paused")

    monkeypatch.setattr("temporalio.workflow.wait_condition", _no_wait)

    result = asyncio.run(workflow_obj.run({"repo_path": "/repo", "plan_input": {}}))

    assert result == {"job_id": "j1", "status": "completed"}
    assert len(calls) == 1


def test_submit_answers_signal_wakes_wait_condition_and_reloops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 'paused' first result makes run() wait on a token-matching submit_answers
    signal; delivering it (a plain method call, simulating Temporal's dispatch)
    wakes wait_condition, and the workflow re-calls the SAME activity object with
    the SAME request, carrying acknowledged_resume_token for that call only."""
    workflow_obj = CodingTeamWorkflow()
    request = {"repo_path": "/repo", "plan_input": {"objective": "o"}}
    calls, snapshots = _patch_execute(
        monkeypatch,
        [
            {"outcome": "paused", "job_id": "j1", "resume_token": "j1:1"},
            {"job_id": "j1", "status": "completed"},
        ],
    )

    async def _fake_wait(pred, timeout=None):
        workflow_obj.submit_answers({"resume_token": "j1:1", "answers": [{"question_id": "q1"}]})
        assert pred()  # the predicate must observe the signal we just delivered

    monkeypatch.setattr("temporalio.workflow.wait_condition", _fake_wait)

    result = asyncio.run(workflow_obj.run(request))

    assert result == {"job_id": "j1", "status": "completed"}
    assert len(calls) == 2
    # Same activity function, same request object, reused across both calls.
    assert calls[0][0] is calls[1][0]
    assert calls[0][1] is request
    assert calls[1][1] is request
    # The first call precedes any pause, so no token has been acknowledged yet.
    assert "acknowledged_resume_token" not in snapshots[0]
    # The second call carries the just-resolved pause's token.
    assert snapshots[1]["acknowledged_resume_token"] == "j1:1"
    # Popped once that call returns -- the field's job is done either way.
    assert "acknowledged_resume_token" not in request


def test_submit_answers_ignores_mismatched_resume_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A submission for a different (stale, or already-resolved) pause must not
    wake wait_condition -- token validation is the workflow's defense against a
    retried or duplicate HTTP call resolving the wrong pause."""
    workflow_obj = CodingTeamWorkflow()
    workflow_obj._active_resume_token = "current-token"

    workflow_obj.submit_answers({"resume_token": "stale-token", "answers": [{"question_id": "q1"}]})

    assert workflow_obj._submitted_answers is None


def test_submit_answers_ignores_second_submission_for_same_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A double-submit (or two clients racing to answer the same pause) must not
    overwrite the first accepted batch -- first submission per token wins."""
    workflow_obj = CodingTeamWorkflow()
    workflow_obj._active_resume_token = "tok-1"
    first = [{"question_id": "q1", "answer": "yes"}]
    workflow_obj.submit_answers({"resume_token": "tok-1", "answers": first})

    workflow_obj.submit_answers(
        {"resume_token": "tok-1", "answers": [{"question_id": "q1", "answer": "no"}]}
    )

    assert workflow_obj._submitted_answers == first


def test_submit_answers_ignores_signal_with_no_active_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal arriving before any pause is active (self._active_resume_token is
    None) is dropped rather than buffered -- buffering early signals is deferred
    to the sibling reconciliation-loop issue (#3988), which has the real
    activity-side pause payload to buffer against."""
    workflow_obj = CodingTeamWorkflow()
    assert workflow_obj._active_resume_token is None

    workflow_obj.submit_answers({"resume_token": "any-token", "answers": [{"question_id": "q1"}]})

    assert workflow_obj._submitted_answers is None


def test_submit_answers_sets_state_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signal handler contract in isolation: a payload whose resume_token matches
    the active pause has its answers stored (not the whole payload -- the
    resume_token itself is already tracked separately in _active_resume_token)."""
    workflow_obj = CodingTeamWorkflow()
    workflow_obj._active_resume_token = "tok-1"
    assert workflow_obj._submitted_answers is None

    answers = [{"question_id": "q1", "answer": "yes"}]
    workflow_obj.submit_answers({"resume_token": "tok-1", "answers": answers})

    assert workflow_obj._submitted_answers == answers


def test_run_request_declares_acknowledged_resume_token() -> None:
    """Regression guard: RunRequest must declare acknowledged_resume_token, or
    Pydantic's default ignore-extra-keys behavior silently drops the value
    CodingTeamWorkflow.run sets on request before run_pipeline_activity's
    RunRequest(**request) call ever sees it."""
    parsed = RunRequest(repo_path="/repo", acknowledged_resume_token="j1:1")

    assert parsed.acknowledged_resume_token == "j1:1"
