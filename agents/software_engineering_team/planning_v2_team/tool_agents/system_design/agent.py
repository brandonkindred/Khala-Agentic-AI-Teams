"""
System Design tool agent for planning-v2.

Participates in all 6 phases: Spec Review, Planning, Implementation, Review, Problem Solving, Deliver.
Focuses on component layout, system boundaries, and integration points.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput
from ...output_templates import (
    parse_fix_output,
    parse_planning_tool_output,
    parse_review_output,
    parse_spec_review_output,
)
from ..json_utils import complete_text_with_continuation

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)


SYSTEM_DESIGN_SPEC_REVIEW_PROMPT = """You are a System Design expert. Review this specification and identify:
1. Component boundaries and responsibilities
2. System integration points
3. Critical design gaps or ambiguities
4. Scalability considerations

Specification:
---
{spec_content}
---

Respond using this EXACT format:

## COMPONENTS ##
- Component 1
- Component 2
## END COMPONENTS ##

## INTEGRATION_POINTS ##
- Point 1
- Point 2
## END INTEGRATION_POINTS ##

## GAPS ##
- Gap 1
- Gap 2
## END GAPS ##

## SCALABILITY_NOTES ##
Scalability considerations (one short paragraph).
## END SCALABILITY_NOTES ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##
"""

SYSTEM_DESIGN_PLANNING_PROMPT = """You are a System Design expert. Create a system design plan for:

Specification:
---
{spec_content}
---

Prior analysis: {prior_analysis}

Respond using this EXACT format:

## COMPONENT_DESIGN ##
ComponentName: responsibility and dependencies
AnotherComponent: what it does
## END COMPONENT_DESIGN ##

## DATA_FLOW ##
Description of data flow between components.
## END DATA_FLOW ##

## INTEGRATION_STRATEGY ##
How components integrate.
## END INTEGRATION_STRATEGY ##

## RECOMMENDATIONS ##
- Recommendation 1
- Recommendation 2
## END RECOMMENDATIONS ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##
"""

SYSTEM_DESIGN_REVIEW_PROMPT = """You are a System Design expert. Review these planning artifacts for design coherence:

Artifacts:
---
{artifacts}
---

Respond using this EXACT format:

## PASSED ##
true or false
## END PASSED ##

## ISSUES ##
- Issue 1
- Issue 2
## END ISSUES ##

## RECOMMENDATIONS ##
- Improvement 1
- Improvement 2
## END RECOMMENDATIONS ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##
"""

SYSTEM_DESIGN_FIX_SINGLE_ISSUE_PROMPT = """You are a System Design expert. Fix this specific issue in the planning artifacts.

ISSUE TO FIX:
---
{issue}
---

CURRENT SYSTEM DESIGN ARTIFACT:
---
{current_artifact}
---

SPECIFICATION CONTEXT:
---
{spec_excerpt}
---

Analyze and fix this issue. Provide the complete updated file content using the format below.

Respond using this EXACT format:

## ROOT_CAUSE ##
Why this issue exists.
## END ROOT_CAUSE ##

## FIX_DESCRIPTION ##
What you are changing to fix it.
## END FIX_DESCRIPTION ##

## RESOLVED ##
true or false
## END RESOLVED ##

## FILE_UPDATES ##
### plan/system_design.md ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##
"""


class SystemDesignToolAgent:
    """
    System Design tool agent: component layout, system boundaries, integration points.
    
    Participates in all 6 phases per the matrix.
    """

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Planning phase: create system design plan."""
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="System Design planning skipped (no LLM).",
                recommendations=["Define component boundaries", "Identify integration points"],
            )
        
        prior_analysis = ""
        if inp.spec_review_result:
            prior_analysis = getattr(inp.spec_review_result, "plan_summary", "") or ""

        spec_content = inp.spec_content or ""
        prompt = SYSTEM_DESIGN_PLANNING_PROMPT.format(
            spec_content=spec_content[:8000],
            prior_analysis=prior_analysis[:2000],
        )
        raw_text = complete_text_with_continuation(
            self.llm, prompt, agent_name="SystemDesign",
        )
        data = parse_planning_tool_output(raw_text)
        recommendations = data.get("recommendations") or []
        if not isinstance(recommendations, list):
            recommendations = [str(recommendations)]
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", "System design planning complete."),
            recommendations=recommendations,
            metadata={
                "component_design": data.get("component_design", []),
                "data_flow": data.get("data_flow", ""),
                "integration_strategy": data.get("integration_strategy", ""),
            },
        )

    def execute(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Implementation phase: generate system design artifacts and fix review issues.
        
        If review_issues are provided, this agent handles fixes however it sees fit.
        """
        if not self.llm:
            return ToolAgentPhaseOutput(summary="System Design execute skipped (no LLM).")
        
        fixes_applied: List[str] = []
        all_files: Dict[str, str] = {}
        
        design_issues = [
            i for i in inp.review_issues
            if any(kw in i.lower() for kw in ["design", "system", "diagram", "flow", "interface", "component"])
        ]
        
        if design_issues:
            logger.info("SystemDesign: handling %d review issues", len(design_issues))
            for issue in design_issues:
                result = self.fix_single_issue(issue, inp)
                if result.files:
                    all_files.update(result.files)
                    fixes_applied.append(result.summary)
            logger.info("SystemDesign: fixed %d/%d issues", len(fixes_applied), len(design_issues))
        
        component_design = inp.metadata.get("component_design", [])
        data_flow = inp.metadata.get("data_flow", "")
        integration_strategy = inp.metadata.get("integration_strategy", "")
        
        content_parts = ["# System Design\n"]
        content_parts.append("## Components\n")
        for comp in component_design:
            if isinstance(comp, dict):
                name = comp.get("name", "Unknown")
                resp = comp.get("responsibility", "")
                deps = comp.get("dependencies", [])
                content_parts.append(f"### {name}\n")
                content_parts.append(f"**Responsibility:** {resp}\n")
                if deps:
                    content_parts.append(f"**Dependencies:** {', '.join(deps)}\n")
                content_parts.append("\n")
        
        if data_flow:
            content_parts.append("## Data Flow\n")
            content_parts.append(f"{data_flow}\n\n")
        
        if integration_strategy:
            content_parts.append("## Integration Strategy\n")
            content_parts.append(f"{integration_strategy}\n\n")
        
        if component_design or data_flow:
            all_files["plan/system_design.md"] = "".join(content_parts)
        
        summary = "System design artifacts generated."
        if fixes_applied:
            summary = f"System design artifacts generated. Fixed {len(fixes_applied)} review issues."
        
        return ToolAgentPhaseOutput(
            summary=summary,
            files=all_files,
            recommendations=fixes_applied if fixes_applied else [],
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Review phase: check system design coherence."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="System Design review skipped (no LLM).")
        
        artifacts = "\n".join(
            f"--- {path} ---\n{content}"
            for path, content in list(inp.current_files.items())[:10]
        )[:8000]
        
        if not artifacts.strip():
            return ToolAgentPhaseOutput(
                summary="System Design review skipped (no artifacts).",
                issues=[],
            )
        
        prompt = SYSTEM_DESIGN_REVIEW_PROMPT.format(artifacts=artifacts)
        raw_text = complete_text_with_continuation(
            self.llm, prompt, agent_name="SystemDesign",
        )
        data = parse_review_output(raw_text)
        issues = data.get("issues") or []
        if not isinstance(issues, list):
            issues = [str(issues)] if issues else []
        
        recommendations = data.get("recommendations") or []
        if not isinstance(recommendations, list):
            recommendations = [str(recommendations)] if recommendations else []
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", "System design review complete."),
            issues=issues,
            recommendations=recommendations,
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Problem-solving phase: address design issues."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="System Design problem_solve skipped (no LLM).")

        design_issues = [i for i in inp.review_issues if "design" in i.lower() or "system" in i.lower()]
        if not design_issues:
            return ToolAgentPhaseOutput(summary="No system design issues to resolve.")

        all_files: Dict[str, str] = {}
        fixes_applied: List[str] = []

        for issue in design_issues:
            result = self.fix_single_issue(issue, inp)
            if result.files:
                all_files.update(result.files)
                fixes_applied.append(result.summary)

        return ToolAgentPhaseOutput(
            summary=f"System design: fixed {len(fixes_applied)}/{len(design_issues)} issue(s).",
            recommendations=fixes_applied,
            files=all_files,
            resolved=len(fixes_applied) == len(design_issues),
        )

    def fix_single_issue(self, issue: str, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix a single system design issue.

        Args:
            issue: The issue description to fix.
            inp: Tool agent phase input with context.

        Returns:
            ToolAgentPhaseOutput with updated files if fix was applied.
        """
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="System Design fix skipped (no LLM).",
                resolved=False,
            )

        current_artifact = inp.current_files.get("plan/system_design.md", "")
        if not current_artifact:
            for path, content in inp.current_files.items():
                if "system" in path.lower() or "design" in path.lower():
                    current_artifact = content
                    break

        prompt = SYSTEM_DESIGN_FIX_SINGLE_ISSUE_PROMPT.format(
            issue=issue,
            current_artifact=current_artifact[:6000] if current_artifact else "(no existing artifact)",
            spec_excerpt=(inp.spec_content or "")[:3000],
        )

        try:
            raw_text = complete_text_with_continuation(
                self.llm, prompt, agent_name="SystemDesign_FixSingleIssue",
            )
            raw = parse_fix_output(raw_text)
            updated_content = raw.get("updated_content", "")
            fix_desc = raw.get("fix_description", "")
            resolved = raw.get("resolved", False)
            file_updates = raw.get("file_updates") or {}
            if not updated_content and file_updates:
                updated_content = next(iter(file_updates.values()), "")

            files: Dict[str, str] = {}
            if updated_content and isinstance(updated_content, str) and updated_content.strip():
                files["plan/system_design.md"] = updated_content
                logger.info("SystemDesign: fix applied — %s", fix_desc[:60])
            elif file_updates:
                for path, content in file_updates.items():
                    if content and isinstance(content, str) and content.strip():
                        files[path] = content
                        logger.info("SystemDesign: fix applied — %s", fix_desc[:60])
                        break

            return ToolAgentPhaseOutput(
                summary=fix_desc or f"System design issue addressed: {issue[:50]}",
                files=files,
                resolved=resolved or bool(files),
                metadata={"root_cause": raw.get("root_cause", "")},
            )

        except Exception as e:
            logger.warning("SystemDesign fix_single_issue failed: %s", e)
            return ToolAgentPhaseOutput(
                summary=f"Fix failed: {str(e)[:50]}",
                resolved=False,
            )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Deliver phase: finalize system design documentation."""
        return ToolAgentPhaseOutput(
            summary="System design documentation finalized.",
            recommendations=["Ensure system design is committed to repo"],
        )

    def spec_review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Spec Review phase: analyze spec for system design concerns."""
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="System Design spec review skipped (no LLM).",
                recommendations=["Review spec for component boundaries"],
            )
        
        prompt = SYSTEM_DESIGN_SPEC_REVIEW_PROMPT.format(
            spec_content=(inp.spec_content or "")[:10000],
        )
        raw_text = complete_text_with_continuation(
            self.llm, prompt, agent_name="SystemDesign",
        )
        data = parse_spec_review_output(raw_text)
        gaps = data.get("gaps") or []
        if not isinstance(gaps, list):
            gaps = [str(gaps)] if gaps else []
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", "System design spec review complete."),
            issues=gaps,
            metadata={
                "components": data.get("components", []),
                "integration_points": data.get("integration_points", []),
                "scalability_notes": data.get("scalability_notes", ""),
            },
        )
