"""
DevOps tool agent for planning-v2.

Participates in phases: Planning, Implementation.
Focuses on CI/CD pipelines, infrastructure, and deployment planning.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput
from ...output_templates import parse_devops_planning_output, parse_fix_output
from ..json_utils import complete_text_with_continuation

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)


DEVOPS_PLANNING_PROMPT = """You are a DevOps expert. Create a DevOps plan for the specification.

If deployment target is NOT specified in the spec, set NEEDS_CLARIFICATION to true and list questions in CLARIFICATION_QUESTIONS. Do not assume cloud provider.

Respond using this EXACT format:

## NEEDS_CLARIFICATION ##
true or false
## END NEEDS_CLARIFICATION ##

## CLARIFICATION_QUESTIONS ##
- Where should this application be deployed?
- What are the expected SLA requirements?
## END CLARIFICATION_QUESTIONS ##

## RECOMMENDATIONS ##
- DevOps recommendation 1
- DevOps recommendation 2
## END RECOMMENDATIONS ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##

Specification:
---
{spec_content}
---

Plan summary from spec review: {plan_summary}
"""

DEVOPS_FIX_SINGLE_ISSUE_PROMPT = """You are a DevOps expert. Fix this specific issue in the DevOps artifacts.

ISSUE TO FIX:
---
{issue}
---

CURRENT DEVOPS ARTIFACT:
---
{current_artifact}
---

SPECIFICATION CONTEXT:
---
{spec_excerpt}
---

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
### plan/devops.md ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##
"""


class DevOpsToolAgent:
    """
    DevOps tool agent: CI/CD pipelines, infrastructure, deployment planning.
    
    Participates in Planning and Implementation phases per the matrix.
    """

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Planning phase: create DevOps plan."""
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="DevOps planning skipped (no LLM).",
                recommendations=["Define CI/CD pipeline", "Plan infrastructure"],
            )
        
        plan_summary = ""
        if inp.spec_review_result:
            plan_summary = getattr(inp.spec_review_result, "plan_summary", "") or ""

        spec_content = inp.spec_content or ""
        prompt = DEVOPS_PLANNING_PROMPT.format(
            spec_content=spec_content[:6000],
            plan_summary=plan_summary[:2000],
        )
        raw_text = complete_text_with_continuation(
            self.llm, prompt, agent_name="DevOps",
        )
        data = parse_devops_planning_output(raw_text)
        
        recommendations = data.get("recommendations") or []
        if not isinstance(recommendations, list):
            recommendations = [str(recommendations)]
        
        needs_clarification = data.get("needs_clarification", False)
        clarification_questions = data.get("clarification_questions", [])
        
        if needs_clarification and clarification_questions:
            logger.warning(
                "DevOps planning requires clarification: %s",
                "; ".join(clarification_questions[:3])
            )
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", "DevOps planning complete."),
            recommendations=recommendations,
            metadata={
                "needs_clarification": needs_clarification,
                "clarification_questions": clarification_questions,
                "pipeline_stages": data.get("pipeline_stages", []),
                "infrastructure": data.get("infrastructure", {}),
                "deployment_strategy": data.get("deployment_strategy", ""),
                "monitoring": data.get("monitoring", []),
                "security": data.get("security", []),
            },
        )

    def execute(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Implementation phase: generate or update DevOps artifacts.
        
        If review_issues are provided, this agent handles fixes first.
        Only regenerates the document if it doesn't already exist.
        """
        all_files: Dict[str, str] = {}
        fixes_applied: List[str] = []
        
        devops_issues = [
            i for i in inp.review_issues
            if any(kw in i.lower() for kw in ["devops", "ci/cd", "pipeline", "infrastructure", "deployment", "monitoring", "security", "cicd"])
        ]
        
        if devops_issues and self.llm:
            logger.info("DevOps: handling %d review issues", len(devops_issues))
            for issue in devops_issues:
                result = self.fix_single_issue(issue, inp)
                if result.files:
                    all_files.update(result.files)
                    fixes_applied.append(result.summary)
            logger.info("DevOps: fixed %d/%d issues", len(fixes_applied), len(devops_issues))
        
        existing_doc = inp.current_files.get("plan/devops.md") if inp.current_files else None
        if existing_doc or all_files.get("plan/devops.md"):
            summary = "DevOps artifacts updated."
            if fixes_applied:
                summary = f"DevOps artifacts updated. Fixed {len(fixes_applied)} review issues."
            return ToolAgentPhaseOutput(
                summary=summary,
                files=all_files,
                recommendations=fixes_applied if fixes_applied else [],
            )
        
        pipeline_stages = inp.metadata.get("pipeline_stages", []) if inp.metadata else []
        infrastructure = inp.metadata.get("infrastructure", {}) if inp.metadata else {}
        deployment_strategy = inp.metadata.get("deployment_strategy", "") if inp.metadata else ""
        monitoring = inp.metadata.get("monitoring", []) if inp.metadata else []
        security = inp.metadata.get("security", []) if inp.metadata else []
        
        content_parts = ["# DevOps Plan\n\n"]
        
        if pipeline_stages:
            content_parts.append("## CI/CD Pipeline Stages\n")
            for i, stage in enumerate(pipeline_stages, 1):
                content_parts.append(f"{i}. {stage}\n")
            content_parts.append("\n")
        
        if infrastructure:
            content_parts.append("## Infrastructure\n")
            for key, value in infrastructure.items():
                content_parts.append(f"- **{key}:** {value}\n")
            content_parts.append("\n")
        
        if deployment_strategy:
            content_parts.append(f"## Deployment Strategy\n{deployment_strategy}\n\n")
        
        if monitoring:
            content_parts.append("## Monitoring\n")
            for item in monitoring:
                content_parts.append(f"- {item}\n")
            content_parts.append("\n")
        
        if security:
            content_parts.append("## Security\n")
            for item in security:
                content_parts.append(f"- {item}\n")
            content_parts.append("\n")
        
        if pipeline_stages or infrastructure:
            all_files["plan/devops.md"] = "".join(content_parts)
        
        return ToolAgentPhaseOutput(
            summary="DevOps artifacts generated.",
            files=all_files,
        )

    def fix_single_issue(self, issue: str, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix a single DevOps issue.

        Args:
            issue: The issue description to fix.
            inp: Tool agent phase input with context.

        Returns:
            ToolAgentPhaseOutput with updated files if fix was applied.
        """
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="DevOps fix skipped (no LLM).",
                resolved=False,
            )

        current_artifact = ""
        if inp.current_files:
            current_artifact = inp.current_files.get("plan/devops.md", "")
            if not current_artifact:
                for path, content in inp.current_files.items():
                    if "devops" in path.lower():
                        current_artifact = content
                        break

        prompt = DEVOPS_FIX_SINGLE_ISSUE_PROMPT.format(
            issue=issue,
            current_artifact=current_artifact[:6000] if current_artifact else "(no existing artifact)",
            spec_excerpt=(inp.spec_content or "")[:3000],
        )

        try:
            raw_text = complete_text_with_continuation(
                self.llm, prompt, agent_name="DevOps_FixSingleIssue",
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
                files["plan/devops.md"] = updated_content
                logger.info("DevOps: fix applied — %s", fix_desc[:60])
            elif file_updates:
                for path, content in file_updates.items():
                    if content and isinstance(content, str) and content.strip():
                        files[path] = content
                        logger.info("DevOps: fix applied — %s", fix_desc[:60])
                        break

            return ToolAgentPhaseOutput(
                summary=fix_desc or f"DevOps issue addressed: {issue[:50]}",
                files=files,
                resolved=resolved or bool(files),
                metadata={"root_cause": raw.get("root_cause", "")},
            )

        except Exception as e:
            logger.warning("DevOps fix_single_issue failed: %s", e)
            return ToolAgentPhaseOutput(
                summary=f"Fix failed: {str(e)[:50]}",
                resolved=False,
            )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Review phase: DevOps does not participate."""
        return ToolAgentPhaseOutput(summary="DevOps review not applicable (per matrix).")

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Problem-solving phase: DevOps does not participate."""
        return ToolAgentPhaseOutput(summary="DevOps problem_solve not applicable (per matrix).")

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Deliver phase: DevOps does not participate."""
        return ToolAgentPhaseOutput(summary="DevOps deliver not applicable (per matrix).")
