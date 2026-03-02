"""
UX Design tool agent for planning-v2.

Participates in phases: Implementation only.
Focuses on user experience, user flows, and interaction design.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput
from ...output_templates import parse_fix_output, parse_planning_tool_output
from ..json_utils import complete_text_with_continuation

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)


UX_DESIGN_IMPLEMENTATION_PROMPT = """You are a UX Design expert. Create UX artifacts for the specification.

Respond using this EXACT format:

## COMPONENT_DESIGN ##
Persona or flow name: description
## END COMPONENT_DESIGN ##

## RECOMMENDATIONS ##
- UX recommendation 1
- UX recommendation 2
## END RECOMMENDATIONS ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##

Specification:
---
{spec_content}
---
"""

UX_DESIGN_FIX_SINGLE_ISSUE_PROMPT = """You are a UX Design expert. Fix this issue. Use this EXACT format:

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
### plan/ux_design.md ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##

ISSUE: --- {issue} ---
CURRENT ARTIFACT: --- {current_artifact} ---
SPEC: --- {spec_excerpt} ---
"""


class UXDesignToolAgent:
    """
    UX Design tool agent: user experience, user flows, interaction design.
    
    Participates in Implementation phase only per the matrix.
    """

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Planning phase: UX Design does not participate."""
        return ToolAgentPhaseOutput(summary="UX Design planning not applicable (per matrix).")

    def execute(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Implementation phase: generate or update UX design artifacts.
        
        If review_issues are provided, this agent handles fixes first.
        Only regenerates the document if it doesn't already exist.
        """
        all_files: Dict[str, str] = {}
        fixes_applied: List[str] = []
        
        existing_doc = inp.current_files.get("plan/ux_design.md") if inp.current_files else None
        
        ux_issues = [
            i for i in inp.review_issues
            if any(kw in i.lower() for kw in ["ux", "persona", "journey", "flow", "usability", "user experience", "interaction"])
        ]
        
        if ux_issues and self.llm:
            logger.info("UXDesign: handling %d review issues", len(ux_issues))
            for issue in ux_issues:
                result = self.fix_single_issue(issue, inp)
                if result.files:
                    all_files.update(result.files)
                    fixes_applied.append(result.summary)
            logger.info("UXDesign: fixed %d/%d issues", len(fixes_applied), len(ux_issues))
        
        if existing_doc or all_files.get("plan/ux_design.md"):
            summary = "UX Design artifacts preserved (no changes needed)."
            if fixes_applied:
                summary = f"UX Design artifacts updated. Fixed {len(fixes_applied)} review issues."
            return ToolAgentPhaseOutput(
                summary=summary,
                files=all_files,
                recommendations=fixes_applied if fixes_applied else [],
            )
        
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="UX Design execute skipped (no LLM).",
                recommendations=["Define user personas", "Map user journeys"],
            )
        
        spec_content = inp.spec_content or ""
        prompt = UX_DESIGN_IMPLEMENTATION_PROMPT.format(
            spec_content=spec_content[:6000],
        )
        raw_text = complete_text_with_continuation(
            self.llm, prompt, agent_name="UXDesign",
        )
        data = parse_planning_tool_output(raw_text)
        component_design = data.get("component_design") or []
        recommendations = data.get("recommendations") or []
        data_flow = data.get("data_flow", "")

        content_parts = ["# UX Design\n\n"]
        if component_design:
            content_parts.append("## Components / Personas / Flows\n")
            for comp in component_design:
                if isinstance(comp, dict):
                    name = comp.get("name", "Item")
                    resp = comp.get("responsibility", "")
                    content_parts.append(f"### {name}\n{resp}\n\n")
        if data_flow:
            content_parts.append("## Data / Flow\n")
            content_parts.append(f"{data_flow}\n\n")
        if recommendations:
            content_parts.append("## Recommendations\n")
            for rec in recommendations:
                content_parts.append(f"- {rec}\n")
            content_parts.append("\n")

        if component_design or recommendations:
            all_files["plan/ux_design.md"] = "".join(content_parts)
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", "UX Design artifacts generated."),
            files=all_files,
        )

    def fix_single_issue(self, issue: str, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix a single UX design issue.

        Args:
            issue: The issue description to fix.
            inp: Tool agent phase input with context.

        Returns:
            ToolAgentPhaseOutput with updated files if fix was applied.
        """
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="UX Design fix skipped (no LLM).",
                resolved=False,
            )

        current_artifact = ""
        if inp.current_files:
            current_artifact = inp.current_files.get("plan/ux_design.md", "")
            if not current_artifact:
                for path, content in inp.current_files.items():
                    if "ux_design" in path.lower() or "ux" in path.lower():
                        current_artifact = content
                        break

        prompt = UX_DESIGN_FIX_SINGLE_ISSUE_PROMPT.format(
            issue=issue,
            current_artifact=current_artifact[:6000] if current_artifact else "(no existing artifact)",
            spec_excerpt=(inp.spec_content or "")[:3000],
        )

        try:
            raw_text = complete_text_with_continuation(
                self.llm, prompt, agent_name="UXDesign_FixSingleIssue",
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
                files["plan/ux_design.md"] = updated_content
                logger.info("UXDesign: fix applied — %s", fix_desc[:60])
            elif file_updates:
                for path, content in file_updates.items():
                    if content and isinstance(content, str) and content.strip():
                        files[path] = content
                        logger.info("UXDesign: fix applied — %s", fix_desc[:60])
                        break

            return ToolAgentPhaseOutput(
                summary=fix_desc or f"UX design issue addressed: {issue[:50]}",
                files=files,
                resolved=resolved or bool(files),
                metadata={"root_cause": raw.get("root_cause", "")},
            )

        except Exception as e:
            logger.warning("UXDesign fix_single_issue failed: %s", e)
            return ToolAgentPhaseOutput(
                summary=f"Fix failed: {str(e)[:50]}",
                resolved=False,
            )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Review phase: UX Design does not participate."""
        return ToolAgentPhaseOutput(summary="UX Design review not applicable (per matrix).")

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Problem-solving phase: UX Design does not participate."""
        return ToolAgentPhaseOutput(summary="UX Design problem_solve not applicable (per matrix).")

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Deliver phase: UX Design does not participate."""
        return ToolAgentPhaseOutput(summary="UX Design deliver not applicable (per matrix).")
