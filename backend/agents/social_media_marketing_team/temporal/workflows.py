"""Temporal workflow for the social media marketing team.

``SocialMarketingTeamWorkflow`` orchestrates the team pipeline as fine-grained,
independently retryable activities -- consensus -> content plan -> platform ->
experiment -> finalize -- threading each phase's serialized DTO into the next. Each
phase retries independently under ``DEFAULT_RETRY_POLICY`` and shows up as a distinct
span in the Temporal UI.

The human gate is a static request field (``human_approved_for_testing``) read
directly in the workflow -- replay-safe -- so an unapproved run goes straight from
consensus to a ``NEEDS_REVISION`` finalize, skipping the downstream stages, exactly
as thread mode's ``orchestrator.run`` returns early.

Workflow histories recorded before the per-phase decomposition scheduled a single
whole-pipeline activity; the ``workflow.patched`` drain-out branch re-schedules that
legacy activity for such in-flight runs so a deploy does not break them.
"""

from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType
from typing import Any, Dict, Mapping

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from social_media_marketing_team.temporal import activities as _activities
    from social_media_marketing_team.temporal.constants import TASK_QUEUE

# Whole-pipeline ceiling, kept for the legacy drain-out branch so replays of
# pre-decomposition histories run under the exact same envelope as the original
# single activity.
RUN_TIMEOUT = timedelta(hours=4)

# Per-stage overall ceiling (schedule-to-close, including retries) for the decomposed
# activities. Each phase is a single bounded unit of work (deterministic Python plus,
# for the content stage, concept generation and a Postgres read), so a whole-pipeline
# 4h budget per stage would let five stages stack to ~20h before Temporal reacts. One
# hour comfortably covers a stage's retries while keeping the total pipeline ceiling
# proportionate.
STAGE_TIMEOUT = timedelta(hours=1)

# Per-attempt ceiling (start-to-close) for the decomposed activities. Without it a
# single hung attempt (e.g. a wedged LLM exemplar rerank in the content stage) would
# never time out, so the retry policy could never re-dispatch it and the attempt would
# hold a worker activity slot for the full STAGE_TIMEOUT. Twenty minutes is generous
# for the only stage that does real work (the rest are sub-second) while ensuring a
# hang is detected and retried onto a healthy worker well before the overall ceiling.
STAGE_START_TO_CLOSE_TIMEOUT = timedelta(minutes=20)

DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)

# One option block shared by every pipeline-stage activity so tuning a timeout/retry
# is a single edit. No heartbeat timeout: the stage activities are short, deterministic
# Python phases with no in-activity human wait (the human gate is a static request flag
# decided here in the workflow); the per-attempt start-to-close timeout bounds a hang
# instead. Immutable (MappingProxyType) so an importer can't accidentally mutate the
# shared options.
_STAGE_ACTIVITY_OPTS: Mapping[str, Any] = MappingProxyType(
    dict(
        task_queue=TASK_QUEUE,
        schedule_to_close_timeout=STAGE_TIMEOUT,
        start_to_close_timeout=STAGE_START_TO_CLOSE_TIMEOUT,
        retry_policy=DEFAULT_RETRY_POLICY,
    )
)

# Options for the legacy whole-pipeline activity in the drain-out branch: reproduce
# the pre-decomposition envelope exactly -- a single 4h schedule-to-close and NO
# per-attempt start-to-close (the option the monolith was scheduled without) -- so
# in-flight histories replay under identical options. Derived from _STAGE_ACTIVITY_OPTS
# (dropping the per-attempt cap) so the task queue and retry policy stay in lockstep.
_LEGACY_ACTIVITY_OPTS: Mapping[str, Any] = MappingProxyType(
    {k: v for k, v in _STAGE_ACTIVITY_OPTS.items() if k != "start_to_close_timeout"}
    | {"schedule_to_close_timeout": RUN_TIMEOUT}
)


@workflow.defn(name="SocialMarketingTeamWorkflow")
class SocialMarketingTeamWorkflow:
    """Runs one social marketing team job (run or revise) as staged activities."""

    @workflow.run
    async def run(self, job_id: str, request_dict: Dict[str, Any]) -> None:
        """Execute the pipeline-phase activities in sequence.

        Preconditions:
            - ``job_id`` identifies a created job record; ``request_dict`` is a
              serialized ``RunMarketingTeamRequest``.
        Postconditions:
            - On success each phase runs once and the finalize activity completes the
              job store. A ``FAIL`` status from any stage (job already failed)
              short-circuits without finalizing. When the request is not
              human-approved, only consensus runs before a ``NEEDS_REVISION``
              finalize. Histories recorded before the per-phase decomposition replay
              the original single-activity path (via ``workflow.patched``).
        """
        if not workflow.patched("social-per-phase-activities"):
            # Drain-out branch: replays of pre-decomposition histories must
            # re-schedule the original monolithic activity deterministically.
            # Removal criterion: once no open workflow histories predate the
            # per-phase decomposition deploy, replace this block with
            # ``workflow.deprecate_patch("social-per-phase-activities")`` for one
            # release, then delete the marker along with ``run_team_job_activity``.
            await workflow.execute_activity(
                _activities.run_team_job_activity,
                args=[job_id, request_dict],
                **_LEGACY_ACTIVITY_OPTS,
            )
            return

        consensus = await workflow.execute_activity(
            _activities.consensus_stage_activity,
            args=[job_id, request_dict],
            **_STAGE_ACTIVITY_OPTS,
        )
        if consensus.get("status") == "FAIL":
            return

        # Human gate: the workflow is the single decision point. Read the static
        # request flag directly (replay-safe) and thread the decision into finalize
        # as an explicit ``approved`` flag so the activity never re-derives it. An
        # unapproved run finalizes NEEDS_REVISION without the downstream stages,
        # matching orchestrator.run's early return.
        approved = bool(request_dict.get("human_approved_for_testing"))
        if not approved:
            await workflow.execute_activity(
                _activities.finalize_stage_activity,
                args=[job_id, request_dict, consensus, approved],
                **_STAGE_ACTIVITY_OPTS,
            )
            return

        content = await workflow.execute_activity(
            _activities.content_plan_stage_activity,
            args=[job_id, request_dict, consensus],
            **_STAGE_ACTIVITY_OPTS,
        )
        if content.get("status") == "FAIL":
            return

        platform = await workflow.execute_activity(
            _activities.platform_stage_activity,
            args=[job_id, request_dict, consensus, content],
            **_STAGE_ACTIVITY_OPTS,
        )
        if platform.get("status") == "FAIL":
            return

        experiment = await workflow.execute_activity(
            _activities.experiment_stage_activity,
            args=[job_id, request_dict, consensus, content],
            **_STAGE_ACTIVITY_OPTS,
        )
        if experiment.get("status") == "FAIL":
            return

        await workflow.execute_activity(
            _activities.finalize_stage_activity,
            args=[job_id, request_dict, consensus, approved, content, platform, experiment],
            **_STAGE_ACTIVITY_OPTS,
        )
