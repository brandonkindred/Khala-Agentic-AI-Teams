"""
Architecture tool agent for planning-v2.

Participates in all 6 phases: Spec Review, Planning, Implementation, Review, Problem Solving, Deliver.
Focuses on high-level architecture, technology choices, and structural decisions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput
from ..json_utils import parse_json_with_recovery

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)

ARCHITECTURE_SPEC_REVIEW_PROMPT = """You are an Architecture expert. Review this specification and identify:
1. Architectural patterns needed (monolith, microservices, event-driven, etc.)
2. Technology stack recommendations
3. Non-functional requirements (performance, security, scalability)
4. Architectural risks or constraints

Specification:
---
{spec_content}
---

Respond with JSON:
{{
  "patterns": ["recommended architectural patterns"],
  "tech_stack": {{"frontend": "...", "backend": "...", "database": "...", "infrastructure": "..."}},
  "nfrs": ["non-functional requirements"],
  "risks": ["architectural risks"],
  "summary": "brief summary"
}}
"""

ARCHITECTURE_PLANNING_PROMPT = """You are an Architecture expert. Create an architecture plan for:

Specification:
---
{spec_content}
---

Prior analysis: {prior_analysis}

Respond with JSON:
{{
  "architecture_style": "chosen architecture pattern",
  "layers": [{{"name": "layer_name", "technologies": ["tech1"], "responsibilities": "what it does"}}],
  "cross_cutting": ["logging", "security", "monitoring"],
  "deployment_model": "how it will be deployed",
  "recommendations": ["architecture recommendations"],
  "summary": "brief summary"
}}
"""

ARCHITECTURE_REVIEW_PROMPT = """You are an Architecture expert. Review these planning artifacts for architectural coherence:

Artifacts:
---
{artifacts}
---

Respond with JSON:
{{
  "passed": true or false,
  "issues": ["list of architecture issues found"],
  "recommendations": ["improvements"],
  "summary": "brief summary"
}}
"""




class ArchitectureToolAgent:
    """
    Architecture tool agent: high-level architecture, technology choices, structural decisions.
    
    Participates in all 6 phases per the matrix.
    """

    def __init__(self, llm: Optional["LLMClient"] = None) -> None:
        self.llm = llm

    def plan(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Planning phase: create architecture plan."""
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="Architecture planning skipped (no LLM).",
                recommendations=["Define architecture style", "Choose technology stack"],
            )
        
        prior_analysis = ""
        if inp.spec_review_result:
            prior_analysis = getattr(inp.spec_review_result, "plan_summary", "") or ""

        prompt = ARCHITECTURE_PLANNING_PROMPT.format(
            spec_content=(inp.spec_content or "")[:8000],
            prior_analysis=prior_analysis[:2000],
        )
        data = parse_json_with_recovery(self.llm, prompt, agent_name="Architecture")
        
        recommendations = data.get("recommendations") or []
        if not isinstance(recommendations, list):
            recommendations = [str(recommendations)]
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", "Architecture planning complete."),
            recommendations=recommendations,
            metadata={
                "architecture_style": data.get("architecture_style", ""),
                "layers": data.get("layers", []),
                "cross_cutting": data.get("cross_cutting", []),
                "deployment_model": data.get("deployment_model", ""),
            },
        )

    def execute(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Implementation phase: generate architecture artifacts."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Architecture execute skipped (no LLM).")
        
        arch_style = inp.metadata.get("architecture_style", "")
        layers = inp.metadata.get("layers", [])
        cross_cutting = inp.metadata.get("cross_cutting", [])
        deployment_model = inp.metadata.get("deployment_model", "")
        
        content_parts = ["# Architecture\n"]
        
        if arch_style:
            content_parts.append(f"## Architecture Style\n{arch_style}\n\n")
        
        if layers:
            content_parts.append("## Layers\n")
            for layer in layers:
                if isinstance(layer, dict):
                    name = layer.get("name", "Unknown")
                    techs = layer.get("technologies", [])
                    resp = layer.get("responsibilities", "")
                    content_parts.append(f"### {name}\n")
                    content_parts.append(f"**Technologies:** {', '.join(techs)}\n")
                    content_parts.append(f"**Responsibilities:** {resp}\n\n")
        
        if cross_cutting:
            content_parts.append("## Cross-Cutting Concerns\n")
            for concern in cross_cutting:
                content_parts.append(f"- {concern}\n")
            content_parts.append("\n")
        
        if deployment_model:
            content_parts.append(f"## Deployment Model\n{deployment_model}\n\n")
        
        files = {}
        if arch_style or layers:
            files["plan/architecture.md"] = "".join(content_parts)
        
        return ToolAgentPhaseOutput(
            summary="Architecture artifacts generated.",
            files=files,
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Review phase: check architecture coherence."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Architecture review skipped (no LLM).")
        
        artifacts = "\n".join(
            f"--- {path} ---\n{content}"
            for path, content in list(inp.current_files.items())[:10]
        )[:8000]
        
        if not artifacts.strip():
            return ToolAgentPhaseOutput(
                summary="Architecture review skipped (no artifacts).",
                issues=[],
            )
        
        prompt = ARCHITECTURE_REVIEW_PROMPT.format(artifacts=artifacts)
        data = parse_json_with_recovery(self.llm, prompt, agent_name="Architecture")
        
        issues = data.get("issues") or []
        if not isinstance(issues, list):
            issues = [str(issues)] if issues else []
        
        recommendations = data.get("recommendations") or []
        if not isinstance(recommendations, list):
            recommendations = [str(recommendations)] if recommendations else []
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", "Architecture review complete."),
            issues=issues,
            recommendations=recommendations,
        )

    def problem_solve(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Problem-solving phase: address architecture issues."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Architecture problem_solve skipped (no LLM).")
        
        arch_issues = [i for i in inp.review_issues if "architect" in i.lower() or "layer" in i.lower()]
        if not arch_issues:
            return ToolAgentPhaseOutput(summary="No architecture issues to resolve.")
        
        return ToolAgentPhaseOutput(
            summary=f"Architecture: {len(arch_issues)} issue(s) identified for resolution.",
            recommendations=[f"Address: {issue}" for issue in arch_issues[:5]],
        )

    def deliver(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Deliver phase: finalize architecture documentation."""
        return ToolAgentPhaseOutput(
            summary="Architecture documentation finalized.",
            recommendations=["Ensure architecture docs are committed to repo"],
        )

    def spec_review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Spec Review phase: analyze spec for architecture concerns."""
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="Architecture spec review skipped (no LLM).",
                recommendations=["Review spec for architecture patterns"],
            )
        
        prompt = ARCHITECTURE_SPEC_REVIEW_PROMPT.format(
            spec_content=(inp.spec_content or "")[:10000],
        )
        data = parse_json_with_recovery(self.llm, prompt, agent_name="Architecture")
        
        risks = data.get("risks") or []
        if not isinstance(risks, list):
            risks = [str(risks)] if risks else []
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", "Architecture spec review complete."),
            issues=risks,
            metadata={
                "patterns": data.get("patterns", []),
                "tech_stack": data.get("tech_stack", {}),
                "nfrs": data.get("nfrs", []),
            },
        )
