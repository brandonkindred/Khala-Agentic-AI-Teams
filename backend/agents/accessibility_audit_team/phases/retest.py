"""
Phase 4: Optional Re-test Loop

Triggered after fixes: targeted re-tests using acceptance criteria,
close findings with evidence of fix.
"""

from typing import Any, Dict, List, Optional

from ..agents import (
    AssistiveTechSpecialist,
    EvidenceEngineer,
    MobileAccessibilitySpecialist,
    WebAuditSpecialist,
)
from ..agents.base import MessageBus
from ..models import (
    Finding,
    FindingState,
    Phase,
    RetestResult,
    Surface,
)


async def run_retest_phase(
    audit_id: str,
    findings_to_retest: List[Finding],
    llm_client: Optional[Any] = None,
    message_bus: Optional[MessageBus] = None,
) -> RetestResult:
    """
    Run the retest phase to verify fixes.

    This phase performs:
    1. Targeted re-tests using acceptance criteria
    2. Evidence capture for fixed findings
    3. Close findings with evidence of fix

    Args:
        audit_id: The audit identifier
        findings_to_retest: Findings to re-verify
        llm_client: Optional LLM client for agent processing

    Returns:
        RetestResult with updated findings
    """
    if not findings_to_retest:
        return RetestResult(
            success=True,
            findings_retested=0,
            findings_closed=0,
            findings_still_open=0,
            updated_findings=[],
            summary="No findings to retest",
        )

    # Initialize agents
    was = WebAuditSpecialist(llm_client, message_bus=message_bus)
    mas = MobileAccessibilitySpecialist(llm_client, message_bus=message_bus)
    ats = AssistiveTechSpecialist(llm_client, message_bus=message_bus)
    ree = EvidenceEngineer(llm_client, message_bus=message_bus)

    updated_findings: List[Finding] = []
    closed_count = 0
    still_open_count = 0

    for finding in findings_to_retest:
        # Re-verify based on acceptance criteria
        passed = await _verify_acceptance_criteria(finding, was, mas, ats)

        if passed:
            # Finding is fixed - close it
            finding.state = FindingState.CLOSED
            closed_count += 1

            # Capture evidence of fix
            ree_context = {
                "phase": Phase.RETEST,
                "audit_id": audit_id,
                "findings": [finding],
            }
            await ree.safe_process(ree_context)
        else:
            # Finding still exists
            still_open_count += 1
            # Keep current state

        updated_findings.append(finding)

    return RetestResult(
        success=True,
        findings_retested=len(findings_to_retest),
        findings_closed=closed_count,
        findings_still_open=still_open_count,
        updated_findings=updated_findings,
        summary=f"Retest complete: {closed_count} closed, {still_open_count} still open",
    )


async def _verify_acceptance_criteria(
    finding: Finding,
    was: WebAuditSpecialist,
    mas: MobileAccessibilitySpecialist,
    ats: AssistiveTechSpecialist,
) -> bool:
    """
    Verify acceptance criteria for a finding.

    Returns True if all acceptance criteria pass.
    """
    if not finding.acceptance_criteria:
        # No acceptance criteria - can't verify
        return False

    # Select appropriate agent based on surface
    if finding.surface == Surface.WEB:
        # Run web verification
        # For now, return False (not implemented)
        # In real implementation, would run actual tests
        return False
    elif finding.surface in [Surface.IOS, Surface.ANDROID]:
        # Run mobile verification
        return False
    else:
        return False


async def retest_single_finding(
    audit_id: str,
    finding: Finding,
    llm_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Retest a single finding.

    Convenience function for targeted retesting.

    Returns:
        Dict with retest result for the single finding
    """
    result = await run_retest_phase(
        audit_id=audit_id,
        findings_to_retest=[finding],
        llm_client=llm_client,
    )

    if result.updated_findings:
        updated = result.updated_findings[0]
        return {
            "finding_id": updated.id,
            "passed": updated.state == FindingState.CLOSED,
            "new_state": updated.state.value,
        }

    return {
        "finding_id": finding.id,
        "passed": False,
        "new_state": finding.state.value,
        "error": "Retest failed",
    }


async def retest_pattern(
    audit_id: str,
    pattern_findings: List[Finding],
    llm_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Retest all findings in a pattern.

    Used when a systemic fix is applied and all related findings
    need to be re-verified together.

    Returns:
        Dict with retest results for the pattern
    """
    result = await run_retest_phase(
        audit_id=audit_id,
        findings_to_retest=pattern_findings,
        llm_client=llm_client,
    )

    return {
        "pattern_size": len(pattern_findings),
        "findings_closed": result.findings_closed,
        "findings_still_open": result.findings_still_open,
        "all_fixed": result.findings_still_open == 0,
        "updated_findings": [{"id": f.id, "state": f.state.value} for f in result.updated_findings],
    }
