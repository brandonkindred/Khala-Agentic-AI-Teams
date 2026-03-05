"""
Assistive Technology Specialist (ATS)

Owns: AT verification as the "truth layer"
Outputs: AT-verified impact statements and AT evidence notes
"""

from typing import Any, Dict, List, Optional

from .base import AgentMessage, BaseSpecialistAgent
from ..models import (
    Finding,
    FindingState,
    Phase,
    WCAGMapping,
)
from ..tools.at import run_script
from ..tools.at.run_script import RunScriptInput, ATScript, TargetInfo


class AssistiveTechSpecialist(BaseSpecialistAgent):
    """
    Assistive Technology Specialist (ATS).

    The ATS is the "truth layer" that validates findings with real AT testing:
    - Validate what's announced (name/role/value)
    - Validate navigation patterns (headings/landmarks/rotor)
    - Validate form mode behavior and error messaging
    - Detect false positives from automated tools
    - Provide user-impact narratives that reflect real usage

    Automated scans are SIGNALS ONLY. The ATS confirms or rejects them.
    """

    agent_code = "ATS"
    agent_name = "Assistive Technology Specialist"
    description = "AT verification as the truth layer"

    async def process(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process an ATS task based on the current phase.

        Phases handled:
        - VERIFICATION: Verify findings with AT testing
        """
        phase = context.get("phase", Phase.VERIFICATION)
        audit_id = context.get("audit_id", "")

        if phase == Phase.VERIFICATION:
            return await self._handle_verification(context)
        else:
            return {"success": False, "error": f"ATS does not handle phase {phase}"}

    async def _handle_verification(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Verify findings with AT testing.
        """
        audit_id = context.get("audit_id", "")
        findings: List[Finding] = context.get("findings", [])

        if not findings:
            return {"success": True, "verified_findings": [], "rejected_findings": []}

        verified_findings: List[Finding] = []
        rejected_finding_ids: List[str] = []

        for finding in findings:
            # Run AT verification script
            surface = finding.surface.value
            tool = self._get_tool_for_surface(surface)

            script = ATScript(
                name=f"verify_{finding.issue_type.value}",
                steps=self._generate_verification_steps(finding),
            )

            target = TargetInfo(url=finding.target if surface == "web" else None)

            script_input = RunScriptInput(
                audit_id=audit_id,
                surface=surface,
                tool=tool,
                script=script,
                target=target,
                capture={"notes": True, "video": False},
            )

            result = await run_script(script_input)

            # Analyze results
            if result.passed_steps > result.failed_steps:
                # Finding verified
                finding.state = FindingState.VERIFIED
                finding.confidence = min(0.95, finding.confidence + 0.2)
                finding.verified_by = "ATS"

                # Add AT-specific user impact
                finding.user_impact = self._refine_user_impact(
                    finding.user_impact, tool, result.summary
                )

                verified_findings.append(finding)

                # Notify SLMS to confirm standards mapping
                self.send_message(
                    AgentMessage(
                        from_agent="ATS",
                        to_agent="SLMS",
                        message_type="confirm_mapping",
                        audit_id=audit_id,
                        payload={"finding_id": finding.id},
                    )
                )
            else:
                # Finding not verified - likely false positive
                rejected_finding_ids.append(finding.id)

        return {
            "success": True,
            "phase": Phase.VERIFICATION,
            "verified_findings": verified_findings,
            "rejected_finding_ids": rejected_finding_ids,
            "verification_rate": len(verified_findings) / len(findings) if findings else 0,
        }

    def _get_tool_for_surface(self, surface: str) -> str:
        """Get the appropriate AT tool for a surface."""
        tool_map = {
            "web": "nvda",
            "ios": "voiceover",
            "android": "talkback",
        }
        return tool_map.get(surface, "nvda")

    def _generate_verification_steps(self, finding: Finding) -> List[str]:
        """Generate AT verification steps based on issue type."""
        issue_type = finding.issue_type.value

        base_steps = [
            f"Navigate to {finding.target}",
            "Enable screen reader",
        ]

        type_specific_steps = {
            "name_role_value": [
                "Navigate to the affected element",
                "Listen to announcement",
                "Verify name, role, and value are announced correctly",
            ],
            "keyboard": [
                "Attempt to tab to the affected element",
                "Attempt to activate with Enter/Space",
                "Verify keyboard access is available",
            ],
            "focus": [
                "Tab to the affected element",
                "Verify focus indicator is perceivable",
                "Verify screen reader announces focus change",
            ],
            "forms": [
                "Navigate to form field",
                "Verify label is announced",
                "Enter invalid data and verify error is announced",
            ],
            "contrast": [
                "Navigate to affected content",
                "Verify content is perceivable with magnification",
            ],
            "structure": [
                "Use heading navigation",
                "Use landmark navigation",
                "Verify structure is navigable",
            ],
        }

        specific = type_specific_steps.get(issue_type, ["Verify the reported issue"])
        return base_steps + specific

    def _refine_user_impact(
        self, original_impact: str, tool: str, at_notes: str
    ) -> str:
        """Refine user impact statement with AT-specific details."""
        tool_name = {
            "nvda": "NVDA",
            "jaws": "JAWS",
            "voiceover": "VoiceOver",
            "talkback": "TalkBack",
        }.get(tool, tool.upper())

        return f"{original_impact} Verified with {tool_name}: {at_notes or 'Issue confirmed.'}"

    async def run_custom_script(
        self,
        audit_id: str,
        surface: str,
        tool: str,
        script: ATScript,
        target: str,
    ) -> Dict[str, Any]:
        """
        Run a custom AT verification script.

        Used for ad-hoc verification outside the normal workflow.
        """
        target_info = TargetInfo(
            url=target if surface == "web" else None,
            screen=target if surface != "web" else None,
        )

        script_input = RunScriptInput(
            audit_id=audit_id,
            surface=surface,
            tool=tool,
            script=script,
            target=target_info,
            capture={"notes": True, "video": True, "audio": True},
        )

        result = await run_script(script_input)

        return {
            "script_name": result.script_name,
            "tool": result.tool,
            "total_steps": result.total_steps,
            "passed_steps": result.passed_steps,
            "failed_steps": result.failed_steps,
            "summary": result.summary,
            "step_results": [s.model_dump() for s in result.step_results],
        }
