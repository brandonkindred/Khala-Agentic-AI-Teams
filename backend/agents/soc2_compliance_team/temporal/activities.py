"""Temporal activities for the SOC2 compliance team.

Each ``@activity.defn`` wraps one stage of the decomposed audit pipeline
(:mod:`soc2_compliance_team.pipeline`) — repo load, per-criterion audit, and
report synthesis — plus a terminal failure marker. Activities are plain sync
functions run in the worker's thread pool; heavy imports live inside the body so
the module stays cheap for the temporalio sandbox to replay. Pydantic models
cross the activity boundary as ``model_dump(mode="json")`` dicts and are
reconstructed with ``model_validate``.

The activities own the durable job-store bookkeeping (via the ``JobServiceClient``
in :mod:`soc2_compliance_team.api.main`): ``load_repo_activity`` marks the job
running, ``write_report_activity`` marks it completed with the result, and
``mark_failed_activity`` marks it failed. Terminal (completed/failed) writes go
through ``_update_job_terminal`` so a later write can never silently clobber a
job that's already reached a terminal status — e.g. a lost activity-completion
ack causing Temporal to retry/fail an activity whose local job-store write
already succeeded. ``load_repo_activity``'s own ``running`` write goes through
the mirror-image ``_update_job_unless_terminal``, so a workflow that keeps
running server-side after the API already wrote a terminal ``failed`` status
(a lost dispatch ack) can't resurrect that job back to non-terminal.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn(name="soc2_load_repo")
def load_repo_activity(job_id: str, repo_path: str) -> str:
    """Load the repository **once** into a durable snapshot and mark job running.

    Loads the ``RepoContext`` a single time and persists it to a blob keyed by
    ``job_id`` (see :mod:`soc2_compliance_team.context_snapshot`), then returns
    only the resolved repo **path** (a short string). The context embeds
    ``code_summary`` — the entire code/config corpus with no size cap
    (``repo_loader``) — so returning it would write that whole snapshot into
    Temporal workflow history as this activity's result and again as input to all
    five audit activities (payload/history-limit risk). Persisting once also
    means every criterion audits the same immutable snapshot, even if the live
    checkout mutates mid-audit.

    Preconditions:
        - ``job_id`` is an existing job; ``repo_path`` is an existing directory.
    Postconditions:
        - Job status is set to ``running`` (skipped if the job already reached
          a terminal status — e.g. ``run_audit``'s dispatch-failure path
          already marked it ``failed`` after a client-side dispatch timeout
          whose workflow was actually accepted server-side; see
          ``_update_job_unless_terminal``); a context snapshot for ``job_id``
          exists; returns the resolved absolute repo path. Raises ``ValueError``
          (after logging) if the path is not a directory.
    """
    from soc2_compliance_team import context_snapshot, pipeline
    from soc2_compliance_team.api.main import _update_job_unless_terminal

    try:
        _update_job_unless_terminal(job_id, status="running", current_stage="Loading repository")
        context = pipeline.load_context(repo_path)
        context_snapshot.save_snapshot(job_id, context)
        _update_job_unless_terminal(job_id, current_stage="Running TSC audits")
        return context.repo_path
    except Exception:
        logger.exception("SOC2 load_repo activity failed for job %s", job_id)
        raise


@activity.defn(name="soc2_audit_criterion")
def audit_criterion_activity(job_id: str, criterion: str) -> Dict[str, Any]:
    """Audit a single Trust Service Criterion against the job's context snapshot.

    Reads the ``RepoContext`` snapshot persisted by :func:`load_repo_activity`
    (keyed by ``job_id``) rather than receiving the corpus as input or re-scanning
    the live path, so only ``job_id`` crosses the boundary and every criterion
    sees an identical repo state.

    Preconditions:
        - ``criterion`` is a ``TSCCategory`` value string.
    Postconditions:
        - Returns a ``TSCAuditResult`` as a JSON-native dict. Per-criterion audit
          failures are isolated into a non-compliant placeholder (matching thread
          mode), so this activity does not raise on an audit error. If the
          context snapshot is missing — a sibling criterion's failure can
          trigger ``mark_failed_activity`` to delete it while this activity is
          still queued or running, since ``asyncio.gather`` in the workflow
          doesn't cancel siblings when one fails — that is ALSO isolated into
          the same kind of fail-closed placeholder rather than raising an
          unhandled exception.
    """
    from soc2_compliance_team import context_snapshot, pipeline
    from soc2_compliance_team.models import TSCCategory

    category = TSCCategory(criterion)
    try:
        ctx = context_snapshot.load_snapshot(job_id)
    except OSError as e:
        logger.warning(
            "SOC2 context snapshot unavailable for job %s criterion %s "
            "(the job is likely already failing)",
            job_id,
            criterion,
        )
        return pipeline.criterion_failure_result(
            category, f"context snapshot unavailable: {e}"
        ).model_dump(mode="json")
    result = pipeline.audit_criterion_safe(category, ctx)
    return result.model_dump(mode="json")


@activity.defn(name="soc2_write_report")
def write_report_activity(
    job_id: str, repo_path: str, tsc_results: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Synthesize the fan-in report and persist the completed result.

    Preconditions:
        - ``tsc_results`` is the list of serialized ``TSCAuditResult`` dicts
          from the per-criterion activities.
    Postconditions:
        - Writes the assembled ``SOC2AuditResult`` to the job store with status
          ``completed`` (via ``_update_job_terminal``, so this can't clobber a
          job already marked ``failed`` by a concurrent
          ``mark_failed_activity``) and returns it as a JSON-native dict.
          Raises (after logging) if report synthesis fails — the workflow's
          own except-block then calls ``mark_failed_activity`` with these
          ``tsc_results`` preserved, rather than this activity persisting a
          failure itself.
    """
    from soc2_compliance_team import context_snapshot, pipeline
    from soc2_compliance_team.api.main import _update_job, _update_job_terminal
    from soc2_compliance_team.models import TSCAuditResult

    try:
        _update_job(job_id, current_stage="Writing report")
        results = [TSCAuditResult.model_validate(d) for d in tsc_results]
        compliance_report, next_steps_document = pipeline.write_report(repo_path, results)
        audit = pipeline.assemble_result(repo_path, results, compliance_report, next_steps_document)
        _update_job_terminal(
            job_id,
            status="completed",
            current_stage="Completed",
            result=audit.model_dump(),
        )
        # The audit is done — drop the context snapshot.
        context_snapshot.delete_snapshot(job_id)
        return audit.model_dump(mode="json")
    except Exception:
        logger.exception("SOC2 write_report activity failed for job %s", job_id)
        raise


@activity.defn(name="soc2_mark_failed")
def mark_failed_activity(
    job_id: str,
    repo_path: str,
    error: str,
    tsc_results: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Mark the job failed after a workflow-level failure.

    Preconditions:
        - ``job_id`` is an existing job; ``error`` is a human-readable message.
        - ``tsc_results``, when provided, is the list of serialized
          ``TSCAuditResult`` dicts from criterion activities that had already
          completed before the failure (e.g. the report-writer step itself
          failed) — preserved in the persisted failure result instead of being
          discarded.
    Postconditions:
        - Job status is set to ``failed`` (via ``_update_job_terminal``, so an
          already-``completed`` job — e.g. from a ``write_report_activity``
          whose local write succeeded but whose Temporal completion ack was
          lost — is never clobbered back to ``failed``) with ``error`` and,
          when given, the preserved ``tsc_results`` recorded in ``result``.
          The context snapshot is dropped. A job-store error is logged and
          re-raised (after snapshot cleanup) so Temporal retries this
          activity per ``MARK_FAILED_RETRY_POLICY`` instead of silently
          leaving the job non-terminal; the workflow's own
          except-around-execute_activity (see ``workflows.py``) already
          guarantees the *original* audit failure is the one that ultimately
          propagates even if this activity's retries are exhausted.
    """
    from soc2_compliance_team import context_snapshot, pipeline
    from soc2_compliance_team.api.main import _update_job_terminal
    from soc2_compliance_team.models import TSCAuditResult

    context_snapshot.delete_snapshot(job_id)
    results = [TSCAuditResult.model_validate(d) for d in tsc_results] if tsc_results else None
    failed = pipeline.failed_result(repo_path, error, tsc_results=results)
    try:
        _update_job_terminal(
            job_id,
            status="failed",
            current_stage="Failed",
            error=error,
            result=failed.model_dump(),
        )
    except Exception:
        logger.exception("SOC2 mark_failed activity could not update job %s", job_id)
        raise
