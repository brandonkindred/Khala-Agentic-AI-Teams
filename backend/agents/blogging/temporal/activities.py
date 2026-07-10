"""Temporal activities for the blogging team.

The full blog pipeline is decomposed into four fine-grained, independently
retryable activities orchestrated by ``BlogFullPipelineWorkflow``:

* ``plan_stage_activity``     -> planning + story elicitation + outline approval
* ``draft_stage_activity``    -> initial draft + interactive review + copy-edit loop
* ``gates_stage_activity``    -> validators + fact-check + compliance + rewrite loop
* ``finalize_job_activity``   -> completes the job-store entry from the final result

State crosses each boundary as a JSON-native dict (the ``temporal.phase_models``
DTOs). Every stage activity re-seeds a ``PipelineContext`` from the previous
stage's DTO and runs the corresponding stage function (shared with thread mode via
``run_pipeline``) under the shared ``_run_stage`` funnel; input-DTO deserialization
happens OUTSIDE the funnel so schema/plumbing defects fail the activity loudly
instead of masquerading as pipeline failures. Heavy imports live inside function
bodies so importing this module stays cheap and sandbox-safe.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from temporalio import activity

logger = logging.getLogger(__name__)


def _build_pipeline_context(job_id: str, request_dict: Dict[str, Any]) -> Any:
    """Construct a ``PipelineContext`` seeded with the run's inputs.

    Preconditions:
        - ``request_dict`` is a serialized full-pipeline request.
    Postconditions:
        - Returns a ``PipelineContext`` with a resolved LLM client, length policy,
          job updater, and work_dir. Stage-produced fields (plan/draft/etc.) are left
          at their defaults for the caller to seed from the prior stage's DTO.
    """
    from blogging.agent_implementations.blog_writing_process_v2 import (
        DRAFT_EDITOR_ITERATIONS,
        PipelineContext,
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

    return PipelineContext(
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


def _fail_activity(job_id: str, exc: Exception, failed_phase: Optional[str]) -> None:
    """Mirror ``run_blog_full_pipeline_job``'s error funnel for a single stage.

    Handled pipeline errors are terminal, matching the pre-decomposition behavior
    (``run_blog_full_pipeline_job`` swallowed them after failing the job, so the
    activity completed and Temporal never retried): the stage-activity callers
    return a FAIL DTO so the workflow short-circuits, keeping Temporal's stage
    retries reserved for worker crashes and timeouts. (``finalize_job_activity``
    is the exception — it deliberately re-raises transient store errors so Temporal
    retries, calling this helper only on its final attempt.)

    Preconditions:
        - ``exc`` is the exception raised by a stage function.
    Postconditions:
        - External (Temporal) cancellation: the job is marked cancelled.
        - Otherwise the job is marked failed and a terminal ``error`` event is
          published. The exception's own ``phase`` (e.g. ``compliance``) wins over
          the coarse ``failed_phase`` stage name, preserving the pre-decomposition
          granularity of the job store's ``failed_phase`` field.
    """
    from blogging.shared.run_pipeline_job import (
        _fail_job,
        _is_external_cancellation,
        _publish_terminal,
        mark_job_cancelled,
    )

    if _is_external_cancellation(exc):
        mark_job_cancelled(job_id)
        return

    planning_failure_reason = getattr(exc, "failure_reason", None)
    phase = getattr(exc, "phase", None) or failed_phase
    logger.exception("Blog pipeline stage %r failed for job %s", failed_phase, job_id)
    _fail_job(job_id, str(exc), failed_phase=phase, planning_failure_reason=planning_failure_reason)
    _publish_terminal(job_id, "error", error=str(exc), failed_phase=phase)


def _is_last_attempt() -> bool:
    """True when this is the final Temporal retry attempt (or no activity context).

    Reads the ``maximum_attempts`` from the retry policy the activity was actually
    scheduled with (``activity.info().retry_policy``) rather than a compile-time
    constant, so the check never drifts from the workflow's policy and stays correct
    for in-flight histories scheduled under an older policy.

    Preconditions:
        - Called from within a stage/finalize activity body (or directly/thread mode).
    Postconditions:
        - Returns True when the current attempt is the last one Temporal will make,
          or when called outside an activity context (direct/thread use — the caller
          then marks the job terminal).
        - Returns False when the scheduled policy allows unlimited retries
          (``maximum_attempts <= 0``): there is no last attempt to gate on, so the
          caller keeps re-raising and defers to Temporal.
    """
    try:
        info = activity.info()
    except RuntimeError:
        return True
    policy = info.retry_policy
    max_attempts = policy.maximum_attempts if policy is not None else 0
    # maximum_attempts <= 0 means unlimited retries in Temporal; there is no
    # "last attempt" to gate on, so never swallow — keep re-raising.
    if max_attempts <= 0:
        return False
    return info.attempt >= max_attempts


def _run_stage(
    job_id: str,
    failed_phase: str,
    fail_dto: Callable[[], Dict[str, Any]],
    body: Callable[[], Dict[str, Any]],
) -> Dict[str, Any]:
    """Run one pipeline-stage body under the shared heartbeat + error funnel.

    The funnel is the correctness contract every stage activity must share: handled
    errors terminate the job store and short-circuit the workflow (never leaking to
    Temporal retry), Temporal-native cancellation propagates, and the background
    heartbeat always stops. Keeping it in one place makes the contract structural —
    a new stage activity cannot forget it.

    Preconditions:
        - ``body`` is a zero-arg callable that runs the stage and returns its
          serialized DTO dict; ``fail_dto`` builds the stage's FAIL DTO dict.
        - ``body`` MUST NOT catch Temporal ``CancelledError`` — this funnel handles
          it (re-raising so cancellation propagates). Swallowing it in ``body`` would
          convert an external cancellation into a spurious FAIL.
        - Anything ``body`` does that is infrastructure setup rather than pipeline
          work (e.g. starting the job, rebuilding an input DTO) belongs OUTSIDE this
          funnel, so its failures propagate to Temporal for retry instead of being
          masked as a pipeline FAIL.
    Postconditions:
        - Returns ``body()``'s DTO on success. On any handled error the job is
          marked cancelled/failed (via ``_fail_activity`` with ``failed_phase``)
          and ``fail_dto()`` is returned. Re-raises ``CancelledError``.
    """
    from temporalio.exceptions import CancelledError

    from blogging.shared.run_pipeline_job import start_pipeline_heartbeat

    # Start the heartbeat OUTSIDE the funnel: a heartbeat-start failure is an
    # infrastructure error (store/thread), so it must propagate to Temporal for
    # retry rather than be caught below and masked as a terminal pipeline FAIL —
    # matching the start_blog_job / DTO-rebuild pattern.
    hb = start_pipeline_heartbeat(job_id)
    try:
        return body()
    except CancelledError:
        logger.info("Blog %s stage cancelled for job %s", failed_phase, job_id)
        raise
    except Exception as e:
        _fail_activity(job_id, e, failed_phase=failed_phase)
        return fail_dto()
    finally:
        if hb is not None:
            hb.stop()


@activity.defn(name="blog_plan_stage")
def plan_stage_activity(job_id: str, request_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Planning stage: content planning, story elicitation, outline approval.

    Preconditions:
        - ``job_id`` identifies a created job record; ``request_dict`` is a serialized
          full-pipeline request.
    Postconditions:
        - Starts the job (outside the funnel, so a store/infra failure propagates to
          Temporal for retry rather than reading as a pipeline FAIL), runs
          ``run_planning_stage``, and returns a serialized ``PlanningStageResult``.
          On any handled stage error the job is marked cancelled/failed and a
          ``FAIL`` DTO is returned so the workflow short-circuits; only
          Temporal-native ``CancelledError`` propagates.
    """
    from blogging.shared.blog_job_store import start_blog_job
    from blogging.temporal.phase_models import PlanningStageResult

    # Start the job OUTSIDE the funnel: a start_blog_job failure (store unavailable,
    # job already exists) is an infrastructure error, not a pipeline failure, so it
    # must propagate to Temporal for retry rather than be masked as a FAIL DTO —
    # matching the pre-decomposition behavior and the draft/gates DTO-rebuild pattern.
    start_blog_job(job_id)

    def _body() -> Dict[str, Any]:
        from blogging.agent_implementations.blog_writing_process_v2 import run_planning_stage

        ctx = _build_pipeline_context(job_id, request_dict)
        ctx.job_updater(work_dir=str(ctx.work_dir))

        abort = run_planning_stage(ctx)
        if abort is not None:
            _, _, status = abort
            return PlanningStageResult(status=status).model_dump()
        return PlanningStageResult(
            planning_phase_result=ctx.planning_phase_result.model_dump(mode="json"),
            elicited_stories_text=ctx.elicited_stories_text,
            status="PASS",
        ).model_dump()

    return _run_stage(
        job_id, "planning", lambda: PlanningStageResult(status="FAIL").model_dump(), _body
    )


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
          On any handled stage error the job is marked cancelled/failed and a
          ``FAIL`` DTO is returned; only Temporal-native ``CancelledError``
          propagates. A malformed input DTO raises out of the activity (a
          code/schema defect must fail loudly, not read as a pipeline failure).
    """
    from blogging.shared.content_plan import PlanningPhaseResult
    from blogging.temporal.phase_models import DraftStageResult

    # Rebuild inputs OUTSIDE the funnel: a malformed inter-activity DTO is a code
    # bug (or cross-deploy schema skew), not a pipeline failure.
    ppr = PlanningPhaseResult.model_validate(planning_stage["planning_phase_result"])
    elicited_stories_text = planning_stage.get("elicited_stories_text")

    def _body() -> Dict[str, Any]:
        from blogging.agent_implementations.blog_writing_process_v2 import run_draft_stage

        ctx = _build_pipeline_context(job_id, request_dict)
        ctx.planning_phase_result = ppr
        ctx.plan = ppr.content_plan
        ctx.elicited_stories_text = elicited_stories_text

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

    return _run_stage(job_id, "draft", lambda: DraftStageResult(status="FAIL").model_dump(), _body)


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
          On any handled stage error the job is marked cancelled/failed and a
          ``FAIL`` DTO is returned so the workflow skips finalize; only
          Temporal-native ``CancelledError`` propagates. A malformed input DTO
          raises out of the activity (code/schema defects fail loudly).
    """
    from blog_writer_agent.models import WriterOutput

    from blogging.shared.content_plan import PlanningPhaseResult
    from blogging.temporal.phase_models import GatesStageResult

    # Rebuild inputs OUTSIDE the funnel: a malformed inter-activity DTO is a code
    # bug (or cross-deploy schema skew), not a pipeline failure.
    ppr = PlanningPhaseResult.model_validate(planning_stage["planning_phase_result"])
    draft_result = WriterOutput.model_validate(draft_stage["draft"])
    elicited_stories_text = draft_stage.get("elicited_stories_text")

    def _body() -> Dict[str, Any]:
        from blogging.agent_implementations.blog_writing_process_v2 import run_gates_stage

        ctx = _build_pipeline_context(job_id, request_dict)
        ctx.planning_phase_result = ppr
        ctx.plan = ppr.content_plan
        ctx.draft_result = draft_result
        ctx.elicited_stories_text = elicited_stories_text

        run_gates_stage(ctx)
        return GatesStageResult(
            draft=ctx.draft_result.model_dump(mode="json"),
            status=ctx.status,
        ).model_dump()

    return _run_stage(job_id, "gates", lambda: GatesStageResult(status="FAIL").model_dump(), _body)


@activity.defn(name="blog_finalize")
def finalize_job_activity(
    job_id: str,
    planning_stage: Dict[str, Any],
    gates_stage: Dict[str, Any],
) -> None:
    """Finalize: complete the job-store entry from the final pipeline result.

    Preconditions:
        - ``planning_stage``/``gates_stage`` are serialized stage results from a run
          that reached the gates stage with a non-FAIL status.
    Postconditions:
        - Reconstructs the planning result and final draft and calls
          ``finalize_blog_job`` (COMPLETED when ``status == "PASS"``, else
          NEEDS_REVIEW). Unlike the stage activities, nothing is terminal before
          finalize runs, so a transient store error must not permanently fail a
          successful pipeline: the store call re-raises (letting Temporal retry)
          until the final attempt, which marks the job failed and swallows.
        - A malformed input DTO raises out of the activity (a code/schema defect
          must fail loudly, not read as a retryable store error) — matching the
          draft/gates contract.
    """
    from blog_writer_agent.models import WriterOutput
    from temporalio.exceptions import CancelledError

    from blogging.shared.content_plan import PlanningPhaseResult
    from blogging.shared.run_pipeline_job import finalize_blog_job

    # Rebuild inputs OUTSIDE the retry funnel: a malformed inter-activity DTO is a
    # code bug (or cross-deploy schema skew), not a transient store error.
    ppr = PlanningPhaseResult.model_validate(planning_stage["planning_phase_result"])
    draft_data = gates_stage.get("draft")
    draft_result = WriterOutput.model_validate(draft_data) if draft_data is not None else None

    try:
        finalize_blog_job(job_id, ppr, draft_result, gates_stage.get("status", "PASS"))
    except CancelledError:
        logger.info("Blog finalize cancelled for job %s", job_id)
        raise
    except Exception as e:
        # Nothing is terminal yet; retry transient store errors, marking the job
        # failed only once Temporal has no attempt left.
        if not _is_last_attempt():
            raise
        _fail_activity(job_id, e, failed_phase="finalize")


@activity.defn(name="run_blog_full_pipeline")
def run_full_pipeline_activity(job_id: str, request_dict: Dict[str, Any]) -> None:
    """Legacy whole-pipeline activity, kept registered for drain-out.

    Workflow histories recorded before the per-phase decomposition contain a
    single scheduled activity of this type; the workflow's unpatched replay
    branch re-schedules it, so it must stay registered until those runs drain.

    Preconditions:
        - ``job_id`` identifies a created job record; ``request_dict`` is a
          serialized full-pipeline request.
    Postconditions:
        - ``run_blog_full_pipeline_job`` has run to completion (it owns all job
          store updates and error handling); re-raises whatever it raises.
    """
    from temporalio.exceptions import CancelledError

    from blogging.shared.run_pipeline_job import run_blog_full_pipeline_job

    try:
        run_blog_full_pipeline_job(job_id, request_dict)
    except CancelledError:
        logger.info("Blog pipeline activity cancelled for job %s", job_id)
        raise
    except Exception:
        logger.exception("Blog full pipeline activity failed for job %s", job_id)
        raise
