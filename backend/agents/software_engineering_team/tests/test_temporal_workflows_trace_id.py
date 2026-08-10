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

from temporalio import workflow as _wf

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
    with _driver({}, calls):
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
    with _driver({}, calls):
        asyncio.run(wfmod.RunTeamWorkflowV2().run("job-2", "/repo", planning_only=True))

    names = [c[0] for c in calls]
    assert names == ["parse_spec_activity", "plan_project_activity"]
    parse_spec_args, plan_args = (c[1] for c in calls)
    trace_ids = {parse_spec_args[-2], plan_args[-1]}
    assert trace_ids == {_FIRST_TRACE_ID}


def test_run_team_workflow_v2_forwards_sprint_id_to_parse_spec_activity():
    calls: list = []
    with _driver({}, calls):
        asyncio.run(wfmod.RunTeamWorkflowV2().run("job-6", "/repo", sprint_id="sprint-456"))

    assert calls[0][0] == "parse_spec_activity"
    assert calls[0][1][-1] == "sprint-456"


def test_retry_failed_workflow_generates_and_forwards_a_trace_id():
    calls: list = []
    with _driver({}, calls):
        asyncio.run(wfmod.RetryFailedWorkflow().run("job-4"))

    assert calls[0] == ("retry_failed_activity", ["job-4", _FIRST_TRACE_ID])
