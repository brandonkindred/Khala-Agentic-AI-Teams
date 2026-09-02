"""
Temporal workflows for the software engineering team.

Workflows are deterministic; they only schedule activities. All I/O and LLM
calls happen inside activities.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from planning_team.temporal.answer_signal import PlanningAnswerSignalMixin
    from software_engineering_team.temporal import activities as _activities
    from software_engineering_team.temporal.constants import (
        STANDALONE_TYPE_BACKEND,
        STANDALONE_TYPE_FRONTEND,
        STANDALONE_TYPE_PRODUCT_ANALYSIS,
        TASK_QUEUE,
    )

RETRY_FAILED_TIMEOUT = timedelta(seconds=24 * 3600)
STANDALONE_TIMEOUT = timedelta(seconds=12 * 3600)

# Hard ceiling on Phase 2 Planning clarification rounds. Planning's question ids are
# LLM-minted (``product_requirements_analysis_agent.question_processing.parse_open_question``
# takes ``id`` straight from model output), and a resume replays Planning from scratch, so a
# re-run can mint a different id for a question the user already answered. The pause test
# would then never be satisfied and the loop would re-ask forever. The last round runs with
# ``allow_repause=False``, which forces the activity to return a plan, so this bound is what
# makes Phase 2 terminate rather than a hope that ids stay put. Sized for a human answering
# in a UI: more rounds than any real clarification exchange needs, few enough that a drifting
# run gives up in minutes rather than never.
MAX_PLANNING_PAUSE_ROUNDS = 8


# Retry policy: limited retries for transient failures
DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)


@workflow.defn(name="RetryFailedWorkflow")
class RetryFailedWorkflow:
    """Runs retry_failed (run_failed_tasks) for a job."""

    @workflow.run
    async def run(self, job_id: str) -> None:
        trace_id = workflow.uuid4().hex[:12]
        await workflow.execute_activity(
            _activities.retry_failed_activity,
            args=[job_id, trace_id],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=RETRY_FAILED_TIMEOUT,
            retry_policy=DEFAULT_RETRY_POLICY,
        )


@workflow.defn(name="RunTeamWorkflowV2")
class RunTeamWorkflowV2(PlanningAnswerSignalMixin):
    """Multi-step orchestration: each pipeline phase is a separate Temporal activity.

    Phases: spec parsing + PRA → Planning → Coding Team execution.
    Each activity can fail and retry independently.

    Inherits ``PlanningAnswerSignalMixin`` so a ``submit_planning_answers`` signal can
    durably resolve a Planning clarification-question pause (see the Phase 2 loop in
    ``run``) without this workflow blocking — matching thread-mode's invariant that
    Planning is never silently auto-answered.

    Invariants:
        - The Phase 2 pause loop runs at most ``MAX_PLANNING_PAUSE_ROUNDS`` times. It
          terminates by construction, not by trusting Planning's LLM-minted question ids
          to stay put across a replay: the final round is dispatched with
          ``allow_repause=False``, and an activity that pauses anyway fails the run
          rather than being re-dispatched.
    """

    @workflow.run
    async def run(
        self,
        job_id: str,
        repo_path: str,
        spec_content_override: Optional[str] = None,
        resolved_questions_override: Optional[List[Dict[str, Any]]] = None,
        planning_only: bool = False,
        sprint_id: Optional[str] = None,
    ) -> None:
        # One trace id for every phase of this job — generated via workflow.uuid4()
        # (Temporal's replay-safe UUID source) rather than
        # shared.observability.new_trace_id()/uuid.uuid4() directly, since workflow
        # code must be deterministic across replays. Each activity runs as its own
        # process/thread invocation, so the id is passed explicitly and re-bound
        # inside each activity rather than relying on contextvar inheritance.
        # ``.hex[:12]`` mirrors new_trace_id()'s documented shape, so both runtime
        # modes emit the same id format.
        trace_id = workflow.uuid4().hex[:12]

        # Phase 1: Spec parsing + Product Requirements Analysis
        spec_result = await workflow.execute_activity(
            _activities.parse_spec_activity,
            args=[job_id, repo_path, spec_content_override, trace_id, sprint_id],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=timedelta(hours=4),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=DEFAULT_RETRY_POLICY,
        )

        # Phase 2: Planning. Loops while the activity reports a pause (a Planning
        # clarification question with no answer yet): each pass durably awaits the
        # matching `submit_planning_answers` signal via `wait_for_planning_answers`
        # (from `PlanningAnswerSignalMixin`) before re-invoking the activity with the
        # resolved answers, mirroring `CodingTeamWorkflow.run`'s HITL pause loop.
        plan_result = await workflow.execute_activity(
            _activities.plan_project_activity,
            args=[job_id, repo_path, spec_result, trace_id],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=timedelta(hours=4),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=DEFAULT_RETRY_POLICY,
        )
        # ``collected_answers`` ACCUMULATES across pause rounds, because the
        # activity replays Planning from scratch on every resume and therefore
        # re-encounters every earlier round's questions. Carrying only the
        # newest batch would leave round 1's questions unmatched on round 2's
        # replay, pause on them again, and ping-pong between the rounds
        # forever.
        #
        # Accumulating still does not guarantee convergence: Planning's
        # question ids are LLM-minted, so a replay can mint fresh ones for
        # questions already answered and every round then pauses on the next
        # batch. ``MAX_PLANNING_PAUSE_ROUNDS`` is what makes this terminate --
        # the final round runs with ``allow_repause=False``, which forbids the
        # activity from pausing again and forces it to return a plan.
        collected_answers: list[dict[str, Any]] = []
        pause_round = 0
        while plan_result.get("outcome") == "paused":
            if pause_round >= MAX_PLANNING_PAUSE_ROUNDS:
                # Unreachable while the activity honours ``allow_repause``: the
                # previous round passed False, which forbids it from pausing
                # again. Reaching here means that contract was broken, and the
                # one thing this loop must never do is spin -- fail the run
                # instead, non-retryable because a retry would spin too.
                raise ApplicationError(
                    f"Planning paused for a {pause_round + 1}th round after being told not to "
                    f"(MAX_PLANNING_PAUSE_ROUNDS={MAX_PLANNING_PAUSE_ROUNDS})",
                    non_retryable=True,
                )
            pause_round += 1
            resume_token = plan_result["resume_token"]
            collected_answers.extend(await self.wait_for_planning_answers(resume_token))
            plan_result = await workflow.execute_activity(
                _activities.plan_project_activity,
                args=[
                    job_id,
                    repo_path,
                    spec_result,
                    trace_id,
                    resume_token,
                    # A snapshot, not the live list: it keeps accumulating for
                    # the next round, and an argument that mutates after the
                    # call is a trap for anything holding it.
                    list(collected_answers),
                    # Last allowed round: the activity must come back with a
                    # plan, not another pause.
                    pause_round < MAX_PLANNING_PAUSE_ROUNDS,
                ],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=timedelta(hours=4),
                heartbeat_timeout=timedelta(minutes=5),
                retry_policy=DEFAULT_RETRY_POLICY,
            )

        if planning_only:
            return

        # Phase 3: Coding Team execution
        await workflow.execute_activity(
            _activities.execute_coding_team_activity,
            args=[job_id, repo_path, plan_result, resolved_questions_override, trace_id],
            task_queue=TASK_QUEUE,
            schedule_to_close_timeout=timedelta(hours=36),
            heartbeat_timeout=timedelta(minutes=10),
            retry_policy=DEFAULT_RETRY_POLICY,
        )


@workflow.defn(name="StandaloneJobWorkflow")
class StandaloneJobWorkflow:
    """Runs a standalone job (frontend-code-v2, backend-code-v2, product-analysis)."""

    @workflow.run
    async def run(
        self,
        job_type: str,
        job_id: str,
        repo_path: str,
        task_dict: Optional[Dict[str, Any]] = None,
        architecture_overview: str = "",
        spec_content: Optional[str] = None,
        inspiration_content: Optional[str] = None,
        initial_spec_path: Optional[str] = None,
    ) -> None:
        if job_type == STANDALONE_TYPE_FRONTEND and task_dict is not None:
            await workflow.execute_activity(
                _activities.run_frontend_code_v2_activity,
                args=[job_id, repo_path, task_dict, architecture_overview],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=STANDALONE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
        elif job_type == STANDALONE_TYPE_BACKEND and task_dict is not None:
            await workflow.execute_activity(
                _activities.run_backend_code_v2_activity,
                args=[job_id, repo_path, task_dict, architecture_overview],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=STANDALONE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
        elif job_type == STANDALONE_TYPE_PRODUCT_ANALYSIS and spec_content is not None:
            await workflow.execute_activity(
                _activities.run_product_analysis_activity,
                args=[job_id, repo_path, spec_content, initial_spec_path],
                task_queue=TASK_QUEUE,
                schedule_to_close_timeout=STANDALONE_TIMEOUT,
                retry_policy=DEFAULT_RETRY_POLICY,
            )
        else:
            raise ValueError(f"Unknown or invalid job_type for StandaloneJobWorkflow: {job_type!r}")
