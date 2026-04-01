"""
Standards & Legal Mapping Specialist (SLMS)

Owns: Correct WCAG mapping + Section 508 tags
Outputs: Standards mapping with confidence + rationale
"""

from typing import Any, Dict, List

from ..models import Finding, FindingState, Phase, WCAGMapping
from ..tools.standards import map_wcag, tag_section508
from ..tools.standards.map_wcag import FindingInfo, MapWcagInput
from ..tools.standards.tag_section508 import TagSection508Input
from .base import AgentMessage, BaseSpecialistAgent


class StandardsMappingSpecialist(BaseSpecialistAgent):
    """
    Standards & Legal Mapping Specialist (SLMS).

    The SLMS ensures correct standards mappings:
    - Select correct WCAG 2.2 SC(s) per issue
    - Add confidence scores and reasoning
    - Flag dubious mappings and request more evidence if needed
    - Maintain consistent taxonomy
    - Apply Section 508 reporting tags

    The SLMS does NOT make legal claims - output is technical assessment only.
    """

    agent_code = "SLMS"
    agent_name = "Standards & Legal Mapping Specialist"
    description = "Correct WCAG mapping + Section 508 tags"

    async def process(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process an SLMS task based on the current phase.

        Phases handled:
        - VERIFICATION: Add/confirm standards mappings
        - INTAKE: Set mapping taxonomy and guardrails
        """
        phase = context.get("phase", Phase.VERIFICATION)

        if phase == Phase.VERIFICATION:
            return await self._handle_verification(context)
        elif phase == Phase.INTAKE:
            return await self._handle_intake(context)
        else:
            return {"success": False, "error": f"SLMS does not handle phase {phase}"}

    async def _handle_intake(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Set up mapping taxonomy and guardrails for the audit.
        """
        # Define mapping guardrails - common mis-maps to avoid
        guardrails = {
            "common_mismaps": [
                {
                    "symptom": "Poor color choice",
                    "wrong_sc": "1.4.1",
                    "correct_sc": "1.4.3 or 1.4.11",
                    "reason": "1.4.1 is about using color alone to convey info, not contrast",
                },
                {
                    "symptom": "Keyboard not working",
                    "wrong_sc": "2.4.7",
                    "correct_sc": "2.1.1",
                    "reason": "2.4.7 is about focus visibility, not keyboard operability",
                },
                {
                    "symptom": "No alt text",
                    "wrong_sc": "4.1.2",
                    "correct_sc": "1.1.1",
                    "reason": "1.1.1 covers non-text content; 4.1.2 is for UI component names",
                },
            ],
            "multi_sc_patterns": [
                {
                    "pattern": "Form field without label",
                    "scs": ["1.3.1", "3.3.2", "4.1.2"],
                    "note": "Usually all three apply",
                },
                {
                    "pattern": "Custom control not keyboard accessible",
                    "scs": ["2.1.1", "4.1.2"],
                    "note": "Both keyboard access and programmatic exposure",
                },
            ],
        }

        return {
            "success": True,
            "phase": Phase.INTAKE,
            "guardrails": guardrails,
        }

    async def _handle_verification(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add or confirm standards mappings for findings.
        """
        audit_id = context.get("audit_id", "")
        findings: List[Finding] = context.get("findings", [])

        if not findings:
            return {"success": True, "mapped_findings": []}

        mapped_findings: List[Finding] = []
        needs_review: List[str] = []

        for finding in findings:
            # Map to WCAG
            finding_info = FindingInfo(
                title=finding.title,
                summary=finding.summary,
                expected=finding.expected,
                actual=finding.actual,
                issue_type=finding.issue_type.value,
                surface=finding.surface.value,
            )

            map_input = MapWcagInput(audit_id=audit_id, finding=finding_info)
            map_output = await map_wcag(map_input)

            # Update finding with mappings
            if map_output.candidates:
                finding.wcag_mappings = [
                    WCAGMapping(
                        sc=c.sc,
                        name=c.name,
                        confidence=c.confidence,
                        rationale=c.rationale,
                    )
                    for c in map_output.candidates
                ]

            # Add Section 508 tags
            if map_output.recommended:
                tag_input = TagSection508Input(
                    audit_id=audit_id,
                    wcag_scs=map_output.recommended,
                    surface=finding.surface.value,
                )
                tag_output = await tag_section508(tag_input)
                finding.section_508_tags = tag_output.tags

            # Check if needs review
            if map_output.needs_verification:
                needs_review.append(finding.id)
            else:
                # Mark as ready for remediation if verified
                if finding.state == FindingState.VERIFIED:
                    finding.state = FindingState.READY_FOR_REPORT

            mapped_findings.append(finding)

        # Notify RA to add remediation guidance
        verified = [f for f in mapped_findings if f.state == FindingState.VERIFIED]
        if verified:
            self.send_message(
                AgentMessage(
                    from_agent="SLMS",
                    to_agent="RA",
                    message_type="add_remediation",
                    audit_id=audit_id,
                    payload={"finding_ids": [f.id for f in verified]},
                )
            )

        return {
            "success": True,
            "phase": Phase.VERIFICATION,
            "mapped_findings": mapped_findings,
            "needs_review": needs_review,
        }

    async def validate_mapping(
        self,
        finding: Finding,
        proposed_scs: List[str],
    ) -> Dict[str, Any]:
        """
        Validate a proposed WCAG mapping.

        Returns analysis of whether the mapping is correct.
        """
        finding_info = FindingInfo(
            title=finding.title,
            summary=finding.summary,
            expected=finding.expected,
            actual=finding.actual,
            issue_type=finding.issue_type.value,
            surface=finding.surface.value,
        )

        map_input = MapWcagInput(audit_id="", finding=finding_info)
        map_output = await map_wcag(map_input)

        recommended = set(map_output.recommended)
        proposed = set(proposed_scs)

        matching = recommended.intersection(proposed)
        missing = recommended - proposed
        extra = proposed - recommended

        return {
            "valid": len(matching) > 0 and len(extra) == 0,
            "matching": list(matching),
            "missing": list(missing),
            "extra": list(extra),
            "recommendation": map_output.recommended,
            "candidates": [c.model_dump() for c in map_output.candidates],
        }
