"""Temporal activities for the AI systems team.

The build pipeline is decomposed into one ``@activity.defn`` per orchestrator
phase — spec intake, architecture, capabilities, evaluation, safety, build —
framed by ``begin``/``finalize`` book-ends and orchestrated by
``AISystemsBuildWorkflow``. Every phase runs, retries, and appears in the
Temporal UI as its own span instead of hiding inside one monolithic activity.

Phase results cross the activity boundary as JSON-native dicts: each phase model
in ``models`` is emitted with ``model_dump(mode="json")`` and rebuilt downstream
with ``model_validate``. Rebuilding an input DTO happens *before* the phase runs
so a malformed inter-activity payload (a code/schema defect) raises loudly rather
than masquerading as a phase failure. On success a phase checkpoints its result
into the job-store blueprint snapshot (``record_phase_result``) so a resumed
workflow can skip it. Heavy imports live inside function bodies so importing this
module stays cheap and sandbox-safe.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from temporalio import activity

from shared.temporal.activity_utils import is_last_attempt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# book-end activities
# ---------------------------------------------------------------------------


@activity.defn(name="ai_systems_begin_run")
def begin_run_activity(job_id: str) -> Dict[str, Any]:
    """Mark the job running and return its resume state (stored blueprint).

    Preconditions:
        - ``job_id`` identifies a created job record.
    Postconditions:
        - The job is marked RUNNING. Returns the stored blueprint dict (carrying
          ``completed_phases`` and each finished phase's result) so the workflow
          can skip already-completed phases on a resume, or ``{}`` for a fresh run
          (or a missing / blueprint-less job).
    """
    from ..shared.job_store import get_job, mark_job_running

    mark_job_running(job_id)
    data = get_job(job_id)
    stored = data.get("blueprint") if data else None
    return stored if isinstance(stored, dict) else {}


@activity.defn(name="ai_systems_finalize")
def finalize_build_activity(job_id: str, error: Optional[str]) -> None:
    """Close out the build: mark the job completed or failed.

    Preconditions:
        - ``job_id`` identifies a created job record.
        - ``error`` is the failure message when any phase aborted, else ``None``.
    Postconditions:
        - When ``error`` is set the job is marked FAILED with that message (the
          partial blueprint checkpointed by the phases is left intact for resume).
        - Otherwise the job's stored blueprint is loaded, flagged ``success=True``
          and marked COMPLETED — mirroring the thread-mode orchestrator's terminal
          transition.
        - The completion write is retried by Temporal on a transient store error;
          on the final attempt the job is instead marked FAILED so it never strands
          in RUNNING, and the error is re-raised so the workflow reflects it too —
          unless a re-read shows the write actually landed (lost ack), in which case
          the job is left COMPLETED and finalize returns as success.
    """
    from ..models import AgentBlueprint
    from ..shared.job_store import (
        JOB_STATUS_COMPLETED,
        get_job,
        mark_job_completed,
        mark_job_failed,
    )

    if error:
        mark_job_failed(job_id, error=error)
        return

    data = get_job(job_id)
    stored = data.get("blueprint") if data else None
    if isinstance(stored, dict):
        blueprint = AgentBlueprint(**stored)
    else:
        blueprint = AgentBlueprint(project_name=(data.get("project_name") if data else "") or "")
    blueprint.success = True
    blueprint.error = None

    try:
        mark_job_completed(job_id, blueprint=blueprint.model_dump(mode="json"))
    except Exception as exc:
        # Nothing is terminal yet; let Temporal retry the completion write while
        # attempts remain.
        if not is_last_attempt():
            raise
        logger.exception("AI Systems finalize failed for job %s", job_id)
        # The completion write may have landed server-side even though the client
        # raised (a lost ack). Re-read: if the job is already COMPLETED the finalize
        # actually succeeded, so leave it COMPLETED and return rather than flipping a
        # successful build to FAILED.
        try:
            current = get_job(job_id)
        except Exception:
            current = None
        if current and current.get("status") == JOB_STATUS_COMPLETED:
            return
        # Final attempt with no completed write: downgrade to a terminal FAILED state
        # so the job does not hang in RUNNING, then re-raise so the workflow fails too.
        try:
            mark_job_failed(job_id, error=f"Finalize failed: {exc}")
        except Exception:
            logger.exception(
                "AI Systems finalize fallback mark-failed also failed for job %s", job_id
            )
        raise


# ---------------------------------------------------------------------------
# per-phase activities
# ---------------------------------------------------------------------------


@activity.defn(name="ai_systems_spec_intake")
def spec_intake_activity(
    job_id: str,
    spec_path: str,
    constraints: Dict[str, Any],
) -> Dict[str, Any]:
    """Spec-intake phase: parse the spec into goals, constraints, and policy.

    Preconditions:
        - ``job_id`` identifies a created job record; ``spec_path`` and
          ``constraints`` are the build request's spec location and extra
          constraints.
    Postconditions:
        - Runs ``run_spec_intake`` and returns a serialized ``SpecIntakeResult``.
          On success the result is checkpointed into the job's blueprint snapshot.
    """
    from ..phases import run_spec_intake
    from ..shared.job_store import make_job_updater, record_phase_result

    result = run_spec_intake(
        spec_path=spec_path,
        constraints=constraints,
        job_updater=make_job_updater(job_id),
    )
    dumped = result.model_dump(mode="json")
    if result.success:
        record_phase_result(job_id, "spec_intake", dumped)
    return dumped


@activity.defn(name="ai_systems_architecture")
def architecture_activity(job_id: str, spec_intake: Dict[str, Any]) -> Dict[str, Any]:
    """Architecture phase: choose topology and design the orchestration graph.

    Preconditions:
        - ``job_id`` identifies a created job record; ``spec_intake`` is a serialized
          ``SpecIntakeResult`` from the upstream phase.
    Postconditions:
        - Runs ``run_architecture`` and returns a serialized ``ArchitectureResult``;
          checkpoints it on success. A malformed ``spec_intake`` DTO raises.
    """
    from ..models import SpecIntakeResult
    from ..phases import run_architecture
    from ..shared.job_store import make_job_updater, record_phase_result

    spec = SpecIntakeResult.model_validate(spec_intake)
    result = run_architecture(spec_intake=spec, job_updater=make_job_updater(job_id))
    dumped = result.model_dump(mode="json")
    if result.success:
        record_phase_result(job_id, "architecture", dumped)
    return dumped


@activity.defn(name="ai_systems_capabilities")
def capabilities_activity(
    job_id: str,
    spec_intake: Dict[str, Any],
    architecture: Dict[str, Any],
) -> Dict[str, Any]:
    """Capabilities phase: map requirements to tools, memory, and models.

    Preconditions:
        - ``job_id`` identifies a created job record; ``spec_intake`` /
          ``architecture`` are serialized ``SpecIntakeResult`` / ``ArchitectureResult``
          from the upstream phases.
    Postconditions:
        - Runs ``run_capabilities`` and returns a serialized ``CapabilitiesResult``;
          checkpoints it on success. A malformed input DTO raises.
    """
    from ..models import ArchitectureResult, SpecIntakeResult
    from ..phases import run_capabilities
    from ..shared.job_store import make_job_updater, record_phase_result

    spec = SpecIntakeResult.model_validate(spec_intake)
    arch = ArchitectureResult.model_validate(architecture)
    result = run_capabilities(
        spec_intake=spec,
        architecture=arch,
        job_updater=make_job_updater(job_id),
    )
    dumped = result.model_dump(mode="json")
    if result.success:
        record_phase_result(job_id, "capabilities", dumped)
    return dumped


@activity.defn(name="ai_systems_evaluation")
def evaluation_activity(job_id: str, spec_intake: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluation phase: build the acceptance/adversarial test harness and KPIs.

    Preconditions:
        - ``job_id`` identifies a created job record; ``spec_intake`` is a serialized
          ``SpecIntakeResult`` from the upstream phase.
    Postconditions:
        - Runs ``run_evaluation`` and returns a serialized ``EvaluationResult``;
          checkpoints it on success. A malformed ``spec_intake`` DTO raises.
    """
    from ..models import SpecIntakeResult
    from ..phases import run_evaluation
    from ..shared.job_store import make_job_updater, record_phase_result

    spec = SpecIntakeResult.model_validate(spec_intake)
    result = run_evaluation(spec_intake=spec, job_updater=make_job_updater(job_id))
    dumped = result.model_dump(mode="json")
    if result.success:
        record_phase_result(job_id, "evaluation", dumped)
    return dumped


@activity.defn(name="ai_systems_safety")
def safety_activity(
    job_id: str,
    spec_intake: Dict[str, Any],
    architecture: Dict[str, Any],
) -> Dict[str, Any]:
    """Safety phase: define checkpoints, guardrails, and policy requirements.

    Preconditions:
        - ``job_id`` identifies a created job record; ``spec_intake`` /
          ``architecture`` are serialized ``SpecIntakeResult`` / ``ArchitectureResult``
          from the upstream phases.
    Postconditions:
        - Runs ``run_safety`` and returns a serialized ``SafetyResult``;
          checkpoints it on success. A malformed input DTO raises.
    """
    from ..models import ArchitectureResult, SpecIntakeResult
    from ..phases import run_safety
    from ..shared.job_store import make_job_updater, record_phase_result

    spec = SpecIntakeResult.model_validate(spec_intake)
    arch = ArchitectureResult.model_validate(architecture)
    result = run_safety(
        spec_intake=spec,
        architecture=arch,
        job_updater=make_job_updater(job_id),
    )
    dumped = result.model_dump(mode="json")
    if result.success:
        record_phase_result(job_id, "safety", dumped)
    return dumped


@activity.defn(name="ai_systems_build_phase")
def build_phase_activity(
    job_id: str,
    project_name: str,
    spec_intake: Dict[str, Any],
    architecture: Dict[str, Any],
    capabilities: Dict[str, Any],
    evaluation: Dict[str, Any],
    safety: Dict[str, Any],
    output_dir: Optional[str],
) -> Dict[str, Any]:
    """Build phase: package every phase output into the final artifact bundle.

    Preconditions:
        - ``job_id`` identifies a created job record; ``project_name`` /
          ``output_dir`` are the build request fields, and ``spec_intake``,
          ``architecture``, ``capabilities``, ``evaluation``, and ``safety`` are the
          serialized results of the five upstream phases.
    Postconditions:
        - Runs ``run_build`` and returns a serialized ``BuildResult``;
          checkpoints it on success. A malformed input DTO raises.
    """
    from ..models import (
        ArchitectureResult,
        CapabilitiesResult,
        EvaluationResult,
        SafetyResult,
        SpecIntakeResult,
    )
    from ..phases import run_build
    from ..shared.job_store import make_job_updater, record_phase_result

    spec = SpecIntakeResult.model_validate(spec_intake)
    arch = ArchitectureResult.model_validate(architecture)
    caps = CapabilitiesResult.model_validate(capabilities)
    eval_result = EvaluationResult.model_validate(evaluation)
    safety_result = SafetyResult.model_validate(safety)

    result = run_build(
        project_name=project_name,
        spec_intake=spec,
        architecture=arch,
        capabilities=caps,
        evaluation=eval_result,
        safety=safety_result,
        output_dir=output_dir,
        job_updater=make_job_updater(job_id),
    )
    dumped = result.model_dump(mode="json")
    if result.success:
        record_phase_result(job_id, "build", dumped)
    return dumped


# ---------------------------------------------------------------------------
# legacy whole-pipeline activity (drain-out only)
# ---------------------------------------------------------------------------


@activity.defn(name="run_ai_systems_build")
def run_build_activity(
    job_id: str,
    project_name: str,
    spec_path: str,
    constraints: Dict[str, Any],
    output_dir: Optional[str],
) -> None:
    """Legacy whole-pipeline activity, kept registered for drain-out.

    Workflow histories recorded before the per-phase decomposition contain a
    single scheduled activity of this type; the workflow's unpatched replay branch
    re-schedules it, so it must stay registered until those runs drain.

    Postconditions:
        - ``_run_build_background`` has run to completion (it owns all job-store
          updates and error handling); re-raises whatever it raises.
    """
    try:
        from ..api.main import _run_build_background

        _run_build_background(job_id, project_name, spec_path, constraints, output_dir)
    except Exception:
        logger.exception("AI Systems build activity failed for job %s", job_id)
        raise
