"""
Web Audit Specialist (WAS)

Owns: Manual web testing + scan orchestration + web-specific evidence
Outputs: Web findings with proof and fix guidance drafts
"""

from typing import Any, Dict, List, Optional
import uuid

from .base import AgentMessage, BaseSpecialistAgent
from ..models import (
    Finding,
    FindingState,
    IssueType,
    Phase,
    Scope,
    Severity,
    Surface,
    WCAGMapping,
)
from ..tools.web import (
    run_automated_scan,
    record_keyboard_flow,
    capture_dom_snapshot,
    check_reflow_zoom,
    compute_contrast_and_focus,
)
from ..tools.web.run_automated_scan import RunAutomatedScanInput
from ..tools.web.record_keyboard_flow import RecordKeyboardFlowInput, KeyboardAction
from ..tools.web.capture_dom_snapshot import CaptureDomSnapshotInput
from ..tools.web.check_reflow_zoom import CheckReflowZoomInput
from ..tools.web.compute_contrast_and_focus import ComputeContrastAndFocusInput


class WebAuditSpecialist(BaseSpecialistAgent):
    """
    Web Audit Specialist (WAS).

    The WAS handles all web-specific accessibility testing:
    - Keyboard navigation and focus order
    - Focus visibility and focus appearance (WCAG 2.4.11)
    - Semantics: headings, landmarks, lists, tables
    - Forms: labels, required indicators, error messages
    - Modals/drawers: focus trap, return focus, background inert
    - SPA routing: title updates, focus management, announcements
    - Resize/reflow: 320 CSS px; zoom to 200%/400%
    - Pointer/drag gestures: alternatives (WCAG 2.5.7)
    - Target size: minimum (WCAG 2.5.8)

    The WAS runs automated scans as SIGNALS ONLY and performs manual
    verification before drafting findings.
    """

    agent_code = "WAS"
    agent_name = "Web Audit Specialist"
    description = "Manual web testing + scan orchestration + web-specific evidence"

    async def process(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a WAS task based on the current phase.

        Phases handled:
        - DISCOVERY: Run scans, perform manual sweep, draft findings
        """
        phase = context.get("phase", Phase.DISCOVERY)
        audit_id = context.get("audit_id", "")

        if phase == Phase.DISCOVERY:
            return await self._handle_discovery(context)
        else:
            return {"success": False, "error": f"WAS does not handle phase {phase}"}

    async def _handle_discovery(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle discovery phase - scan and manual testing.
        """
        audit_id = context.get("audit_id", "")
        urls: List[str] = context.get("urls", [])

        if not urls:
            return {"success": False, "error": "No URLs provided for discovery"}

        all_findings: List[Finding] = []
        scan_results = []

        for url in urls:
            # Run automated scans
            scan_input = RunAutomatedScanInput(
                audit_id=audit_id,
                url=url,
                browser="chromium",
                viewport={"width": 1920, "height": 1080},
                tools=["axe"],
            )
            scan_output = await run_automated_scan(scan_input)
            scan_results.append(scan_output)

            # Record keyboard flow
            keyboard_input = RecordKeyboardFlowInput(
                audit_id=audit_id,
                start_url=url,
                actions=[
                    KeyboardAction(type="key", value="Tab"),
                    KeyboardAction(type="key", value="Tab"),
                    KeyboardAction(type="key", value="Tab"),
                ],
                capture={"video": False, "screenshots": True},
            )
            keyboard_output = await record_keyboard_flow(keyboard_input)

            # Check keyboard issues
            if keyboard_output.keyboard_traps_found:
                for trap in keyboard_output.keyboard_traps_found:
                    finding = self._create_finding(
                        audit_id=audit_id,
                        target=url,
                        issue_type=IssueType.KEYBOARD,
                        severity=Severity.CRITICAL,
                        title=f"Keyboard trap detected",
                        summary=f"Keyboard focus gets trapped at element: {trap}",
                        expected="Keyboard focus should be able to move away from any focusable element",
                        actual=f"Keyboard focus cannot escape from {trap}",
                        user_impact="Keyboard users cannot navigate past this element, blocking access to content",
                        wcag_scs=["2.1.2"],
                    )
                    all_findings.append(finding)

            # Check for focus visibility issues
            for step in keyboard_output.focus_trace:
                if not step.visible_focus:
                    finding = self._create_finding(
                        audit_id=audit_id,
                        target=url,
                        issue_type=IssueType.FOCUS,
                        severity=Severity.HIGH,
                        title=f"Focus indicator not visible",
                        summary=f"Element {step.selector} does not have a visible focus indicator",
                        expected="A visible focus indicator when element receives keyboard focus",
                        actual="No visible focus indicator present",
                        user_impact="Keyboard users cannot see which element has focus",
                        wcag_scs=["2.4.7"],
                    )
                    all_findings.append(finding)

            # Check reflow
            reflow_input = CheckReflowZoomInput(
                audit_id=audit_id,
                url=url,
                capture={"screenshots": True},
            )
            reflow_output = await check_reflow_zoom(reflow_input)

            for issue in reflow_output.issues:
                finding = self._create_finding(
                    audit_id=audit_id,
                    target=url,
                    issue_type=IssueType.RESIZING_REFLOW,
                    severity=Severity.HIGH,
                    title=f"Reflow issue at {issue.mode}",
                    summary=issue.description,
                    expected="Content reflows without horizontal scrolling at 320px width",
                    actual=issue.description,
                    user_impact="Users with low vision who zoom or use narrow viewports cannot access content",
                    wcag_scs=["1.4.10"],
                )
                all_findings.append(finding)

            # Check contrast
            contrast_input = ComputeContrastAndFocusInput(
                audit_id=audit_id,
                url=url,
                elements=[],
                test_all_focusable=True,
            )
            contrast_output = await compute_contrast_and_focus(contrast_input)

            for result in contrast_output.contrast_results:
                if not result.meets_aa_normal and not result.is_large_text:
                    finding = self._create_finding(
                        audit_id=audit_id,
                        target=url,
                        issue_type=IssueType.CONTRAST,
                        severity=Severity.HIGH,
                        title=f"Insufficient text contrast",
                        summary=f"Element {result.selector} has contrast ratio of {result.contrast_ratio:.2f}:1",
                        expected="Contrast ratio of at least 4.5:1 for normal text",
                        actual=f"Contrast ratio is {result.contrast_ratio:.2f}:1",
                        user_impact="Users with low vision may not be able to read this text",
                        wcag_scs=["1.4.3"],
                    )
                    all_findings.append(finding)

        # Notify REE to start evidence capture
        self.send_message(
            AgentMessage(
                from_agent="WAS",
                to_agent="REE",
                message_type="capture_evidence",
                audit_id=audit_id,
                payload={"finding_ids": [f.id for f in all_findings]},
            )
        )

        # Notify ATS for findings needing verification
        high_impact = [f for f in all_findings if f.severity in [Severity.CRITICAL, Severity.HIGH]]
        if high_impact:
            self.send_message(
                AgentMessage(
                    from_agent="WAS",
                    to_agent="ATS",
                    message_type="verify_findings",
                    audit_id=audit_id,
                    payload={"finding_ids": [f.id for f in high_impact]},
                )
            )

        return {
            "success": True,
            "phase": Phase.DISCOVERY,
            "findings": all_findings,
            "scan_results": scan_results,
            "urls_tested": len(urls),
        }

    def _create_finding(
        self,
        audit_id: str,
        target: str,
        issue_type: IssueType,
        severity: Severity,
        title: str,
        summary: str,
        expected: str,
        actual: str,
        user_impact: str,
        wcag_scs: List[str],
    ) -> Finding:
        """Create a draft finding."""
        return Finding(
            id=f"finding_{uuid.uuid4().hex[:8]}",
            state=FindingState.DRAFT,
            surface=Surface.WEB,
            target=target,
            issue_type=issue_type,
            severity=severity,
            scope=Scope.LOCALIZED,
            confidence=0.7,
            title=title,
            summary=summary,
            repro_steps=[],  # To be filled during verification
            expected=expected,
            actual=actual,
            user_impact=user_impact,
            wcag_mappings=[
                WCAGMapping(sc=sc, name="", confidence=0.8, rationale="")
                for sc in wcag_scs
            ],
            created_by="WAS",
        )
