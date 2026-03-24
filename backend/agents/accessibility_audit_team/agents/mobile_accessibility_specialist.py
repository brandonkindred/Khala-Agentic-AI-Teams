"""
Mobile Accessibility Specialist (MAS)

Owns: iOS/Android testing, platform-specific issues, device repro steps
Outputs: Mobile findings with platform remediation and verification
"""

import uuid
from typing import Any, Dict, List

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
from ..tools.mobile import (
    check_font_scaling,
    check_touch_targets,
    record_screen_reader_flow,
)
from ..tools.mobile.check_font_scaling import CheckFontScalingInput
from ..tools.mobile.check_touch_targets import CheckTouchTargetsInput
from ..tools.mobile.record_screen_reader_flow import (
    AppInfo,
    DeviceInfo,
    RecordScreenReaderFlowInput,
)
from .base import AgentMessage, BaseSpecialistAgent


class MobileAccessibilitySpecialist(BaseSpecialistAgent):
    """
    Mobile Accessibility Specialist (MAS).

    The MAS handles all iOS and Android accessibility testing:
    - Screen reader focus order and element labeling
    - Touch target size and spacing
    - Dynamic type / font scaling
    - Color contrast and theming (including dark mode)
    - Orientation support and responsive layouts
    - Gesture alternatives (dragging, multi-pointer)
    - Error identification + instructions
    - Proper grouping/structure for screen readers
    """

    agent_code = "MAS"
    agent_name = "Mobile Accessibility Specialist"
    description = "iOS/Android testing, platform-specific issues, device repro steps"

    async def process(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a MAS task based on the current phase.

        Phases handled:
        - DISCOVERY: Test mobile apps, draft findings
        """
        phase = context.get("phase", Phase.DISCOVERY)
        context.get("audit_id", "")

        if phase == Phase.DISCOVERY:
            return await self._handle_discovery(context)
        else:
            return {"success": False, "error": f"MAS does not handle phase {phase}"}

    async def _handle_discovery(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle discovery phase - mobile app testing.
        """
        audit_id = context.get("audit_id", "")
        apps: List[Dict[str, Any]] = context.get("apps", [])

        if not apps:
            return {"success": False, "error": "No mobile apps provided for discovery"}

        all_findings: List[Finding] = []
        screens_tested = 0

        for app_data in apps:
            platform = app_data.get("platform", "ios")
            app_name = app_data.get("name", "")
            app_version = app_data.get("version", "")

            surface = Surface.IOS if platform == "ios" else Surface.ANDROID
            screen_reader = "voiceover" if platform == "ios" else "talkback"

            # Test screen reader flow
            sr_input = RecordScreenReaderFlowInput(
                audit_id=audit_id,
                platform=platform,
                device=DeviceInfo(
                    model="iPhone 15" if platform == "ios" else "Pixel 8",
                    os_version="iOS 17.2" if platform == "ios" else "Android 14",
                ),
                app=AppInfo(
                    name=app_name,
                    version=app_version,
                    build=app_data.get("build", ""),
                ),
                flow_steps=["Launch app", "Navigate main screen", "Test key interactions"],
                capture={"screen_recording": True},
                screen_reader=screen_reader,
            )
            sr_output = await record_screen_reader_flow(sr_input)

            # Create findings for failed announcements
            for announcement in sr_output.announcements:
                if not announcement.passed:
                    finding = self._create_finding(
                        audit_id=audit_id,
                        target=f"{app_name} - {announcement.element}",
                        surface=surface,
                        issue_type=IssueType.NAME_ROLE_VALUE,
                        severity=Severity.HIGH,
                        title="Screen reader announcement issue",
                        summary=f"Element '{announcement.element}' announces incorrectly",
                        expected=announcement.expected,
                        actual=announcement.announcement,
                        user_impact=f"{screen_reader.title()} users hear incorrect information",
                        wcag_scs=["4.1.2"],
                    )
                    all_findings.append(finding)

            # Create findings for missing labels
            for missing in sr_output.missing_labels:
                finding = self._create_finding(
                    audit_id=audit_id,
                    target=f"{app_name} - {missing}",
                    surface=surface,
                    issue_type=IssueType.NAME_ROLE_VALUE,
                    severity=Severity.CRITICAL,
                    title="Missing accessibility label",
                    summary=f"Element '{missing}' has no accessibility label",
                    expected="Accessible name that describes the element's purpose",
                    actual="No accessible name present",
                    user_impact=f"{screen_reader.title()} users cannot understand this element",
                    wcag_scs=["4.1.2", "1.1.1"],
                )
                all_findings.append(finding)

            # Check touch targets
            tt_input = CheckTouchTargetsInput(
                audit_id=audit_id,
                platform=platform,
                screen="main",
                min_target_px=24.0,
            )
            tt_output = await check_touch_targets(tt_input)

            for violation in tt_output.violations:
                finding = self._create_finding(
                    audit_id=audit_id,
                    target=f"{app_name} - {violation.element}",
                    surface=surface,
                    issue_type=IssueType.TARGET_SIZE,
                    severity=Severity.MEDIUM,
                    title="Touch target too small",
                    summary=f"Element has touch target of {violation.size_px}px, below minimum of {violation.required_px}px",
                    expected=f"Touch target of at least {violation.required_px}px",
                    actual=f"Touch target is {violation.size_px}px",
                    user_impact="Users with motor impairments may have difficulty tapping this element",
                    wcag_scs=["2.5.8"],
                )
                all_findings.append(finding)

            # Check font scaling
            fs_input = CheckFontScalingInput(
                audit_id=audit_id,
                platform=platform,
                scales=["default", "large", "largest"],
                screens=["main"],
                capture={"screenshots": True},
            )
            fs_output = await check_font_scaling(fs_input)

            for issue in fs_output.issues:
                finding = self._create_finding(
                    audit_id=audit_id,
                    target=f"{app_name} - {issue.screen}",
                    surface=surface,
                    issue_type=IssueType.RESIZING_REFLOW,
                    severity=Severity.HIGH,
                    title=f"Font scaling issue at {issue.scale}",
                    summary=issue.description,
                    expected="Text scales with system settings without loss of content",
                    actual=issue.description,
                    user_impact="Users who need larger text cannot use the app effectively",
                    wcag_scs=["1.4.4"],
                )
                all_findings.append(finding)

            screens_tested += 1

        # Notify REE to capture evidence
        self.send_message(
            AgentMessage(
                from_agent="MAS",
                to_agent="REE",
                message_type="capture_evidence",
                audit_id=audit_id,
                payload={"finding_ids": [f.id for f in all_findings]},
            )
        )

        # Notify ATS for high-impact findings
        high_impact = [f for f in all_findings if f.severity in [Severity.CRITICAL, Severity.HIGH]]
        if high_impact:
            self.send_message(
                AgentMessage(
                    from_agent="MAS",
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
            "apps_tested": len(apps),
            "screens_tested": screens_tested,
        }

    def _create_finding(
        self,
        audit_id: str,
        target: str,
        surface: Surface,
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
            surface=surface,
            target=target,
            issue_type=issue_type,
            severity=severity,
            scope=Scope.LOCALIZED,
            confidence=0.7,
            title=title,
            summary=summary,
            repro_steps=[],
            expected=expected,
            actual=actual,
            user_impact=user_impact,
            wcag_mappings=[
                WCAGMapping(sc=sc, name="", confidence=0.8, rationale="") for sc in wcag_scs
            ],
            created_by="MAS",
        )
