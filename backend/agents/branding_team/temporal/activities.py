"""Temporal activities for the Branding team's per-phase durable pipeline.

The branding pipeline is decomposed into fine-grained activities the durable
``BrandingWorkflow`` drives one at a time, so a worker restart re-runs only the
unfinished unit instead of the whole ~2-hour pipeline:

- :func:`begin_branding_job_activity` — mark the job RUNNING via the shared
  ``branding_team.shared.job_store.begin_job`` guard; returns False if already
  cancelled.
- :func:`run_branding_phase_activity` — run ONE pipeline phase in isolation via
  ``orchestrator.run_single_phase`` (the per-phase fan-out unit), checkpointing
  its output so a retry after a post-LLM crash skips the expensive re-run.
- :func:`run_market_research_activity` / :func:`run_design_assets_activity` — the
  two optional sibling-team integrations (wrap the ``adapters`` module).
- :func:`finalize_branding_activity` — compliance + assemble ``TeamOutput`` (via
  the orchestrator's shared ``_assemble_team_output``) + persist brand version +
  mark COMPLETED via the shared ``job_store.mark_completed`` guard.
- :func:`mark_branding_failed_activity` — record a FAILED job row via the shared
  ``job_store.mark_failed`` guard.
- :func:`check_branding_cancelled_activity` — cooperative between-phase cancel.

``begin_job``/``mark_completed``/``mark_failed`` (in
``branding_team.shared.job_store``) are the same guarded cancel-check +
status-write helpers the thread path uses in ``api.main._run_branding_core``, so
the RUNNING/COMPLETED/FAILED bookkeeping lives in exactly one place across both
execution modes.

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


def _degraded_checkpoint_key(phase: str) -> str:
    """Checkpoint key for ``phase``'s degradation flag, distinct from its output payload.

    Preconditions:
        ``phase`` is a ``BrandPhase`` value string.
    Postconditions:
        Returns a key that never collides with a phase-name checkpoint key,
        used by both the writer (``run_branding_phase_activity``) and the
        reader (``finalize_branding_activity``).
    """
    return f"{phase}__degraded"


@activity.defn(name="branding_begin_job")
def begin_branding_job_activity(job_id: str) -> bool:
    """Transition the job to RUNNING; report whether the run should proceed.

    Preconditions:
        - ``job_id`` refers to a job row already created by ``_submit_brand_run``.
    Postconditions:
        - Returns False without side effects when the job is already cancelled
          (terminal — the workflow returns without failing).
        - Otherwise sets the row to RUNNING and returns True.
        - Raises ``branding_team.shared.job_store.JobNotFoundError`` if
          ``job_id`` does not exist — dispatched under a retry policy that
          treats that specific error as non-retryable (a missing row will not
          resolve itself on retry).
    """
    from branding_team.shared.job_store import begin_job

    return begin_job(job_id)


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
        - Idempotency: if a checkpoint for this phase already exists with a
          non-None payload (a prior attempt finished the LLM work then crashed
          before returning), the stored output is returned without re-running
          the phase. A checkpoint with a ``None`` payload does not short-circuit.
          Otherwise the freshly computed output is checkpointed before return.
        - The phase's degradation flag (whether ``orchestrator.run_single_phase``
          fell back to a default-constructed model because the LLM text couldn't
          be parsed) is checkpointed under ``_degraded_checkpoint_key(phase)``
          — written *before* the output checkpoint so a crash between the two
          writes can never leave an output checkpoint (which short-circuits a
          retry) without its paired degradation flag. ``finalize_branding_activity``
          reads it back to populate ``TeamOutput.degraded_phases``.
    """
    from branding_team.models import BrandingMission, BrandPhase
    from branding_team.orchestrator import orchestrator

    # shared.concurrency is stdlib-only (threading/contextvars/logging) with no
    # import side effects, and this runs in the worker thread pool (outside the
    # workflow sandbox), so the call-time import is safe.
    from shared.concurrency import BackgroundHeartbeat
    from shared.temporal import load_checkpoint, save_checkpoint

    job_id = payload["job_id"]
    existing = load_checkpoint(_CHECKPOINT_TEAM, job_id, phase)
    if existing and existing.get("payload") is not None:
        return existing["payload"]

    mission = BrandingMission(**payload["mission"])
    with BackgroundHeartbeat(activity.heartbeat, _PHASE_HEARTBEAT_INTERVAL_S, copy_context=True):
        model, degraded = orchestrator.run_single_phase(
            mission, BrandPhase(phase), prior_outputs or {}
        )
    out = model.model_dump(mode="json")
    save_checkpoint(_CHECKPOINT_TEAM, job_id, _degraded_checkpoint_key(phase), degraded)
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
    # request_market_research is the sync wrapper over request_market_research_async
    # (it runs the same coroutine via the shared coro_runner.run_coroutine helper), so it
    # shares the async path's timeout/error handling exactly — used here because
    # activities are sync.
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
          thread mode). ``TeamOutput.degraded_phases`` is populated from the
          per-phase degradation checkpoints ``run_branding_phase_activity`` wrote,
          so a Temporal run surfaces the same degradation signal as the thread path.
        - The brand-version append is gated by the ``"finalized"`` checkpoint so a
          finalize retry that runs after the checkpoint is durably written does not
          re-append. The append and the checkpoint are two separate job-service
          calls, so a crash in the narrow window between them can still re-append
          on retry (``append_brand_version`` is not idempotent); the gate is a
          best-effort dedup, not an exactly-once guarantee.
        - The COMPLETED write is idempotent and always applied unless the job was
          cancelled (cancel is terminal, not completed).
        - If the underlying ``append_brand_version`` write returns ``None`` (the
          brand row vanished between resolve and append), this activity raises to
          force the workflow into its failure path and record a FAILED job row.
        - Raises ``branding_team.shared.job_store.JobNotFoundError`` if
          ``job_id`` does not exist — dispatched under a retry policy that
          treats that specific error as non-retryable.
    """
    from branding_team.models import (
        BrandCheckRequest,
        BrandingMission,
        BrandPhase,
        ChannelActivationOutput,
        CompetitiveSnapshot,
        DesignAssetRequestResult,
        GovernanceOutput,
        HumanReview,
        NarrativeMessagingOutput,
        StrategicCoreOutput,
        VisualIdentityOutput,
    )
    from branding_team.orchestrator import orchestrator
    from branding_team.shared.job_store import mark_completed
    from branding_team.store import get_default_store
    from shared.temporal import load_checkpoint, save_checkpoint

    branding_store = get_default_store()
    job_id = payload["job_id"]
    mission = BrandingMission(**payload["mission"])
    human_review = HumanReview(**payload["human_review"])
    brand_checks = [BrandCheckRequest(**c) for c in payload.get("brand_checks") or []]
    client_id = payload.get("client_id")
    brand_id = payload.get("brand_id")

    # The keys below are BrandPhase value strings — the same keys the workflow
    # accumulates in prior_outputs (from PHASE_SEQUENCE) and passes here as
    # phase_outputs. A phase not reached (partial target_phase run) is absent, so
    # _model returns None and _assemble_team_output tolerates it.
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

    degraded_phases = [
        BrandPhase(key)
        for key in phase_outputs
        if (load_checkpoint(_CHECKPOINT_TEAM, job_id, _degraded_checkpoint_key(key)) or {}).get(
            "payload"
        )
    ]

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
        degraded_phases=degraded_phases,
    )

    # Best-effort dedup of the non-idempotent brand-version append: the checkpoint
    # is written right after the append so a later retry skips it. Append-then-
    # checkpoint (rather than the reverse) is deliberate — a crash in between can
    # duplicate a version, but never drops one. The COMPLETED write below is
    # idempotent, so it is left outside the gate and always applied (unless the
    # job was cancelled).
    if not load_checkpoint(_CHECKPOINT_TEAM, job_id, _FINALIZED_CHECKPOINT):
        if client_id and brand_id:
            appended = branding_store.append_brand_version(client_id, brand_id, output)
            if appended is None:
                # Brand could have been deleted between checkpoint read and write.
                from branding_team.store import BrandVersionAppendConflict

                raise BrandVersionAppendConflict(
                    "Brand row disappeared while appending brand version "
                    f"(client_id={client_id}, brand_id={brand_id})"
                )
        save_checkpoint(_CHECKPOINT_TEAM, job_id, _FINALIZED_CHECKPOINT, True)

    mark_completed(job_id, output.model_dump())


@activity.defn(name="branding_mark_failed")
def mark_branding_failed_activity(job_id: str, error: str) -> bool:
    """Record a FAILED job row for a pipeline failure.

    Preconditions:
        - ``job_id`` refers to an existing job row; ``error`` is a short message.
    Postconditions:
        - Sets the row to FAILED with ``error`` unless the job was cancelled (a
          cancelled run is terminal, not a failure), via the shared
          ``job_store.mark_failed`` guard.
        - Returns ``mark_failed``'s bool: True if the FAILED write happened,
          False if a cancel raced in between the workflow's own cancellation
          check and this write. The workflow caller uses False to reclassify
          its outcome as cancelled rather than raising into what would look
          like a failed run.
        - Raises ``branding_team.shared.job_store.JobNotFoundError`` (a
          ``ValueError`` subclass) if ``job_id`` does not exist — the workflow
          dispatches this activity under a retry policy that treats that
          specific error as non-retryable, since a missing row will not
          resolve itself on retry.
    """
    from branding_team.shared.job_store import mark_failed

    return mark_failed(job_id, error)


@activity.defn(name="branding_check_cancelled")
def check_branding_cancelled_activity(job_id: str) -> bool:
    """Report whether the job has been cancelled (cooperative between-phase check).

    Preconditions:
        - ``job_id`` refers to an existing job row.
    Postconditions:
        - Returns True iff the job row is in the cancelled state; no side effects.
    """
    from branding_team.shared.job_store import is_job_cancelled

    return bool(is_job_cancelled(job_id))
