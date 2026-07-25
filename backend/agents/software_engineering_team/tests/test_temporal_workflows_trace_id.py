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
from typing import Any
from unittest import mock

from temporalio import workflow as _wf

from software_engineering_team.temporal import workflows as wfmod


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

    with (
        mock.patch.object(_wf, "execute_activity", _fake_exec),
        mock.patch.object(_wf, "uuid4", lambda: f"uuid-{next(counter)}"),
    ):
        yield


def test_run_team_workflow_v2_generates_one_trace_id_shared_by_all_three_activities():
    calls: list = []
    with _driver({}, calls):
        asyncio.run(wfmod.RunTeamWorkflowV2().run("job-1", "/repo"))

    names = [c[0] for c in calls]
    assert names == ["parse_spec_activity", "plan_project_activity", "execute_coding_team_activity"]

    # Each activity's trailing positional arg is the trace_id (last element passed).
    trace_ids = {c[1][-1] for c in calls}
    assert len(trace_ids) == 1, f"expected one shared trace id across all phases, got {calls}"
    assert next(iter(trace_ids)) == "uuid-0"  # workflow.uuid4(), not stdlib uuid4/new_trace_id


def test_run_team_workflow_v2_planning_only_still_shares_the_trace_id():
    calls: list = []
    with _driver({}, calls):
        asyncio.run(wfmod.RunTeamWorkflowV2().run("job-2", "/repo", planning_only=True))

    names = [c[0] for c in calls]
    assert names == ["parse_spec_activity", "plan_project_activity"]
    trace_ids = {c[1][-1] for c in calls}
    assert trace_ids == {"uuid-0"}


def test_run_team_workflow_generates_and_forwards_a_trace_id():
    calls: list = []
    with _driver({}, calls):
        asyncio.run(wfmod.RunTeamWorkflow().run("job-3", "/repo"))

    assert calls[0][0] == "run_orchestrator_activity"
    # run_orchestrator_activity(job_id, repo_path, spec_content_override,
    #                            resolved_questions_override, planning_only, trace_id)
    assert calls[0][1][-1] == "uuid-0"


def test_retry_failed_workflow_generates_and_forwards_a_trace_id():
    calls: list = []
    with _driver({}, calls):
        asyncio.run(wfmod.RetryFailedWorkflow().run("job-4"))

    assert calls[0] == ("retry_failed_activity", ["job-4", "uuid-0"])
