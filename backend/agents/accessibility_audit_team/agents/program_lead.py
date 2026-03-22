"""
Accessibility Program Lead (APL)

Owns: Scope, strategy, coverage, final report, risk posture
Outputs: AuditPlan, CoverageMatrix, FinalReport
"""

from typing import Any, Dict, List

from ..models import (
    AuditRequest,
    Finding,
    IntakeResult,
    PatternCluster,
    Phase,
    ReportPackagingResult,
    Severity,
)
from ..tools.audit import build_coverage_matrix, create_plan, export_backlog
from ..tools.audit.build_coverage_matrix import BuildCoverageMatrixInput
from ..tools.audit.create_plan import CreatePlanInput
from ..tools.audit.export_backlog import ExportBacklogInput
from .base import AgentMessage, BaseSpecialistAgent


class AccessibilityProgramLead(BaseSpecialistAgent):
    """
    Accessibility Program Lead (APL).

    The APL is the top-level coordinator for accessibility audits.
    Responsible for:
    - Defining test scope and critical journeys
    - Defining sampling strategy
    - Defining severity model and consistency rules
    - Running daily triage
    - Approving final report and backlog export

    The APL does NOT do hands-on testing - that's delegated to
    WAS (web) and MAS (mobile) specialists.
    """

    agent_code = "APL"
    agent_name = "Accessibility Program Lead"
    description = "Top-level coordinator owning scope, strategy, coverage, and final report"

    async def process(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process an APL task based on the current phase.

        Phases handled:
        - INTAKE: Create audit plan and coverage matrix
        - REPORT_PACKAGING: Produce final report and export backlog
        """
        phase = context.get("phase", Phase.INTAKE)
        context.get("audit_id", "")

        if phase == Phase.INTAKE:
            return await self._handle_intake(context)
        elif phase == Phase.REPORT_PACKAGING:
            return await self._handle_report_packaging(context)
        else:
            return {"success": False, "error": f"APL does not handle phase {phase}"}

    async def _handle_intake(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle the intake phase - create audit plan and coverage matrix.
        """
        audit_request: AuditRequest = context.get("audit_request")
        if not audit_request:
            return {"success": False, "error": "Missing audit_request in context"}

        audit_id = audit_request.audit_id or f"audit_{hash(str(audit_request)) % 100000}"

        # Create audit plan
        plan_input = CreatePlanInput(
            audit_id=audit_id,
            name=audit_request.name,
            targets={
                "web_urls": audit_request.web_urls,
                "mobile_apps": [app.model_dump() for app in audit_request.mobile_apps],
            },
            constraints={
                "timebox_hours": audit_request.timebox_hours,
                "environments": audit_request.environments,
                "auth_required": audit_request.auth_required,
            },
            critical_journeys=audit_request.critical_journeys,
            sampling={
                "max_pages": audit_request.max_pages,
                "strategy": audit_request.sampling_strategy,
            },
        )

        plan_output = await create_plan(plan_input)

        # Build coverage matrix
        surfaces = []
        if audit_request.web_urls:
            surfaces.append("web")
        for app in audit_request.mobile_apps:
            if app.platform not in surfaces:
                surfaces.append(app.platform)

        matrix_input = BuildCoverageMatrixInput(
            audit_id=audit_id,
            surfaces=surfaces or ["web"],
            journeys=audit_request.critical_journeys,
            wcag_version="2.2",
            focus_sc=[],
        )

        matrix_output = await build_coverage_matrix(matrix_input)

        # Update plan with matrix reference
        plan_output.audit_plan.coverage_matrix_ref = matrix_output.matrix_ref

        intake_result = IntakeResult(
            success=True,
            audit_plan=plan_output.audit_plan,
            coverage_matrix=matrix_output.coverage_matrix,
            test_run_config=plan_output.audit_plan.test_run_config,
            summary=f"Created audit plan for {len(audit_request.web_urls)} URLs and {len(audit_request.mobile_apps)} mobile apps. Coverage matrix has {matrix_output.sc_count} success criteria.",
        )

        # Notify WAS and MAS to begin discovery
        if audit_request.web_urls:
            self.send_message(
                AgentMessage(
                    from_agent="APL",
                    to_agent="WAS",
                    message_type="begin_discovery",
                    audit_id=audit_id,
                    payload={"urls": audit_request.web_urls},
                )
            )

        if audit_request.mobile_apps:
            self.send_message(
                AgentMessage(
                    from_agent="APL",
                    to_agent="MAS",
                    message_type="begin_discovery",
                    audit_id=audit_id,
                    payload={"apps": [app.model_dump() for app in audit_request.mobile_apps]},
                )
            )

        return {
            "success": True,
            "phase": Phase.INTAKE,
            "intake_result": intake_result,
        }

    async def _handle_report_packaging(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle report packaging phase - produce final report.
        """
        audit_id = context.get("audit_id", "")
        findings: List[Finding] = context.get("findings", [])
        patterns: List[PatternCluster] = context.get("patterns", [])

        # Export backlog
        export_input = ExportBacklogInput(
            audit_id=audit_id,
            findings=findings,
            patterns=patterns,
            format="json",
            include_evidence_refs=True,
            include_patterns=True,
        )

        export_output = await export_backlog(export_input)

        # Generate executive summary
        critical_count = sum(1 for f in findings if f.severity == Severity.CRITICAL)
        high_count = sum(1 for f in findings if f.severity == Severity.HIGH)
        medium_count = sum(1 for f in findings if f.severity == Severity.MEDIUM)
        low_count = sum(1 for f in findings if f.severity == Severity.LOW)

        exec_summary = f"""
## Executive Summary

### Findings Overview
- **Total Findings:** {len(findings)}
- **Critical:** {critical_count}
- **High:** {high_count}
- **Medium:** {medium_count}
- **Low:** {low_count}

### Patterns Identified
- **Total Patterns:** {len(patterns)}
- **Systemic Issues:** {sum(1 for p in patterns if p.scope.value == 'Systemic')}

### Recommended Roadmap
1. Address systemic patterns first (design system fixes)
2. Fix critical/high findings blocking core user journeys
3. Address medium/low findings in subsequent releases
"""

        # Generate roadmap
        roadmap = [
            "1. Fix systemic component accessibility issues",
            "2. Address critical findings blocking core journeys",
            "3. Fix high-impact issues affecting multiple areas",
            "4. Address remaining medium/low issues",
            "5. Implement regression prevention measures",
        ]

        result = ReportPackagingResult(
            success=True,
            final_backlog=findings,
            patterns=patterns,
            executive_summary=exec_summary,
            roadmap=roadmap,
            export_refs={"backlog": export_output.artifact_ref},
            summary=f"Packaged {len(findings)} findings and {len(patterns)} patterns for report.",
        )

        return {
            "success": True,
            "phase": Phase.REPORT_PACKAGING,
            "report_packaging_result": result,
        }

    async def run_triage(
        self,
        findings: List[Finding],
        patterns: List[PatternCluster],
    ) -> Dict[str, Any]:
        """
        Run triage on findings - decide what gets verified vs dropped.

        Returns prioritized list and any decisions made.
        """
        # Sort findings by severity and confidence
        severity_order = {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 2,
            Severity.LOW: 3,
        }

        sorted_findings = sorted(
            findings,
            key=lambda f: (severity_order.get(f.severity, 4), -f.confidence),
        )

        # Identify findings needing verification (low confidence)
        needs_verification = [f for f in sorted_findings if f.confidence < 0.6]

        # Identify ready for report
        ready_for_report = [
            f for f in sorted_findings
            if f.confidence >= 0.6 and f.evidence_pack_ref
        ]

        return {
            "prioritized_findings": sorted_findings,
            "needs_verification": needs_verification,
            "ready_for_report": ready_for_report,
            "patterns": patterns,
        }
