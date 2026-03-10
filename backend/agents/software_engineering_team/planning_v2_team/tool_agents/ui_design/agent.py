"""
UI Design tool agent for planning-v2.

Participates in phases: Planning, Implementation.
Focuses on visual design, component library, and design system planning.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput, planning_asset_path
from ...output_templates import looks_like_truncated_file_content, parse_fix_output, parse_planning_tool_output
from ..json_utils import complete_text_with_continuation

if TYPE_CHECKING:
    from software_engineering_team.shared.llm import LLMClient

logger = logging.getLogger(__name__)


UI_DESIGN_PLANNING_PROMPT = """You are a UI Design expert. Create a UI design plan for the specification.

Respond using this EXACT format:

## COMPONENT_DESIGN ##
ComponentName: purpose
AnotherComponent: purpose
## END COMPONENT_DESIGN ##

## DATA_FLOW ##
(optional) Brief data/design flow.
## END DATA_FLOW ##

## INTEGRATION_STRATEGY ##
(optional) How UI integrates with backend.
## END INTEGRATION_STRATEGY ##

## RECOMMENDATIONS ##
- UI recommendation 1
- UI recommendation 2
## END RECOMMENDATIONS ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##

Specification:
---
{spec_content}
---
"""

UI_DESIGN_FIX_SINGLE_ISSUE_PROMPT = """You are a UI Design expert. Fix this specific issue. Use this EXACT format:

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
### """ + planning_asset_path("ui_design.md") + """ ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##

ISSUE TO FIX:
---
{issue}
---

CURRENT ARTIFACT:
---
{current_artifact}
---

SPEC CONTEXT:
---
{spec_excerpt}
---
"""


class UIDesignToolAgent:
    """
    UI Design tool agent: visual design, component library, design system.
    
    Participates in Planning and Implementation phases per the matrix.
    """

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Planning phase: create UI design plan."""
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="UI Design planning skipped (no LLM).",
                recommendations=["Define design tokens", "Plan component library"],
            )
        
        spec_content = inp.spec_content or ""
        prompt = UI_DESIGN_PLANNING_PROMPT.format(
            spec_content=spec_content[:6000],
        )
        raw_text = complete_text_with_continuation(
            self.llm, prompt, agent_name="UIDesign",
        )
        data = parse_planning_tool_output(raw_text)
        
        recommendations = data.get("recommendations") or []
        if not isinstance(recommendations, list):
            recommendations = [str(recommendations)]
        
        component_design = data.get("component_design") or []
        components = [c.get("name", "") for c in component_design if isinstance(c, dict) and c.get("name")]
        return ToolAgentPhaseOutput(
            summary=data.get("summary", "UI Design planning complete."),
            recommendations=recommendations,
            metadata={
                "design_tokens": {},
                "components": components,
                "layouts": [],
                "breakpoints": {},
                "accessibility": [],
            },
        )

    def execute(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Implementation phase: generate or update UI design artifacts.
        Writes to disk as fixes are applied; returns files_written so implementation phase does not overwrite.
        """
        fixes_applied: List[str] = []
        files_written: List[str] = []
        current_files: Dict[str, str] = dict(inp.current_files or {})
        
        ui_issues = [
            i for i in inp.review_issues
            if any(kw in i.lower() for kw in ["ui", "design token", "component", "layout", "breakpoint", "accessibility", "visual", "responsive"])
        ]
        
        if ui_issues and self.llm:
            logger.info(
                "UIDesign: handling %d review issue(s) (will apply fixes and write updated artifacts to disk).",
                len(ui_issues),
            )
            fix_inp = inp.model_copy(update={"current_files": current_files})
            for issue in ui_issues:
                result = self.fix_single_issue(issue, fix_inp)
                if result.files:
                    repo = Path(inp.repo_path or ".")
                    for rel_path, content in result.files.items():
                        full_path = repo / rel_path
                        full_path.parent.mkdir(parents=True, exist_ok=True)
                        full_path.write_text(content, encoding="utf-8")
                        file_name = full_path.name
                        logger.info(
                            "UIDesign: applied fix — writing to file: %s; full contents:\n%s",
                            file_name,
                            content,
                        )
                        if rel_path not in files_written:
                            files_written.append(rel_path)
                        current_files[rel_path] = content
                    fix_inp = inp.model_copy(update={"current_files": current_files})
                    fixes_applied.append(result.summary)
            logger.info(
                "UIDesign: fixed %d out of %d review issue(s) (all fixes written to planning artifacts).",
                len(fixes_applied),
                len(ui_issues),
            )
        
        existing_doc = inp.current_files.get(planning_asset_path("ui_design.md")) if inp.current_files else None
        if existing_doc and not ui_issues:
            return ToolAgentPhaseOutput(
                summary="UI Design artifacts unchanged (file exists, no review issues).",
                files={},
                recommendations=[],
                files_written=[],
            )
        if files_written:
            summary = "UI Design artifacts updated."
            if fixes_applied:
                summary = f"UI Design artifacts updated. Fixed {len(fixes_applied)} review issues."
            return ToolAgentPhaseOutput(
                summary=summary,
                files={},
                recommendations=fixes_applied if fixes_applied else [],
                files_written=files_written,
            )
        
        design_tokens = inp.metadata.get("design_tokens", {}) if inp.metadata else {}
        components = inp.metadata.get("components", []) if inp.metadata else []
        layouts = inp.metadata.get("layouts", []) if inp.metadata else []
        breakpoints = inp.metadata.get("breakpoints", {}) if inp.metadata else {}
        accessibility = inp.metadata.get("accessibility", []) if inp.metadata else []
        
        content_parts = ["# UI Design Plan\n\n"]
        
        if design_tokens:
            content_parts.append("## Design Tokens\n")
            for category, tokens in design_tokens.items():
                content_parts.append(f"### {category.title()}\n")
                if isinstance(tokens, list):
                    for token in tokens:
                        content_parts.append(f"- {token}\n")
                else:
                    content_parts.append(f"- {tokens}\n")
            content_parts.append("\n")
        
        if components:
            content_parts.append("## Component Library\n")
            for comp in components:
                content_parts.append(f"- {comp}\n")
            content_parts.append("\n")
        
        if layouts:
            content_parts.append("## Page Layouts\n")
            for layout in layouts:
                content_parts.append(f"- {layout}\n")
            content_parts.append("\n")
        
        if breakpoints:
            content_parts.append("## Responsive Breakpoints\n")
            for name, value in breakpoints.items():
                content_parts.append(f"- **{name}:** {value}\n")
            content_parts.append("\n")
        
        if accessibility:
            content_parts.append("## Accessibility\n")
            for item in accessibility:
                content_parts.append(f"- {item}\n")
            content_parts.append("\n")
        
        if design_tokens or components:
            rel_path = planning_asset_path("ui_design.md")
            content = "".join(content_parts)
            repo = Path(inp.repo_path or ".")
            full_path = repo / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            files_written.append(rel_path)
        
        return ToolAgentPhaseOutput(
            summary="UI Design artifacts generated.",
            files={},
            files_written=files_written,
        )

    def fix_single_issue(self, issue: str, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix a single UI design issue.

        Args:
            issue: The issue description to fix.
            inp: Tool agent phase input with context.

        Returns:
            ToolAgentPhaseOutput with updated files if fix was applied.
        """
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="UI Design fix skipped (no LLM).",
                resolved=False,
            )

        current_artifact = ""
        if inp.current_files:
            current_artifact = inp.current_files.get(planning_asset_path("ui_design.md"), "")
            if not current_artifact:
                for path, content in inp.current_files.items():
                    if "ui_design" in path.lower():
                        current_artifact = content
                        break

        prompt = UI_DESIGN_FIX_SINGLE_ISSUE_PROMPT.format(
            issue=issue,
            current_artifact=current_artifact[:6000] if current_artifact else "(no existing artifact)",
            spec_excerpt=(inp.spec_content or "")[:3000],
        )

        try:
            raw_text = complete_text_with_continuation(
                self.llm, prompt, agent_name="UIDesign_FixSingleIssue",
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
                if looks_like_truncated_file_content(updated_content):
                    logger.warning(
                        "UIDesign: fix output appears truncated (file content incomplete); skipping write to avoid incomplete artifact.",
                    )
                else:
                    files[planning_asset_path("ui_design.md")] = updated_content
                    logger.info("UIDesign: fix applied (single-issue) — %s", fix_desc[:120])
            elif file_updates:
                for path, content in file_updates.items():
                    if content and isinstance(content, str) and content.strip() and not looks_like_truncated_file_content(content):
                        files[path] = content
                        logger.info("UIDesign: fix applied (single-issue) — %s", fix_desc[:120])
                        break
                else:
                    if file_updates and any(isinstance(c, str) and c.strip() for c in file_updates.values()):
                        logger.warning(
                            "UIDesign: fix output appears truncated (file content incomplete); skipping write to avoid incomplete artifact.",
                        )

            return ToolAgentPhaseOutput(
                summary=fix_desc or f"UI design issue addressed: {issue[:50]}",
                files=files,
                resolved=resolved or bool(files),
                metadata={"root_cause": raw.get("root_cause", "")},
            )

        except Exception as e:
            logger.warning("UIDesign fix_single_issue failed: %s", e)
            return ToolAgentPhaseOutput(
                summary=f"Fix failed: {str(e)[:50]}",
                resolved=False,
            )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Review phase: UI Design does not participate."""
        return ToolAgentPhaseOutput(summary="UI Design review not applicable (per matrix).")

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Problem-solving phase: UI Design does not participate."""
        return ToolAgentPhaseOutput(summary="UI Design problem_solve not applicable (per matrix).")

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Deliver phase: UI Design does not participate."""
        return ToolAgentPhaseOutput(summary="UI Design deliver not applicable (per matrix).")
