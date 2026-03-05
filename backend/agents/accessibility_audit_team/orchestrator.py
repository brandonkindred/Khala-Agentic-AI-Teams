"""
Orchestrator for the Digital Accessibility Audit Team.

Coordinates the 8 specialist agents through the 5-phase workflow:
1. Intake - APL creates audit plan
2. Discovery - WAS/MAS run scans and manual testing
3. Verification - ATS/SLMS/REE verify and enrich findings
4. Report Packaging - QCR/APL produce final report
5. Retest - Re-verify after fixes (optional)

Two-Lane Execution Model:
- Lane A (Coverage): WAS, MAS - fast discovery, wide sweep
- Lane B (Credibility): ATS, SLMS, REE - AT verification, proper evidence

QCR ensures Lane A doesn't dump garbage into Lane B.
"""

from typing import Any, Dict, List, Optional
import logging

from .agents import (
    AccessibilityProgramLead,
    WebAuditSpecialist,
    MobileAccessibilitySpecialist,
    AssistiveTechSpecialist,
    StandardsMappingSpecialist,
    EvidenceEngineer,
    RemediationAdvisor,
    QAConsistencyReviewer,
)
from .models import (
    AccessibilityAuditResult,
    AuditPlan,
    AuditRequest,
    CoverageMatrix,
    Finding,
    MobileAppTarget,
    PatternCluster,
    Phase,
    Severity,
    WCAGLevel,
)
from .phases import (
    run_intake_phase,
    run_discovery_phase,
    run_verification_phase,
    run_report_packaging_phase,
    run_retest_phase,
)


logger = logging.getLogger(__name__)


class AccessibilityAuditOrchestrator:
    """
    Top-level orchestrator for accessibility audits.

    Manages the full audit lifecycle through all phases and coordinates
    the specialist agents.
    """

    def __init__(self, llm_client: Optional[Any] = None):
        """
        Initialize the orchestrator.

        Args:
            llm_client: Optional LLM client for agent processing
        """
        self.llm_client = llm_client

        # Initialize all specialist agents
        self.apl = AccessibilityProgramLead(llm_client)
        self.was = WebAuditSpecialist(llm_client)
        self.mas = MobileAccessibilitySpecialist(llm_client)
        self.ats = AssistiveTechSpecialist(llm_client)
        self.slms = StandardsMappingSpecialist(llm_client)
        self.ree = EvidenceEngineer(llm_client)
        self.ra = RemediationAdvisor(llm_client)
        self.qcr = QAConsistencyReviewer(llm_client)

        # Store for in-progress audits
        self._audits: Dict[str, AccessibilityAuditResult] = {}

    async def run_audit(
        self,
        audit_request: AuditRequest,
        tech_stack: Dict[str, str] = None,
    ) -> AccessibilityAuditResult:
        """
        Run a complete accessibility audit.

        Executes all phases:
        1. Intake - Create audit plan
        2. Discovery - Scan and manual testing
        3. Verification - AT verification and enrichment
        4. Report Packaging - Final QA and report

        Args:
            audit_request: The audit request with targets and constraints
            tech_stack: Optional tech stack info for remediation guidance

        Returns:
            AccessibilityAuditResult with all findings and report
        """
        tech_stack = tech_stack or {"web": "other", "mobile": "other"}

        # Initialize result
        result = AccessibilityAuditResult(
            audit_id=audit_request.audit_id or "audit_temp",
            current_phase=Phase.INTAKE,
        )

        try:
            # Phase 0: Intake
            logger.info(f"Starting intake phase for audit")
            result.current_phase = Phase.INTAKE

            intake_result = await run_intake_phase(
                audit_request=audit_request,
                llm_client=self.llm_client,
            )

            if not intake_result.success:
                result.failure_reason = intake_result.error or "Intake failed"
                return result

            result.intake_result = intake_result
            result.audit_id = intake_result.audit_plan.audit_id
            result.completed_phases.append(Phase.INTAKE)

            # Store audit
            self._audits[result.audit_id] = result

            # Phase 1: Discovery
            logger.info(f"Starting discovery phase for audit {result.audit_id}")
            result.current_phase = Phase.DISCOVERY

            discovery_result = await run_discovery_phase(
                audit_plan=intake_result.audit_plan,
                llm_client=self.llm_client,
            )

            if not discovery_result.success:
                result.failure_reason = discovery_result.error or "Discovery failed"
                return result

            result.discovery_result = discovery_result
            result.completed_phases.append(Phase.DISCOVERY)

            # Phase 2: Verification
            logger.info(f"Starting verification phase for audit {result.audit_id}")
            result.current_phase = Phase.VERIFICATION

            verification_result = await run_verification_phase(
                audit_id=result.audit_id,
                draft_findings=discovery_result.draft_findings,
                stack=tech_stack,
                llm_client=self.llm_client,
            )

            if not verification_result.success:
                result.failure_reason = verification_result.error or "Verification failed"
                return result

            result.verification_result = verification_result
            result.completed_phases.append(Phase.VERIFICATION)

            # Phase 3: Report Packaging
            logger.info(f"Starting report packaging phase for audit {result.audit_id}")
            result.current_phase = Phase.REPORT_PACKAGING

            report_result = await run_report_packaging_phase(
                audit_id=result.audit_id,
                verified_findings=verification_result.verified_findings,
                coverage_matrix=intake_result.coverage_matrix,
                llm_client=self.llm_client,
            )

            if not report_result.success:
                result.failure_reason = report_result.error or "Report packaging failed"
                return result

            result.report_packaging_result = report_result
            result.completed_phases.append(Phase.REPORT_PACKAGING)

            # Finalize result
            result.success = True
            result.final_findings = report_result.final_backlog
            result.final_patterns = report_result.patterns
            result.coverage_matrix = report_result.coverage_matrix

            # Count by severity
            result.total_findings = len(result.final_findings)
            result.critical_count = sum(
                1 for f in result.final_findings if f.severity == Severity.CRITICAL
            )
            result.high_count = sum(
                1 for f in result.final_findings if f.severity == Severity.HIGH
            )
            result.medium_count = sum(
                1 for f in result.final_findings if f.severity == Severity.MEDIUM
            )
            result.low_count = sum(
                1 for f in result.final_findings if f.severity == Severity.LOW
            )

            result.summary = (
                f"Audit complete. {result.total_findings} findings "
                f"({result.critical_count} critical, {result.high_count} high, "
                f"{result.medium_count} medium, {result.low_count} low). "
                f"{len(result.final_patterns)} patterns identified."
            )

            logger.info(f"Audit {result.audit_id} complete: {result.summary}")

            return result

        except Exception as e:
            logger.exception(f"Audit failed: {e}")
            result.success = False
            result.failure_reason = str(e)
            return result

    async def run_retest(
        self,
        audit_id: str,
        finding_ids: List[str] = None,
    ) -> AccessibilityAuditResult:
        """
        Run retest phase for specific findings or all findings.

        Args:
            audit_id: The audit identifier
            finding_ids: Optional list of specific finding IDs to retest

        Returns:
            Updated AccessibilityAuditResult
        """
        if audit_id not in self._audits:
            result = AccessibilityAuditResult(
                audit_id=audit_id,
                success=False,
                failure_reason=f"Audit {audit_id} not found",
            )
            return result

        result = self._audits[audit_id]

        # Get findings to retest
        if finding_ids:
            findings_to_retest = [
                f for f in result.final_findings if f.id in finding_ids
            ]
        else:
            findings_to_retest = result.final_findings

        if not findings_to_retest:
            result.summary = "No findings to retest"
            return result

        # Run retest phase
        logger.info(f"Starting retest phase for audit {audit_id}")
        result.current_phase = Phase.RETEST

        retest_result = await run_retest_phase(
            audit_id=audit_id,
            findings_to_retest=findings_to_retest,
            llm_client=self.llm_client,
        )

        result.retest_result = retest_result
        result.completed_phases.append(Phase.RETEST)

        # Update final findings
        if retest_result.updated_findings:
            finding_map = {f.id: f for f in retest_result.updated_findings}
            result.final_findings = [
                finding_map.get(f.id, f) for f in result.final_findings
            ]

        result.summary = (
            f"Retest complete. {retest_result.findings_closed} findings closed, "
            f"{retest_result.findings_still_open} still open."
        )

        return result

    def get_audit_status(self, audit_id: str) -> Dict[str, Any]:
        """
        Get the current status of an audit.

        Args:
            audit_id: The audit identifier

        Returns:
            Dict with audit status information
        """
        if audit_id not in self._audits:
            return {
                "audit_id": audit_id,
                "status": "not_found",
                "error": f"Audit {audit_id} not found",
            }

        result = self._audits[audit_id]

        return {
            "audit_id": audit_id,
            "status": "complete" if result.success else "in_progress",
            "current_phase": result.current_phase.value,
            "completed_phases": [p.value for p in result.completed_phases],
            "findings_count": result.total_findings,
            "critical_count": result.critical_count,
            "high_count": result.high_count,
            "medium_count": result.medium_count,
            "low_count": result.low_count,
            "patterns_count": len(result.final_patterns),
            "summary": result.summary,
            "error": result.failure_reason if not result.success else None,
        }

    def get_findings(
        self,
        audit_id: str,
        severity: Severity = None,
        state: str = None,
    ) -> List[Finding]:
        """
        Get findings for an audit with optional filters.

        Args:
            audit_id: The audit identifier
            severity: Optional severity filter
            state: Optional state filter

        Returns:
            List of findings matching the filters
        """
        if audit_id not in self._audits:
            return []

        findings = self._audits[audit_id].final_findings

        if severity:
            findings = [f for f in findings if f.severity == severity]

        if state:
            findings = [f for f in findings if f.state.value == state]

        return findings

    def get_patterns(self, audit_id: str) -> List[PatternCluster]:
        """
        Get pattern clusters for an audit.

        Args:
            audit_id: The audit identifier

        Returns:
            List of pattern clusters
        """
        if audit_id not in self._audits:
            return []

        return self._audits[audit_id].final_patterns


# Convenience functions for standalone usage


async def run_accessibility_audit(
    web_urls: List[str] = None,
    mobile_apps: List[Dict[str, str]] = None,
    critical_journeys: List[str] = None,
    audit_name: str = "",
    timebox_hours: int = None,
    auth_required: bool = False,
    max_pages: int = None,
    tech_stack: Dict[str, str] = None,
    llm_client: Optional[Any] = None,
) -> AccessibilityAuditResult:
    """
    Run an accessibility audit with simplified parameters.

    Args:
        web_urls: List of web URLs to audit
        mobile_apps: List of mobile app dicts with platform, name, version
        critical_journeys: List of critical user journeys
        audit_name: Human-readable audit name
        timebox_hours: Maximum hours for the audit
        auth_required: Whether authentication is required
        max_pages: Maximum pages to test
        tech_stack: Tech stack info for remediation guidance
        llm_client: Optional LLM client

    Returns:
        AccessibilityAuditResult
    """
    import uuid

    # Build mobile app targets
    mobile_app_targets = []
    if mobile_apps:
        for app in mobile_apps:
            mobile_app_targets.append(
                MobileAppTarget(
                    platform=app.get("platform", "ios"),
                    name=app.get("name", ""),
                    version=app.get("version", ""),
                    build=app.get("build", ""),
                )
            )

    # Create audit request
    audit_request = AuditRequest(
        audit_id=f"audit_{uuid.uuid4().hex[:8]}",
        name=audit_name,
        web_urls=web_urls or [],
        mobile_apps=mobile_app_targets,
        critical_journeys=critical_journeys or [],
        timebox_hours=timebox_hours,
        auth_required=auth_required,
        max_pages=max_pages,
        sampling_strategy="journey_based",
        wcag_levels=[WCAGLevel.A, WCAGLevel.AA],
    )

    # Run audit
    orchestrator = AccessibilityAuditOrchestrator(llm_client)
    return await orchestrator.run_audit(audit_request, tech_stack)
