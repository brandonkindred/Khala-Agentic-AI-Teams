"""Tests that the SE Temporal workflows generate one ``trace_id`` per run and forward it to
every phase activity — the workflow-side half of threading ``trace_id`` through the 4-phase
pipeline (the activity-side half is covered in ``test_temporal_activities.py``).

The full server-backed ``WorkflowEnvironment`` needs a test-server binary that is unavailable
here, so (as in ``deepthought/tests/test_temporal_workflow.py``) these tests drive each
workflow's ``run`` coroutine directly with ``asyncio`` while patching ``workflow.execute_activity``
(a fake dispatcher keyed on activity ``__name__``) and ``workflow.uuid4`` (a deterministic
counter — the real ``uuid.uuid4``/``shared.observability.new_trace_id`` must never be called
from workflow code, since workflow code must be deterministic across replays).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import uuid
from typing import Any
from unittest import mock

import pytest
from temporalio import workflow as _wf
from temporalio.exceptions import ApplicationError

from software_engineering_team.temporal import workflows as wfmod

# What the stubbed ``workflow.uuid4()`` returns on its first call, and the trace id the
# workflow must derive from it. A real ``uuid.UUID`` (not a string) so the assertions
# exercise the production ``.hex[:12]`` expression rather than a stub-shaped stand-in.
_FIRST_UUID = uuid.UUID(int=0xABCDEF0123456789ABCDEF0123456789)
_FIRST_TRACE_ID = _FIRST_UUID.hex[:12]


@contextlib.contextmanager
def _driver(handlers: dict[str, Any], calls: list):
    """Patch the workflow-context primitives and record every activity call."""
    counter = itertools.count()

    async def _fake_exec(fn, *pos, **kw):
        name = getattr(fn, "__name__", str(fn))
        args = list(kw["args"]) if "args" in kw else list(pos)
        calls.append((name, args))
        handler = handlers.get(name, lambda a: None)
        return handler(args) if callable(handler) else handler

    def _fake_uuid4() -> uuid.UUID:
        return uuid.UUID(int=_FIRST_UUID.int + next(counter))

    with (
        mock.patch.object(_wf, "execute_activity", _fake_exec),
        mock.patch.object(_wf, "uuid4", _fake_uuid4),
    ):
        yield


def test_run_team_workflow_v2_generates_one_trace_id_shared_by_all_three_activities():
    calls: list = []
    # plan_project_activity must return a dict (never None, as the driver's untouched
    # default does) — the workflow's Phase 2 pause loop calls `.get("outcome")` on it.
    with _driver({"plan_project_activity": lambda a: {}}, calls):
        asyncio.run(wfmod.RunTeamWorkflowV2().run("job-1", "/repo"))

    names = [c[0] for c in calls]
    assert names == ["parse_spec_activity", "plan_project_activity", "execute_coding_team_activity"]

    # parse_spec_activity(job_id, repo_path, spec_content_override, trace_id, sprint_id)
    # — trace_id is second-to-last now that sprint_id trails it; the other two phases
    # are unchanged and still end with trace_id.
    parse_spec_args, plan_args, execute_args = (c[1] for c in calls)
    assert parse_spec_args[-1] is None  # sprint_id defaults to None when the caller omits it
    trace_ids = {parse_spec_args[-2], plan_args[-1], execute_args[-1]}
    assert len(trace_ids) == 1, f"expected one shared trace id across all phases, got {calls}"
    trace_id = next(iter(trace_ids))
    assert trace_id == _FIRST_TRACE_ID  # workflow.uuid4(), not stdlib uuid4/new_trace_id
    # Same shape as shared.observability.new_trace_id() promises, so thread mode and
    # Temporal mode emit ids a log filter can match with one pattern. Guards against a
    # regression to ``str(workflow.uuid4())[:12]``, which yields a hyphenated fragment.
    assert len(trace_id) == 12
    assert all(ch in "0123456789abcdef" for ch in trace_id)


def test_run_team_workflow_v2_planning_only_still_shares_the_trace_id():
    calls: list = []
    with _driver({"plan_project_activity": lambda a: {}}, calls):
        asyncio.run(wfmod.RunTeamWorkflowV2().run("job-2", "/repo", planning_only=True))

    names = [c[0] for c in calls]
    assert names == ["parse_spec_activity", "plan_project_activity"]
    parse_spec_args, plan_args = (c[1] for c in calls)
    trace_ids = {parse_spec_args[-2], plan_args[-1]}
    assert trace_ids == {_FIRST_TRACE_ID}


def test_run_team_workflow_v2_forwards_sprint_id_to_parse_spec_activity():
    calls: list = []
    with _driver({"plan_project_activity": lambda a: {}}, calls):
        asyncio.run(wfmod.RunTeamWorkflowV2().run("job-6", "/repo", sprint_id="sprint-456"))

    assert calls[0][0] == "parse_spec_activity"
    assert calls[0][1][-1] == "sprint-456"


def test_run_team_workflow_v2_pauses_and_resumes_on_planning_answer_signal():
    """Phase 2 durably waits for a ``submit_planning_answers`` signal instead of
    proceeding to Phase 3 with no answer, when ``plan_project_activity`` reports a
    pause -- the workflow-side half of that wiring (the activity-side half,
    catching ``PlanningAnswerPauseSignal``, is covered in ``test_temporal_activities.py``).
    Mirrors ``CodingTeamWorkflow``'s equivalent pause-loop test but through
    ``PlanningAnswerSignalMixin``'s ``submit_planning_answers``/``wait_for_planning_answers``
    instead of hand-rolled signal state.
    """
    calls: list = []
    plan_calls = {"n": 0}

    def _fake_plan(args):
        plan_calls["n"] += 1
        if plan_calls["n"] == 1:
            return {"outcome": "paused", "resume_token": "job-7:tok1"}
        return {"outcome": "completed", "requirements_title": "Widget"}

    workflow_obj = wfmod.RunTeamWorkflowV2()

    async def _fake_wait_condition(pred, timeout=None):
        workflow_obj.submit_planning_answers(
            {
                "resume_token": "job-7:tok1",
                "answers": [{"question_id": "q1", "selected_option_id": "a"}],
            }
        )
        assert pred()  # the predicate must observe the signal we just delivered

    with _driver({"plan_project_activity": _fake_plan}, calls):
        with mock.patch.object(_wf, "wait_condition", _fake_wait_condition):
            asyncio.run(workflow_obj.run("job-7", "/repo"))

    names = [c[0] for c in calls]
    assert names == [
        "parse_spec_activity",
        "plan_project_activity",
        "plan_project_activity",
        "execute_coding_team_activity",
    ]
    # The re-invocation carries the same resume_token, the resolved answers, and
    # the pause budget still being open.
    _, _, second_plan_args = (c[1] for c in calls[:3])
    # The allow_repause flag is `pause_round < MAX_PLANNING_PAUSE_ROUNDS`, so it
    # is derived rather than hard-coded: a literal True here would only be right
    # while the constant stays above 1, and lowering it would fail with a bare
    # assertion diff that says nothing about the coupling.
    assert second_plan_args[-3:] == [
        "job-7:tok1",
        [{"question_id": "q1", "selected_option_id": "a"}],
        1 < wfmod.MAX_PLANNING_PAUSE_ROUNDS,
    ]


def test_run_team_workflow_v2_accumulates_answers_across_pause_rounds():
    """Two pause rounds must hand the activity every answer gathered so far.

    Planning replays from scratch on every resume, so round 2's invocation
    re-encounters round 1's questions. Carrying only the newest batch leaves
    those unmatched, pauses on them again, and ping-pongs between the rounds
    forever.
    """
    calls: list = []
    plan_calls = {"n": 0}
    current_token = {"value": ""}

    def _fake_plan(args):
        plan_calls["n"] += 1
        if plan_calls["n"] == 1:
            current_token["value"] = "job-8:tok1"
            return {
                "outcome": "paused",
                "resume_token": current_token["value"],
                "pending_questions": [{"id": "q1"}],
            }
        if plan_calls["n"] == 2:
            current_token["value"] = "job-8:tok2"
            return {
                "outcome": "paused",
                "resume_token": current_token["value"],
                "pending_questions": [{"id": "q2"}],
            }
        return {"outcome": "completed", "requirements_title": "Widget"}

    workflow_obj = wfmod.RunTeamWorkflowV2()
    answers = {
        "job-8:tok1": [{"question_id": "q1", "selected_option_id": "a"}],
        "job-8:tok2": [{"question_id": "q2", "selected_option_id": "b"}],
    }

    async def _fake_wait_condition(pred, timeout=None):
        # The token comes from the pause envelope the activity returned, which is
        # what a real signal sender holds — not from the workflow's private state.
        token = current_token["value"]
        workflow_obj.submit_planning_answers({"resume_token": token, "answers": answers[token]})
        assert pred()

    with _driver({"plan_project_activity": _fake_plan}, calls):
        with mock.patch.object(_wf, "wait_condition", _fake_wait_condition):
            asyncio.run(workflow_obj.run("job-8", "/repo"))

    plan_args = [c[1] for c in calls if c[0] == "plan_project_activity"]
    assert len(plan_args) == 3
    # Round 2 carries round 1's answer. The trailing flag is
    # `pause_round < MAX_PLANNING_PAUSE_ROUNDS`, derived here for the same
    # reason as above rather than pinned to the constant's current value.
    assert plan_args[1][-3:] == [
        "job-8:tok1",
        answers["job-8:tok1"],
        1 < wfmod.MAX_PLANNING_PAUSE_ROUNDS,
    ]
    # Round 3 carries BOTH rounds', in order.
    assert plan_args[2][-3:] == [
        "job-8:tok2",
        answers["job-8:tok1"] + answers["job-8:tok2"],
        2 < wfmod.MAX_PLANNING_PAUSE_ROUNDS,
    ]


def test_run_team_workflow_v2_bounds_the_planning_pause_loop():
    """A run whose question ids drift on every replay must still finish.

    Planning's ``OpenQuestion.id`` comes straight from LLM output, so a
    from-scratch replay can mint a fresh id for a question the user already
    answered. The pause test ("does this batch contain a question nobody has
    seen?") is then true forever. The workflow bounds the loop and dispatches
    its last round with ``allow_repause=False``, which forbids the activity
    from pausing again -- without that, this test never returns.
    """
    calls: list = []
    round_no = {"n": 0}
    current_token = {"value": ""}

    def _fake_plan(args):
        round_no["n"] += 1
        # Honour the flag the way the real activity does (its callback stops
        # raising PlanningAnswerPauseSignal), and otherwise keep minting a
        # brand-new question id -- the drift this bound exists for.
        if len(args) > 6 and args[6] is False:
            return {"outcome": "completed", "requirements_title": "Widget"}
        current_token["value"] = f"job-9:tok{round_no['n']}"
        return {
            "outcome": "paused",
            "resume_token": current_token["value"],
            "pending_questions": [{"id": f"q-drifted-{round_no['n']}"}],
        }

    workflow_obj = wfmod.RunTeamWorkflowV2()

    async def _fake_wait_condition(pred, timeout=None):
        workflow_obj.submit_planning_answers(
            {"resume_token": current_token["value"], "answers": []}
        )
        assert pred()

    with _driver({"plan_project_activity": _fake_plan}, calls):
        with mock.patch.object(_wf, "wait_condition", _fake_wait_condition):
            asyncio.run(workflow_obj.run("job-9", "/repo"))

    plan_args = [c[1] for c in calls if c[0] == "plan_project_activity"]
    # One unpaused opening call plus exactly MAX_PLANNING_PAUSE_ROUNDS resumes.
    assert len(plan_args) == wfmod.MAX_PLANNING_PAUSE_ROUNDS + 1
    # Every resume but the last leaves the budget open; the last closes it.
    assert [a[6] for a in plan_args[1:]] == [True] * (wfmod.MAX_PLANNING_PAUSE_ROUNDS - 1) + [False]
    # And the run reached Phase 3 rather than dying in the loop.
    assert calls[-1][0] == "execute_coding_team_activity"


def test_run_team_workflow_v2_fails_if_planning_pauses_past_its_budget():
    """``allow_repause=False`` is a contract, and a loop that trusts a broken
    one spins forever. An activity that pauses after being told not to fails
    the run instead."""
    calls: list = []

    current_token = {"value": "job-10:tok"}

    def _fake_plan(args):
        return {
            "outcome": "paused",
            "resume_token": current_token["value"],
            "pending_questions": [],
        }

    workflow_obj = wfmod.RunTeamWorkflowV2()

    async def _fake_wait_condition(pred, timeout=None):
        workflow_obj.submit_planning_answers(
            {"resume_token": current_token["value"], "answers": []}
        )
        assert pred()

    with _driver({"plan_project_activity": _fake_plan}, calls):
        with mock.patch.object(_wf, "wait_condition", _fake_wait_condition):
            with pytest.raises(ApplicationError, match="MAX_PLANNING_PAUSE_ROUNDS") as exc_info:
                asyncio.run(workflow_obj.run("job-10", "/repo"))

    # Non-retryable is the load-bearing half: a plain ApplicationError would be
    # re-run by the workflow's retry policy, which is the unbounded re-dispatch
    # this guard exists to stop.
    assert exc_info.value.non_retryable is True

    plan_args = [c[1] for c in calls if c[0] == "plan_project_activity"]
    assert len(plan_args) == wfmod.MAX_PLANNING_PAUSE_ROUNDS + 1


def test_retry_failed_workflow_generates_and_forwards_a_trace_id():
    calls: list = []
    with _driver({}, calls):
        asyncio.run(wfmod.RetryFailedWorkflow().run("job-4"))

    assert calls[0] == ("retry_failed_activity", ["job-4", _FIRST_TRACE_ID])
