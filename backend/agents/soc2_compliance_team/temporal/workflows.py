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
from typing import Any, Dict, List, Optional

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

# Per-activity execution timeouts (time once a worker starts running the
# activity) and schedule-to-close timeouts (total time including queue wait,
# so a scheduled-but-never-polled activity — e.g. the worker crashed
# post-boot with nothing to restart it — still eventually fails the job
# instead of hanging indefinitely).
LOAD_TIMEOUT = timedelta(minutes=10)
LOAD_SCHEDULE_TO_CLOSE_TIMEOUT = timedelta(minutes=20)
AUDIT_TIMEOUT = timedelta(minutes=30)
AUDIT_SCHEDULE_TO_CLOSE_TIMEOUT = timedelta(hours=1)
REPORT_TIMEOUT = timedelta(minutes=30)
REPORT_SCHEDULE_TO_CLOSE_TIMEOUT = timedelta(hours=1)
MARK_FAILED_TIMEOUT = timedelta(minutes=1)
MARK_FAILED_SCHEDULE_TO_CLOSE_TIMEOUT = timedelta(minutes=15)

# Deterministic repo I/O may be retried; LLM steps are non-idempotent (retrying
# duplicates cost), so they run once. ValueError from a bad repo_path is
# permanently fatal (identical on every retry), so it's excluded from the
# retry — matches agent_provisioning_team's TOOL_RETRY_POLICY convention for
# the same reason.
LOAD_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    non_retryable_error_types=["ValueError"],
)
LLM_RETRY_POLICY = RetryPolicy(maximum_attempts=1)

# No maximum_attempts cap: mark_failed_activity's own default RetryPolicy()
# equivalent (3 attempts, ~1s initial backoff) exhausts in ~3 seconds total —
# far short of the 15 minutes MARK_FAILED_SCHEDULE_TO_CLOSE_TIMEOUT already
# allocates it. A job-service blip (e.g. a rolling restart) that outlasts a
# few seconds of retrying would otherwise leave the job stuck reporting
# "running" indefinitely, until the stale-job monitor's much coarser backstop
# eventually catches it. Retrying with capped backoff for as long as
# schedule_to_close allows gives a transient outage real room to recover.
MARK_FAILED_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)


@workflow.defn(name="Soc2AuditWorkflow")
class Soc2AuditWorkflow:
    """Runs one SOC2 audit job as load → fan-out audits → report.

    Invariants:
        - The workflow body performs no I/O, time, or randomness of its own —
          only ``execute_activity`` calls — so replay always reproduces the
          same command sequence (temporalio's determinism requirement).
        - The five criteria are always fanned out in the same canonical
          ``TSCCategory`` enum order (``_TSC_CRITERIA``), matching
          ``pipeline.TSC_CRITERIA`` so both execution modes audit the
          identical criteria set in the identical order.
    """

    @workflow.run
    async def run(self, job_id: str, repo_path: str) -> Dict[str, Any]:
        """Execute the decomposed SOC2 audit.

        Preconditions:
            - ``job_id`` is an existing job row; ``repo_path`` is a directory
              path on the worker host.
        Postconditions:
            - Returns the ``SOC2AuditResult`` as a JSON-native dict and leaves
              the job ``completed``. On any activity failure, marks the job
              ``failed`` (via ``soc2_mark_failed``, preserving any criterion
              results already completed before the failure) and re-raises so
              the workflow itself fails.
        """
        tsc_results: Optional[List[Dict[str, Any]]] = None
        try:
            # load_repo_activity loads the repo once, persists it to a snapshot
            # keyed by job_id, and returns only the resolved repo *path* (a short
            # string) — the uncapped code corpus never enters workflow history.
            # Each audit reads that same snapshot by job_id (consistent state);
            # the resolved path is only used for the report's scope label.
            resolved_path = await workflow.execute_activity(
                _activities.load_repo_activity,
                args=[job_id, repo_path],
                start_to_close_timeout=LOAD_TIMEOUT,
                schedule_to_close_timeout=LOAD_SCHEDULE_TO_CLOSE_TIMEOUT,
                retry_policy=LOAD_RETRY_POLICY,
            )

            gathered = await asyncio.gather(
                *[
                    workflow.execute_activity(
                        _activities.audit_criterion_activity,
                        args=[job_id, criterion],
                        start_to_close_timeout=AUDIT_TIMEOUT,
                        schedule_to_close_timeout=AUDIT_SCHEDULE_TO_CLOSE_TIMEOUT,
                        retry_policy=LLM_RETRY_POLICY,
                    )
                    for criterion in _TSC_CRITERIA
                ],
                return_exceptions=True,
            )
            # asyncio.gather does not cancel sibling activities when one
            # raises — with return_exceptions=True every criterion still gets
            # to finish, so a single activity-level failure (e.g. a criterion
            # exhausting its retries and timing out) doesn't discard results
            # sibling criteria already produced. tsc_results is assigned here,
            # before any raise below, so the except block's mark_failed_activity
            # call still receives whatever completed.
            tsc_results = []
            failures: list[tuple[str, BaseException]] = []
            for criterion, outcome in zip(_TSC_CRITERIA, gathered):
                if isinstance(outcome, BaseException):
                    failures.append((criterion, outcome))
                else:
                    tsc_results.append(outcome)
            if failures:
                summary = "; ".join(f"{criterion}: {exc}" for criterion, exc in failures)
                raise RuntimeError(
                    f"{len(failures)} SOC2 criterion audit(s) failed: {summary}"
                ) from failures[0][1]

            return await workflow.execute_activity(
                _activities.write_report_activity,
                args=[job_id, resolved_path, tsc_results],
                start_to_close_timeout=REPORT_TIMEOUT,
                schedule_to_close_timeout=REPORT_SCHEDULE_TO_CLOSE_TIMEOUT,
                retry_policy=LLM_RETRY_POLICY,
            )
        except Exception as e:
            # Best-effort terminal marker — if it fails (e.g. the mark-failed
            # activity times out), swallow that so the bare ``raise`` below still
            # propagates the ORIGINAL audit failure rather than the marker's error.
            try:
                await workflow.execute_activity(
                    _activities.mark_failed_activity,
                    args=[job_id, repo_path, f"SOC2 audit failed: {e}", tsc_results],
                    start_to_close_timeout=MARK_FAILED_TIMEOUT,
                    schedule_to_close_timeout=MARK_FAILED_SCHEDULE_TO_CLOSE_TIMEOUT,
                    retry_policy=MARK_FAILED_RETRY_POLICY,
                )
            except Exception:
                workflow.logger.exception("Failed to mark SOC2 job %s failed", job_id)
            raise
