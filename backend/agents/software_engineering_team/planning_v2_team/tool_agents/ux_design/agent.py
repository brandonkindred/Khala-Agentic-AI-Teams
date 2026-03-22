"""
UX Design tool agent for planning-v2.

Participates in phases: Implementation only.
Focuses on user experience, user flows, and interaction design.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput, planning_asset_path
from ...output_templates import (
    looks_like_truncated_file_content,
    parse_fix_output,
    parse_planning_tool_output,
)
from ..json_utils import attempt_fix_output_continuation, complete_text_with_continuation

if TYPE_CHECKING:
    from llm_service import LLMClient

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

Output the complete updated file content; do not truncate. Include every section in full.

## FILE_UPDATES ##
### plan/planning_team/ux_design.md ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##

ISSUE: --- {issue} ---
CURRENT ARTIFACT: --- {current_artifact} ---
SPEC: --- {spec_excerpt} ---
"""

UX_DESIGN_FIX_ALL_ISSUES_PROMPT = """You are a UX Design expert. Address ALL of the following issues in ONE coherent update. Use this EXACT format:

## ROOT_CAUSE ##
Brief combined root cause for the issues.
## END ROOT_CAUSE ##

## FIX_DESCRIPTION ##
What you are changing to address all issues.
## END FIX_DESCRIPTION ##

## RESOLVED ##
true or false
## END RESOLVED ##

Output the complete updated file content; do not truncate. Include every section in full.

## FILE_UPDATES ##
### plan/planning_team/ux_design.md ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##

ISSUES TO FIX (address every one):
---
{issues_list}
---

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
        
        Writes to disk as fixes are applied; returns files_written so implementation phase does not overwrite.
        """
        fixes_applied: List[str] = []
        files_written: List[str] = []
        current_files: Dict[str, str] = dict(inp.current_files or {})
        
        existing_doc = inp.current_files.get(planning_asset_path("ux_design.md")) if inp.current_files else None
        ux_issues = [
            i for i in inp.review_issues
            if any(kw in i.lower() for kw in ["ux", "persona", "journey", "flow", "usability", "user experience", "interaction"])
        ]
        
        if ux_issues and self.llm:
            logger.info(
                "UXDesign: handling %d review issue(s) (will apply fixes in one update and write to disk).",
                len(ux_issues),
            )
            fix_inp = inp.model_copy(update={"current_files": current_files})
            result = self.fix_all_issues(ux_issues, fix_inp)
            if result.files:
                repo = Path(inp.repo_path or ".")
                for rel_path, content in result.files.items():
                    full_path = repo / rel_path
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(content, encoding="utf-8")
                    file_name = full_path.name
                    logger.info(
                        "UXDesign: applied fix — writing to file: %s (%d chars)",
                        file_name,
                        len(content),
                    )
                    if rel_path not in files_written:
                        files_written.append(rel_path)
                    current_files[rel_path] = content
                fixes_applied.append(result.summary)
            logger.info(
                "UXDesign: fixed %d review issue(s) in one update (all fixes written to planning artifacts).",
                len(ux_issues),
            )
        
        if existing_doc and not ux_issues:
            return ToolAgentPhaseOutput(
                summary="UX Design artifacts preserved (no changes needed).",
                files={},
                recommendations=[],
                files_written=[],
            )
        if files_written:
            summary = "UX Design artifacts updated."
            if fixes_applied:
                summary = f"UX Design artifacts updated. Fixed {len(ux_issues)} review issue(s) in one update."
            return ToolAgentPhaseOutput(
                summary=summary,
                files={},
                recommendations=fixes_applied if fixes_applied else [],
                files_written=files_written,
            )
        
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="UX Design execute skipped (no LLM).",
                recommendations=["Define user personas", "Map user journeys"],
                files_written=[],
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
            rel_path = planning_asset_path("ux_design.md")
            content = "".join(content_parts)
            repo = Path(inp.repo_path or ".")
            full_path = repo / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            files_written.append(rel_path)
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", "UX Design artifacts generated."),
            files={},
            files_written=files_written,
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
            current_artifact = inp.current_files.get(planning_asset_path("ux_design.md"), "")
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
            ux_design_path = planning_asset_path("ux_design.md")
            if file_updates.get(ux_design_path):
                updated_content = file_updates[ux_design_path]
            elif not updated_content and file_updates:
                updated_content = next(iter(file_updates.values()), "")

            files: Dict[str, str] = {}
            if updated_content and isinstance(updated_content, str) and updated_content.strip():
                if looks_like_truncated_file_content(updated_content):
                    continued = attempt_fix_output_continuation(
                        self.llm, prompt, raw_text, "UXDesign_FixSingleIssue",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    updated_content = fu.get(ux_design_path) or raw.get("updated_content", "") or next(iter(fu.values()), "")
                    if updated_content and not looks_like_truncated_file_content(updated_content):
                        files[planning_asset_path("ux_design.md")] = updated_content
                        logger.info("UXDesign: fix applied after continuation (single-issue).")
                    else:
                        logger.warning(
                            "UXDesign: fix output still truncated after continuation; not writing.",
                        )
                else:
                    files[planning_asset_path("ux_design.md")] = updated_content
                    logger.info("UXDesign: fix applied (single-issue) — %s", fix_desc[:120])
            elif file_updates:
                for path, content in file_updates.items():
                    if content and isinstance(content, str) and content.strip() and not looks_like_truncated_file_content(content):
                        files[path] = content
                        logger.info("UXDesign: fix applied (single-issue) — %s", fix_desc[:120])
                        break
                else:
                    continued = attempt_fix_output_continuation(
                        self.llm, prompt, raw_text, "UXDesign_FixSingleIssue",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    uc = fu.get(ux_design_path) or raw.get("updated_content", "") or next(iter(fu.values()), "")
                    if uc and not looks_like_truncated_file_content(uc):
                        files[planning_asset_path("ux_design.md")] = uc
                        logger.info("UXDesign: fix applied after continuation (single-issue).")
                    else:
                        for p, c in fu.items():
                            if c and isinstance(c, str) and c.strip() and not looks_like_truncated_file_content(c):
                                files[p] = c
                                logger.info("UXDesign: fix applied after continuation (single-issue).")
                                break
                        else:
                            logger.warning(
                                "UXDesign: fix output still truncated after continuation; not writing.",
                            )

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

    def fix_all_issues(
        self, issues: List[str], inp: ToolAgentPhaseInput
    ) -> ToolAgentPhaseOutput:
        """Fix all listed UX design issues in one LLM call."""
        if not issues:
            return ToolAgentPhaseOutput(
                summary="No UX design issues to fix.",
                resolved=True,
            )
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="UX Design fix skipped (no LLM).",
                resolved=False,
            )

        current_artifact = ""
        if inp.current_files:
            current_artifact = inp.current_files.get(planning_asset_path("ux_design.md"), "")
            if not current_artifact:
                for path, content in inp.current_files.items():
                    if "ux_design" in path.lower() or "ux" in path.lower():
                        current_artifact = content
                        break

        issues_list = "\n".join(f"{i + 1}. {issue}" for i, issue in enumerate(issues))
        prompt = UX_DESIGN_FIX_ALL_ISSUES_PROMPT.format(
            issues_list=issues_list,
            current_artifact=current_artifact[:6000] if current_artifact else "(no existing artifact)",
            spec_excerpt=(inp.spec_content or "")[:3000],
        )

        try:
            raw_text = complete_text_with_continuation(
                self.llm, prompt, agent_name="UXDesign_FixAllIssues",
            )
            raw = parse_fix_output(raw_text)
            updated_content = raw.get("updated_content", "")
            fix_desc = raw.get("fix_description", "")
            resolved = raw.get("resolved", False)
            file_updates = raw.get("file_updates") or {}
            ux_design_path = planning_asset_path("ux_design.md")
            if file_updates.get(ux_design_path):
                updated_content = file_updates[ux_design_path]
            elif not updated_content and file_updates:
                updated_content = next(iter(file_updates.values()), "")

            files: Dict[str, str] = {}
            if updated_content and isinstance(updated_content, str) and updated_content.strip():
                if looks_like_truncated_file_content(updated_content):
                    continued = attempt_fix_output_continuation(
                        self.llm, prompt, raw_text, "UXDesign_FixAllIssues",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    updated_content = fu.get(ux_design_path) or raw.get("updated_content", "") or next(iter(fu.values()), "")
                    if updated_content and not looks_like_truncated_file_content(updated_content):
                        files[planning_asset_path("ux_design.md")] = updated_content
                    else:
                        logger.warning(
                            "UXDesign: fix_all_issues output still truncated after continuation; not writing.",
                        )
                else:
                    files[planning_asset_path("ux_design.md")] = updated_content
            elif file_updates:
                for path, content in file_updates.items():
                    if content and isinstance(content, str) and content.strip() and not looks_like_truncated_file_content(content):
                        files[path] = content
                        break
                else:
                    continued = attempt_fix_output_continuation(
                        self.llm, prompt, raw_text, "UXDesign_FixAllIssues",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    uc = fu.get(ux_design_path) or raw.get("updated_content", "") or next(iter(fu.values()), "")
                    if uc and not looks_like_truncated_file_content(uc):
                        files[planning_asset_path("ux_design.md")] = uc
                    else:
                        for p, c in fu.items():
                            if c and isinstance(c, str) and c.strip() and not looks_like_truncated_file_content(c):
                                files[p] = c
                                break
                        else:
                            logger.warning(
                                "UXDesign: fix_all_issues output still truncated after continuation; not writing.",
                            )

            summary = fix_desc or f"Addressed {len(issues)} issue(s) in one update."
            if len(issues) > 1:
                summary = f"Addressed {len(issues)} issues in one update. {summary[:200]}"
            return ToolAgentPhaseOutput(
                summary=summary,
                files=files,
                resolved=resolved or bool(files),
                metadata={"root_cause": raw.get("root_cause", "")},
            )
        except Exception as e:
            logger.warning("UXDesign fix_all_issues failed: %s", e)
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
