"""
Phase 2: Manual Verification (Deep)

ATS verifies high-impact issues with AT scripts, adds AT evidence
and refined impact statements.

SLMS confirms mappings and adds confidence scores.

RA adds remediation guidance + acceptance criteria.

Outputs: FindingVerified[]
"""

from typing import Any, Dict, List, Optional

from ..agents import (
    AssistiveTechSpecialist,
    EvidenceEngineer,
    RemediationAdvisor,
    StandardsMappingSpecialist,
)
from ..models import (
    Finding,
    FindingState,
    Phase,
    Severity,
    VerificationResult,
)


async def run_verification_phase(
    audit_id: str,
    draft_findings: List[Finding],
    stack: Dict[str, str] = None,
    llm_client: Optional[Any] = None,
) -> VerificationResult:
    """
    Run the verification phase for deep AT verification.

    This phase performs:
    1. AT verification of high-impact findings
    2. Standards mapping confirmation
    3. Remediation guidance addition

    Args:
        audit_id: The audit identifier
        draft_findings: Findings from discovery phase
        stack: Tech stack info (web: react/angular/vue, mobile: native/rn/flutter)
        llm_client: Optional LLM client for agent processing

    Returns:
        VerificationResult with verified findings
    """
    if not draft_findings:
        return VerificationResult(
            success=True,
            verified_findings=[],
            rejected_findings=[],
            summary="No findings to verify",
        )

    stack = stack or {"web": "other", "mobile": "other"}

    # Initialize agents
    ats = AssistiveTechSpecialist(llm_client)
    slms = StandardsMappingSpecialist(llm_client)
    ra = RemediationAdvisor(llm_client)
    ree = EvidenceEngineer(llm_client)

    # Prioritize high-impact findings for AT verification
    high_impact = [
        f for f in draft_findings
        if f.severity in [Severity.CRITICAL, Severity.HIGH]
    ]
    other_findings = [
        f for f in draft_findings
        if f.severity not in [Severity.CRITICAL, Severity.HIGH]
    ]

    verified_findings: List[Finding] = []
    rejected_finding_ids: List[str] = []

    # ATS verifies high-impact findings
    if high_impact:
        ats_context = {
            "phase": Phase.VERIFICATION,
            "audit_id": audit_id,
            "findings": high_impact,
        }

        ats_result = await ats.process(ats_context)

        if ats_result.get("success"):
            verified_findings.extend(ats_result.get("verified_findings", []))
            rejected_finding_ids.extend(ats_result.get("rejected_finding_ids", []))

    # Medium/low findings get lighter verification
    for finding in other_findings:
        # Mark as verified with lower confidence if evidence exists
        if finding.evidence_pack_ref:
            finding.state = FindingState.VERIFIED
            finding.confidence = min(0.75, finding.confidence)
            verified_findings.append(finding)
        else:
            finding.state = FindingState.NEEDS_VERIFICATION
            # Don't reject, but don't verify either
            verified_findings.append(finding)

    # SLMS confirms standards mappings for verified findings
    if verified_findings:
        slms_context = {
            "phase": Phase.VERIFICATION,
            "audit_id": audit_id,
            "findings": verified_findings,
        }

        slms_result = await slms.process(slms_context)

        if slms_result.get("success"):
            verified_findings = slms_result.get("mapped_findings", verified_findings)

    # RA adds remediation guidance
    if verified_findings:
        ra_context = {
            "phase": Phase.VERIFICATION,
            "audit_id": audit_id,
            "findings": verified_findings,
            "stack": stack,
        }

        ra_result = await ra.process(ra_context)

        if ra_result.get("success"):
            verified_findings = ra_result.get("remediated_findings", verified_findings)

    # REE supplements evidence for verified findings
    needs_more_evidence = [
        f for f in verified_findings
        if not f.evidence_pack_ref
    ]

    if needs_more_evidence:
        ree_context = {
            "phase": Phase.VERIFICATION,
            "audit_id": audit_id,
            "findings": needs_more_evidence,
        }

        await ree.process(ree_context)

    return VerificationResult(
        success=True,
        verified_findings=verified_findings,
        rejected_findings=rejected_finding_ids,
        summary=f"Verification complete: {len(verified_findings)} verified, {len(rejected_finding_ids)} rejected",
    )
