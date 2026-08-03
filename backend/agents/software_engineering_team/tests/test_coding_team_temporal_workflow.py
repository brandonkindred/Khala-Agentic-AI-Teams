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
SHAPE can be proven now, independent of that future change.
"""

from __future__ import annotations

import asyncio

import pytest

from software_engineering_team.temporal.coding_team_workflow import CodingTeamWorkflow


def _patch_execute(monkeypatch: pytest.MonkeyPatch, results: list) -> list:
    """Patch workflow.execute_activity to return successive ``results`` per call,
    recording each ``(fn, request)`` pair it was invoked with."""
    calls: list = []
    results_iter = iter(results)

    async def _fake_exec(fn, request, **_kw):
        calls.append((fn, request))
        return next(results_iter)

    monkeypatch.setattr("temporalio.workflow.execute_activity", _fake_exec)
    return calls


def test_run_returns_immediately_when_not_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Today's real activity result (no 'outcome' key) returns on the first
    iteration without ever touching wait_condition -- the pause loop is dead
    code until activity-side pause detection lands."""
    workflow_obj = CodingTeamWorkflow()
    calls = _patch_execute(monkeypatch, [{"job_id": "j1", "status": "completed"}])

    async def _no_wait(*_a, **_kw):  # pragma: no cover - must not be called
        raise AssertionError("wait_condition must not be called when not paused")

    monkeypatch.setattr("temporalio.workflow.wait_condition", _no_wait)

    result = asyncio.run(workflow_obj.run({"repo_path": "/repo", "plan_input": {}}))

    assert result == {"job_id": "j1", "status": "completed"}
    assert len(calls) == 1


def test_submit_answers_signal_wakes_wait_condition_and_reloops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 'paused' first result makes run() wait on submit_answers; delivering the
    signal (a plain method call, simulating Temporal's dispatch) wakes
    wait_condition, and the workflow re-calls the SAME activity with the SAME
    request unmodified, returning its second result."""
    workflow_obj = CodingTeamWorkflow()
    request = {"repo_path": "/repo", "plan_input": {"objective": "o"}}
    calls = _patch_execute(
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
    # Same activity function, same request object, called twice, unmodified.
    assert calls[0][0] is calls[1][0]
    assert calls[0][1] is request
    assert calls[1][1] is request


def test_submit_answers_sets_state_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signal handler contract in isolation: calling submit_answers stores the
    payload verbatim (no validation/matching yet -- that's future work)."""
    workflow_obj = CodingTeamWorkflow()
    assert workflow_obj._submitted_answers is None

    payload = {"resume_token": "tok-1", "answers": [{"question_id": "q1", "answer": "yes"}]}
    workflow_obj.submit_answers(payload)

    assert workflow_obj._submitted_answers == payload
