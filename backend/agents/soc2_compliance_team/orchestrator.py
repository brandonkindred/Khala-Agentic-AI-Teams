"""Thread-mode driver for the SOC2 compliance audit.

Runs the decomposed pipeline (:mod:`soc2_compliance_team.pipeline`) in-process:
load the repo, audit the five Trust Service Criteria concurrently, then
synthesize the report. The Temporal workflow drives the exact same pipeline
steps as durable activities — this orchestrator is the non-Temporal path over
the identical logic.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import pipeline
from .models import SOC2AuditResult

logger = logging.getLogger(__name__)


class SOC2AuditOrchestrator:
    """Runs a full SOC2 compliance audit over the shared pipeline steps.

    Five TSC specialist agents run in parallel (via
    :func:`pipeline.run_all_criteria`), then a report writer synthesizes all
    findings into either a compliance report or a next-steps certification
    document.

    Invariants:
        - ``run`` always returns a ``SOC2AuditResult`` (never raises); failures
          surface as ``status="failed"`` with an ``error`` message.
    """

    def run(self, repo_path: str | Path) -> SOC2AuditResult:
        """Execute the full audit on the given repository path.

        Preconditions:
            - ``repo_path`` refers to an existing directory.
        Postconditions:
            - Returns a ``SOC2AuditResult``: ``status="completed"`` with
              per-criterion results and a report/next-steps document on success,
              or ``status="failed"`` with ``error`` set on failure.
        """
        repo_path = Path(repo_path).resolve()
        logger.info("SOC2 audit starting for repo: %s", repo_path)

        try:
            context = pipeline.load_context(repo_path)
        except Exception as e:
            logger.exception("Failed to load repo context")
            return SOC2AuditResult(
                status="failed",
                repo_path=str(repo_path),
                tsc_results=[],
                has_findings=False,
                error=str(e),
            )

        try:
            tsc_results = pipeline.run_all_criteria(context)
            compliance_report, next_steps_document = pipeline.write_report(
                str(repo_path), tsc_results
            )
        except Exception as e:
            logger.exception("SOC2 audit pipeline failed")
            return SOC2AuditResult(
                status="failed",
                repo_path=str(repo_path),
                tsc_results=[],
                has_findings=False,
                error=str(e),
            )

        return pipeline.assemble_result(
            str(repo_path), tsc_results, compliance_report, next_steps_document
        )


def run_soc2_audit(repo_path: str | Path) -> SOC2AuditResult:
    """One-shot SOC2 audit over the shared pipeline.

    Preconditions:
        - ``repo_path`` refers to an existing directory. Each pipeline step
          resolves its own LLM client from the provider list, so no client is
          injected here.
    Postconditions:
        - Returns a ``SOC2AuditResult`` (see :meth:`SOC2AuditOrchestrator.run`).
    """
    return SOC2AuditOrchestrator().run(repo_path)
