"""
Phase 3: Report Packaging

QCR runs quality gate across all findings, ensures consistency,
removes duplicates, normalizes severity.

APL produces exec summary and prioritized roadmap.

Outputs: FinalBacklog, ExecutiveSummary, Roadmap
"""

from typing import Any, Dict, List, Optional

from ..agents import (
    AccessibilityProgramLead,
    QAConsistencyReviewer,
)
from ..models import (
    CoverageMatrix,
    Finding,
    PatternCluster,
    Phase,
    ReportPackagingResult,
)


async def run_report_packaging_phase(
    audit_id: str,
    verified_findings: List[Finding],
    coverage_matrix: Optional[CoverageMatrix] = None,
    llm_client: Optional[Any] = None,
) -> ReportPackagingResult:
    """
    Run the report packaging phase for final QA and report generation.

    This phase performs:
    1. Final quality gate enforcement
    2. Severity normalization
    3. Pattern clustering
    4. Executive summary generation
    5. Roadmap creation
    6. Backlog export

    Args:
        audit_id: The audit identifier
        verified_findings: Findings from verification phase
        coverage_matrix: Optional coverage matrix to include
        llm_client: Optional LLM client for agent processing

    Returns:
        ReportPackagingResult with final backlog and report
    """
    if not verified_findings:
        return ReportPackagingResult(
            success=True,
            final_backlog=[],
            patterns=[],
            executive_summary="No findings to report.",
            roadmap=[],
            summary="No findings in backlog",
        )

    # Initialize agents
    qcr = QAConsistencyReviewer(llm_client)
    apl = AccessibilityProgramLead(llm_client)

    # QCR final quality gate
    qcr_context = {
        "phase": Phase.REPORT_PACKAGING,
        "audit_id": audit_id,
        "findings": verified_findings,
    }

    qcr_result = await qcr.process(qcr_context)

    approved_findings: List[Finding] = []
    patterns: List[PatternCluster] = []
    rejected_findings: List[Finding] = []

    if qcr_result.get("success"):
        approved_findings = qcr_result.get("approved_findings", [])
        patterns = qcr_result.get("patterns", [])
        rejected_findings = qcr_result.get("rejected_findings", [])

    # APL produces final report
    apl_context = {
        "phase": Phase.REPORT_PACKAGING,
        "audit_id": audit_id,
        "findings": approved_findings,
        "patterns": patterns,
    }

    apl_result = await apl.process(apl_context)

    if apl_result.get("success"):
        report_result: ReportPackagingResult = apl_result.get("report_packaging_result")

        # Add coverage matrix if provided
        if coverage_matrix:
            report_result.coverage_matrix = coverage_matrix

        # Add rejected findings info to summary
        if rejected_findings:
            report_result.summary += f" {len(rejected_findings)} findings rejected by QA."

        return report_result

    # Fallback if APL fails
    return ReportPackagingResult(
        success=True,
        final_backlog=approved_findings,
        patterns=patterns,
        executive_summary="Report generation incomplete.",
        roadmap=["Review findings and create remediation plan."],
        coverage_matrix=coverage_matrix,
        summary=f"Packaged {len(approved_findings)} findings (APL processing failed)",
    )


async def export_final_report(
    audit_id: str,
    findings: List[Finding],
    patterns: List[PatternCluster],
    format: str = "json",
    include_evidence: bool = True,
) -> Dict[str, Any]:
    """
    Export the final report in the specified format.

    Args:
        audit_id: The audit identifier
        findings: Final approved findings
        patterns: Pattern clusters
        format: Export format (json, csv)
        include_evidence: Whether to include evidence refs

    Returns:
        Dict with export artifact reference and content
    """
    from ..tools.audit import export_backlog
    from ..tools.audit.export_backlog import ExportBacklogInput

    export_input = ExportBacklogInput(
        audit_id=audit_id,
        findings=findings,
        patterns=patterns,
        format=format,
        include_evidence_refs=include_evidence,
        include_patterns=True,
    )

    export_output = await export_backlog(export_input)

    return {
        "artifact_ref": export_output.artifact_ref,
        "format": export_output.format,
        "counts": export_output.counts,
        "content": export_output.content,
    }
