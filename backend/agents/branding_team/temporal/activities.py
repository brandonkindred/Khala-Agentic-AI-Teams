"""Temporal activities for the Branding team's per-phase durable pipeline.

The branding pipeline is decomposed into fine-grained activities the durable
``BrandingWorkflow`` drives one at a time, so a worker restart re-runs only the
unfinished unit instead of the whole ~2-hour pipeline:

- :func:`begin_branding_job_activity` — mark the job RUNNING (head of the old
  ``_run_branding_core``); returns False if already cancelled.
- :func:`run_branding_phase_activity` — run ONE pipeline phase in isolation via
  ``orchestrator.run_single_phase`` (the per-phase fan-out unit), checkpointing
  its output so a retry after a post-LLM crash skips the expensive re-run.
- :func:`run_market_research_activity` / :func:`run_design_assets_activity` — the
  two optional sibling-team integrations (wrap the ``adapters`` module).
- :func:`finalize_branding_activity` — compliance + assemble ``TeamOutput`` (via
  the orchestrator's shared ``_assemble_team_output``) + persist brand version +
  mark COMPLETED.
- :func:`mark_branding_failed_activity` — record a FAILED job row (except-branch
  of the old ``_run_branding_core``).
- :func:`check_branding_cancelled_activity` — cooperative between-phase cancel.

Each activity is a plain **sync** function (run in the worker's thread-pool
executor) whose heavy imports live inside the body, keeping module import — which
the workflow sandbox replays during registration — cheap and side-effect free.
All payloads cross the workflow/activity boundary as JSON-native dicts and are
reconstructed with pydantic inside the body.

Invariant: checkpoints are written under the ``"branding_team"`` job-service slug
— the same slug ``branding_team.shared.job_store`` created the job row under — so
``save_checkpoint``/``load_checkpoint`` address the run's actual row.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from temporalio import activity

logger = logging.getLogger(__name__)

# Job-service team slug the branding job row lives under (JobServiceClient(team=...)
# in shared/job_store.py). Checkpoints MUST use this, not the "branding" worker
# slug, or they would land on a different (non-existent) job row.
_CHECKPOINT_TEAM = "branding_team"

# Checkpoint key that gates the non-idempotent brand-version append in finalize.
_FINALIZED_CHECKPOINT = "finalized"

# Background-heartbeat cadence (seconds) for a phase's (potentially multi-minute)
# graph run, kept under the activity's heartbeat_timeout so a live phase is not
# mistaken for a stalled worker.
_PHASE_HEARTBEAT_INTERVAL_S = 30.0


@activity.defn(name="branding_begin_job")
def begin_branding_job_activity(job_id: str) -> bool:
    """Transition the job to RUNNING; report whether the run should proceed.

    Preconditions:
        - ``job_id`` refers to a job row already created by ``_submit_brand_run``.
    Postconditions:
        - Returns False without side effects when the job is already cancelled
          (terminal — the workflow returns without failing).
        - Otherwise sets the row to RUNNING and returns True.
    """
    from branding_team.api.main import JOB_STATUS_RUNNING, is_job_cancelled, update_job

    if is_job_cancelled(job_id):
        return False
    update_job(job_id, status=JOB_STATUS_RUNNING)
    return True


@activity.defn(name="branding_run_phase")
def run_branding_phase_activity(
    payload: dict[str, Any], phase: str, prior_outputs: dict[str, Any]
) -> dict[str, Any]:
    """Run one branding pipeline phase in isolation and return its output dict.

    Preconditions:
        - ``payload`` carries a ``job_id`` and a ``mission`` dict validating
          against ``BrandingMission``.
        - ``phase`` is a ``BrandPhase`` value string for a runnable phase.
        - ``prior_outputs`` maps upstream phase value strings to their JSON-safe
          output dicts (accumulated by the workflow from earlier phase returns).
    Postconditions:
        - Returns the phase output as a JSON-safe dict (``model_dump(mode="json")``).
        - Idempotency: if a checkpoint for this phase already exists (a prior
          attempt finished the LLM work then crashed before returning), the stored
          output is returned without re-running the phase. Otherwise the freshly
          computed output is checkpointed before return.
    """
    from branding_team.api.main import orchestrator
    from branding_team.models import BrandingMission, BrandPhase
    from shared_concurrency import BackgroundHeartbeat
    from shared_temporal import load_checkpoint, save_checkpoint

    job_id = payload["job_id"]
    existing = load_checkpoint(_CHECKPOINT_TEAM, job_id, phase)
    if existing and existing.get("payload") is not None:
        return existing["payload"]

    mission = BrandingMission(**payload["mission"])
    with BackgroundHeartbeat(activity.heartbeat, _PHASE_HEARTBEAT_INTERVAL_S, copy_context=True):
        model = orchestrator.run_single_phase(mission, BrandPhase(phase), prior_outputs or {})
    out = model.model_dump(mode="json")
    save_checkpoint(_CHECKPOINT_TEAM, job_id, phase, out)
    return out


@activity.defn(name="branding_market_research")
def run_market_research_activity(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Run the optional market-research integration; best-effort.

    Preconditions:
        - ``payload`` carries a ``mission`` dict validating against
          ``BrandingMission``. The workflow only dispatches this activity when
          ``include_market_research`` is set.
    Postconditions:
        - Returns the ``CompetitiveSnapshot`` as a dict, or ``None`` when the
          service is unconfigured or the call fails (research is best-effort
          context — a failure must not fail the branding run), matching the
          thread-mode ``_gather_integrations`` behavior.
    """
    from branding_team.adapters.market_research import request_market_research
    from branding_team.models import BrandingMission

    mission = BrandingMission(**payload["mission"])
    try:
        snapshot = request_market_research(mission)
    except Exception:
        logger.warning(
            "branding market research failed for job %s", payload.get("job_id"), exc_info=True
        )
        return None
    return snapshot.model_dump(mode="json") if snapshot is not None else None


@activity.defn(name="branding_design_assets")
def run_design_assets_activity(
    payload: dict[str, Any], strategic_core: Optional[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """Run the optional design-assets integration.

    Preconditions:
        - ``payload`` carries a ``mission`` dict; ``strategic_core`` is the Phase 1
          output dict (or ``None`` if Phase 1 was not run). The workflow only
          dispatches this activity when ``include_design_assets`` is set.
    Postconditions:
        - Returns the ``DesignAssetRequestResult`` as a dict. Errors propagate
          (matching thread-mode ``_gather_integrations`` where design-asset errors
          are not swallowed); the workflow's retry policy then governs re-attempts.
    """
    from branding_team.adapters.design_assets import request_design_assets
    from branding_team.models import BrandingMission, StrategicCoreOutput

    mission = BrandingMission(**payload["mission"])
    core = StrategicCoreOutput(**strategic_core) if strategic_core else None
    result = request_design_assets(core, mission.company_name)
    return result.model_dump(mode="json") if result is not None else None


@activity.defn(name="branding_finalize")
def finalize_branding_activity(
    payload: dict[str, Any],
    phase_outputs: dict[str, Any],
    competitive_snapshot: Optional[dict[str, Any]],
    design_asset_result: Optional[dict[str, Any]],
) -> None:
    """Assemble the final output, persist the brand version, and complete the job.

    Preconditions:
        - ``payload`` carries ``job_id``/``mission``/``human_review`` (+ optional
          ``brand_checks``/``client_id``/``brand_id``/``target_phase``).
        - ``phase_outputs`` maps each completed phase's value string to its output
          dict; ``competitive_snapshot``/``design_asset_result`` are the
          integration result dicts (or ``None``).
    Postconditions:
        - Reconstructs the phase models, runs compliance, and builds ``TeamOutput``
          via the orchestrator's shared ``_assemble_team_output`` (same assembly as
          thread mode).
        - The brand-version append is gated by the ``"finalized"`` checkpoint so a
          finalize retry never duplicates it; the COMPLETED write is idempotent and
          always applied unless the job was cancelled (cancel is terminal, not
          completed).
    """
    from branding_team.api.main import (
        JOB_STATUS_COMPLETED,
        branding_store,
        is_job_cancelled,
        orchestrator,
        update_job,
    )
    from branding_team.models import (
        BrandCheckRequest,
        BrandingMission,
        ChannelActivationOutput,
        CompetitiveSnapshot,
        DesignAssetRequestResult,
        GovernanceOutput,
        HumanReview,
        NarrativeMessagingOutput,
        StrategicCoreOutput,
        VisualIdentityOutput,
    )
    from branding_team.temporal.constants import PHASE_SEQUENCE
    from shared_temporal import load_checkpoint, save_checkpoint

    job_id = payload["job_id"]
    mission = BrandingMission(**payload["mission"])
    human_review = HumanReview(**payload["human_review"])
    brand_checks = [BrandCheckRequest(**c) for c in payload.get("brand_checks") or []]
    client_id = payload.get("client_id")
    brand_id = payload.get("brand_id")

    target_phase = payload.get("target_phase")
    stop_idx = PHASE_SEQUENCE.index(target_phase) if target_phase else len(PHASE_SEQUENCE) - 1

    def _model(cls: type, key: str) -> Any:
        data = phase_outputs.get(key)
        return cls(**data) if data else None

    strategic_core = _model(StrategicCoreOutput, "strategic_core")
    narrative = _model(NarrativeMessagingOutput, "narrative_messaging")
    visual_identity = _model(VisualIdentityOutput, "visual_identity")
    channel_activation = _model(ChannelActivationOutput, "channel_activation")
    governance = _model(GovernanceOutput, "governance")

    checks = orchestrator.compliance.evaluate(brand_checks, mission)
    snapshot = CompetitiveSnapshot(**competitive_snapshot) if competitive_snapshot else None
    design_result = DesignAssetRequestResult(**design_asset_result) if design_asset_result else None

    output = orchestrator._assemble_team_output(
        mission=mission,
        human_review=human_review,
        strategic_core=strategic_core,
        narrative=narrative,
        visual_identity=visual_identity,
        channel_activation=channel_activation,
        governance=governance,
        checks=checks,
        competitive_snapshot=snapshot,
        design_asset_result=design_result,
        stop_idx=stop_idx,
    )

    # Gate the non-idempotent persistence so a finalize retry cannot append a
    # duplicate brand version. The COMPLETED write below is idempotent, so it is
    # left outside the gate and always applied (unless the job was cancelled).
    if not load_checkpoint(_CHECKPOINT_TEAM, job_id, _FINALIZED_CHECKPOINT):
        if client_id and brand_id:
            branding_store.append_brand_version(client_id, brand_id, output)
        save_checkpoint(_CHECKPOINT_TEAM, job_id, _FINALIZED_CHECKPOINT, True)

    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_COMPLETED, result=output.model_dump())


@activity.defn(name="branding_mark_failed")
def mark_branding_failed_activity(job_id: str, error: str) -> None:
    """Record a FAILED job row for a pipeline failure.

    Preconditions:
        - ``job_id`` refers to an existing job row; ``error`` is a short message.
    Postconditions:
        - Sets the row to FAILED with ``error`` unless the job was cancelled (a
          cancelled run is terminal, not a failure), matching the except-branch of
          the old ``_run_branding_core``.
    """
    from branding_team.api.main import JOB_STATUS_FAILED, is_job_cancelled, update_job

    if is_job_cancelled(job_id):
        return
    update_job(job_id, status=JOB_STATUS_FAILED, error=error)


@activity.defn(name="branding_check_cancelled")
def check_branding_cancelled_activity(job_id: str) -> bool:
    """Report whether the job has been cancelled (cooperative between-phase check).

    Preconditions:
        - ``job_id`` refers to an existing job row.
    Postconditions:
        - Returns True iff the job row is in the cancelled state; no side effects.
    """
    from branding_team.api.main import is_job_cancelled

    return bool(is_job_cancelled(job_id))
