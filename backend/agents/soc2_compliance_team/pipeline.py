"""Decomposed SOC2 audit pipeline steps — the single source of truth.

The SOC2 audit is a fan-out/fan-in pipeline: load a repository, run the five
Trust Service Criteria (TSC) auditors, then synthesize a report. This module
exposes each stage as a small pure function returning a typed model, with **no**
job-store or transport concerns. Two thin drivers share these functions:

- the thread-mode orchestrator (:mod:`soc2_compliance_team.orchestrator`), which
  runs the TSC audits concurrently via :func:`run_all_criteria`; and
- the Temporal activities (:mod:`soc2_compliance_team.temporal.activities`),
  which wrap one function per ``@activity.defn`` and fan out across the workflow.

Each step reuses the class-based agents in :mod:`soc2_compliance_team.agents`
(one clean ``llm.complete_json`` call each) and resolves its LLM client from the
central provider list via ``get_client("soc2")``.

Invariants:
    - Functions here never mutate their inputs and never touch the job store.
    - The LLM client is obtained internally (per call); callers pass only plain
      data models, so the functions are safe to call from worker threads and
      Temporal activity threads alike.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple

from llm_service import get_client
from shared_concurrency import parallel_map

from .agents import (
    AvailabilityTSCAgent,
    ConfidentialityTSCAgent,
    PrivacyTSCAgent,
    ProcessingIntegrityTSCAgent,
    ReportWriterAgent,
    SecurityTSCAgent,
)
from .models import (
    FindingSeverity,
    NextStepsDocument,
    RepoContext,
    SOC2AuditResult,
    SOC2ComplianceReport,
    TSCAuditResult,
    TSCCategory,
    TSCFinding,
)
from .repo_loader import load_repo_context

logger = logging.getLogger(__name__)

# agent_key routed through the LLM provider list (matches the Strands factories).
_AGENT_KEY = "soc2"

# One class-based auditor per Trust Service Criterion. Ordering here defines the
# canonical criterion order used by both drivers.
_TSC_AGENTS = {
    TSCCategory.SECURITY: SecurityTSCAgent(),
    TSCCategory.AVAILABILITY: AvailabilityTSCAgent(),
    TSCCategory.PROCESSING_INTEGRITY: ProcessingIntegrityTSCAgent(),
    TSCCategory.CONFIDENTIALITY: ConfidentialityTSCAgent(),
    TSCCategory.PRIVACY: PrivacyTSCAgent(),
}

# Invariant: every Trust Service Criterion has a registered auditor. Both drivers
# fan out over the full ``TSCCategory`` enum — the Temporal workflow derives its
# list directly from the enum (it can't import this module, which pulls in
# ``strands``), so this guard keeps the two lists from silently diverging if a
# criterion is ever added without an agent. Explicit raise (not ``assert``) so it
# still fires under ``python -O``.
_MISSING_AGENTS = set(TSCCategory) - set(_TSC_AGENTS)
if _MISSING_AGENTS:  # pragma: no cover - build-time invariant
    raise RuntimeError(f"Every TSCCategory needs a _TSC_AGENTS entry; missing {_MISSING_AGENTS}")

# The TSC categories in canonical (enum) order — the fan-out set, shared with the
# Temporal workflow via the ``TSCCategory`` enum (see the invariant above).
TSC_CRITERIA: List[TSCCategory] = list(TSCCategory)


def load_context(repo_path: str | Path) -> RepoContext:
    """Scan the repository into a ``RepoContext`` for the TSC auditors.

    Preconditions:
        - ``repo_path`` refers to an existing directory.
    Postconditions:
        - Returns a populated ``RepoContext`` whose ``repo_path`` is the
          resolved absolute path. Raises ``ValueError`` if the path is not a
          directory (propagated from ``load_repo_context``).
    """
    return load_repo_context(repo_path)


def audit_criterion(category: TSCCategory, context: RepoContext) -> TSCAuditResult:
    """Audit one Trust Service Criterion against the repository context.

    Preconditions:
        - ``category`` is a valid ``TSCCategory``.
        - ``context`` is a ``RepoContext`` (typically from :func:`load_context`).
    Postconditions:
        - Returns a ``TSCAuditResult`` whose ``category`` equals ``category``.
        - Performs exactly one LLM call (via the criterion's agent).
    """
    # Explicit raise (not ``assert``) so the precondition still holds under
    # ``python -O``, matching the module-level guard and ``audit_criterion_safe``.
    if category not in _TSC_AGENTS:
        raise KeyError(f"Unknown TSC category: {category}")
    llm = get_client(_AGENT_KEY)
    return _TSC_AGENTS[category].run(llm, context)


def criterion_failure_result(category: TSCCategory, reason: str) -> TSCAuditResult:
    """Build the fail-closed placeholder for a criterion that could not be audited.

    Shared by :func:`audit_criterion_safe` (a runtime audit error) and the
    Temporal activity layer (a missing/deleted context snapshot — see
    ``temporal/activities.py::audit_criterion_activity``) so both failure
    origins produce an identical, actionable placeholder instead of raising.

    Postconditions:
        - Returns a non-compliant ``TSCAuditResult`` for ``category`` carrying
          a synthetic HIGH finding describing ``reason``, so the failure is
          visible in the structured report (``findings_by_tsc``), not only in
          free-text summary.
    """
    return TSCAuditResult(
        category=category,
        summary=f"Audit for {category.value} could not be completed: {reason}",
        findings=[
            TSCFinding(
                severity=FindingSeverity.HIGH,
                category=category,
                title=f"{category.value} audit could not be completed",
                description=f"The audit for this criterion failed to run: {reason}",
                recommendation="Re-run the SOC2 audit for this criterion.",
            )
        ],
        compliant=False,
    )


def audit_criterion_safe(category: TSCCategory, context: RepoContext) -> TSCAuditResult:
    """Audit one criterion, isolating *runtime* failures into a placeholder result.

    Both drivers use this so a single failed auditor never sinks the whole
    audit and both execution modes produce identical output.

    Preconditions:
        - ``category`` is a valid ``TSCCategory``; ``context`` is a ``RepoContext``.
          An unknown category is a caller/contract bug and is raised (never
          masked as an audit failure) — validated before the isolation boundary.
    Postconditions:
        - Returns a ``TSCAuditResult`` for ``category``. On a runtime audit error
          the result is **fail-closed**: ``compliant=False`` (never silently
          "compliant" for a criterion that could not be assessed) with the error
          in the summary, so an un-auditable criterion surfaces in the report
          instead of being reported as a pass. Never raises for a runtime error.
    """
    # Surface a contract violation directly — do not let the try/except below
    # mask an unknown category as a fabricated "audit failed" finding. Explicit
    # raise (not ``assert``) so it holds under ``python -O``.
    if category not in _TSC_AGENTS:
        raise KeyError(f"Unknown TSC category: {category}")
    try:
        return audit_criterion(category, context)
    except Exception as e:  # noqa: BLE001 - isolate a single criterion's runtime failure
        logger.exception("TSC audit failed for %s", category.value)
        return criterion_failure_result(category, str(e))


def run_all_criteria(context: RepoContext) -> List[TSCAuditResult]:
    """Audit all five criteria concurrently (thread-mode fan-out).

    Preconditions:
        - ``context`` is a ``RepoContext``.
    Postconditions:
        - Returns one ``TSCAuditResult`` per criterion in ``TSC_CRITERIA``
          order (failures isolated per :func:`audit_criterion_safe`). Uses the
          shared context-propagating fan-out so each audit thread inherits the
          caller's LLM attribution / request-id contextvars.
    """
    return parallel_map(
        TSC_CRITERIA,
        lambda c: audit_criterion_safe(c, context),
        max_workers=len(TSC_CRITERIA),
    )


def write_report(
    repo_path: str | Path,
    tsc_results: List[TSCAuditResult],
) -> Tuple[Optional[SOC2ComplianceReport], Optional[NextStepsDocument]]:
    """Synthesize the fan-in output from all TSC results.

    Preconditions:
        - ``tsc_results`` holds the per-criterion audit results.
    Postconditions:
        - Returns ``(compliance_report, next_steps_document)`` with exactly one
          element non-None: the compliance report when material findings exist,
          otherwise the next-steps document. Performs exactly one LLM call.
    """
    return ReportWriterAgent().run(get_client(_AGENT_KEY), str(repo_path), tsc_results)


def failed_result(
    repo_path: str | Path,
    error: str,
    tsc_results: Optional[List[TSCAuditResult]] = None,
) -> SOC2AuditResult:
    """Build a ``status="failed"`` result, preserving any completed criterion audits.

    Both drivers use this at every failure point (repo load, criteria fan-out,
    report synthesis) so a failure *after* the criteria audits already
    succeeded — e.g. the report-writer step itself fails — doesn't discard
    real, already-paid-for ``TSCAuditResult`` objects; they're carried in
    ``tsc_results`` instead of being silently dropped.

    Postconditions:
        - Returns ``SOC2AuditResult(status="failed", tsc_results=tsc_results or
          [])``. ``has_findings`` is derived from ``tsc_results`` (true iff any
          preserved result is non-compliant or carries a critical/high
          finding), not hardcoded false — a failure that occurs after material
          gaps were already discovered must not report itself as clean.
    """
    results = tsc_results or []
    has_findings = any(
        not r.compliant
        or any(f.severity in (FindingSeverity.CRITICAL, FindingSeverity.HIGH) for f in r.findings)
        for r in results
    )
    return SOC2AuditResult(
        status="failed",
        repo_path=str(repo_path),
        tsc_results=results,
        has_findings=has_findings,
        error=error,
    )


def assemble_result(
    repo_path: str | Path,
    tsc_results: List[TSCAuditResult],
    compliance_report: Optional[SOC2ComplianceReport],
    next_steps_document: Optional[NextStepsDocument],
) -> SOC2AuditResult:
    """Combine the per-criterion results and the fan-in output into the final result.

    Preconditions:
        - ``tsc_results`` is the list of per-criterion results.
        - Exactly one of ``compliance_report`` / ``next_steps_document`` is
          non-None (as returned by :func:`write_report`).
    Postconditions:
        - Returns a ``SOC2AuditResult`` with ``status="completed"``.
          ``has_findings`` is taken from the single decision already made by the
          report writer (a compliance report is produced iff material findings
          exist), so it can never disagree with the report-vs-next-steps choice.
    """
    return SOC2AuditResult(
        status="completed",
        repo_path=str(repo_path),
        tsc_results=tsc_results,
        has_findings=compliance_report is not None,
        compliance_report=compliance_report,
        next_steps_document=next_steps_document,
    )
