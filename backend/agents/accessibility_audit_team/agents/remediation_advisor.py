"""
Remediation Advisor (RA)

Owns: Fix guidance + acceptance criteria + verification plan
Outputs: Developer-ready remediation guidance
"""

from typing import Any, Dict, List

from ..models import Finding, Phase
from ..tools.remediation import generate_regression_checks, suggest_fix
from ..tools.remediation.generate_regression_checks import (
    GenerateRegressionChecksInput,
)
from ..tools.remediation.suggest_fix import (
    FindingInput,
    StackInfo,
    SuggestFixInput,
)
from .base import AgentMessage, BaseSpecialistAgent


class RemediationAdvisor(BaseSpecialistAgent):
    """
    Remediation Advisor (RA).

    The RA provides developer-ready guidance:
    - "Fix recipes" tied to common component patterns
    - Acceptance criteria that is testable
    - Verification steps (manual + automated)
    - Regression prevention suggestions (component contracts, tests)

    Findings without acceptance criteria are NOT complete.
    """

    agent_code = "RA"
    agent_name = "Remediation Advisor"
    description = "Fix guidance + acceptance criteria + verification plan"

    async def process(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process an RA task based on the current phase.

        Phases handled:
        - VERIFICATION: Add remediation guidance to verified findings
        """
        phase = context.get("phase", Phase.VERIFICATION)
        context.get("audit_id", "")

        if phase == Phase.VERIFICATION:
            return await self._handle_remediation(context)
        else:
            return {"success": False, "error": f"RA does not handle phase {phase}"}

    async def _handle_remediation(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add remediation guidance to findings.
        """
        audit_id = context.get("audit_id", "")
        findings: List[Finding] = context.get("findings", [])
        stack: Dict[str, str] = context.get("stack", {})

        if not findings:
            return {"success": True, "remediated_findings": []}

        remediated_findings: List[Finding] = []
        regression_tests_generated = 0

        for finding in findings:
            # Generate fix suggestion
            finding_input = FindingInput(
                issue_type=finding.issue_type.value,
                surface=finding.surface.value,
                component=finding.component_id or "",
                summary=finding.summary,
                evidence_pack_ref=finding.evidence_pack_ref or "",
            )

            stack_info = StackInfo(
                web=stack.get("web", "other"),
                mobile=stack.get("mobile", "other"),
            )

            fix_input = SuggestFixInput(
                audit_id=audit_id,
                finding=finding_input,
                stack=stack_info,
            )

            fix_output = await suggest_fix(fix_input)

            # Update finding with remediation
            finding.root_cause_hypothesis = fix_output.root_cause_hypothesis
            finding.recommended_fix = fix_output.recommended_fix
            finding.acceptance_criteria = fix_output.acceptance_criteria
            finding.test_plan = fix_output.test_plan

            if fix_output.code_examples_ref:
                finding.code_examples_ref = fix_output.code_examples_ref

            # Generate regression checks for systemic issues
            if finding.pattern_id or finding.scope.value == "Systemic":
                regression_input = GenerateRegressionChecksInput(
                    audit_id=audit_id,
                    component=finding.component_id or "general",
                    issue_types=[finding.issue_type.value],
                    preferred_runner="playwright",
                    generate_scripts=True,
                )

                regression_output = await generate_regression_checks(regression_input)
                regression_tests_generated += regression_output.total_tests

            remediated_findings.append(finding)

        # Notify QCR to validate findings
        self.send_message(
            AgentMessage(
                from_agent="RA",
                to_agent="QCR",
                message_type="validate_findings",
                audit_id=audit_id,
                payload={"finding_ids": [f.id for f in remediated_findings]},
            )
        )

        return {
            "success": True,
            "phase": Phase.VERIFICATION,
            "remediated_findings": remediated_findings,
            "regression_tests_generated": regression_tests_generated,
        }

    async def generate_fix_for_pattern(
        self,
        audit_id: str,
        pattern_name: str,
        issue_type: str,
        component: str,
        stack: Dict[str, str],
    ) -> Dict[str, Any]:
        """
        Generate a comprehensive fix guide for a pattern.

        Used for systemic issues that affect multiple findings.
        """
        finding_input = FindingInput(
            issue_type=issue_type,
            surface="web",
            component=component,
            summary=f"Pattern: {pattern_name}",
            evidence_pack_ref="",
        )

        stack_info = StackInfo(
            web=stack.get("web", "other"),
            mobile=stack.get("mobile", "other"),
        )

        fix_input = SuggestFixInput(
            audit_id=audit_id,
            finding=finding_input,
            stack=stack_info,
        )

        fix_output = await suggest_fix(fix_input)

        # Generate regression tests
        regression_input = GenerateRegressionChecksInput(
            audit_id=audit_id,
            component=component,
            issue_types=[issue_type],
            preferred_runner="playwright",
            generate_scripts=True,
        )

        regression_output = await generate_regression_checks(regression_input)

        return {
            "pattern_name": pattern_name,
            "root_cause": fix_output.root_cause_hypothesis,
            "recommended_fix": fix_output.recommended_fix,
            "acceptance_criteria": fix_output.acceptance_criteria,
            "test_plan": fix_output.test_plan,
            "regression_tests": [t.model_dump() for t in regression_output.tests],
            "regression_prevention": fix_output.regression_prevention,
        }
