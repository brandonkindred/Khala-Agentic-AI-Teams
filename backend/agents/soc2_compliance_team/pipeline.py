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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Tuple

from llm_service import get_client

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

# The five TSC categories, in canonical order (used to fan out).
TSC_CRITERIA: List[TSCCategory] = list(_TSC_AGENTS.keys())


def _has_material_findings(tsc_results: List[TSCAuditResult]) -> bool:
    """Return True if any criterion is non-compliant or has a critical/high finding.

    Preconditions:
        - ``tsc_results`` is a list of ``TSCAuditResult``.
    Postconditions:
        - Returns a bool; does not mutate ``tsc_results``.
    """
    return any(
        not r.compliant
        or any(f.severity in (FindingSeverity.CRITICAL, FindingSeverity.HIGH) for f in r.findings)
        for r in tsc_results
    )


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
    assert category in _TSC_AGENTS, f"Unknown TSC category: {category}"
    llm = get_client(_AGENT_KEY)
    return _TSC_AGENTS[category].run(llm, context)


def audit_criterion_safe(category: TSCCategory, context: RepoContext) -> TSCAuditResult:
    """Audit one criterion, isolating failures into a placeholder result.

    Both drivers use this so a single failed auditor never sinks the whole
    audit and both execution modes produce identical output.

    Preconditions:
        - ``category`` is a valid ``TSCCategory``; ``context`` is a ``RepoContext``.
    Postconditions:
        - Returns a ``TSCAuditResult`` for ``category``. On failure, returns a
          non-compliant placeholder whose summary carries the error (never
          raises).
    """
    try:
        return audit_criterion(category, context)
    except Exception as e:  # noqa: BLE001 - isolate a single criterion's failure
        logger.exception("TSC audit failed for %s", category.value)
        return TSCAuditResult(
            category=category,
            summary=f"Audit for {category.value} failed: {e}",
            findings=[],
            compliant=False,
        )


def run_all_criteria(context: RepoContext) -> List[TSCAuditResult]:
    """Audit all five criteria concurrently (thread-mode fan-out).

    Preconditions:
        - ``context`` is a ``RepoContext``.
    Postconditions:
        - Returns one ``TSCAuditResult`` per criterion in ``TSC_CRITERIA``
          order (failures isolated per :func:`audit_criterion_safe`).
    """
    with ThreadPoolExecutor(max_workers=len(TSC_CRITERIA), thread_name_prefix="soc2-tsc") as pool:
        return list(pool.map(lambda c: audit_criterion_safe(c, context), TSC_CRITERIA))


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
        - Returns a ``SOC2AuditResult`` with ``status="completed"`` and
          ``has_findings`` reflecting whether material findings exist.
    """
    return SOC2AuditResult(
        status="completed",
        repo_path=str(repo_path),
        tsc_results=tsc_results,
        has_findings=_has_material_findings(tsc_results),
        compliance_report=compliance_report,
        next_steps_document=next_steps_document,
    )
