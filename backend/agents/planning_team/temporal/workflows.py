"""Temporal workflow for the Planning team.

``PlanningWorkflow`` is the durable orchestrator: it drives the same phase
sequence as ``planning_team.orchestrator.run_workflow`` (intake → discovery →
requirements → optional market research → synthesis → document production →
optional sub-agent provisioning → finalize), but each phase is its own
``@activity.defn`` (see :mod:`.activities`) so Temporal records, times out, and
retries every phase independently instead of one opaque black-box activity.

The workflow body is deterministic: it only threads a JSON-native ``context``
dict from one ``workflow.execute_activity`` call to the next and branches on the
run flags — no I/O, no ``os.getenv``, no time/randomness. Activity/constant
imports are kept under ``workflow.unsafe.imports_passed_through()`` so the
temporalio sandbox reuses the already-imported modules rather than re-executing
them during workflow registration.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from planning_team.temporal import activities as _activities
    from planning_team.temporal.constants import TASK_QUEUE

# --- Per-phase timeouts -----------------------------------------------------
#: Deterministic/cheap phases (intake, synthesis, finalize).
QUICK_TIMEOUT = timedelta(minutes=5)
#: LLM phases (discovery, requirements) + the optional market-research call —
#: a large spec becomes many per-section LLM round-trips.
LLM_TIMEOUT = timedelta(hours=1)
#: Long external-poll phases (document production's PRA wait, sub-agent
#: provisioning's AI-Systems wait). The PRA poll alone can run up to an hour.
EXTERNAL_TIMEOUT = timedelta(hours=2)
#: Heartbeat window for the external-poll phases (their activities emit a
#: background heartbeat every 30s).
HEARTBEAT_TIMEOUT = timedelta(minutes=5)

# --- Per-phase retry policies ----------------------------------------------
#: Deterministic phases are safe to retry (no external side effects).
SAFE_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
)
#: LLM / side-effecting phases run once: the LLM calls are non-idempotent (and
#: llm_service already fails over on transient provider errors internally), and
#: document production writes files + submits a PRA job, so a workflow-level
#: retry must not re-run them. A failure surfaces as a failed workflow + FAILED
#: job row for explicit resubmission rather than being auto-retried.
NO_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn(name="PlanningWorkflow")
class PlanningWorkflow:
    """Durable, per-phase Planning orchestrator."""

    @workflow.run
    async def run(
        self,
        job_id: str,
        repo_path: str,
        client_name: Optional[str],
        initial_brief: Optional[str],
        spec_content: Optional[str],
        use_product_analysis: bool,
        use_market_research: bool,
    ) -> Dict[str, Any]:
        """Run one Planning job phase by phase, each phase a separate activity.

        Preconditions:
            - ``job_id`` refers to a job already created in the job store (the API
              endpoint calls ``create_job`` before dispatch); ``repo_path`` is the
              resolved workspace; at least one of ``initial_brief``/``spec_content``
              is set.

        Postconditions:
            - Threads the ``context`` dict through the per-phase activities in the
              same order and branches as the in-process orchestrator, and returns
              the finalize activity's ``{"success": True, "summary": ...}``. Each
              activity owns its own job-store progress writes and marks the job
              FAILED (then re-raises) on its own error, so a phase failure fails
              this workflow at that specific activity rather than re-running the
              whole plan.
        """
        context = await workflow.execute_activity(
            _activities.intake_activity,
            args=[job_id, repo_path, client_name, initial_brief, spec_content],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=QUICK_TIMEOUT,
            retry_policy=SAFE_RETRY,
        )

        context = await workflow.execute_activity(
            _activities.discovery_activity,
            args=[job_id, context],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=LLM_TIMEOUT,
            retry_policy=NO_RETRY,
        )

        context = await workflow.execute_activity(
            _activities.requirements_activity,
            args=[job_id, context],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=LLM_TIMEOUT,
            retry_policy=NO_RETRY,
        )

        market_evidence: Optional[Dict[str, Any]] = None
        if use_market_research:
            market_evidence = await workflow.execute_activity(
                _activities.market_research_activity,
                args=[job_id, context],
                task_queue=TASK_QUEUE,
                start_to_close_timeout=LLM_TIMEOUT,
                retry_policy=NO_RETRY,
            )

        context = await workflow.execute_activity(
            _activities.synthesis_activity,
            args=[job_id, context, market_evidence],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=QUICK_TIMEOUT,
            retry_policy=SAFE_RETRY,
        )

        context = await workflow.execute_activity(
            _activities.document_production_activity,
            args=[job_id, context, use_product_analysis],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=EXTERNAL_TIMEOUT,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=NO_RETRY,
        )

        # capability_gap is not part of the HTTP dispatch surface (the thread path
        # never sets it either), so this phase is a fast no-op skip today; it is
        # still driven so the seam exists for a future gated caller.
        context = await workflow.execute_activity(
            _activities.sub_agent_provisioning_activity,
            args=[job_id, context, None],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=EXTERNAL_TIMEOUT,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=NO_RETRY,
        )

        return await workflow.execute_activity(
            _activities.finalize_planning_activity,
            args=[job_id, context],
            task_queue=TASK_QUEUE,
            start_to_close_timeout=QUICK_TIMEOUT,
            retry_policy=SAFE_RETRY,
        )
