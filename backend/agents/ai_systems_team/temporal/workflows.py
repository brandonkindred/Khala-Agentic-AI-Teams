"""Temporal workflows for the AI systems team.

``AISystemsBuildWorkflow`` orchestrates the six-phase build as a sequence of
fine-grained activities — spec intake -> architecture -> capabilities ->
evaluation -> safety -> build — framed by ``begin`` and ``finalize`` book-ends.
Each phase runs and retries independently under ``DEFAULT_RETRY_POLICY`` and shows
up as a distinct span in the Temporal UI, replacing the former single monolithic
activity.

State crosses each activity boundary as a JSON-native dict (the ``models`` phase
results). The ``begin`` activity returns the job's stored blueprint so a resumed
run can skip phases already checkpointed (mirroring the thread-mode orchestrator's
skip-resume). Any phase whose result reports ``success == False`` short-circuits
the pipeline straight to ``finalize`` with the phase's error.
"""

from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from ai_systems_team.temporal import activities as _activities
    from ai_systems_team.temporal.constants import TASK_QUEUE

# Legacy whole-pipeline ceiling. The unpatched drain-out branch below MUST schedule
# ``run_build_activity`` with byte-identical options to the pre-decomposition
# workflow, so replays of in-flight histories stay deterministic.
BUILD_TIMEOUT = timedelta(hours=12)
# Per-phase ceiling — the phases are fast, deterministic transforms (spec parsing,
# graph design, artifact writes), so an hour is a generous safety net, not a budget.
PHASE_TIMEOUT = timedelta(hours=1)
# Book-end activities (begin/finalize) are tiny job-store writes.
BOOKEND_TIMEOUT = timedelta(minutes=10)

DEFAULT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=30),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)

# One option block per activity class so tuning a timeout/retry is a single edit.
# Immutable (MappingProxyType) so an importer can't mutate the shared options;
# ``**_PHASE_ACTIVITY_OPTS`` unpacking works on any mapping.
_PHASE_ACTIVITY_OPTS: Mapping[str, Any] = MappingProxyType(
    dict(
        task_queue=TASK_QUEUE,
        schedule_to_close_timeout=PHASE_TIMEOUT,
        retry_policy=DEFAULT_RETRY_POLICY,
    )
)
_BOOKEND_ACTIVITY_OPTS: Mapping[str, Any] = MappingProxyType(
    dict(
        task_queue=TASK_QUEUE,
        schedule_to_close_timeout=BOOKEND_TIMEOUT,
        retry_policy=DEFAULT_RETRY_POLICY,
    )
)
# Identical to the pre-decomposition monolith's options (12h schedule-to-close,
# same retry policy) so replays of pre-decomposition histories keep the exact same
# command. Do not retune without a fresh drain-out.
_LEGACY_ACTIVITY_OPTS: Mapping[str, Any] = MappingProxyType(
    dict(
        task_queue=TASK_QUEUE,
        schedule_to_close_timeout=BUILD_TIMEOUT,
        retry_policy=DEFAULT_RETRY_POLICY,
    )
)


def _resumed(
    resume: Dict[str, Any],
    phase: str,
    completed: set,
) -> Optional[Dict[str, Any]]:
    """Return a phase's checkpointed result when it can be skipped, else ``None``.

    Pure/deterministic (dict lookups only) so it is safe to call inside the
    workflow.

    Preconditions:
        - ``resume`` is the ``begin`` activity's stored-blueprint dict; ``completed``
          is ``resume["completed_phases"]`` as a set.
    Postconditions:
        - Returns ``resume[phase]`` when ``phase`` is in ``completed`` and its stored
          result is a dict (skip-resume path); otherwise ``None`` (run the phase).
    """
    if phase in completed:
        result = resume.get(phase)
        if isinstance(result, dict):
            return result
    return None


@workflow.defn(name="AISystemsBuildWorkflow")
class AISystemsBuildWorkflow:
    """Runs one AI-system build job as a sequence of per-phase activities."""

    @workflow.run
    async def run(
        self,
        job_id: str,
        project_name: str,
        spec_path: str,
        constraints: Dict[str, Any],
        output_dir: Optional[str],
    ) -> None:
        """Execute the six phase activities in order, threading each phase result.

        Preconditions:
            - ``job_id`` identifies a created job record; the remaining args are the
              build request fields.
        Postconditions:
            - On success every not-yet-completed phase runs once and ``finalize``
              marks the job completed. A phase whose result reports
              ``success == False`` short-circuits to ``finalize`` with its error
              (job marked failed). Phases already checkpointed (resume) are skipped
              and their stored results threaded forward. Histories recorded before
              the per-phase decomposition replay the original single-activity path
              (via ``workflow.patched``) so in-flight runs survive the deploy.
        """
        if not workflow.patched("ai-systems-per-phase-activities"):
            # Drain-out branch: replays of pre-decomposition histories must
            # re-schedule the original monolithic activity deterministically.
            # Removal criterion: once every workflow open at the decomposition
            # deploy has drained, replace this block with
            # ``workflow.deprecate_patch("ai-systems-per-phase-activities")`` for one
            # release, then delete the marker and ``run_build_activity`` entirely.
            await workflow.execute_activity(
                _activities.run_build_activity,
                args=[job_id, project_name, spec_path, constraints, output_dir],
                **_LEGACY_ACTIVITY_OPTS,
            )
            return

        resume = await workflow.execute_activity(
            _activities.begin_run_activity,
            args=[job_id],
            **_BOOKEND_ACTIVITY_OPTS,
        )
        completed = set(resume.get("completed_phases") or [])

        # -- SPEC INTAKE --
        spec_intake = _resumed(resume, "spec_intake", completed)
        if spec_intake is None:
            spec_intake = await workflow.execute_activity(
                _activities.spec_intake_activity,
                args=[job_id, spec_path, constraints],
                **_PHASE_ACTIVITY_OPTS,
            )
            if not spec_intake.get("success"):
                await self._finalize(job_id, spec_intake.get("error") or "Spec intake failed")
                return

        # -- ARCHITECTURE --
        architecture = _resumed(resume, "architecture", completed)
        if architecture is None:
            architecture = await workflow.execute_activity(
                _activities.architecture_activity,
                args=[job_id, spec_intake],
                **_PHASE_ACTIVITY_OPTS,
            )
            if not architecture.get("success"):
                await self._finalize(job_id, architecture.get("error") or "Architecture failed")
                return

        # -- CAPABILITIES --
        capabilities = _resumed(resume, "capabilities", completed)
        if capabilities is None:
            capabilities = await workflow.execute_activity(
                _activities.capabilities_activity,
                args=[job_id, spec_intake, architecture],
                **_PHASE_ACTIVITY_OPTS,
            )
            if not capabilities.get("success"):
                await self._finalize(job_id, capabilities.get("error") or "Capabilities failed")
                return

        # -- EVALUATION --
        evaluation = _resumed(resume, "evaluation", completed)
        if evaluation is None:
            evaluation = await workflow.execute_activity(
                _activities.evaluation_activity,
                args=[job_id, spec_intake],
                **_PHASE_ACTIVITY_OPTS,
            )
            if not evaluation.get("success"):
                await self._finalize(job_id, evaluation.get("error") or "Evaluation failed")
                return

        # -- SAFETY --
        safety = _resumed(resume, "safety", completed)
        if safety is None:
            safety = await workflow.execute_activity(
                _activities.safety_activity,
                args=[job_id, spec_intake, architecture],
                **_PHASE_ACTIVITY_OPTS,
            )
            if not safety.get("success"):
                await self._finalize(job_id, safety.get("error") or "Safety failed")
                return

        # -- BUILD --
        if _resumed(resume, "build", completed) is None:
            build = await workflow.execute_activity(
                _activities.build_phase_activity,
                args=[
                    job_id,
                    project_name,
                    spec_intake,
                    architecture,
                    capabilities,
                    evaluation,
                    safety,
                    output_dir,
                ],
                **_PHASE_ACTIVITY_OPTS,
            )
            if not build.get("success"):
                await self._finalize(job_id, build.get("error") or "Build failed")
                return

        await self._finalize(job_id, None)

    async def _finalize(self, job_id: str, error: Optional[str]) -> None:
        """Schedule the finalize activity (mark the job completed or failed)."""
        await workflow.execute_activity(
            _activities.finalize_build_activity,
            args=[job_id, error],
            **_BOOKEND_ACTIVITY_OPTS,
        )
