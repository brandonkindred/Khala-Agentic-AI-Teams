"""Temporal activities for the social media marketing team.

The team pipeline is decomposed into fine-grained, independently retryable
activities orchestrated by ``SocialMarketingTeamWorkflow``:

* ``consensus_stage_activity``     -> fetch/validate brand + collaboration consensus
* ``content_plan_stage_activity``  -> winners load + concept generation/filtering
* ``platform_stage_activity``      -> per-platform execution plans
* ``experiment_stage_activity``    -> control/variant experiment design
* ``finalize_stage_activity``      -> assemble ``TeamOutput`` + complete the job store

State crosses each boundary as a JSON-native dict (the ``temporal.phase_models``
DTOs). Every stage rebuilds its inputs via ``model_validate`` and runs the matching
orchestrator phase method (shared with thread mode) under the shared ``_run_stage``
error funnel; input-DTO deserialization happens OUTSIDE the funnel so schema/plumbing
defects fail the activity loudly instead of masquerading as pipeline failures. Heavy
imports live inside function bodies so importing this module stays cheap and
sandbox-safe.

The legacy whole-pipeline ``run_team_job_activity`` stays registered so workflow
histories recorded before the per-phase decomposition can drain out via the
workflow's unpatched replay branch.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fail_activity(job_id: str, exc: Exception, failed_phase: str) -> None:
    """Mark the job failed, mirroring ``_run_team_job``'s error funnel.

    Preconditions:
        - ``exc`` is the exception raised by a stage body.
    Postconditions:
        - The job store entry is marked failed with the exception detail; a
          best-effort update failure is swallowed so the FAIL DTO still returns.
    """
    from job_service_client import JOB_STATUS_FAILED
    from social_media_marketing_team.api.main import _update_job

    logger.exception("Social marketing stage %r failed for job %s", failed_phase, job_id)
    try:
        _update_job(
            job_id,
            status=JOB_STATUS_FAILED,
            current_stage="failed",
            error=f"{type(exc).__name__}: {exc}",
            eta_hint=None,
        )
    except Exception:  # pragma: no cover - job store best-effort on the failure path
        logger.warning("Failed to mark job %s failed after stage %r", job_id, failed_phase)


def _mark_cancelled(job_id: str) -> None:
    """Mark the job cancelled (best-effort) on external cancellation.

    Preconditions:
        - ``job_id`` identifies a job record (may already be terminal).
    Postconditions:
        - The job store entry is marked ``cancelled``; a best-effort update failure
          is swallowed so the caller can still propagate the cancellation.
    """
    from job_service_client import JOB_STATUS_CANCELLED
    from social_media_marketing_team.api.main import _update_job

    try:
        _update_job(job_id, status=JOB_STATUS_CANCELLED, current_stage="cancelled", eta_hint=None)
    except Exception:  # pragma: no cover - job store best-effort on the cancel path
        logger.warning("Failed to mark job %s cancelled", job_id)


def _is_cancelled() -> bool:
    """True when the current activity has been cancelled.

    Preconditions:
        - None (safe to call outside an activity context).
    Postconditions:
        - Returns ``activity.is_cancelled()`` inside an activity context, else
          ``False`` (direct/thread use has no cancellation to observe).
    """
    try:
        return activity.is_cancelled()
    except RuntimeError:
        return False


def _is_last_attempt() -> bool:
    """True when this is the final Temporal retry attempt (or no activity context).

    Reads ``maximum_attempts`` from the retry policy the activity was scheduled with
    (``activity.info().retry_policy``) rather than a compile-time constant, so the
    check never drifts from the workflow's policy.

    Preconditions:
        - Called from within an activity body (or directly / thread mode).
    Postconditions:
        - Returns True when the current attempt is the last Temporal will make, or
          when called outside an activity context (the caller then marks the job
          terminal). Returns False when the policy allows unlimited retries
          (``maximum_attempts <= 0``) -- there is no last attempt to gate on.
    """
    try:
        info = activity.info()
    except RuntimeError:
        return True
    policy = info.retry_policy
    max_attempts = policy.maximum_attempts if policy is not None else 0
    if max_attempts <= 0:
        return False
    return info.attempt >= max_attempts


def _run_stage(
    job_id: str,
    failed_phase: str,
    fail_dto: Callable[[], Dict[str, Any]],
    body: Callable[[], Dict[str, Any]],
) -> Dict[str, Any]:
    """Run one pipeline-stage body under the shared error funnel.

    Handled errors terminate the job store and short-circuit the workflow (via the
    returned FAIL DTO) rather than leaking to Temporal retry, while an external
    cancellation propagates as a Temporal ``CancelledError`` (job marked cancelled,
    not failed). Keeping the funnel in one place makes the contract structural -- a
    new stage activity cannot forget it.

    Preconditions:
        - ``body`` is a zero-arg callable returning the stage's serialized DTO dict;
          ``fail_dto`` builds the stage's FAIL DTO dict.
        - ``body`` MUST NOT catch Temporal ``CancelledError``.
    Postconditions:
        - Returns ``body()``'s DTO on success. When the activity was cancelled the
          job is marked cancelled and a ``CancelledError`` propagates. On any other
          handled error the job is marked failed and ``fail_dto()`` is returned.
    """
    from temporalio.exceptions import CancelledError

    try:
        return body()
    except CancelledError:
        logger.info("Social marketing %s stage cancelled for job %s", failed_phase, job_id)
        _mark_cancelled(job_id)
        raise
    except Exception as e:
        # A worker-delivered cancellation can surface as a non-CancelledError from a
        # sync body (these activities don't heartbeat, so cancellation is observed
        # via activity.is_cancelled() rather than a raised CancelledError). Treat an
        # in-flight cancellation as cancelled, not a pipeline failure, and re-raise a
        # CancelledError so Temporal records the activity as cancelled.
        if _is_cancelled():
            logger.info("Social marketing %s stage cancelled for job %s", failed_phase, job_id)
            _mark_cancelled(job_id)
            raise CancelledError(f"{failed_phase} stage cancelled") from e
        _fail_activity(job_id, e, failed_phase)
        return fail_dto()


def _build_orchestrator(request: Any) -> Any:
    """Construct the orchestrator for a request (shared with thread mode).

    Preconditions:
        - ``request`` is a ``RunMarketingTeamRequest`` exposing ``llm_model_name``.
    Postconditions:
        - Returns a fresh ``SocialMediaMarketingOrchestrator`` configured with the
          request's LLM model name; holds no per-job state.
    """
    from social_media_marketing_team.orchestrator import SocialMediaMarketingOrchestrator

    return SocialMediaMarketingOrchestrator(llm_model_name=request.llm_model_name)


def _build_performance(job_id: str, campaign_name: str) -> Any:
    """Build the performance snapshot from stored observations (as ``_run_team_job``).

    Preconditions:
        - ``job_id`` identifies a created job record; ``campaign_name`` is the
          campaign the observations belong to.
    Postconditions:
        - Returns a ``CampaignPerformanceSnapshot`` over the job's stored
          ``performance_observations`` (empty when the job or field is absent).
    """
    from social_media_marketing_team.api.main import _job_manager
    from social_media_marketing_team.models import CampaignPerformanceSnapshot

    observations = (_job_manager.get_job(job_id) or {}).get("performance_observations", [])
    return CampaignPerformanceSnapshot(campaign_name=campaign_name, observations=observations)


# ---------------------------------------------------------------------------
# Fine-grained stage activities
# ---------------------------------------------------------------------------


@activity.defn(name="social_marketing_consensus_stage")
def consensus_stage_activity(job_id: str, request_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Consensus stage: re-fetch/validate brand and drive the collaboration loop.

    The brand is re-fetched here so context is current even for replays/retries.
    Brand-related failures (missing or incomplete brand) are non-retryable since
    retrying won't fix them.

    Preconditions:
        - ``job_id`` identifies a created job record; ``request_dict`` is a serialized
          ``RunMarketingTeamRequest``.
    Postconditions:
        - Updates job progress to 30, returns a serialized ``ConsensusStageResult``
          (proposal + resolved goals + brand name). A brand error marks the job
          failed and raises a non-retryable ``ApplicationError``. An unexpected
          brand-fetch error (network/timeout/etc.) marks the job failed and
          re-raises so Temporal can still retry a transient fault while the store
          reflects the failure. Any other handled stage error marks the job failed
          and returns a ``FAIL`` DTO.
    """
    from temporalio.exceptions import CancelledError

    from social_media_marketing_team.adapters.branding import (
        BrandIncompleteError,
        BrandNotFoundError,
        fetch_brand,
        validate_brand_for_social_marketing,
    )
    from social_media_marketing_team.api.main import RunMarketingTeamRequest, _update_job
    from social_media_marketing_team.temporal.phase_models import ConsensusStageResult

    # Rebuild the request and re-fetch the brand OUTSIDE the funnel: a malformed
    # request is a code/schema defect and a brand error is permanent -- both must
    # surface (as a raised error) rather than read as a pipeline FAIL.
    request = RunMarketingTeamRequest(**request_dict)
    try:
        brand_data = fetch_brand(request.client_id, request.brand_id)
        brand_ctx = validate_brand_for_social_marketing(
            brand_data, request.client_id, request.brand_id
        )
    except (BrandNotFoundError, BrandIncompleteError) as exc:
        _fail_activity(job_id, exc, "brand_validation")
        raise ApplicationError(str(exc), non_retryable=True) from exc
    except Exception as exc:
        # Unexpected brand-fetch errors (network/timeout/RuntimeError from the
        # branding API) are retryable, but the job store must still reflect the
        # failure rather than sit in "running" until the stale monitor fires --
        # mirroring the thread-mode ``_run_team_job`` funnel. Re-raise so Temporal
        # can still retry a transient fault; surface an in-flight cancellation as a
        # cancellation rather than a failure.
        if _is_cancelled():
            _mark_cancelled(job_id)
            raise CancelledError("consensus brand fetch cancelled") from exc
        _fail_activity(job_id, exc, "brand_validation")
        raise

    _update_job(
        job_id,
        status="running",
        current_stage="building_campaign_proposal",
        progress=30,
        eta_hint="~1 minute",
    )

    def _body() -> Dict[str, Any]:
        goals = brand_ctx.to_brand_goals(
            goals=request.goals,
            cadence_posts_per_day=request.cadence_posts_per_day,
            duration_days=request.duration_days,
        )
        orchestrator = _build_orchestrator(request)
        proposal = orchestrator.build_consensus_proposal(goals)
        return ConsensusStageResult(
            proposal=proposal.model_dump(mode="json"),
            goals=goals.model_dump(mode="json"),
            brand_name=brand_ctx.brand_name,
            status="PASS",
        ).model_dump()

    return _run_stage(
        job_id, "consensus", lambda: ConsensusStageResult(status="FAIL").model_dump(), _body
    )


@activity.defn(name="social_marketing_content_plan_stage")
def content_plan_stage_activity(
    job_id: str,
    request_dict: Dict[str, Any],
    consensus: Dict[str, Any],
) -> Dict[str, Any]:
    """Content-plan stage: load winners and generate/filter concept ideas.

    Preconditions:
        - ``consensus`` is a serialized ``ConsensusStageResult`` with
          ``status == "PASS"`` (the workflow short-circuits otherwise).
    Postconditions:
        - Updates job progress to 60 and returns a serialized
          ``ContentPlanStageResult`` (content plan + winners count). A malformed
          input DTO raises out of the activity; any handled stage error marks the
          job failed and returns a ``FAIL`` DTO.
    """
    from social_media_marketing_team.api.main import RunMarketingTeamRequest, _update_job
    from social_media_marketing_team.models import BrandGoals, CampaignProposal
    from social_media_marketing_team.temporal.phase_models import ContentPlanStageResult

    # Rebuild inputs OUTSIDE the funnel: a malformed inter-activity DTO is a code
    # bug (or cross-deploy schema skew), not a pipeline failure.
    request = RunMarketingTeamRequest(**request_dict)
    goals = BrandGoals.model_validate(consensus["goals"])
    proposal = CampaignProposal.model_validate(consensus["proposal"])
    brand_name = consensus.get("brand_name", "")

    _update_job(
        job_id,
        current_stage="running_collaboration_and_planning",
        progress=60,
        eta_hint="~30-60 seconds",
    )

    def _body() -> Dict[str, Any]:
        orchestrator = _build_orchestrator(request)
        performance = _build_performance(job_id, f"{brand_name} multi-platform growth sprint")
        winners = orchestrator._load_winners(request.brand_id, proposal, goals)
        content_plan = orchestrator._plan_content(proposal, goals, performance, winners=winners)
        return ContentPlanStageResult(
            content_plan=content_plan.model_dump(mode="json"),
            winners_retrieved=len(winners),
            status="PASS",
        ).model_dump()

    return _run_stage(
        job_id, "content_plan", lambda: ContentPlanStageResult(status="FAIL").model_dump(), _body
    )


@activity.defn(name="social_marketing_platform_stage")
def platform_stage_activity(
    job_id: str,
    request_dict: Dict[str, Any],
    consensus: Dict[str, Any],
    content: Dict[str, Any],
) -> Dict[str, Any]:
    """Platform stage: fan out per-platform execution plans.

    Preconditions:
        - ``consensus``/``content`` are serialized stage results with
          ``status == "PASS"`` (the workflow short-circuits otherwise).
    Postconditions:
        - Returns a serialized ``PlatformStageResult`` (one plan per specialist). A
          malformed input DTO raises out of the activity; any handled stage error
          marks the job failed and returns a ``FAIL`` DTO.
    """
    from social_media_marketing_team.api.main import RunMarketingTeamRequest
    from social_media_marketing_team.models import BrandGoals, CampaignProposal, ContentPlan
    from social_media_marketing_team.temporal.phase_models import PlatformStageResult

    request = RunMarketingTeamRequest(**request_dict)
    goals = BrandGoals.model_validate(consensus["goals"])
    proposal = CampaignProposal.model_validate(consensus["proposal"])
    content_plan = ContentPlan.model_validate(content["content_plan"])

    def _body() -> Dict[str, Any]:
        orchestrator = _build_orchestrator(request)
        plans = orchestrator.build_platform_plans(
            goals, proposal.campaign_name, len(content_plan.approved_ideas)
        )
        return PlatformStageResult(
            platform_execution_plans=[p.model_dump(mode="json") for p in plans],
            status="PASS",
        ).model_dump()

    return _run_stage(
        job_id, "platform", lambda: PlatformStageResult(status="FAIL").model_dump(), _body
    )


@activity.defn(name="social_marketing_experiment_stage")
def experiment_stage_activity(
    job_id: str,
    request_dict: Dict[str, Any],
    consensus: Dict[str, Any],
    content: Dict[str, Any],
) -> Dict[str, Any]:
    """Experiment stage: design control/variant arms for the approved ideas.

    Preconditions:
        - ``consensus``/``content`` are serialized stage results with
          ``status == "PASS"`` (the workflow short-circuits otherwise).
    Postconditions:
        - Returns a serialized ``ExperimentStageResult``. A malformed input DTO
          raises out of the activity; any handled stage error marks the job failed
          and returns a ``FAIL`` DTO.
    """
    from social_media_marketing_team.api.main import RunMarketingTeamRequest
    from social_media_marketing_team.models import CampaignProposal, ContentPlan
    from social_media_marketing_team.temporal.phase_models import ExperimentStageResult

    request = RunMarketingTeamRequest(**request_dict)
    proposal = CampaignProposal.model_validate(consensus["proposal"])
    content_plan = ContentPlan.model_validate(content["content_plan"])

    def _body() -> Dict[str, Any]:
        orchestrator = _build_orchestrator(request)
        experiment = orchestrator.build_experiment(
            proposal.campaign_name, content_plan.approved_ideas
        )
        return ExperimentStageResult(
            experiment_plan=experiment.model_dump(mode="json"),
            status="PASS",
        ).model_dump()

    return _run_stage(
        job_id, "experiment", lambda: ExperimentStageResult(status="FAIL").model_dump(), _body
    )


@activity.defn(name="social_marketing_finalize_stage")
def finalize_stage_activity(
    job_id: str,
    request_dict: Dict[str, Any],
    consensus: Dict[str, Any],
    approved: bool,
    content: Optional[Dict[str, Any]] = None,
    platform: Optional[Dict[str, Any]] = None,
    experiment: Optional[Dict[str, Any]] = None,
) -> None:
    """Finalize: assemble ``TeamOutput`` and complete the job store.

    Handles both outcomes: when ``approved`` is False the downstream artifacts are
    absent and a ``NEEDS_REVISION`` output is produced; otherwise the full
    ``APPROVED_FOR_TESTING`` output is assembled from the stage DTOs. The
    human-gate decision is made once, in the workflow, and passed in via
    ``approved`` -- this activity never re-derives it (single source of truth).

    Preconditions:
        - ``consensus`` is a serialized ``ConsensusStageResult``. ``approved`` is the
          workflow's human-gate decision. When ``approved`` is True, ``content`` is a
          present serialized ``ContentPlanStageResult`` (``platform``/``experiment``
          may be absent).
    Postconditions:
        - The job store entry is marked completed (progress 100) with the serialized
          ``TeamOutput`` result. A malformed or missing required input DTO raises a
          non-retryable ``ApplicationError`` (a contract/schema defect Temporal
          should not retry). Nothing is terminal before the completion write, so a
          transient store error re-raises for Temporal to retry until the final
          attempt, which marks the job failed and re-raises rather than completing as
          if finalization had succeeded.
    """
    from temporalio.exceptions import CancelledError

    from job_service_client import JOB_STATUS_COMPLETED
    from social_media_marketing_team.api.main import RunMarketingTeamRequest, _update_job
    from social_media_marketing_team.models import (
        CampaignProposal,
        ContentPlan,
        ExperimentPlan,
        HumanReview,
        PlatformExecutionPlan,
    )
    from social_media_marketing_team.temporal.phase_models import (
        ContentPlanStageResult,
        ExperimentStageResult,
        PlatformStageResult,
    )

    # Rebuild inputs OUTSIDE any funnel: a malformed inter-activity DTO is a code bug.
    request = RunMarketingTeamRequest(**request_dict)
    proposal = CampaignProposal.model_validate(consensus["proposal"])
    brand_name = consensus.get("brand_name", "")
    human_review = HumanReview(
        approved=approved,
        feedback=request.human_feedback,
    )

    content_plan: Optional[ContentPlan] = None
    platform_plans: list[PlatformExecutionPlan] = []
    experiment_plan: Optional[ExperimentPlan] = None
    winners_retrieved = 0
    performance = None

    if human_review.approved:
        # Defend the precondition explicitly: an approved run must carry a content
        # stage result. ContentPlan has required no-default fields, so validating an
        # empty dict would raise an opaque ValidationError after three retries;
        # surface the contract violation loudly and non-retryably instead.
        if not content:
            raise ApplicationError(
                "finalize_stage invoked with human approval but no content-plan stage result",
                non_retryable=True,
            )
        content_dto = ContentPlanStageResult.model_validate(content)
        content_plan = ContentPlan.model_validate(content_dto.content_plan)
        winners_retrieved = content_dto.winners_retrieved
        platform_dto = PlatformStageResult.model_validate(platform or {})
        platform_plans = [
            PlatformExecutionPlan.model_validate(p) for p in platform_dto.platform_execution_plans
        ]
        experiment_dto = ExperimentStageResult.model_validate(experiment or {})
        if experiment_dto.experiment_plan is not None:
            experiment_plan = ExperimentPlan.model_validate(experiment_dto.experiment_plan)
        performance = _build_performance(job_id, f"{brand_name} multi-platform growth sprint")

    orchestrator = _build_orchestrator(request)
    output = orchestrator.assemble_team_output(
        proposal,
        human_review,
        content_plan=content_plan,
        platform_plans=platform_plans,
        experiment_plan=experiment_plan,
        performance=performance,
        winners_retrieved=winners_retrieved,
    )

    # Nothing is terminal before this write: a transient store error must not
    # permanently fail an otherwise-successful run. Re-raise (letting Temporal
    # retry) until the final attempt, which marks the job failed and re-raises so the
    # workflow also reflects that finalization failed.
    try:
        _update_job(
            job_id,
            status=JOB_STATUS_COMPLETED,
            current_stage="completed",
            progress=100,
            eta_hint="done",
            result=output.model_dump(),
        )
    except CancelledError:
        logger.info("Social marketing finalize cancelled for job %s", job_id)
        _mark_cancelled(job_id)
        raise
    except Exception as e:
        if _is_cancelled():
            logger.info("Social marketing finalize cancelled for job %s", job_id)
            _mark_cancelled(job_id)
            raise CancelledError("finalize stage cancelled") from e
        if not _is_last_attempt():
            raise
        _fail_activity(job_id, e, "finalize")
        raise


# ---------------------------------------------------------------------------
# Legacy whole-pipeline activity (kept registered for drain-out)
# ---------------------------------------------------------------------------


@activity.defn(name="run_social_marketing_team_job")
def run_team_job_activity(job_id: str, request_dict: Dict[str, Any]) -> None:
    """Legacy whole-pipeline activity, kept registered for drain-out.

    Workflow histories recorded before the per-phase decomposition contain a single
    scheduled activity of this type; the workflow's unpatched replay branch
    re-schedules it, so it must stay registered until those runs drain. The activity
    re-fetches the brand from the branding API so brand context is always current,
    even for Temporal replays or retries. Brand-related failures (missing or
    incomplete brand) are marked non-retryable since retrying won't fix them.

    Preconditions:
        - ``job_id`` identifies a created job record; ``request_dict`` is a serialized
          ``RunMarketingTeamRequest``.
    Postconditions:
        - ``_run_team_job`` has run to completion (it owns all job-store updates and
          error handling); brand errors raise a non-retryable ``ApplicationError``.
    """
    try:
        from social_media_marketing_team.adapters.branding import (
            BrandIncompleteError,
            BrandNotFoundError,
            fetch_brand,
            validate_brand_for_social_marketing,
        )
        from social_media_marketing_team.api.main import RunMarketingTeamRequest, _run_team_job

        request = RunMarketingTeamRequest(**request_dict)
        brand_data = fetch_brand(request.client_id, request.brand_id)
        brand_ctx = validate_brand_for_social_marketing(
            brand_data, request.client_id, request.brand_id
        )
        _run_team_job(job_id, request, brand_ctx)
    except (BrandNotFoundError, BrandIncompleteError) as exc:
        logger.error("Brand unavailable for job %s: %s", job_id, exc)
        raise ApplicationError(str(exc), non_retryable=True) from exc
    except Exception:
        logger.exception("Social marketing team job activity failed for job %s", job_id)
        raise
