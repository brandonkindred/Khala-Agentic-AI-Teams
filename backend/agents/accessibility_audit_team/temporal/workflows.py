"""Temporal workflows for the accessibility audit team.

``AccessibilityAuditWorkflow`` is the durable orchestrator: it drives the same
phase sequence as the in-process orchestrator (intake -> discovery -> verification
-> report packaging -> finalize), but each phase is its own ``@activity.defn`` (see
:mod:`.activities`) so Temporal records, times out, and retries every phase
independently instead of one opaque 2-hour black-box activity.
``AccessibilityRetestWorkflow`` wraps the (single-phase) retest flow.

The workflow bodies are deterministic: they only thread a slim ``{status}`` dict
between activities and index plain dicts — no I/O, no ``os.getenv``, no
time/randomness. The large audit state crosses phases via the artifact store
(loaded inside each activity, never here). Activity/constant imports are kept under
``workflow.unsafe.imports_passed_through()`` so the temporalio sandbox reuses the
already-imported modules rather than re-executing them during registration.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from accessibility_audit_team.temporal import activities as _activities
    from accessibility_audit_team.temporal.constants import PER_PHASE_PATCH, TASK_QUEUE

# --- Per-phase timeouts -----------------------------------------------------
#: Cheap/deterministic phases (intake, finalize).
QUICK_TIMEOUT = timedelta(minutes=10)
#: LLM/scan phases (discovery, verification, report packaging).
LLM_PHASE_TIMEOUT = timedelta(hours=1)
#: Single-activity retest flow.
RETEST_TIMEOUT = timedelta(hours=1)
#: Heartbeat window for the long phases (their activities beat every 30s).
HEARTBEAT_TIMEOUT = timedelta(minutes=5)

# --- Retry policies ---------------------------------------------------------
#: Every phase writes nothing terminal until its own success and is idempotent
#: (re-running a phase re-derives its findings from the persisted prior state), so
#: a transient LLM/store blip should retry rather than fail the whole audit.
_PHASE_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)

# --- Legacy single-activity path (rollout compatibility only) --------------
#: Timeout + policy for the legacy ``run_pipeline_activity`` branch. These MUST stay
#: byte-for-byte what pre-decomposition ``AccessibilityAuditWorkflow`` histories
#: recorded, or replay is non-deterministic. ``_AUDIT_RETRY_POLICY`` keeps its name
#: (re-exported from the package) for back-compat with existing callers/tests.
LEGACY_TIMEOUT = timedelta(hours=2)
_AUDIT_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)


@workflow.defn(name="AccessibilityAuditWorkflow")
class AccessibilityAuditWorkflow:
    """Durable, per-phase accessibility-audit orchestrator.

    Invariants:
        - Each phase activity owns its own job-store progress write; the terminal
          job status is written by ``finalize_activity`` (success) or by the phase
          activity that detected a logical failure. The workflow body writes nothing
          to the job store directly.
    """

    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Run one audit-create job phase by phase, each phase a separate activity.

        Preconditions:
            - ``payload`` carries ``job_id``, ``audit_id`` and a ``request`` dict; a
              ``pending`` job row already exists for ``job_id`` (the API creates it
              before dispatch).
        Postconditions:
            - Threads a ``{status}`` dict through the per-phase activities in order,
              short-circuiting (returning that dict) the first time a phase reports
              ``status == "FAIL"``; otherwise returns the finalize activity's status
              dict. A pre-decomposition history replays the single legacy activity
              via the ``workflow.patched`` gate and completes after a worker rolls
              forward.
        """
        job_id = payload["job_id"]
        audit_id = payload["audit_id"]
        request = payload["request"]

        # TODO: Remove this legacy branch, the PER_PHASE_PATCH gate, and
        # activities.run_pipeline_activity once no pre-migration
        # AccessibilityAuditWorkflow histories remain open (confirm via the Temporal
        # UI), then deprecate the marker with workflow.deprecate_patch before deleting.
        if not workflow.patched(PER_PHASE_PATCH):
            # Legacy path: reached only when replaying a history recorded before the
            # per-phase migration. Reproduce the old single coarse activity exactly.
            return await workflow.execute_activity(
                _activities.run_pipeline_activity,
                payload,
                task_queue=TASK_QUEUE,
                start_to_close_timeout=LEGACY_TIMEOUT,
                retry_policy=_AUDIT_RETRY_POLICY,
            )

        # Enforce the caller's ``timebox_hours`` as an overall wall-clock budget
        # (parity with thread mode's ``asyncio.wait_for``). Without it the per-phase
        # timeouts sum to more than a short timebox, so a ``timebox_hours=1`` audit
        # could overrun the caller's requested budget.
        timebox_hours = request.get("timebox_hours")
        if not timebox_hours:
            return await self._run_phases(job_id, audit_id, request)

        phases = asyncio.ensure_future(self._run_phases(job_id, audit_id, request))
        timer = asyncio.ensure_future(workflow.sleep(timedelta(hours=timebox_hours)))
        await asyncio.wait([phases, timer], return_when=asyncio.FIRST_COMPLETED)
        if phases.done():
            timer.cancel()
            return phases.result()

        # Timebox expired first: abandon the in-flight phase (its heartbeat delivers
        # the activity cancellation) and mark the job failed with the timeout reason.
        phases.cancel()
        return await workflow.execute_activity(
            _activities.mark_timed_out_activity,
            args=[job_id, audit_id, timebox_hours],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=QUICK_TIMEOUT,
            retry_policy=_PHASE_RETRY_POLICY,
        )

    async def _run_phases(
        self, job_id: str, audit_id: str, request: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Chain the per-phase activities, short-circuiting on the first ``FAIL``.

        Postconditions:
            - Runs intake -> discovery -> verification -> report_packaging ->
              finalize, returning the first phase's ``FAIL`` status dict or the
              finalize status dict. Kept separate from :meth:`run` so the whole
              chain can be raced against the timebox timer as one cancellable task.
        """
        intake = await workflow.execute_activity(
            _activities.intake_activity,
            args=[job_id, audit_id, request],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=QUICK_TIMEOUT,
            retry_policy=_PHASE_RETRY_POLICY,
        )
        if intake.get("status") == "FAIL":
            return intake

        discovery = await workflow.execute_activity(
            _activities.discovery_activity,
            args=[job_id, audit_id],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=LLM_PHASE_TIMEOUT,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=_PHASE_RETRY_POLICY,
        )
        if discovery.get("status") == "FAIL":
            return discovery

        # tech_stack is a small map; extracting it here (plain dict access) keeps the
        # verification activity's signature explicit and the workflow deterministic.
        tech_stack = request.get("tech_stack") or {"web": "other", "mobile": "other"}
        verification = await workflow.execute_activity(
            _activities.verification_activity,
            args=[job_id, audit_id, tech_stack],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=LLM_PHASE_TIMEOUT,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=_PHASE_RETRY_POLICY,
        )
        if verification.get("status") == "FAIL":
            return verification

        report = await workflow.execute_activity(
            _activities.report_packaging_activity,
            args=[job_id, audit_id],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=LLM_PHASE_TIMEOUT,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=_PHASE_RETRY_POLICY,
        )
        if report.get("status") == "FAIL":
            return report

        return await workflow.execute_activity(
            _activities.finalize_activity,
            args=[job_id, audit_id],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=QUICK_TIMEOUT,
            retry_policy=_PHASE_RETRY_POLICY,
        )


@workflow.defn(name="AccessibilityRetestWorkflow")
class AccessibilityRetestWorkflow:
    """Durable wrapper for the (single-phase) accessibility retest flow."""

    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Run one retest job as a single durable activity.

        Preconditions:
            - ``payload`` carries ``job_id``, ``audit_id`` and ``finding_ids`` (a
              possibly-empty list; empty means retest all findings). A ``pending``
              retest job row already exists for ``job_id``.
        Postconditions:
            - Returns the retest activity's status dict once the retest reaches a
              terminal state; the activity owns the job-store lifecycle.
        """
        return await workflow.execute_activity(
            _activities.retest_activity,
            args=[payload["job_id"], payload["audit_id"], payload["finding_ids"]],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=RETEST_TIMEOUT,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=_PHASE_RETRY_POLICY,
        )
