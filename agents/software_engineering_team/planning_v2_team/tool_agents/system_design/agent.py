"""
System Design tool agent for planning-v2.

Participates in all 6 phases: Spec Review, Planning, Implementation, Review, Problem Solving, Deliver.
Focuses on component layout, system boundaries, and integration points.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput
from ..json_utils import parse_json_with_recovery

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

Respond with JSON:
{{
  "components": ["list of identified components"],
  "integration_points": ["list of integration points"],
  "gaps": ["list of design gaps"],
  "scalability_notes": "scalability considerations",
  "summary": "brief summary"
}}
"""

SYSTEM_DESIGN_PLANNING_PROMPT = """You are a System Design expert. Create a system design plan for:

Specification:
---
{spec_content}
---

Prior analysis: {prior_analysis}

Respond with JSON:
{{
  "component_design": [{{"name": "component_name", "responsibility": "what it does", "dependencies": ["dep1"]}}],
  "data_flow": "description of data flow between components",
  "integration_strategy": "how components integrate",
  "recommendations": ["design recommendations"],
  "summary": "brief summary"
}}
"""

SYSTEM_DESIGN_REVIEW_PROMPT = """You are a System Design expert. Review these planning artifacts for design coherence:

Artifacts:
---
{artifacts}
---

Respond with JSON:
{{
  "passed": true or false,
  "issues": ["list of design issues found"],
  "recommendations": ["improvements"],
  "summary": "brief summary"
}}
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

        prompt = SYSTEM_DESIGN_PLANNING_PROMPT.format(
            spec_content=(inp.spec_content or "")[:8000],
            prior_analysis=prior_analysis[:2000],
        )
        data = parse_json_with_recovery(self.llm, prompt, agent_name="SystemDesign")
        
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
        """Implementation phase: generate system design artifacts."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="System Design execute skipped (no LLM).")
        
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
        
        files = {}
        if component_design or data_flow:
            files["plan/system_design.md"] = "".join(content_parts)
        
        return ToolAgentPhaseOutput(
            summary="System design artifacts generated.",
            files=files,
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
        data = parse_json_with_recovery(self.llm, prompt, agent_name="SystemDesign")
        
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
        
        return ToolAgentPhaseOutput(
            summary=f"System design: {len(design_issues)} issue(s) identified for resolution.",
            recommendations=[f"Address: {issue}" for issue in design_issues[:5]],
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
        data = parse_json_with_recovery(self.llm, prompt, agent_name="SystemDesign")
        
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
