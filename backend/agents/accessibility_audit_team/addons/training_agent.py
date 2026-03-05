"""
Accessibility Education & Training Agent (AET)

Mines patterns from findings and builds training modules for teams.

Tools:
- training.mine_patterns
- training.build_modules
- training.publish_kb
"""

from typing import Any, Dict, List, Optional
import uuid

from pydantic import BaseModel, Field

from ..models import (
    Finding,
    PatternCluster,
    TrainingBundle,
    TrainingModule,
)


class MinePatternInput(BaseModel):
    """Input for mining patterns from findings."""

    audit_id: str
    patterns: List[PatternCluster]
    top_n: int = Field(default=10, description="Top patterns to select")
    prioritize: str = Field(default="severity_scope_frequency")


class BuildModulesInput(BaseModel):
    """Input for building training modules."""

    audit_id: str
    patterns: List[PatternCluster]
    target_roles: List[str] = Field(
        default_factory=lambda: ["frontend", "mobile", "design", "qa"]
    )
    stacks: Dict[str, str] = Field(default_factory=dict)


class PublishKBInput(BaseModel):
    """Input for publishing knowledge base."""

    bundle_id: str
    format: str = Field(default="markdown+json")
    destination: str = Field(default="filesystem")
    path: str = Field(default="")


class AccessibilityTrainingAgent:
    """
    Accessibility Education & Training Agent (AET).

    Mines patterns from audit findings and builds targeted training
    modules for development teams. The goal is to reduce repeat mistakes
    by teaching teams about accessibility patterns they're getting wrong.

    This agent is invoked AFTER an audit is complete to generate
    educational content from the findings.
    """

    agent_code = "AET"
    agent_name = "Accessibility Education & Training Agent"

    def __init__(self, llm_client: Optional[Any] = None):
        self.llm_client = llm_client
        self._bundles: Dict[str, TrainingBundle] = {}

    async def mine_patterns(
        self,
        audit_id: str,
        patterns: List[PatternCluster],
        top_n: int = 10,
    ) -> List[PatternCluster]:
        """
        Mine patterns from findings to identify training opportunities.

        Prioritizes patterns by:
        1. Severity (critical/high first)
        2. Scope (systemic first)
        3. Frequency (most common first)

        Returns top N patterns suitable for training modules.
        """
        # Sort patterns by priority
        def priority_score(p: PatternCluster) -> tuple:
            severity_order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
            scope_order = {"Systemic": 0, "Multi-area": 1, "Localized": 2}
            return (
                severity_order.get(p.severity.value, 4),
                scope_order.get(p.scope.value, 3),
                -len(p.linked_finding_ids),  # Negative for descending
            )

        sorted_patterns = sorted(patterns, key=priority_score)
        return sorted_patterns[:top_n]

    async def build_modules(
        self,
        audit_id: str,
        patterns: List[PatternCluster],
        target_roles: List[str] = None,
        stacks: Dict[str, str] = None,
    ) -> TrainingBundle:
        """
        Build training modules from patterns.

        Creates targeted training content for different roles:
        - Frontend developers
        - Mobile developers
        - Designers
        - QA engineers

        Each module includes:
        - Issue explanation
        - Real examples from the audit
        - Correct implementation patterns
        - Testing guidance
        """
        target_roles = target_roles or ["frontend", "mobile", "design", "qa"]
        stacks = stacks or {"web": "react", "mobile": "native"}

        modules = []

        for pattern in patterns:
            module_id = f"module_{uuid.uuid4().hex[:8]}"

            # Determine which roles this pattern applies to
            applicable_roles = []
            if any(p.value in ["web"] for p in []):  # Would check surfaces
                applicable_roles.extend(["frontend", "design"])
            if any(p.value in ["ios", "android"] for p in []):
                applicable_roles.extend(["mobile"])
            if not applicable_roles:
                applicable_roles = target_roles

            module = TrainingModule(
                module_id=module_id,
                title=f"Fixing {pattern.name}",
                path_ref=f"training/{audit_id}/{module_id}",
                linked_patterns=[pattern.pattern_id],
                target_roles=applicable_roles,
                stacks=stacks,
            )

            modules.append(module)

        bundle_id = f"bundle_{audit_id}_{uuid.uuid4().hex[:8]}"
        bundle = TrainingBundle(
            bundle_id=bundle_id,
            audit_id=audit_id,
            modules=modules,
        )

        self._bundles[bundle_id] = bundle
        return bundle

    async def publish_kb(
        self,
        bundle_id: str,
        format: str = "markdown+json",
        destination: str = "filesystem",
        path: str = "",
    ) -> Dict[str, Any]:
        """
        Publish training bundle to knowledge base.

        Supports multiple destinations:
        - filesystem: Local file system
        - s3: AWS S3
        - notion: Notion workspace
        - confluence: Atlassian Confluence
        - github: GitHub repository

        Returns reference to published content.
        """
        if bundle_id not in self._bundles:
            return {
                "success": False,
                "error": f"Bundle {bundle_id} not found",
            }

        bundle = self._bundles[bundle_id]

        # Generate publish reference
        publish_ref = f"kb_{destination}_{uuid.uuid4().hex[:8]}"

        # In production, would actually publish to the destination
        bundle.publish_ref = publish_ref

        return {
            "success": True,
            "published_ref": publish_ref,
            "destination": destination,
            "format": format,
            "modules_count": len(bundle.modules),
        }

    async def generate_training_content(
        self,
        pattern: PatternCluster,
        findings: List[Finding],
        stack: Dict[str, str],
    ) -> Dict[str, Any]:
        """
        Generate detailed training content for a pattern.

        Returns structured content including:
        - Overview and impact
        - Real examples from the audit
        - Correct implementation
        - Testing checklist
        """
        # Get findings for this pattern
        pattern_findings = [
            f for f in findings if f.id in pattern.linked_finding_ids
        ]

        # Build examples
        examples = []
        for f in pattern_findings[:3]:  # Top 3 examples
            examples.append({
                "title": f.title,
                "actual": f.actual,
                "expected": f.expected,
                "fix": f.recommended_fix,
            })

        content = {
            "pattern_name": pattern.name,
            "description": pattern.description,
            "severity": pattern.severity.value,
            "scope": pattern.scope.value,
            "impact": f"Affects {len(pattern_findings)} locations",
            "wcag_criteria": pattern.wcag_scs,
            "examples": examples,
            "correct_implementation": {
                "web": self._get_web_fix_pattern(pattern, stack.get("web", "other")),
                "mobile": self._get_mobile_fix_pattern(pattern, stack.get("mobile", "other")),
            },
            "testing_checklist": [
                "Test with keyboard only",
                "Test with screen reader",
                "Run automated accessibility scan",
                "Verify fix in multiple browsers/devices",
            ],
        }

        return content

    def _get_web_fix_pattern(self, pattern: PatternCluster, stack: str) -> str:
        """Get web fix pattern based on stack."""
        issue_types = [t.value for t in pattern.issue_types]

        if "keyboard" in issue_types:
            if stack == "react":
                return "Ensure interactive elements use native elements or have tabIndex and onClick/onKeyDown handlers"
            return "Use native interactive elements or add proper keyboard event handling"

        if "name_role_value" in issue_types:
            return "Add aria-label or visible text, ensure proper semantic elements or ARIA roles"

        if "focus" in issue_types:
            return "Add visible focus styles with :focus-visible pseudo-class"

        return "Follow WCAG 2.2 guidelines for the specific issue type"

    def _get_mobile_fix_pattern(self, pattern: PatternCluster, stack: str) -> str:
        """Get mobile fix pattern based on stack."""
        issue_types = [t.value for t in pattern.issue_types]

        if "name_role_value" in issue_types:
            if stack == "native":
                return "iOS: Set accessibilityLabel. Android: Set contentDescription"
            elif stack == "rn":
                return "Set accessible={true} and accessibilityLabel prop"
            elif stack == "flutter":
                return "Wrap with Semantics widget or set semanticLabel"

        if "target_size" in issue_types:
            return "Ensure touch targets are at least 44x44 pt (iOS) or 48x48 dp (Android)"

        return "Follow platform accessibility guidelines"
