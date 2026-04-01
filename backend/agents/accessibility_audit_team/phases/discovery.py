"""
Phase 1: Discovery Pass (Wide)

WAS/MAS run scanner signals, perform fast manual sweep on critical flows,
draft findings for high-signal items.

REE starts evidence capture for likely-reportable items.

QCR dedupes early and identifies systemic patterns.

Outputs: FindingDraft[], SignalScanResults, InitialPatternClusters
"""

import asyncio
from typing import Any, List, Optional

from ..agents import (
    EvidenceEngineer,
    MobileAccessibilitySpecialist,
    QAConsistencyReviewer,
    WebAuditSpecialist,
)
from ..agents.base import MessageBus
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
    message_bus: Optional[MessageBus] = None,
) -> DiscoveryResult:
    """
    Run the discovery phase for wide coverage testing.

    WAS and MAS run concurrently when both web URLs and mobile apps are
    present.  REE and QCR run sequentially after discovery agents finish.

    Args:
        audit_plan: The audit plan from intake phase
        llm_client: Optional LLM client for agent processing
        message_bus: Optional shared message bus

    Returns:
        DiscoveryResult with draft findings and patterns
    """
    audit_id = audit_plan.audit_id
    all_findings: List[Finding] = []
    all_scan_results: List[ScanResult] = []

    # Initialize agents
    was = WebAuditSpecialist(llm_client, message_bus=message_bus)
    mas = MobileAccessibilitySpecialist(llm_client, message_bus=message_bus)
    ree = EvidenceEngineer(llm_client, message_bus=message_bus)
    qcr = QAConsistencyReviewer(llm_client, message_bus=message_bus)

    # ---- Run WAS and MAS concurrently ----
    discovery_tasks = []

    if audit_plan.targets.web_urls:
        was_context = {
            "phase": Phase.DISCOVERY,
            "audit_id": audit_id,
            "urls": audit_plan.targets.web_urls,
        }
        discovery_tasks.append(("web", was.safe_process(was_context)))

    if audit_plan.targets.mobile_apps:
        mas_context = {
            "phase": Phase.DISCOVERY,
            "audit_id": audit_id,
            "apps": [app.model_dump() for app in audit_plan.targets.mobile_apps],
        }
        discovery_tasks.append(("mobile", mas.safe_process(mas_context)))

    if discovery_tasks:
        labels, coros = zip(*discovery_tasks)
        results = await asyncio.gather(*coros, return_exceptions=True)

        for label, result in zip(labels, results):
            if isinstance(result, Exception):
                continue
            if not result.get("success"):
                continue

            all_findings.extend(result.get("findings", []))

            if label == "web":
                for sr in result.get("scan_results", []):
                    all_scan_results.append(
                        ScanResult(
                            tool="axe",
                            url=sr.url,
                            violations=[v.model_dump() for v in sr.tool_results[0].violations]
                            if sr.tool_results
                            else [],
                            raw_ref=sr.raw_ref,
                        )
                    )

    # REE captures evidence for findings
    if all_findings:
        ree_context = {
            "phase": Phase.DISCOVERY,
            "audit_id": audit_id,
            "findings": all_findings,
        }
        await ree.safe_process(ree_context)

    # QCR performs early deduplication and pattern identification
    qcr_context = {
        "phase": Phase.DISCOVERY,
        "audit_id": audit_id,
        "findings": all_findings,
    }

    qcr_result = await qcr.safe_process(qcr_context)

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
