"""
UI Design tool agent for planning-v2.

Participates in phases: Planning, Implementation.
Focuses on visual design, component library, and design system planning.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput
from ..json_utils import parse_json_with_recovery, default_decompose_by_sections, complete_with_continuation

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)


def _merge_ui_design_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge UI design results from multiple chunks."""
    merged: Dict[str, Any] = {
        "design_tokens": {},
        "components": [],
        "layouts": [],
        "breakpoints": {},
        "accessibility": [],
        "recommendations": [],
        "summary": "",
    }
    summaries = []

    for r in results:
        if isinstance(r.get("design_tokens"), dict):
            for k, v in r["design_tokens"].items():
                if k not in merged["design_tokens"]:
                    merged["design_tokens"][k] = v
                elif isinstance(v, list) and isinstance(merged["design_tokens"][k], list):
                    merged["design_tokens"][k].extend(v)
        if isinstance(r.get("components"), list):
            for c in r["components"]:
                if c not in merged["components"]:
                    merged["components"].append(c)
        if isinstance(r.get("layouts"), list):
            for lay in r["layouts"]:
                if lay not in merged["layouts"]:
                    merged["layouts"].append(lay)
        if isinstance(r.get("breakpoints"), dict):
            merged["breakpoints"].update(r["breakpoints"])
        if isinstance(r.get("accessibility"), list):
            merged["accessibility"].extend(r["accessibility"])
        if isinstance(r.get("recommendations"), list):
            merged["recommendations"].extend(r["recommendations"])
        if r.get("summary"):
            summaries.append(str(r["summary"]))

    merged["summary"] = f"Merged {len(results)} sections. " + " ".join(summaries[:2])
    return merged

UI_DESIGN_PLANNING_PROMPT = """You are a UI Design expert. Create a UI design plan for:

Specification:
---
{spec_content}
---

Plan for:
1. Design system (colors, typography, spacing)
2. Component library (buttons, forms, cards, etc.)
3. Page layouts and templates
4. Responsive breakpoints
5. Accessibility considerations

Respond with JSON:
{{
  "design_tokens": {{"colors": ["primary", "secondary"], "typography": ["heading", "body"], "spacing": ["sm", "md", "lg"]}},
  "components": ["Button", "Card", "Form", "Modal", "Navigation"],
  "layouts": ["Dashboard", "Detail", "List", "Auth"],
  "breakpoints": {{"mobile": "320px", "tablet": "768px", "desktop": "1024px"}},
  "accessibility": ["WCAG 2.1 AA", "keyboard navigation", "screen reader support"],
  "recommendations": ["ui design recommendations"],
  "summary": "brief summary"
}}
"""

UI_DESIGN_PLANNING_CHUNK_PROMPT = """You are a UI Design expert. Analyze this SECTION for UI design:

SECTION:
---
{chunk_content}
---

Respond with concise JSON for THIS section only:
{{
  "design_tokens": {{"relevant": "tokens"}},
  "components": ["components needed"],
  "layouts": ["layouts needed"],
  "accessibility": ["considerations"],
  "recommendations": ["recommendations"],
  "summary": "brief summary"
}}
"""

UI_DESIGN_FIX_SINGLE_ISSUE_PROMPT = """You are a UI Design expert. Fix this specific issue in the UI design artifacts.

ISSUE TO FIX:
---
{issue}
---

CURRENT UI DESIGN ARTIFACT:
---
{current_artifact}
---

SPECIFICATION CONTEXT:
---
{spec_excerpt}
---

Analyze and fix this issue. If the issue relates to design tokens, components, layouts, breakpoints, or accessibility, provide the complete updated file content.

Respond with JSON:
{{
  "root_cause": "why this issue exists",
  "fix_description": "what you are changing to fix it",
  "resolved": true or false,
  "updated_content": "the complete updated file content (or empty string if no change needed)"
}}
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
        data = parse_json_with_recovery(
            self.llm,
            prompt,
            agent_name="UIDesign",
            decompose_fn=default_decompose_by_sections,
            merge_fn=_merge_ui_design_results,
            original_content=spec_content,
            chunk_prompt_template=UI_DESIGN_PLANNING_CHUNK_PROMPT,
        )
        
        recommendations = data.get("recommendations") or []
        if not isinstance(recommendations, list):
            recommendations = [str(recommendations)]
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", "UI Design planning complete."),
            recommendations=recommendations,
            metadata={
                "design_tokens": data.get("design_tokens", {}),
                "components": data.get("components", []),
                "layouts": data.get("layouts", []),
                "breakpoints": data.get("breakpoints", {}),
                "accessibility": data.get("accessibility", []),
            },
        )

    def execute(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Implementation phase: generate or update UI design artifacts.
        
        If review_issues are provided, this agent handles fixes first.
        Only regenerates the document if it doesn't already exist.
        """
        all_files: Dict[str, str] = {}
        fixes_applied: List[str] = []
        
        ui_issues = [
            i for i in inp.review_issues
            if any(kw in i.lower() for kw in ["ui", "design token", "component", "layout", "breakpoint", "accessibility", "visual", "responsive"])
        ]
        
        if ui_issues and self.llm:
            logger.info("UIDesign: handling %d review issues", len(ui_issues))
            for issue in ui_issues:
                result = self.fix_single_issue(issue, inp)
                if result.files:
                    all_files.update(result.files)
                    fixes_applied.append(result.summary)
            logger.info("UIDesign: fixed %d/%d issues", len(fixes_applied), len(ui_issues))
        
        existing_doc = inp.current_files.get("plan/ui_design.md") if inp.current_files else None
        if existing_doc or all_files.get("plan/ui_design.md"):
            summary = "UI Design artifacts updated."
            if fixes_applied:
                summary = f"UI Design artifacts updated. Fixed {len(fixes_applied)} review issues."
            return ToolAgentPhaseOutput(
                summary=summary,
                files=all_files,
                recommendations=fixes_applied if fixes_applied else [],
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
            all_files["plan/ui_design.md"] = "".join(content_parts)
        
        return ToolAgentPhaseOutput(
            summary="UI Design artifacts generated.",
            files=all_files,
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
            current_artifact = inp.current_files.get("plan/ui_design.md", "")
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
            raw = complete_with_continuation(
                llm=self.llm,
                prompt=prompt,
                mode="json",
                agent_name="UIDesign_FixSingleIssue",
            )

            if not isinstance(raw, dict):
                return ToolAgentPhaseOutput(
                    summary="Fix failed: invalid response format",
                    resolved=False,
                )

            updated_content = raw.get("updated_content", "")
            fix_desc = raw.get("fix_description", "")
            resolved = raw.get("resolved", False)

            files: Dict[str, str] = {}
            if updated_content and isinstance(updated_content, str) and updated_content.strip():
                files["plan/ui_design.md"] = updated_content
                logger.info("UIDesign: fix applied — %s", fix_desc[:60])

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
