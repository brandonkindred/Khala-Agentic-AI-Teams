"""
Phase 1: Discovery Pass (Wide)

WAS/MAS run scanner signals, perform fast manual sweep on critical flows,
draft findings for high-signal items.

REE starts evidence capture for likely-reportable items.

QCR dedupes early and identifies systemic patterns.

Outputs: FindingDraft[], SignalScanResults, InitialPatternClusters
"""

from typing import Any, Dict, List, Optional

from ..agents import (
    WebAuditSpecialist,
    MobileAccessibilitySpecialist,
    EvidenceEngineer,
    QAConsistencyReviewer,
)
from ..models import (
    AuditPlan,
    DiscoveryResult,
    Finding,
    PatternCluster,
    Phase,
    ScanResult,
)


async def run_discovery_phase(
    audit_plan: AuditPlan,
    llm_client: Optional[Any] = None,
) -> DiscoveryResult:
    """
    Run the discovery phase for wide coverage testing.

    This phase performs:
    1. Automated scans (signal only, not confirmed issues)
    2. Fast manual sweep on critical flows
    3. Early evidence capture
    4. Pattern identification and deduplication

    Args:
        audit_plan: The audit plan from intake phase
        llm_client: Optional LLM client for agent processing

    Returns:
        DiscoveryResult with draft findings and patterns
    """
    audit_id = audit_plan.audit_id
    all_findings: List[Finding] = []
    all_scan_results: List[ScanResult] = []

    # Initialize agents
    was = WebAuditSpecialist(llm_client)
    mas = MobileAccessibilitySpecialist(llm_client)
    ree = EvidenceEngineer(llm_client)
    qcr = QAConsistencyReviewer(llm_client)

    # Run web discovery if web URLs present
    if audit_plan.targets.web_urls:
        was_context = {
            "phase": Phase.DISCOVERY,
            "audit_id": audit_id,
            "urls": audit_plan.targets.web_urls,
        }

        was_result = await was.process(was_context)

        if was_result.get("success"):
            all_findings.extend(was_result.get("findings", []))
            # scan_results would come from tool outputs
            for sr in was_result.get("scan_results", []):
                all_scan_results.append(
                    ScanResult(
                        tool="axe",
                        url=sr.url,
                        violations=[v.model_dump() for v in sr.tool_results[0].violations] if sr.tool_results else [],
                        raw_ref=sr.raw_ref,
                    )
                )

    # Run mobile discovery if mobile apps present
    if audit_plan.targets.mobile_apps:
        mas_context = {
            "phase": Phase.DISCOVERY,
            "audit_id": audit_id,
            "apps": [app.model_dump() for app in audit_plan.targets.mobile_apps],
        }

        mas_result = await mas.process(mas_context)

        if mas_result.get("success"):
            all_findings.extend(mas_result.get("findings", []))

    # REE captures evidence for findings
    if all_findings:
        ree_context = {
            "phase": Phase.DISCOVERY,
            "audit_id": audit_id,
            "findings": all_findings,
        }

        ree_result = await ree.process(ree_context)

        if ree_result.get("success"):
            # Evidence refs are updated in the findings
            pass

    # QCR performs early deduplication and pattern identification
    qcr_context = {
        "phase": Phase.DISCOVERY,
        "audit_id": audit_id,
        "findings": all_findings,
    }

    qcr_result = await qcr.process(qcr_context)

    patterns: List[PatternCluster] = []
    if qcr_result.get("success"):
        patterns = qcr_result.get("patterns", [])
        all_findings = qcr_result.get("deduped_findings", all_findings)

    # Count pages/screens scanned
    pages_scanned = len(audit_plan.targets.web_urls)
    pages_scanned += len(audit_plan.targets.mobile_apps)

    return DiscoveryResult(
        success=True,
        draft_findings=all_findings,
        scan_results=all_scan_results,
        initial_patterns=patterns,
        pages_scanned=pages_scanned,
        summary=f"Discovery complete: {len(all_findings)} draft findings, {len(patterns)} patterns identified",
    )
