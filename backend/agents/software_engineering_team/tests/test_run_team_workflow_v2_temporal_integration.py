"""Real-server integration coverage for ``RunTeamWorkflowV2``.

``test_temporal_workflows_trace_id.py`` proves the 3-phase execution order
and ``planning_only`` short-circuit by driving the workflow's ``run``
coroutine directly with mocked ``workflow.execute_activity``/``workflow.uuid4``
-- its own docstring notes that a full server-backed ``WorkflowEnvironment``
needs a test-server binary unavailable in that context. This file closes that
gap: it drives ``RunTeamWorkflowV2`` through a real (time-skipping) Temporal
test server and a real ``Worker``, with fake activities registered under the
same names the production activities use (Temporal resolves by registered
name, not Python identity), so no real orchestrator/job-store/LLM machinery
runs. Mirrors ``test_issue_grooming_temporal_workflow.py``'s
``_workflow_environment_worker`` helper -- the lightest-weight existing
example of this pattern, since ``RunTeamWorkflowV2`` (like grooming's
workflow) is a plain sequential-activity workflow, not signal-driven like
``CodingTeamWorkflow``.
"""

from __future__ import annotations

import contextlib

import pytest

from software_engineering_team.temporal.constants import TASK_QUEUE
from software_engineering_team.temporal.workflows import RunTeamWorkflowV2


@contextlib.asynccontextmanager
async def _workflow_environment_worker(activities):
    """Start a time-skipping ``WorkflowEnvironment`` with a ``RunTeamWorkflowV2``
    worker attached, on the real SE task queue.

    Preconditions:
        ``activities`` is a non-empty list of fakes registered under the real
        activity names (``parse_spec_and_analyze``, ``plan_project``,
        ``execute_coding_team``) so ``RunTeamWorkflowV2.run``'s
        ``workflow.execute_activity(...)`` calls dispatch to them unchanged.
    Postconditions:
        Yields a live ``WorkflowEnvironment``. Skips (rather than fails) when
        the ephemeral Temporal test-server binary can't be downloaded (no
        egress) -- same caveat as ``test_coding_team_temporal_workflow.py``
        and ``test_issue_grooming_temporal_workflow.py``'s helpers of the
        same name.
    """
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    try:
        test_env = await WorkflowEnvironment.start_time_skipping()
    except RuntimeError as exc:
        pytest.skip(f"Temporal ephemeral test server unavailable (no egress?): {exc}")

    async with test_env as env:
        worker = Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[RunTeamWorkflowV2],
            activities=activities,
        )
        async with worker:
            yield env


def _make_fake_phase_activities(calls: list) -> list:
    """Build fake parse/plan/execute activities, registered under the real
    names, that each append ``(name, args)`` to ``calls`` and return an
    opaque placeholder result for the next phase to receive.

    Preconditions: ``calls`` is a list the caller can inspect after the run.
    Postconditions: returns activities in an order-independent list; Temporal
    dispatches by registered name, so list order here doesn't matter.
    """
    from temporalio import activity

    @activity.defn(name="parse_spec_and_analyze")
    def _fake_parse_spec(job_id, repo_path, spec_content_override, trace_id, sprint_id):
        calls.append(
            (
                "parse_spec_and_analyze",
                [job_id, repo_path, spec_content_override, trace_id, sprint_id],
            )
        )
        return {"spec": "parsed"}

    @activity.defn(name="plan_project")
    def _fake_plan_project(job_id, repo_path, spec_result, trace_id):
        calls.append(("plan_project", [job_id, repo_path, spec_result, trace_id]))
        return {"plan": "ready"}

    @activity.defn(name="execute_coding_team")
    def _fake_execute_coding_team(
        job_id, repo_path, plan_result, resolved_questions_override, trace_id
    ):
        calls.append(
            (
                "execute_coding_team",
                [job_id, repo_path, plan_result, resolved_questions_override, trace_id],
            )
        )

    return [_fake_parse_spec, _fake_plan_project, _fake_execute_coding_team]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_team_workflow_v2_executes_all_three_phases_in_order_against_real_server() -> (
    None
):
    """Acceptance: a real Temporal worker/sandbox round-trip drives
    ``RunTeamWorkflowV2`` through all three phases, in order, dispatching to
    each real activity name -- not just the mocked-coroutine shape asserted
    in ``test_temporal_workflows_trace_id.py``."""
    calls: list = []

    async with _workflow_environment_worker(_make_fake_phase_activities(calls)) as env:
        await env.client.execute_workflow(
            RunTeamWorkflowV2.run,
            args=["job-real-1", "/repo"],
            id="run-team-workflow-v2-integration-all-phases",
            task_queue=TASK_QUEUE,
        )

    names = [c[0] for c in calls]
    assert names == ["parse_spec_and_analyze", "plan_project", "execute_coding_team"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_team_workflow_v2_planning_only_stops_after_plan_phase_against_real_server() -> (
    None
):
    """Acceptance: ``planning_only=True`` returns after phase 2 in a real
    Temporal run -- ``execute_coding_team`` is never scheduled."""
    calls: list = []

    async with _workflow_environment_worker(_make_fake_phase_activities(calls)) as env:
        await env.client.execute_workflow(
            RunTeamWorkflowV2.run,
            args=["job-real-2", "/repo", None, None, True],
            id="run-team-workflow-v2-integration-planning-only",
            task_queue=TASK_QUEUE,
        )

    names = [c[0] for c in calls]
    assert names == ["parse_spec_and_analyze", "plan_project"]
