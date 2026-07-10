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
``mark_failed_activity`` marks it failed.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn(name="soc2_load_repo")
def load_repo_activity(job_id: str, repo_path: str) -> Dict[str, Any]:
    """Load the repository into a serialized ``RepoContext``.

    Preconditions:
        - ``job_id`` is an existing job; ``repo_path`` is an existing directory.
    Postconditions:
        - Job status is set to ``running``; returns ``RepoContext`` as a
          JSON-native dict. Raises (after logging) if the repo cannot be loaded.
    """
    from soc2_compliance_team import pipeline
    from soc2_compliance_team.api.main import _update_job

    try:
        _update_job(job_id, status="running", current_stage="Loading repository")
        context = pipeline.load_context(repo_path)
        _update_job(job_id, current_stage="Running TSC audits")
        return context.model_dump(mode="json")
    except Exception:
        logger.exception("SOC2 load_repo activity failed for job %s", job_id)
        raise


@activity.defn(name="soc2_audit_criterion")
def audit_criterion_activity(
    job_id: str, criterion: str, context: Dict[str, Any]
) -> Dict[str, Any]:
    """Audit a single Trust Service Criterion.

    Preconditions:
        - ``criterion`` is a ``TSCCategory`` value string.
        - ``context`` is a serialized ``RepoContext`` (from
          :func:`load_repo_activity`).
    Postconditions:
        - Returns a ``TSCAuditResult`` as a JSON-native dict. Per-criterion
          failures are isolated into a non-compliant placeholder (matching
          thread mode), so this activity does not raise on an audit error.
    """
    from soc2_compliance_team import pipeline
    from soc2_compliance_team.models import RepoContext, TSCCategory

    ctx = RepoContext.model_validate(context)
    result = pipeline.audit_criterion_safe(TSCCategory(criterion), ctx)
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
          ``completed`` and returns it as a JSON-native dict. Raises (after
          logging) if report synthesis fails.
    """
    from soc2_compliance_team import pipeline
    from soc2_compliance_team.api.main import _update_job
    from soc2_compliance_team.models import TSCAuditResult

    try:
        _update_job(job_id, current_stage="Writing report")
        results = [TSCAuditResult.model_validate(d) for d in tsc_results]
        compliance_report, next_steps_document = pipeline.write_report(repo_path, results)
        audit = pipeline.assemble_result(repo_path, results, compliance_report, next_steps_document)
        _update_job(
            job_id,
            status="completed",
            current_stage="Completed",
            result=audit.model_dump(),
        )
        return audit.model_dump(mode="json")
    except Exception:
        logger.exception("SOC2 write_report activity failed for job %s", job_id)
        raise


@activity.defn(name="soc2_mark_failed")
def mark_failed_activity(job_id: str, error: str) -> None:
    """Mark the job failed after a workflow-level failure.

    Preconditions:
        - ``job_id`` is an existing job; ``error`` is a human-readable message.
    Postconditions:
        - Job status is set to ``failed`` with ``error`` recorded. Never raises
          on a job-store error (best-effort terminal write).
    """
    from soc2_compliance_team.api.main import _update_job

    try:
        _update_job(job_id, status="failed", current_stage="Failed", error=error)
    except Exception:
        logger.exception("SOC2 mark_failed activity could not update job %s", job_id)
