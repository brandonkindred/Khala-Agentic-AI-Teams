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

import asyncio
import logging
from typing import Any, Dict, List, Optional

from .agents import (
    AccessibilityProgramLead,
    AssistiveTechSpecialist,
    EvidenceEngineer,
    MobileAccessibilitySpecialist,
    QAConsistencyReviewer,
    RemediationAdvisor,
    StandardsMappingSpecialist,
    WebAuditSpecialist,
)
from .agents.base import MessageBus
from .models import (
    AccessibilityAuditResult,
    AuditRequest,
    Finding,
    MobileAppTarget,
    PatternCluster,
    Phase,
    Severity,
    WCAGLevel,
)
from .phases import (
    run_discovery_phase,
    run_intake_phase,
    run_report_packaging_phase,
    run_retest_phase,
    run_verification_phase,
)

logger = logging.getLogger(__name__)


class AccessibilityAuditOrchestrator:
    """
    Top-level orchestrator for accessibility audits.

    Manages the full audit lifecycle through all phases and coordinates
    the specialist agents.
    """

    def __init__(self, llm_client: Optional[Any] = None, enable_addons: bool = False):
        """
        Initialize the orchestrator.

        Args:
            llm_client: Optional LLM client for agent processing
            enable_addons: Enable optional addon agents (ARM, ADSE, AET)
        """
        self.llm_client = llm_client
        self.enable_addons = enable_addons

        # Shared message bus for inter-agent communication
        self.message_bus = MessageBus()

        # Initialize all specialist agents with shared bus
        self.apl = AccessibilityProgramLead(llm_client, message_bus=self.message_bus)
        self.was = WebAuditSpecialist(llm_client, message_bus=self.message_bus)
        self.mas = MobileAccessibilitySpecialist(llm_client, message_bus=self.message_bus)
        self.ats = AssistiveTechSpecialist(llm_client, message_bus=self.message_bus)
        self.slms = StandardsMappingSpecialist(llm_client, message_bus=self.message_bus)
        self.ree = EvidenceEngineer(llm_client, message_bus=self.message_bus)
        self.ra = RemediationAdvisor(llm_client, message_bus=self.message_bus)
        self.qcr = QAConsistencyReviewer(llm_client, message_bus=self.message_bus)

        # Optional addon agents
        self.arm = None
        self.adse = None
        self.aet = None
        if enable_addons:
            from .addons.design_system_agent import AccessibleDesignSystemAgent
            from .addons.monitoring_agent import AccessibilityMonitoringAgent
            from .addons.training_agent import AccessibilityTrainingAgent

            self.arm = AccessibilityMonitoringAgent(llm_client)
            self.adse = AccessibleDesignSystemAgent(llm_client)
            self.aet = AccessibilityTrainingAgent(llm_client)

        # Store for in-progress audits (also persisted via artifact store)
        self._audits: Dict[str, AccessibilityAuditResult] = {}

    async def run_audit(
        self,
        audit_request: AuditRequest,
        tech_stack: Dict[str, str] = None,
    ) -> AccessibilityAuditResult:
        """
        Run a complete accessibility audit.

        Enforces ``timebox_hours`` from the audit request as an overall
        timeout.  State is persisted to the artifact store after each phase
        so progress survives restarts.
        """
        tech_stack = tech_stack or {"web": "other", "mobile": "other"}

        timeout_seconds: Optional[float] = None
        if audit_request.timebox_hours:
            timeout_seconds = audit_request.timebox_hours * 3600

        result = AccessibilityAuditResult(
            audit_id=audit_request.audit_id or "audit_temp",
            current_phase=Phase.INTAKE,
        )

        try:
            if timeout_seconds:
                result = await asyncio.wait_for(
                    self._run_audit_phases(audit_request, tech_stack, result),
                    timeout=timeout_seconds,
                )
            else:
                result = await self._run_audit_phases(audit_request, tech_stack, result)
        except asyncio.TimeoutError:
            logger.warning("Audit %s timed out after %s hours", result.audit_id, audit_request.timebox_hours)
            result.success = False
            result.failure_reason = (
                f"Audit timed out after {audit_request.timebox_hours} hour(s). "
                f"Completed phases: {[p.value for p in result.completed_phases]}"
            )
            await self._persist_audit(result)
        except Exception as e:
            logger.exception("Audit failed: %s", e)
            result.success = False
            result.failure_reason = str(e)
            await self._persist_audit(result)

        return result

    async def _run_audit_phases(
        self,
        audit_request: AuditRequest,
        tech_stack: Dict[str, str],
        result: AccessibilityAuditResult,
    ) -> AccessibilityAuditResult:
        """Execute all audit phases sequentially, persisting after each."""

        # Phase 0: Intake
        logger.info("Starting intake phase for audit")
        result.current_phase = Phase.INTAKE

        intake_result = await run_intake_phase(
            audit_request=audit_request,
            llm_client=self.llm_client,
            message_bus=self.message_bus,
        )

        if not intake_result.success:
            result.failure_reason = intake_result.error or "Intake failed"
            return result

        result.intake_result = intake_result
        result.audit_id = intake_result.audit_plan.audit_id
        result.completed_phases.append(Phase.INTAKE)
        self._audits[result.audit_id] = result
        await self._persist_audit(result)

        # Phase 1: Discovery
        logger.info("Starting discovery phase for audit %s", result.audit_id)
        result.current_phase = Phase.DISCOVERY

        discovery_result = await run_discovery_phase(
            audit_plan=intake_result.audit_plan,
            llm_client=self.llm_client,
            message_bus=self.message_bus,
        )

        if not discovery_result.success:
            result.failure_reason = discovery_result.error or "Discovery failed"
            return result

        result.discovery_result = discovery_result
        result.completed_phases.append(Phase.DISCOVERY)
        await self._persist_audit(result)

        # Phase 2: Verification
        logger.info("Starting verification phase for audit %s", result.audit_id)
        result.current_phase = Phase.VERIFICATION

        verification_result = await run_verification_phase(
            audit_id=result.audit_id,
            draft_findings=discovery_result.draft_findings,
            stack=tech_stack,
            llm_client=self.llm_client,
            message_bus=self.message_bus,
        )

        if not verification_result.success:
            result.failure_reason = verification_result.error or "Verification failed"
            return result

        result.verification_result = verification_result
        result.completed_phases.append(Phase.VERIFICATION)
        await self._persist_audit(result)

        # Phase 3: Report Packaging
        logger.info("Starting report packaging phase for audit %s", result.audit_id)
        result.current_phase = Phase.REPORT_PACKAGING

        report_result = await run_report_packaging_phase(
            audit_id=result.audit_id,
            verified_findings=verification_result.verified_findings,
            coverage_matrix=intake_result.coverage_matrix,
            llm_client=self.llm_client,
            message_bus=self.message_bus,
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
        result.high_count = sum(1 for f in result.final_findings if f.severity == Severity.HIGH)
        result.medium_count = sum(
            1 for f in result.final_findings if f.severity == Severity.MEDIUM
        )
        result.low_count = sum(1 for f in result.final_findings if f.severity == Severity.LOW)

        result.summary = (
            f"Audit complete. {result.total_findings} findings "
            f"({result.critical_count} critical, {result.high_count} high, "
            f"{result.medium_count} medium, {result.low_count} low). "
            f"{len(result.final_patterns)} patterns identified."
        )

        logger.info("Audit %s complete: %s", result.audit_id, result.summary)

        # Run optional addon agents after successful audit
        if self.enable_addons:
            await self._run_addons(result)

        await self._persist_audit(result)

        return result

    async def _run_addons(self, result: AccessibilityAuditResult) -> None:
        """Run optional addon agents after a successful audit."""
        audit_id = result.audit_id
        try:
            # ARM: Create monitoring baseline from final findings
            if self.arm and result.final_findings:
                web_targets = [
                    {"url": f.target, "journey": "audit"}
                    for f in result.final_findings
                    if f.surface.value == "web"
                ]
                if web_targets:
                    await self.arm.create_baseline(
                        audit_id=audit_id,
                        env="prod",
                        targets=web_targets[:20],
                    )
                    logger.info("ARM created monitoring baseline for audit %s", audit_id)

            # AET: Generate training modules from patterns
            if self.aet and result.final_patterns:
                await self.aet.mine_patterns(
                    audit_id=audit_id,
                    patterns=result.final_patterns,
                )
                logger.info("AET generated training modules for audit %s", audit_id)

        except Exception as e:
            logger.warning("Addon execution failed (non-fatal): %s", e)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    async def _persist_audit(self, result: AccessibilityAuditResult) -> None:
        """Persist audit state to the artifact store for crash recovery."""
        try:
            from .artifact_store import (
                ArtifactMetadata,
                ArtifactType,
                RetentionPolicy,
                get_artifact_store,
            )

            store = get_artifact_store()
            ref = f"audit_state_{result.audit_id}"
            content = result.model_dump_json().encode()
            metadata = ArtifactMetadata(
                artifact_ref=ref,
                artifact_type=ArtifactType.AUDIT_STATE,
                audit_id=result.audit_id,
                mime_type="application/json",
                retention_policy=RetentionPolicy.STANDARD,
            )
            await store.backend.store(ref, content, metadata)
        except Exception as e:
            logger.warning("Failed to persist audit state: %s", e)

    async def _load_audit(self, audit_id: str) -> Optional[AccessibilityAuditResult]:
        """Load audit state from the artifact store."""
        try:
            from .artifact_store import get_artifact_store

            store = get_artifact_store()
            ref = f"audit_state_{audit_id}"
            content = await store.retrieve(ref)
            if content:
                return AccessibilityAuditResult.model_validate_json(content)
        except Exception as e:
            logger.warning("Failed to load audit state: %s", e)
        return None

    async def _ensure_loaded(self, audit_id: str) -> Optional[AccessibilityAuditResult]:
        """Return the audit from cache or load from persistent store."""
        if audit_id in self._audits:
            return self._audits[audit_id]
        loaded = await self._load_audit(audit_id)
        if loaded:
            self._audits[audit_id] = loaded
        return loaded

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
        result = await self._ensure_loaded(audit_id)
        if not result:
            return AccessibilityAuditResult(
                audit_id=audit_id,
                success=False,
                failure_reason=f"Audit {audit_id} not found",
            )

        # Get findings to retest
        if finding_ids:
            findings_to_retest = [f for f in result.final_findings if f.id in finding_ids]
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
            message_bus=self.message_bus,
        )

        result.retest_result = retest_result
        result.completed_phases.append(Phase.RETEST)

        # Update final findings
        if retest_result.updated_findings:
            finding_map = {f.id: f for f in retest_result.updated_findings}
            result.final_findings = [finding_map.get(f.id, f) for f in result.final_findings]

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
