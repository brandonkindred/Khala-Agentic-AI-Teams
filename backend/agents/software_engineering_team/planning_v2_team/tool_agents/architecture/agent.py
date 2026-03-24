"""
Architecture tool agent for planning-v2.

Participates in all 6 phases: Spec Review, Planning, Implementation, Review, Problem Solving, Deliver.
Focuses on high-level architecture, technology choices, and structural decisions.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from software_engineering_team.shared.models import LicenseType, PricingTier, ToolRecommendation

from ...models import ToolAgentPhaseInput, ToolAgentPhaseOutput, planning_asset_path
from ...output_templates import (
    looks_like_truncated_file_content,
    parse_architecture_planning_output,
    parse_fix_output,
    parse_review_output,
    parse_spec_review_output,
)
from ..json_utils import attempt_fix_output_continuation, complete_text_with_continuation

if TYPE_CHECKING:
    from llm_service import LLMClient

logger = logging.getLogger(__name__)


def _parse_tool_recommendations(
    raw_recommendations: List[Dict[str, Any]],
) -> List[ToolRecommendation]:
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


ARCHITECTURE_SPEC_REVIEW_PROMPT = """You are an Architecture expert. Review this specification and identify architectural patterns, gaps (as issues), and a brief summary.

Specification:
---
{spec_content}
---

Respond using this EXACT format:

## COMPONENTS ##
- Component or pattern 1
- Component or pattern 2
## END COMPONENTS ##

## INTEGRATION_POINTS ##
- Integration point 1
## END INTEGRATION_POINTS ##

## GAPS ##
- Architectural gap or risk 1
- Architectural gap or risk 2
## END GAPS ##

## SCALABILITY_NOTES ##
Brief scalability considerations.
## END SCALABILITY_NOTES ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##
"""

ARCHITECTURE_PLANNING_PROMPT = """You are an Architecture expert. Create an architecture plan for:

Specification:
---
{spec_content}
---

Prior analysis: {prior_analysis}

Respond using this EXACT format:

## ARCHITECTURE_STYLE ##
Chosen architecture pattern (e.g. layered, microservices).
## END ARCHITECTURE_STYLE ##

## LAYERS ##
Presentation: Angular, React - UI and user interaction
Business: Python, Node - business logic
Data: PostgreSQL, Redis - persistence and cache
## END LAYERS ##

## CROSS_CUTTING ##
- Logging
- Security
- Monitoring
## END CROSS_CUTTING ##

## DEPLOYMENT_MODEL ##
How the system will be deployed (e.g. Docker, K8s).
## END DEPLOYMENT_MODEL ##

## RECOMMENDATIONS ##
- Recommendation 1
- Recommendation 2
## END RECOMMENDATIONS ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##
"""

ARCHITECTURE_REVIEW_PROMPT = """You are an Architecture expert. Review these planning artifacts for architectural coherence:

Artifacts:
---
{artifacts}
---

Respond using this EXACT format:

## PASSED ##
true or false
## END PASSED ##

## ISSUES ##
- Issue 1
- Issue 2
## END ISSUES ##

## RECOMMENDATIONS ##
- Improvement 1
- Improvement 2
## END RECOMMENDATIONS ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##
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

Analyze and fix this issue. Provide the complete updated file content using the format below.

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

Output the complete updated file content; do not truncate. Include every section in full.

## FILE_UPDATES ##
### plan/planning_team/architecture.md ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##
"""

ARCHITECTURE_FIX_ALL_ISSUES_PROMPT = """You are an Architecture expert. Address ALL of the following issues in the planning artifacts in ONE coherent update.

ISSUES TO FIX (address every one):
---
{issues_list}
---

CURRENT ARCHITECTURE ARTIFACT:
---
{current_artifact}
---

SPECIFICATION CONTEXT:
---
{spec_excerpt}
---

Analyze and fix every listed issue in a single coherent update. Provide the complete updated file content using the format below.
Output the complete updated file content; do not truncate. Include every section in full.

Respond using this EXACT format:

## ROOT_CAUSE ##
Brief combined root cause for the issues.
## END ROOT_CAUSE ##

## FIX_DESCRIPTION ##
What you are changing to address all issues.
## END FIX_DESCRIPTION ##

## RESOLVED ##
true or false
## END RESOLVED ##

## FILE_UPDATES ##
### plan/planning_team/architecture.md ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##
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
        raw_text = complete_text_with_continuation(
            self.llm,
            prompt,
            agent_name="Architecture",
        )
        data = parse_architecture_planning_output(raw_text)
        recommendations = data.get("recommendations") or []
        if not isinstance(recommendations, list):
            recommendations = [str(recommendations)]

        return ToolAgentPhaseOutput(
            summary=data.get("summary", "Architecture planning complete."),
            recommendations=recommendations,
            tool_recommendations=[],
            metadata={
                "architecture_style": data.get("architecture_style", ""),
                "layers": data.get("layers", []),
                "cross_cutting": data.get("cross_cutting", []),
                "deployment_model": data.get("deployment_model", ""),
            },
        )

    def execute(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Implementation phase: generate architecture artifacts and fix review issues.
        Writes to disk as fixes are applied; returns files_written so implementation phase does not overwrite.
        """
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Architecture execute skipped (no LLM).")

        fixes_applied: List[str] = []
        files_written: List[str] = []
        current_files: Dict[str, str] = dict(inp.current_files or {})

        arch_issues = [
            i
            for i in inp.review_issues
            if any(
                kw in i.lower()
                for kw in ["architect", "layer", "module", "component", "integration", "deployment"]
            )
        ]

        if arch_issues:
            logger.info(
                "Architecture: handling %d review issue(s) (will apply fixes in one update and write to disk).",
                len(arch_issues),
            )
            fix_inp = inp.model_copy(update={"current_files": current_files})
            result = self.fix_all_issues(arch_issues, fix_inp)
            if result.files:
                repo = Path(inp.repo_path or ".")
                for rel_path, content in result.files.items():
                    full_path = repo / rel_path
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(content, encoding="utf-8")
                    file_name = full_path.name
                    logger.info(
                        "Architecture: applied fix — writing to file: %s (%d chars)",
                        file_name,
                        len(content),
                    )
                    if rel_path not in files_written:
                        files_written.append(rel_path)
                    current_files[rel_path] = content
                fixes_applied.append(result.summary)
            logger.info(
                "Architecture: fixed %d review issue(s) in one update (all fixes written to planning artifacts).",
                len(arch_issues),
            )

        existing_arch = (inp.current_files or {}).get(planning_asset_path("architecture.md"))
        if existing_arch and not arch_issues:
            return ToolAgentPhaseOutput(
                summary="Architecture artifacts unchanged (file exists, no review issues).",
                files={},
                recommendations=fixes_applied if fixes_applied else [],
                files_written=[],
            )

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

        if (arch_style or layers) and planning_asset_path("architecture.md") not in files_written:
            rel_path = planning_asset_path("architecture.md")
            content = "".join(content_parts)
            repo = Path(inp.repo_path or ".")
            full_path = repo / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            files_written.append(rel_path)

        summary = "Architecture artifacts generated."
        if fixes_applied:
            summary = f"Architecture artifacts generated. Fixed {len(arch_issues)} review issue(s) in one update."

        return ToolAgentPhaseOutput(
            summary=summary,
            files={},
            recommendations=fixes_applied if fixes_applied else [],
            files_written=files_written,
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Review phase: check architecture coherence."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="Architecture review skipped (no LLM).")

        artifacts = "\n".join(
            f"--- {path} ---\n{content}" for path, content in list(inp.current_files.items())[:10]
        )[:8000]

        if not artifacts.strip():
            return ToolAgentPhaseOutput(
                summary="Architecture review skipped (no artifacts).",
                issues=[],
            )

        prompt = ARCHITECTURE_REVIEW_PROMPT.format(artifacts=artifacts)
        raw_text = complete_text_with_continuation(
            self.llm,
            prompt,
            agent_name="Architecture",
        )
        data = parse_review_output(raw_text)
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

        arch_issues = [
            i for i in inp.review_issues if "architect" in i.lower() or "layer" in i.lower()
        ]
        if not arch_issues:
            return ToolAgentPhaseOutput(summary="No architecture issues to resolve.")

        result = self.fix_all_issues(arch_issues, inp)
        return ToolAgentPhaseOutput(
            summary=result.summary
            or f"Architecture: addressed {len(arch_issues)} issue(s) in one update.",
            recommendations=[result.summary] if result.summary else [],
            files=result.files or {},
            resolved=result.resolved or bool(result.files),
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

        current_artifact = inp.current_files.get(planning_asset_path("architecture.md"), "")
        if not current_artifact:
            for path, content in inp.current_files.items():
                if "architect" in path.lower():
                    current_artifact = content
                    break

        prompt = ARCHITECTURE_FIX_SINGLE_ISSUE_PROMPT.format(
            issue=issue,
            current_artifact=current_artifact[:6000]
            if current_artifact
            else "(no existing artifact)",
            spec_excerpt=(inp.spec_content or "")[:3000],
        )

        try:
            raw_text = complete_text_with_continuation(
                self.llm,
                prompt,
                agent_name="Architecture_FixSingleIssue",
            )
            raw = parse_fix_output(raw_text)
            updated_content = raw.get("updated_content", "")
            fix_desc = raw.get("fix_description", "")
            resolved = raw.get("resolved", False)
            file_updates = raw.get("file_updates") or {}
            architecture_path = planning_asset_path("architecture.md")
            if file_updates.get(architecture_path):
                updated_content = file_updates[architecture_path]
            elif not updated_content and file_updates:
                updated_content = next(iter(file_updates.values()), "")

            files: Dict[str, str] = {}
            if updated_content and isinstance(updated_content, str) and updated_content.strip():
                if looks_like_truncated_file_content(updated_content):
                    continued = attempt_fix_output_continuation(
                        self.llm,
                        prompt,
                        raw_text,
                        "Architecture_FixSingleIssue",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    updated_content = (
                        fu.get(architecture_path)
                        or raw.get("updated_content", "")
                        or next(iter(fu.values()), "")
                    )
                    if updated_content and not looks_like_truncated_file_content(updated_content):
                        files[planning_asset_path("architecture.md")] = updated_content
                        logger.info("Architecture: fix applied after continuation (single-issue).")
                    else:
                        logger.warning(
                            "Architecture: fix output still truncated after continuation; not writing.",
                        )
                else:
                    files[planning_asset_path("architecture.md")] = updated_content
                    logger.info("Architecture: fix applied (single-issue) — %s", fix_desc[:120])
            elif file_updates:
                for path, content in file_updates.items():
                    if (
                        content
                        and isinstance(content, str)
                        and content.strip()
                        and not looks_like_truncated_file_content(content)
                    ):
                        files[path] = content
                        logger.info("Architecture: fix applied (single-issue) — %s", fix_desc[:120])
                        break
                else:
                    continued = attempt_fix_output_continuation(
                        self.llm,
                        prompt,
                        raw_text,
                        "Architecture_FixSingleIssue",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    uc = (
                        fu.get(architecture_path)
                        or raw.get("updated_content", "")
                        or next(iter(fu.values()), "")
                    )
                    if uc and not looks_like_truncated_file_content(uc):
                        files[planning_asset_path("architecture.md")] = uc
                        logger.info("Architecture: fix applied after continuation (single-issue).")
                    else:
                        for p, c in fu.items():
                            if (
                                c
                                and isinstance(c, str)
                                and c.strip()
                                and not looks_like_truncated_file_content(c)
                            ):
                                files[p] = c
                                logger.info(
                                    "Architecture: fix applied after continuation (single-issue)."
                                )
                                break
                        else:
                            logger.warning(
                                "Architecture: fix output still truncated after continuation; not writing.",
                            )

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

    def fix_all_issues(self, issues: List[str], inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix all listed architecture issues in one LLM call."""
        if not issues:
            return ToolAgentPhaseOutput(
                summary="No architecture issues to fix.",
                resolved=True,
            )
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="Architecture fix skipped (no LLM).",
                resolved=False,
            )

        current_artifact = inp.current_files.get(planning_asset_path("architecture.md"), "")
        if not current_artifact:
            for path, content in inp.current_files.items():
                if "architect" in path.lower():
                    current_artifact = content
                    break

        issues_list = "\n".join(f"{i + 1}. {issue}" for i, issue in enumerate(issues))
        prompt = ARCHITECTURE_FIX_ALL_ISSUES_PROMPT.format(
            issues_list=issues_list,
            current_artifact=current_artifact[:6000]
            if current_artifact
            else "(no existing artifact)",
            spec_excerpt=(inp.spec_content or "")[:3000],
        )

        try:
            raw_text = complete_text_with_continuation(
                self.llm,
                prompt,
                agent_name="Architecture_FixAllIssues",
            )
            raw = parse_fix_output(raw_text)
            updated_content = raw.get("updated_content", "")
            fix_desc = raw.get("fix_description", "")
            resolved = raw.get("resolved", False)
            file_updates = raw.get("file_updates") or {}
            architecture_path = planning_asset_path("architecture.md")
            if file_updates.get(architecture_path):
                updated_content = file_updates[architecture_path]
            elif not updated_content and file_updates:
                updated_content = next(iter(file_updates.values()), "")

            files: Dict[str, str] = {}
            if updated_content and isinstance(updated_content, str) and updated_content.strip():
                if looks_like_truncated_file_content(updated_content):
                    continued = attempt_fix_output_continuation(
                        self.llm,
                        prompt,
                        raw_text,
                        "Architecture_FixAllIssues",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    updated_content = (
                        fu.get(architecture_path)
                        or raw.get("updated_content", "")
                        or next(iter(fu.values()), "")
                    )
                    if updated_content and not looks_like_truncated_file_content(updated_content):
                        files[planning_asset_path("architecture.md")] = updated_content
                    else:
                        logger.warning(
                            "Architecture: fix_all_issues output still truncated after continuation; not writing.",
                        )
                else:
                    files[planning_asset_path("architecture.md")] = updated_content
            elif file_updates:
                for path, content in file_updates.items():
                    if (
                        content
                        and isinstance(content, str)
                        and content.strip()
                        and not looks_like_truncated_file_content(content)
                    ):
                        files[path] = content
                        break
                else:
                    continued = attempt_fix_output_continuation(
                        self.llm,
                        prompt,
                        raw_text,
                        "Architecture_FixAllIssues",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    uc = (
                        fu.get(architecture_path)
                        or raw.get("updated_content", "")
                        or next(iter(fu.values()), "")
                    )
                    if uc and not looks_like_truncated_file_content(uc):
                        files[planning_asset_path("architecture.md")] = uc
                    else:
                        for p, c in fu.items():
                            if (
                                c
                                and isinstance(c, str)
                                and c.strip()
                                and not looks_like_truncated_file_content(c)
                            ):
                                files[p] = c
                                break
                        else:
                            logger.warning(
                                "Architecture: fix_all_issues output still truncated after continuation; not writing.",
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
            logger.warning("Architecture fix_all_issues failed: %s", e)
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
        raw_text = complete_text_with_continuation(
            self.llm,
            prompt,
            agent_name="Architecture",
        )
        data = parse_spec_review_output(raw_text)
        gaps = data.get("gaps") or []
        if not isinstance(gaps, list):
            gaps = [str(gaps)] if gaps else []

        return ToolAgentPhaseOutput(
            summary=data.get("summary", "Architecture spec review complete."),
            issues=gaps,
            tool_recommendations=[],
            metadata={
                "patterns": data.get("components", []),
                "integration_points": data.get("integration_points", []),
                "scalability_notes": data.get("scalability_notes", ""),
            },
        )
