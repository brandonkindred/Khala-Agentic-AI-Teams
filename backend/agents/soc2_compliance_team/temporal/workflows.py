"""Temporal workflow for the SOC2 compliance team.

``Soc2AuditWorkflow`` orchestrates the decomposed audit as durable activities:
load the repo, fan out one ``soc2_audit_criterion`` activity per Trust Service
Criterion via ``asyncio.gather``, then synthesize the report. The workflow body
performs no I/O, time, or randomness — only ``execute_activity`` calls — so it is
deterministic and replay-safe. Team imports are wrapped in
``workflow.unsafe.imports_passed_through()`` for the temporalio sandbox.

No explicit ``task_queue`` is passed to the ``execute_activity`` calls, so each
activity is dispatched to the same task queue the workflow worker polls (the
temporalio default).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from soc2_compliance_team.models import TSCCategory
    from soc2_compliance_team.temporal import activities as _activities

# The five Trust Service Criteria value strings, in enum order — the fan-out set.
# Derived from the ``TSCCategory`` enum, the same source ``pipeline.TSC_CRITERIA``
# uses (pipeline asserts every category has a registered auditor), so the thread
# and Temporal drivers can never fan out over different criteria. We derive from
# the enum here rather than importing ``pipeline`` because that module pulls in
# ``strands`` / ``llm_service``, which the temporalio sandbox replays on registration.
_TSC_CRITERIA = [c.value for c in TSCCategory]

# Per-activity timeouts.
LOAD_TIMEOUT = timedelta(minutes=10)
AUDIT_TIMEOUT = timedelta(minutes=30)
REPORT_TIMEOUT = timedelta(minutes=30)
MARK_FAILED_TIMEOUT = timedelta(minutes=1)

# Deterministic repo I/O may be retried; LLM steps are non-idempotent (retrying
# duplicates cost), so they run once.
LOAD_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
)
LLM_RETRY_POLICY = RetryPolicy(maximum_attempts=1)
MARK_FAILED_RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@workflow.defn(name="Soc2AuditWorkflow")
class Soc2AuditWorkflow:
    """Runs one SOC2 audit job as load → fan-out audits → report."""

    @workflow.run
    async def run(self, job_id: str, repo_path: str) -> Dict[str, Any]:
        """Execute the decomposed SOC2 audit.

        Preconditions:
            - ``job_id`` is an existing job row; ``repo_path`` is a directory
              path on the worker host.
        Postconditions:
            - Returns the ``SOC2AuditResult`` as a JSON-native dict and leaves
              the job ``completed``. On any activity failure, marks the job
              ``failed`` (via ``soc2_mark_failed``) and re-raises so the
              workflow itself fails.
        """
        try:
            # load_repo_activity returns only the resolved repo *path* (a short
            # string), not the loaded context — the uncapped code corpus is
            # re-loaded inside each audit activity so it never enters workflow
            # history. Every downstream activity gets that path.
            resolved_path = await workflow.execute_activity(
                _activities.load_repo_activity,
                args=[job_id, repo_path],
                start_to_close_timeout=LOAD_TIMEOUT,
                retry_policy=LOAD_RETRY_POLICY,
            )

            tsc_results = await asyncio.gather(
                *[
                    workflow.execute_activity(
                        _activities.audit_criterion_activity,
                        args=[job_id, criterion, resolved_path],
                        start_to_close_timeout=AUDIT_TIMEOUT,
                        retry_policy=LLM_RETRY_POLICY,
                    )
                    for criterion in _TSC_CRITERIA
                ]
            )

            return await workflow.execute_activity(
                _activities.write_report_activity,
                args=[job_id, resolved_path, list(tsc_results)],
                start_to_close_timeout=REPORT_TIMEOUT,
                retry_policy=LLM_RETRY_POLICY,
            )
        except Exception as e:
            # Best-effort terminal marker — if it fails (e.g. the mark-failed
            # activity times out), swallow that so the bare ``raise`` below still
            # propagates the ORIGINAL audit failure rather than the marker's error.
            try:
                await workflow.execute_activity(
                    _activities.mark_failed_activity,
                    args=[job_id, f"SOC2 audit failed: {e}"],
                    start_to_close_timeout=MARK_FAILED_TIMEOUT,
                    retry_policy=MARK_FAILED_RETRY_POLICY,
                )
            except Exception:
                workflow.logger.exception("Failed to mark SOC2 job %s failed", job_id)
            raise
