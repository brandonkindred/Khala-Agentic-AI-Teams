"""
DevOps tool agent for planning-v2.

Participates in phases: Planning, Implementation.
Focuses on CI/CD pipelines, infrastructure, and deployment planning.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput
from ..json_utils import parse_json_with_recovery, default_decompose_by_sections, complete_with_continuation

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)


def _merge_devops_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge devops results from multiple chunks."""
    merged: Dict[str, Any] = {
        "needs_clarification": False,
        "clarification_questions": [],
        "pipeline_stages": [],
        "infrastructure": {},
        "deployment_strategy": "",
        "monitoring": [],
        "security": [],
        "recommendations": [],
        "summary": "",
    }
    strategies = []
    summaries = []

    for r in results:
        if r.get("needs_clarification"):
            merged["needs_clarification"] = True
        if isinstance(r.get("clarification_questions"), list):
            for q in r["clarification_questions"]:
                if q not in merged["clarification_questions"]:
                    merged["clarification_questions"].append(q)
        if isinstance(r.get("pipeline_stages"), list):
            for stage in r["pipeline_stages"]:
                if stage not in merged["pipeline_stages"]:
                    merged["pipeline_stages"].append(stage)
        if isinstance(r.get("infrastructure"), dict):
            merged["infrastructure"].update(r["infrastructure"])
        if r.get("deployment_strategy"):
            strategies.append(str(r["deployment_strategy"]))
        if isinstance(r.get("monitoring"), list):
            merged["monitoring"].extend(r["monitoring"])
        if isinstance(r.get("security"), list):
            merged["security"].extend(r["security"])
        if isinstance(r.get("recommendations"), list):
            merged["recommendations"].extend(r["recommendations"])
        if r.get("summary"):
            summaries.append(str(r["summary"]))

    merged["deployment_strategy"] = strategies[0] if strategies else ""
    merged["summary"] = f"Merged {len(results)} sections. " + " ".join(summaries[:2])
    return merged

DEVOPS_PLANNING_PROMPT = """You are a DevOps expert. Create a DevOps plan for:

Specification:
---
{spec_content}
---

Plan summary from spec review: {plan_summary}

CRITICAL RULES:
1. Do NOT assume any deployment target or cloud provider if not explicitly stated in the specification.
2. If the spec does not clearly specify WHERE the application should be deployed (e.g., Heroku, AWS, DigitalOcean, on-premises), you MUST:
   - Set "needs_clarification": true
   - Add questions about deployment target to "clarification_questions"
   - Do NOT generate provider-specific infrastructure recommendations
3. If deployment IS specified, prefer cost-effective solutions:
   - For simple apps: Heroku, Railway, Render, DigitalOcean App Platform
   - Only suggest AWS/GCP/Azure if the requirements explicitly justify it (scale, compliance, specific services)

Plan for:
1. CI/CD pipeline stages
2. Infrastructure requirements (ONLY if deployment target is specified)
3. Deployment strategy
4. Monitoring and observability
5. Security considerations

Respond with JSON:
{{
  "needs_clarification": false,
  "clarification_questions": [],
  "pipeline_stages": ["build", "test", "deploy"],
  "infrastructure": {{"compute": "...", "database": "...", "networking": "..."}},
  "deployment_strategy": "blue-green|rolling|canary",
  "monitoring": ["metrics", "logs", "traces"],
  "security": ["secrets management", "network policies"],
  "recommendations": ["devops recommendations"],
  "summary": "brief summary"
}}

If deployment target is NOT specified, respond with:
{{
  "needs_clarification": true,
  "clarification_questions": [
    "Where should this application be deployed? (e.g., Heroku, Railway, DigitalOcean, AWS, on-premises)",
    "What are the expected SLA requirements (uptime, RTO, RPO)?",
    "Are there any budget constraints for infrastructure?"
  ],
  "pipeline_stages": ["build", "test"],
  "infrastructure": {{}},
  "deployment_strategy": "",
  "monitoring": [],
  "security": [],
  "recommendations": ["Deployment target must be specified before infrastructure planning can proceed"],
  "summary": "Cannot complete DevOps planning - deployment target not specified in specification"
}}
"""

DEVOPS_PLANNING_CHUNK_PROMPT = """You are a DevOps expert. Analyze this SECTION of a specification for DevOps:

SECTION:
---
{chunk_content}
---

Respond with concise JSON for THIS section only:
{{
  "pipeline_stages": ["relevant stages"],
  "infrastructure": {{"relevant": "requirements"}},
  "deployment_strategy": "strategy if applicable",
  "monitoring": ["monitoring needs"],
  "security": ["security considerations"],
  "recommendations": ["devops recommendations"],
  "summary": "brief summary"
}}
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

Analyze and fix this issue. If the issue relates to CI/CD pipelines, infrastructure, deployment, monitoring, or security, provide the complete updated file content.

Respond with JSON:
{{
  "root_cause": "why this issue exists",
  "fix_description": "what you are changing to fix it",
  "resolved": true or false,
  "updated_content": "the complete updated file content (or empty string if no change needed)"
}}
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
        data = parse_json_with_recovery(
            self.llm,
            prompt,
            agent_name="DevOps",
            decompose_fn=default_decompose_by_sections,
            merge_fn=_merge_devops_results,
            original_content=spec_content,
            chunk_prompt_template=DEVOPS_PLANNING_CHUNK_PROMPT,
        )
        
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
            raw = complete_with_continuation(
                llm=self.llm,
                prompt=prompt,
                mode="json",
                agent_name="DevOps_FixSingleIssue",
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
                files["plan/devops.md"] = updated_content
                logger.info("DevOps: fix applied — %s", fix_desc[:60])

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
