"""
QA & Consistency Reviewer (QCR)

Owns: Quality bar enforcement + dedupe + consistency
Outputs: Approved backlog + report readiness
"""

from typing import Any, Dict, List

from ..models import Finding, FindingState, Phase, Severity
from ..tools.qa import cluster_patterns, validate_finding
from ..tools.qa.cluster_patterns import ClusterPatternsInput
from ..tools.qa.validate_finding import ValidateFindingInput
from .base import AgentMessage, BaseSpecialistAgent


class QAConsistencyReviewer(BaseSpecialistAgent):
    """
    QA & Consistency Reviewer (QCR).

    The QCR enforces quality standards:
    - Reject findings that lack evidence or reproducibility
    - Normalize severity across similar issues
    - Dedupe into patterns (systemic grouping)
    - Spot-check findings vs evidence
    - Ensure report language is consistent and non-hyped

    QCR is the gatekeeper between Lane A (Coverage) and Lane B (Credibility).
    """

    agent_code = "QCR"
    agent_name = "QA & Consistency Reviewer"
    description = "Quality bar enforcement + dedupe + consistency"

    async def process(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process a QCR task based on the current phase.

        Phases handled:
        - DISCOVERY: Early dedupe and pattern identification
        - REPORT_PACKAGING: Final quality gate
        """
        phase = context.get("phase", Phase.REPORT_PACKAGING)
        context.get("audit_id", "")

        if phase == Phase.DISCOVERY:
            return await self._handle_early_qa(context)
        elif phase == Phase.REPORT_PACKAGING:
            return await self._handle_final_qa(context)
        else:
            return {"success": False, "error": f"QCR does not handle phase {phase}"}

    async def _handle_early_qa(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Early QA pass during discovery - dedupe and pattern identification.
        """
        audit_id = context.get("audit_id", "")
        findings: List[Finding] = context.get("findings", [])

        if not findings:
            return {"success": True, "patterns": [], "deduped_findings": []}

        # Cluster findings into patterns
        cluster_input = ClusterPatternsInput(
            audit_id=audit_id,
            findings=findings,
            cluster_by=["component", "issue_type"],
            min_cluster_size=2,
        )

        cluster_output = await cluster_patterns(cluster_input)

        # Update findings with pattern IDs
        pattern_map = {}
        for pattern in cluster_output.patterns:
            for finding_id in pattern.linked_finding_ids:
                pattern_map[finding_id] = pattern.pattern_id

        for finding in findings:
            if finding.id in pattern_map:
                finding.pattern_id = pattern_map[finding.id]

        return {
            "success": True,
            "phase": Phase.DISCOVERY,
            "patterns": cluster_output.patterns,
            "systemic_patterns": cluster_output.systemic_patterns,
            "deduped_findings": findings,
        }

    async def _handle_final_qa(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Final QA gate before report packaging.
        """
        audit_id = context.get("audit_id", "")
        findings: List[Finding] = context.get("findings", [])

        if not findings:
            return {"success": True, "approved_findings": [], "rejected_findings": []}

        approved_findings: List[Finding] = []
        rejected_findings: List[Finding] = []
        issues_by_finding: Dict[str, List[dict]] = {}

        for finding in findings:
            # Validate each finding
            validate_input = ValidateFindingInput(
                audit_id=audit_id,
                finding=finding,
                ruleset="strict",
            )

            validate_output = await validate_finding(validate_input)

            if validate_output.passed:
                # Normalize severity if needed
                if validate_output.normalized_severity:
                    finding.severity = validate_output.normalized_severity

                # Mark as ready for report
                finding.state = FindingState.READY_FOR_REPORT
                approved_findings.append(finding)
            else:
                # Collect issues for this finding
                issues_by_finding[finding.id] = [
                    issue.model_dump() for issue in validate_output.issues
                ]
                rejected_findings.append(finding)

        # Final clustering pass
        if approved_findings:
            cluster_input = ClusterPatternsInput(
                audit_id=audit_id,
                findings=approved_findings,
                cluster_by=["component", "issue_type", "wcag_sc"],
                min_cluster_size=2,
            )

            cluster_output = await cluster_patterns(cluster_input)
            patterns = cluster_output.patterns
        else:
            patterns = []

        # Normalize severity across patterns
        for pattern in patterns:
            pattern_findings = [
                f for f in approved_findings if f.id in pattern.linked_finding_ids
            ]
            if pattern_findings:
                # All findings in a pattern should have consistent severity
                pattern_severity = pattern.severity
                for f in pattern_findings:
                    if f.severity != pattern_severity:
                        # Log but don't override - just note inconsistency
                        pass

        # Notify APL that QA is complete
        self.send_message(
            AgentMessage(
                from_agent="QCR",
                to_agent="APL",
                message_type="qa_complete",
                audit_id=audit_id,
                payload={
                    "approved_count": len(approved_findings),
                    "rejected_count": len(rejected_findings),
                    "pattern_count": len(patterns),
                },
            )
        )

        return {
            "success": True,
            "phase": Phase.REPORT_PACKAGING,
            "approved_findings": approved_findings,
            "rejected_findings": rejected_findings,
            "issues_by_finding": issues_by_finding,
            "patterns": patterns,
        }

    async def spot_check_finding(
        self,
        finding: Finding,
    ) -> Dict[str, Any]:
        """
        Perform a spot check on a finding.

        Used for random QA sampling.
        """
        checks = []

        # Check evidence exists
        if finding.evidence_pack_ref:
            checks.append({"check": "evidence_exists", "passed": True})
        else:
            checks.append({
                "check": "evidence_exists",
                "passed": False,
                "issue": "No evidence pack attached",
            })

        # Check repro steps
        if finding.repro_steps and len(finding.repro_steps) >= 2:
            checks.append({"check": "repro_steps_adequate", "passed": True})
        else:
            checks.append({
                "check": "repro_steps_adequate",
                "passed": False,
                "issue": "Repro steps missing or too brief",
            })

        # Check user impact
        if finding.user_impact and len(finding.user_impact) > 20:
            checks.append({"check": "user_impact_described", "passed": True})
        else:
            checks.append({
                "check": "user_impact_described",
                "passed": False,
                "issue": "User impact not adequately described",
            })

        # Check WCAG mapping
        if finding.wcag_mappings:
            high_confidence = any(m.confidence >= 0.7 for m in finding.wcag_mappings)
            checks.append({
                "check": "wcag_mapping_confident",
                "passed": high_confidence,
                "issue": None if high_confidence else "Low confidence in WCAG mapping",
            })
        else:
            checks.append({
                "check": "wcag_mapping_confident",
                "passed": False,
                "issue": "No WCAG mapping",
            })

        # Check acceptance criteria
        if finding.acceptance_criteria and len(finding.acceptance_criteria) >= 1:
            checks.append({"check": "acceptance_criteria", "passed": True})
        else:
            checks.append({
                "check": "acceptance_criteria",
                "passed": False,
                "issue": "No acceptance criteria",
            })

        all_passed = all(c["passed"] for c in checks)

        return {
            "finding_id": finding.id,
            "passed": all_passed,
            "checks": checks,
            "ready_for_report": all_passed and finding.confidence >= 0.6,
        }

    def normalize_severity(
        self,
        findings: List[Finding],
    ) -> List[Finding]:
        """
        Normalize severity across findings for consistency.

        Same pattern → same severity unless strong reason.
        """
        # Group by pattern
        by_pattern: Dict[str, List[Finding]] = {}
        for f in findings:
            pattern_id = f.pattern_id or f.issue_type.value
            if pattern_id not in by_pattern:
                by_pattern[pattern_id] = []
            by_pattern[pattern_id].append(f)

        # Normalize within each pattern
        severity_order = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW]

        for pattern_id, pattern_findings in by_pattern.items():
            if len(pattern_findings) > 1:
                # Use most common severity in the pattern
                severity_counts: Dict[Severity, int] = {}
                for f in pattern_findings:
                    severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1

                most_common = max(severity_counts, key=severity_counts.get)

                for f in pattern_findings:
                    if f.severity != most_common:
                        # Only normalize down, not up
                        current_idx = severity_order.index(f.severity)
                        target_idx = severity_order.index(most_common)
                        if current_idx < target_idx:
                            # Would normalize down - keep original
                            pass
                        else:
                            f.severity = most_common

        return findings
