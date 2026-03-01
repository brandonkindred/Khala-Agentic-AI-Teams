"""
Architecture tool agent for planning-v2.

Participates in all 6 phases: Spec Review, Planning, Implementation, Review, Problem Solving, Deliver.
Focuses on high-level architecture, technology choices, and structural decisions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput
from ..json_utils import parse_json_with_recovery, default_decompose_by_sections, complete_with_continuation
from shared.models import ToolRecommendation, PricingTier, LicenseType

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)


def _parse_tool_recommendations(raw_recommendations: List[Dict[str, Any]]) -> List[ToolRecommendation]:
    """Parse raw tool recommendation dicts into ToolRecommendation models."""
    parsed = []
    for rec in raw_recommendations:
        if not isinstance(rec, dict):
            continue
        try:
            pricing_tier_str = rec.get("pricing_tier", "paid").lower()
            try:
                pricing_tier = PricingTier(pricing_tier_str)
            except ValueError:
                pricing_tier = PricingTier.PAID

            license_type_str = rec.get("license_type", "unknown").lower()
            try:
                license_type = LicenseType(license_type_str)
            except ValueError:
                license_type = LicenseType.UNKNOWN

            tool_rec = ToolRecommendation(
                name=rec.get("name", "Unknown"),
                category=rec.get("category", "unknown"),
                description=rec.get("description", ""),
                rationale=rec.get("rationale", ""),
                pricing_tier=pricing_tier,
                pricing_details=rec.get("pricing_details", ""),
                estimated_monthly_cost=rec.get("estimated_monthly_cost"),
                license_type=license_type,
                is_open_source=rec.get("is_open_source", False),
                source_url=rec.get("source_url"),
                ease_of_integration=rec.get("ease_of_integration", "medium"),
                learning_curve=rec.get("learning_curve", "moderate"),
                documentation_quality=rec.get("documentation_quality", "good"),
                community_size=rec.get("community_size", "medium"),
                maturity=rec.get("maturity", "mature"),
                vendor_lock_in_risk=rec.get("vendor_lock_in_risk", "low"),
                migration_complexity=rec.get("migration_complexity", "moderate"),
                alternatives=rec.get("alternatives", []),
                why_not_alternatives=rec.get("why_not_alternatives", ""),
                confidence=float(rec.get("confidence", 0.8)),
            )
            parsed.append(tool_rec)
        except Exception as e:
            logger.warning("Failed to parse tool recommendation: %s - %s", rec.get("name", "?"), e)
    return parsed


def _merge_architecture_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge architecture results from multiple chunks."""
    merged: Dict[str, Any] = {
        "architecture_style": "",
        "layers": [],
        "cross_cutting": [],
        "deployment_model": "",
        "recommendations": [],
        "summary": "",
    }
    styles = []
    deployments = []
    summaries = []

    for r in results:
        if r.get("architecture_style"):
            styles.append(str(r["architecture_style"]))
        if isinstance(r.get("layers"), list):
            merged["layers"].extend(r["layers"])
        if isinstance(r.get("cross_cutting"), list):
            merged["cross_cutting"].extend(r["cross_cutting"])
        if r.get("deployment_model"):
            deployments.append(str(r["deployment_model"]))
        if isinstance(r.get("recommendations"), list):
            merged["recommendations"].extend(r["recommendations"])
        if r.get("summary"):
            summaries.append(str(r["summary"]))

    merged["architecture_style"] = styles[0] if styles else ""
    merged["deployment_model"] = " ".join(deployments)
    merged["summary"] = f"Merged {len(results)} sections. " + " ".join(summaries[:2])
    return merged

ARCHITECTURE_SPEC_REVIEW_PROMPT = """You are an Architecture expert. Review this specification and identify:
1. Architectural patterns needed (monolith, microservices, event-driven, etc.)
2. Technology stack recommendations with structured details
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
  "tool_recommendations": [
    {{
      "name": "Technology Name",
      "category": "framework|database|cache|queue|monitoring|etc",
      "description": "What it does",
      "rationale": "Why recommended for this use case",
      "pricing_tier": "free|freemium|paid|enterprise|usage_based",
      "pricing_details": "Specific pricing info",
      "estimated_monthly_cost": "Cost estimate or null",
      "license_type": "mit|apache_2|gpl|bsd|proprietary|custom_oss|unknown",
      "is_open_source": true,
      "source_url": "GitHub URL if open source",
      "ease_of_integration": "low|medium|high",
      "learning_curve": "minimal|moderate|steep",
      "documentation_quality": "poor|adequate|good|excellent",
      "community_size": "small|medium|large|massive",
      "maturity": "emerging|growing|mature|legacy",
      "vendor_lock_in_risk": "none|low|medium|high",
      "migration_complexity": "trivial|moderate|complex",
      "alternatives": ["Alt1", "Alt2"],
      "why_not_alternatives": "Tradeoff explanation",
      "confidence": 0.85
    }}
  ],
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
  "tool_recommendations": [
    {{
      "name": "Technology Name",
      "category": "framework|database|cache|queue|monitoring|etc",
      "description": "What it does",
      "rationale": "Why recommended for this use case",
      "pricing_tier": "free|freemium|paid|enterprise|usage_based",
      "pricing_details": "Specific pricing info",
      "estimated_monthly_cost": "Cost estimate or null",
      "license_type": "mit|apache_2|gpl|bsd|proprietary|custom_oss|unknown",
      "is_open_source": true,
      "source_url": "GitHub URL if open source",
      "ease_of_integration": "low|medium|high",
      "learning_curve": "minimal|moderate|steep",
      "documentation_quality": "poor|adequate|good|excellent",
      "community_size": "small|medium|large|massive",
      "maturity": "emerging|growing|mature|legacy",
      "vendor_lock_in_risk": "none|low|medium|high",
      "migration_complexity": "trivial|moderate|complex",
      "alternatives": ["Alt1", "Alt2"],
      "why_not_alternatives": "Tradeoff explanation",
      "confidence": 0.85
    }}
  ],
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

ARCHITECTURE_PLANNING_CHUNK_PROMPT = """You are an Architecture expert. Analyze this SECTION of a specification for architecture:

SECTION:
---
{chunk_content}
---

Respond with concise JSON for THIS section only:
{{
  "architecture_style": "suggested pattern for this section",
  "layers": [{{"name": "layer_name", "technologies": ["tech1"], "responsibilities": "what it does"}}],
  "cross_cutting": ["concerns relevant to this section"],
  "deployment_model": "deployment considerations",
  "recommendations": ["architecture recommendations"],
  "summary": "brief summary"
}}
"""

ARCHITECTURE_FIX_SINGLE_ISSUE_PROMPT = """You are an Architecture expert. Fix this specific issue in the planning artifacts.

ISSUE TO FIX:
---
{issue}
---

CURRENT ARCHITECTURE ARTIFACT:
---
{current_artifact}
---

SPECIFICATION CONTEXT:
---
{spec_excerpt}
---

Analyze and fix this issue. Provide the complete updated file content.

Respond with JSON:
{{
  "root_cause": "why this issue exists",
  "fix_description": "what you are changing to fix it",
  "resolved": true or false,
  "updated_content": "the complete updated file content (or empty string if no change needed)"
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

        spec_content = inp.spec_content or ""
        prompt = ARCHITECTURE_PLANNING_PROMPT.format(
            spec_content=spec_content[:8000],
            prior_analysis=prior_analysis[:2000],
        )
        data = parse_json_with_recovery(
            self.llm,
            prompt,
            agent_name="Architecture",
            decompose_fn=default_decompose_by_sections,
            merge_fn=_merge_architecture_results,
            original_content=spec_content,
            chunk_prompt_template=ARCHITECTURE_PLANNING_CHUNK_PROMPT,
        )
        
        recommendations = data.get("recommendations") or []
        if not isinstance(recommendations, list):
            recommendations = [str(recommendations)]

        raw_tool_recs = data.get("tool_recommendations") or []
        if not isinstance(raw_tool_recs, list):
            raw_tool_recs = []
        tool_recommendations = _parse_tool_recommendations(raw_tool_recs)
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", "Architecture planning complete."),
            recommendations=recommendations,
            tool_recommendations=tool_recommendations,
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

        all_files: Dict[str, str] = {}
        fixes_applied: List[str] = []

        for issue in arch_issues:
            result = self.fix_single_issue(issue, inp)
            if result.files:
                all_files.update(result.files)
                fixes_applied.append(result.summary)

        return ToolAgentPhaseOutput(
            summary=f"Architecture: fixed {len(fixes_applied)}/{len(arch_issues)} issue(s).",
            recommendations=fixes_applied,
            files=all_files,
            resolved=len(fixes_applied) == len(arch_issues),
        )

    def fix_single_issue(self, issue: str, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix a single architecture issue.

        Args:
            issue: The issue description to fix.
            inp: Tool agent phase input with context.

        Returns:
            ToolAgentPhaseOutput with updated files if fix was applied.
        """
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="Architecture fix skipped (no LLM).",
                resolved=False,
            )

        current_artifact = inp.current_files.get("plan/architecture.md", "")
        if not current_artifact:
            for path, content in inp.current_files.items():
                if "architect" in path.lower():
                    current_artifact = content
                    break

        prompt = ARCHITECTURE_FIX_SINGLE_ISSUE_PROMPT.format(
            issue=issue,
            current_artifact=current_artifact[:6000] if current_artifact else "(no existing artifact)",
            spec_excerpt=(inp.spec_content or "")[:3000],
        )

        try:
            raw = complete_with_continuation(
                llm=self.llm,
                prompt=prompt,
                mode="json",
                agent_name="Architecture_FixSingleIssue",
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
                files["plan/architecture.md"] = updated_content
                logger.info("Architecture: fix applied — %s", fix_desc[:60])

            return ToolAgentPhaseOutput(
                summary=fix_desc or f"Architecture issue addressed: {issue[:50]}",
                files=files,
                resolved=resolved or bool(files),
                metadata={"root_cause": raw.get("root_cause", "")},
            )

        except Exception as e:
            logger.warning("Architecture fix_single_issue failed: %s", e)
            return ToolAgentPhaseOutput(
                summary=f"Fix failed: {str(e)[:50]}",
                resolved=False,
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

        raw_tool_recs = data.get("tool_recommendations") or []
        if not isinstance(raw_tool_recs, list):
            raw_tool_recs = []
        tool_recommendations = _parse_tool_recommendations(raw_tool_recs)
        
        return ToolAgentPhaseOutput(
            summary=data.get("summary", "Architecture spec review complete."),
            issues=risks,
            tool_recommendations=tool_recommendations,
            metadata={
                "patterns": data.get("patterns", []),
                "tech_stack": data.get("tech_stack", {}),
                "nfrs": data.get("nfrs", []),
            },
        )
