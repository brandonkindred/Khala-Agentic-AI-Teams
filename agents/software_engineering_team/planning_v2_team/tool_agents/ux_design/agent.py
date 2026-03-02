"""
UX Design tool agent for planning-v2.

Participates in phases: Implementation only.
Focuses on user experience, user flows, and interaction design.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput
from ..json_utils import parse_json_with_recovery, default_decompose_by_sections, complete_with_continuation

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)


def _merge_ux_design_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge UX design results from multiple chunks."""
    merged: Dict[str, Any] = {
        "personas": [],
        "user_journeys": [],
        "user_flows": [],
        "interaction_patterns": [],
        "usability": [],
        "summary": "",
    }
    summaries = []

    for r in results:
        if isinstance(r.get("personas"), list):
            merged["personas"].extend(r["personas"])
        if isinstance(r.get("user_journeys"), list):
            merged["user_journeys"].extend(r["user_journeys"])
        if isinstance(r.get("user_flows"), list):
            merged["user_flows"].extend(r["user_flows"])
        if isinstance(r.get("interaction_patterns"), list):
            merged["interaction_patterns"].extend(r["interaction_patterns"])
        if isinstance(r.get("usability"), list):
            merged["usability"].extend(r["usability"])
        if r.get("summary"):
            summaries.append(str(r["summary"]))

    merged["summary"] = f"Merged {len(results)} sections. " + " ".join(summaries[:2])
    return merged

UX_DESIGN_IMPLEMENTATION_PROMPT = """You are a UX Design expert. Create UX artifacts for:

Specification:
---
{spec_content}
---

Create:
1. User personas
2. User journey maps
3. Key user flows
4. Interaction patterns
5. Usability considerations

Respond with JSON:
{{
  "personas": [{{"name": "User type", "goals": ["goal1"], "pain_points": ["pain1"]}}],
  "user_journeys": [{{"name": "Journey name", "stages": ["awareness", "consideration", "action"]}}],
  "user_flows": [{{"name": "Flow name", "steps": ["step1", "step2"]}}],
  "interaction_patterns": ["pattern1", "pattern2"],
  "usability": ["consideration1", "consideration2"],
  "summary": "brief summary"
}}
"""

UX_DESIGN_IMPLEMENTATION_CHUNK_PROMPT = """You are a UX Design expert. Analyze this SECTION for UX:

SECTION:
---
{chunk_content}
---

Respond with concise JSON for THIS section only:
{{
  "personas": [{{"name": "User type", "goals": ["goal"], "pain_points": ["pain"]}}],
  "user_journeys": [{{"name": "Journey", "stages": ["stage"]}}],
  "user_flows": [{{"name": "Flow", "steps": ["step"]}}],
  "interaction_patterns": ["patterns"],
  "usability": ["considerations"],
  "summary": "brief summary"
}}
"""

UX_DESIGN_FIX_SINGLE_ISSUE_PROMPT = """You are a UX Design expert. Fix this specific issue in the UX design artifacts.

ISSUE TO FIX:
---
{issue}
---

CURRENT UX DESIGN ARTIFACT:
---
{current_artifact}
---

SPECIFICATION CONTEXT:
---
{spec_excerpt}
---

Analyze and fix this issue. If the issue relates to personas, user journeys, user flows, interaction patterns, or usability considerations, provide the complete updated file content.

Respond with JSON:
{{
  "root_cause": "why this issue exists",
  "fix_description": "what you are changing to fix it",
  "resolved": true or false,
  "updated_content": "the complete updated file content (or empty string if no change needed)"
}}
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
        data = parse_json_with_recovery(
            self.llm,
            prompt,
            agent_name="UXDesign",
            decompose_fn=default_decompose_by_sections,
            merge_fn=_merge_ux_design_results,
            original_content=spec_content,
            chunk_prompt_template=UX_DESIGN_IMPLEMENTATION_CHUNK_PROMPT,
        )
        
        personas = data.get("personas") or []
        user_journeys = data.get("user_journeys") or []
        user_flows = data.get("user_flows") or []
        interaction_patterns = data.get("interaction_patterns") or []
        usability = data.get("usability") or []
        
        content_parts = ["# UX Design\n\n"]
        
        if personas:
            content_parts.append("## User Personas\n")
            for persona in personas:
                if isinstance(persona, dict):
                    name = persona.get("name", "User")
                    goals = persona.get("goals", [])
                    pain_points = persona.get("pain_points", [])
                    content_parts.append(f"### {name}\n")
                    if goals:
                        content_parts.append("**Goals:**\n")
                        for g in goals:
                            content_parts.append(f"- {g}\n")
                    if pain_points:
                        content_parts.append("**Pain Points:**\n")
                        for p in pain_points:
                            content_parts.append(f"- {p}\n")
                    content_parts.append("\n")
        
        if user_journeys:
            content_parts.append("## User Journeys\n")
            for journey in user_journeys:
                if isinstance(journey, dict):
                    name = journey.get("name", "Journey")
                    stages = journey.get("stages", [])
                    content_parts.append(f"### {name}\n")
                    stages_str = [str(s) if not isinstance(s, str) else s for s in stages]
                    content_parts.append(f"Stages: {' → '.join(stages_str)}\n\n")
        
        if user_flows:
            content_parts.append("## User Flows\n")
            for flow in user_flows:
                if isinstance(flow, dict):
                    name = flow.get("name", "Flow")
                    steps = flow.get("steps", [])
                    content_parts.append(f"### {name}\n")
                    for i, step in enumerate(steps, 1):
                        content_parts.append(f"{i}. {step}\n")
                    content_parts.append("\n")
        
        if interaction_patterns:
            content_parts.append("## Interaction Patterns\n")
            for pattern in interaction_patterns:
                content_parts.append(f"- {pattern}\n")
            content_parts.append("\n")
        
        if usability:
            content_parts.append("## Usability Considerations\n")
            for item in usability:
                content_parts.append(f"- {item}\n")
            content_parts.append("\n")
        
        if personas or user_flows:
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
            raw = complete_with_continuation(
                llm=self.llm,
                prompt=prompt,
                mode="json",
                agent_name="UXDesign_FixSingleIssue",
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
                files["plan/ux_design.md"] = updated_content
                logger.info("UXDesign: fix applied — %s", fix_desc[:60])

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
