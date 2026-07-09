"""Temporal activities for the blogging team.

The full blog pipeline is decomposed into four fine-grained, independently
retryable activities orchestrated by ``BlogFullPipelineWorkflow``:

* ``plan_stage_activity``     -> planning + story elicitation + outline approval
* ``draft_stage_activity``    -> initial draft + interactive review + copy-edit loop
* ``gates_stage_activity``    -> validators + fact-check + compliance + rewrite loop
* ``finalize_job_activity``   -> completes the job-store entry from the final result

State crosses each boundary as a JSON-native dict (the ``temporal.phase_models``
DTOs). Every activity re-seeds a ``_PipelineContext`` from the previous stage's DTO,
runs the corresponding stage function (shared with thread mode via ``run_pipeline``),
and serializes its output. Heavy imports live inside the activity bodies so importing
this module stays cheap and sandbox-safe.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from temporalio import activity

logger = logging.getLogger(__name__)


def _build_pipeline_context(job_id: str, request_dict: Dict[str, Any]) -> Any:
    """Construct a ``_PipelineContext`` seeded with the run's inputs.

    Preconditions:
        - ``request_dict`` is a serialized full-pipeline request.
    Postconditions:
        - Returns a ``_PipelineContext`` with a resolved LLM client, length policy,
          job updater, and work_dir. Stage-produced fields (plan/draft/etc.) are left
          at their defaults for the caller to seed from the prior stage's DTO.
    """
    from blogging.agent_implementations.blog_writing_process_v2 import (
        DRAFT_EDITOR_ITERATIONS,
        _PipelineContext,
    )
    from blogging.shared.content_profile import resolve_length_policy_from_request_dict
    from blogging.shared.run_pipeline_job import (
        _get_run_artifacts_base,
        build_brief_input,
        make_job_updater,
    )
    from llm_service import get_strands_model

    work_dir = _get_run_artifacts_base() / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    return _PipelineContext(
        brief=build_brief_input(request_dict),
        work_dir=work_dir,
        llm_client=get_strands_model("blog"),
        length_policy=resolve_length_policy_from_request_dict(request_dict),
        series_context=None,
        job_id=job_id,
        job_updater=make_job_updater(job_id),
        draft_editor_iterations=DRAFT_EDITOR_ITERATIONS,
        max_rewrite_iterations=int(request_dict.get("max_rewrite_iterations", 3)),
        run_gates=bool(request_dict.get("run_gates", True)),
    )


def _fail_activity(job_id: str, exc: Exception, failed_phase: Optional[str]) -> bool:
    """Mirror ``run_blog_full_pipeline_job``'s error funnel for a single stage.

    Preconditions:
        - ``exc`` is the exception raised by a stage function.
    Postconditions:
        - Returns True when ``exc`` is an external (Temporal) cancellation: the job
          is marked cancelled and the caller should swallow the error.
        - Otherwise marks the job failed (with ``failed_phase``/planning reason) and
          publishes a terminal ``error`` event, then returns False so the caller
          re-raises (letting Temporal retry within the workflow's retry policy).
    """
    from blogging.shared.run_pipeline_job import (
        _fail_job,
        _is_external_cancellation,
        _publish_terminal,
        mark_job_cancelled,
    )

    if _is_external_cancellation(exc):
        mark_job_cancelled(job_id)
        return True

    planning_failure_reason = getattr(exc, "failure_reason", None)
    phase = failed_phase or getattr(exc, "phase", None)
    logger.exception("Blog pipeline stage %r failed for job %s", failed_phase, job_id)
    _fail_job(job_id, str(exc), failed_phase=phase, planning_failure_reason=planning_failure_reason)
    _publish_terminal(job_id, "error", error=str(exc), failed_phase=phase)
    return False


@activity.defn(name="blog_plan_stage")
def plan_stage_activity(job_id: str, request_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Planning stage: content planning, story elicitation, outline approval.

    Preconditions:
        - ``job_id`` identifies a created job record; ``request_dict`` is a serialized
          full-pipeline request.
    Postconditions:
        - Starts the job, runs ``run_planning_stage``, and returns a serialized
          ``PlanningStageResult``. Marks the job cancelled and returns a ``FAIL`` DTO
          on external cancellation; fails the job and re-raises on any other error.
    """
    from temporalio.exceptions import CancelledError

    from blogging.agent_implementations.blog_writing_process_v2 import run_planning_stage
    from blogging.shared.blog_job_store import start_blog_job
    from blogging.shared.run_pipeline_job import start_pipeline_heartbeat
    from blogging.temporal.phase_models import PlanningStageResult

    hb = None
    try:
        ctx = _build_pipeline_context(job_id, request_dict)
        start_blog_job(job_id)
        ctx.job_updater(work_dir=str(ctx.work_dir))
        hb = start_pipeline_heartbeat(job_id)

        abort = run_planning_stage(ctx)
        if abort is not None:
            _, _, status = abort
            return PlanningStageResult(status=status).model_dump()
        return PlanningStageResult(
            planning_phase_result=ctx.planning_phase_result.model_dump(mode="json"),
            elicited_stories_text=ctx.elicited_stories_text,
            status="PASS",
        ).model_dump()
    except CancelledError:
        logger.info("Blog plan stage cancelled for job %s", job_id)
        raise
    except Exception as e:
        if _fail_activity(job_id, e, failed_phase="planning"):
            return PlanningStageResult(status="FAIL").model_dump()
        raise
    finally:
        if hb is not None:
            hb.stop()


@activity.defn(name="blog_draft_stage")
def draft_stage_activity(
    job_id: str,
    request_dict: Dict[str, Any],
    planning_stage: Dict[str, Any],
) -> Dict[str, Any]:
    """Draft stage: initial draft, interactive review, and the copy-edit loop.

    Preconditions:
        - ``planning_stage`` is a serialized ``PlanningStageResult`` with
          ``status == "PASS"`` (the workflow short-circuits otherwise).
    Postconditions:
        - Runs ``run_draft_stage`` and returns a serialized ``DraftStageResult``.
          Marks the job cancelled and returns a ``FAIL`` DTO on external
          cancellation; fails the job and re-raises on any other error.
    """
    from temporalio.exceptions import CancelledError

    from blogging.agent_implementations.blog_writing_process_v2 import run_draft_stage
    from blogging.shared.content_plan import PlanningPhaseResult
    from blogging.shared.run_pipeline_job import start_pipeline_heartbeat
    from blogging.temporal.phase_models import DraftStageResult

    hb = None
    try:
        ctx = _build_pipeline_context(job_id, request_dict)
        ppr = PlanningPhaseResult.model_validate(planning_stage["planning_phase_result"])
        ctx.planning_phase_result = ppr
        ctx.plan = ppr.content_plan
        ctx.elicited_stories_text = planning_stage.get("elicited_stories_text")
        hb = start_pipeline_heartbeat(job_id)

        abort = run_draft_stage(ctx)
        if abort is not None:
            _, draft_result, status = abort
            return DraftStageResult(
                draft=draft_result.model_dump(mode="json") if draft_result is not None else None,
                elicited_stories_text=ctx.elicited_stories_text,
                status=status,
            ).model_dump()
        return DraftStageResult(
            draft=ctx.draft_result.model_dump(mode="json"),
            elicited_stories_text=ctx.elicited_stories_text,
            status="PASS",
        ).model_dump()
    except CancelledError:
        logger.info("Blog draft stage cancelled for job %s", job_id)
        raise
    except Exception as e:
        if _fail_activity(job_id, e, failed_phase="draft"):
            return DraftStageResult(status="FAIL").model_dump()
        raise
    finally:
        if hb is not None:
            hb.stop()


@activity.defn(name="blog_gates_stage")
def gates_stage_activity(
    job_id: str,
    request_dict: Dict[str, Any],
    planning_stage: Dict[str, Any],
    draft_stage: Dict[str, Any],
) -> Dict[str, Any]:
    """Gates stage: validators, fact-check, compliance, rewrite loop, and finalize.

    Preconditions:
        - ``planning_stage``/``draft_stage`` are serialized stage results with
          ``status == "PASS"`` (the workflow short-circuits otherwise).
    Postconditions:
        - Runs ``run_gates_stage`` and returns a serialized ``GatesStageResult``
          carrying the final draft and terminal status (PASS or NEEDS_HUMAN_REVIEW).
          Fails the job and re-raises on error.
    """
    from blog_writer_agent.models import WriterOutput
    from temporalio.exceptions import CancelledError

    from blogging.agent_implementations.blog_writing_process_v2 import run_gates_stage
    from blogging.shared.content_plan import PlanningPhaseResult
    from blogging.shared.run_pipeline_job import start_pipeline_heartbeat
    from blogging.temporal.phase_models import GatesStageResult

    hb = None
    try:
        ctx = _build_pipeline_context(job_id, request_dict)
        ppr = PlanningPhaseResult.model_validate(planning_stage["planning_phase_result"])
        ctx.planning_phase_result = ppr
        ctx.plan = ppr.content_plan
        ctx.draft_result = WriterOutput.model_validate(draft_stage["draft"])
        ctx.elicited_stories_text = draft_stage.get("elicited_stories_text")
        hb = start_pipeline_heartbeat(job_id)

        run_gates_stage(ctx)
        return GatesStageResult(
            draft=ctx.draft_result.model_dump(mode="json"),
            status=ctx.status,
        ).model_dump()
    except CancelledError:
        logger.info("Blog gates stage cancelled for job %s", job_id)
        raise
    except Exception as e:
        if _fail_activity(job_id, e, failed_phase="gates"):
            return GatesStageResult(status="FAIL").model_dump()
        raise
    finally:
        if hb is not None:
            hb.stop()


@activity.defn(name="blog_finalize")
def finalize_job_activity(
    job_id: str,
    planning_stage: Dict[str, Any],
    gates_stage: Dict[str, Any],
) -> None:
    """Finalize: complete the job-store entry from the final pipeline result.

    Preconditions:
        - ``planning_stage``/``gates_stage`` are serialized stage results from a run
          that reached the gates stage.
    Postconditions:
        - Reconstructs the planning result and final draft and calls
          ``finalize_blog_job`` (COMPLETED when ``status == "PASS"``, else
          NEEDS_REVIEW). Fails the job and re-raises on error.
    """
    from blog_writer_agent.models import WriterOutput
    from temporalio.exceptions import CancelledError

    from blogging.shared.content_plan import PlanningPhaseResult
    from blogging.shared.run_pipeline_job import finalize_blog_job

    try:
        ppr = PlanningPhaseResult.model_validate(planning_stage["planning_phase_result"])
        draft_data = gates_stage.get("draft")
        draft_result = WriterOutput.model_validate(draft_data) if draft_data is not None else None
        finalize_blog_job(job_id, ppr, draft_result, gates_stage.get("status", "PASS"))
    except CancelledError:
        logger.info("Blog finalize cancelled for job %s", job_id)
        raise
    except Exception as e:
        if not _fail_activity(job_id, e, failed_phase="finalize"):
            raise
