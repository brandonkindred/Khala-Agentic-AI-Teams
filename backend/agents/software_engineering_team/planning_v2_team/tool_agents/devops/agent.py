"""
DevOps tool agent for planning-v2.

Participates in phases: Planning, Implementation.
Focuses on CI/CD pipelines, infrastructure, and deployment planning.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from ...models import ToolAgentKind, ToolAgentPhaseInput, ToolAgentPhaseOutput, planning_asset_path
from ...output_templates import (
    looks_like_truncated_file_content,
    parse_devops_planning_output,
    parse_fix_output,
)
from ...shared_planning_document import (
    AGENT_SECTION_MAP,
    read_other_sections,
    read_section,
    shared_doc_asset_path,
    write_section,
)
from ..json_utils import attempt_fix_output_continuation, complete_text_with_continuation

if TYPE_CHECKING:
    from llm_service import LLMClient

logger = logging.getLogger(__name__)


DEVOPS_PLANNING_PROMPT = """You are a DevOps expert. Create a DevOps plan for the specification.

If deployment target is NOT specified in the spec, set NEEDS_CLARIFICATION to true and list questions in CLARIFICATION_QUESTIONS. Do not assume cloud provider.

Respond using this EXACT format:

## NEEDS_CLARIFICATION ##
true or false
## END NEEDS_CLARIFICATION ##

## CLARIFICATION_QUESTIONS ##
- Where should this application be deployed?
- What are the expected SLA requirements?
## END CLARIFICATION_QUESTIONS ##

## RECOMMENDATIONS ##
- DevOps recommendation 1
- DevOps recommendation 2
## END RECOMMENDATIONS ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##

Specification:
---
{spec_content}
---

Plan summary from spec review: {plan_summary}
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

Output the complete updated file content; do not truncate. Include every section in full.

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

## FILE_UPDATES ##
### plan/planning_team/devops.md ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##
"""

DEVOPS_FIX_ALL_ISSUES_PROMPT = """You are a DevOps expert. Address ALL of the following issues in the DevOps artifacts in ONE coherent update.

ISSUES TO FIX (address every one):
---
{issues_list}
---

CURRENT DEVOPS ARTIFACT:
---
{current_artifact}
---

SPECIFICATION CONTEXT:
---
{spec_excerpt}
---

Analyze and fix every listed issue in a single coherent update. Provide the complete updated file content.
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
### plan/planning_team/devops.md ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##
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
        raw_text = complete_text_with_continuation(
            self.llm,
            prompt,
            agent_name="DevOps",
        )
        data = parse_devops_planning_output(raw_text)

        recommendations = data.get("recommendations") or []
        if not isinstance(recommendations, list):
            recommendations = [str(recommendations)]

        needs_clarification = data.get("needs_clarification", False)
        clarification_questions = data.get("clarification_questions", [])

        if needs_clarification and clarification_questions:
            logger.warning(
                "DevOps planning requires clarification: %s", "; ".join(clarification_questions[:3])
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
        Writes to disk as fixes are applied; returns files_written so implementation phase does not overwrite.
        """
        fixes_applied: List[str] = []
        files_written: List[str] = []
        current_files: Dict[str, str] = dict(inp.current_files or {})

        devops_issues = [
            i
            for i in inp.review_issues
            if any(
                kw in i.lower()
                for kw in [
                    "devops",
                    "ci/cd",
                    "pipeline",
                    "infrastructure",
                    "deployment",
                    "monitoring",
                    "security",
                    "cicd",
                ]
            )
        ]

        if devops_issues and self.llm:
            logger.info(
                "DevOps: handling %d review issue(s) (will apply fixes in one update and write to disk).",
                len(devops_issues),
            )
            fix_inp = inp.model_copy(update={"current_files": current_files})
            result = self.fix_all_issues(devops_issues, fix_inp)
            if result.files:
                for rel_path, content in result.files.items():
                    repo = Path(inp.repo_path or ".")
                    write_section(repo, AGENT_SECTION_MAP[ToolAgentKind.DEVOPS], content)
                    logger.info(
                        "DevOps: applied fix — writing to shared doc (%d chars)",
                        len(content),
                    )
                    if shared_doc_asset_path() not in files_written:
                        files_written.append(shared_doc_asset_path())
                    current_files[rel_path] = content
                fixes_applied.append(result.summary)
            logger.info(
                "DevOps: fixed %d review issue(s) in one update (all fixes written to planning artifacts).",
                len(devops_issues),
            )

        existing_doc = (
            (inp.current_files.get(planning_asset_path("devops.md")) if inp.current_files else None)
            or read_section(Path(inp.repo_path or "."), AGENT_SECTION_MAP[ToolAgentKind.DEVOPS])
        )
        if existing_doc and not devops_issues:
            return ToolAgentPhaseOutput(
                summary="DevOps artifacts unchanged (file exists, no review issues).",
                files={},
                recommendations=[],
                files_written=[],
            )
        if files_written:
            summary = "DevOps artifacts updated."
            if fixes_applied:
                summary = f"DevOps artifacts updated. Fixed {len(devops_issues)} review issue(s) in one update."
            return ToolAgentPhaseOutput(
                summary=summary,
                files={},
                recommendations=fixes_applied if fixes_applied else [],
                files_written=files_written,
            )

        pipeline_stages = inp.metadata.get("pipeline_stages", []) if inp.metadata else []
        infrastructure = inp.metadata.get("infrastructure", {}) if inp.metadata else {}
        deployment_strategy = inp.metadata.get("deployment_strategy", "") if inp.metadata else ""
        monitoring = inp.metadata.get("monitoring", []) if inp.metadata else []
        security = inp.metadata.get("security", []) if inp.metadata else []

        # Blackboard: read other agents' sections for cross-referencing
        blackboard_context = read_other_sections(
            Path(inp.repo_path or "."), AGENT_SECTION_MAP[ToolAgentKind.DEVOPS]
        )
        if blackboard_context:
            logger.info("DevOps: read %d chars of cross-agent context from blackboard", len(blackboard_context))

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
            content = "".join(content_parts)
            repo = Path(inp.repo_path or ".")
            write_section(repo, AGENT_SECTION_MAP[ToolAgentKind.DEVOPS], content)
            files_written.append(shared_doc_asset_path())

        return ToolAgentPhaseOutput(
            summary="DevOps artifacts generated.",
            files={},
            files_written=files_written,
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
            current_artifact = inp.current_files.get(planning_asset_path("devops.md"), "")
            if not current_artifact:
                for path, content in inp.current_files.items():
                    if "devops" in path.lower():
                        current_artifact = content
                        break
        if not current_artifact:
            current_artifact = read_section(Path(inp.repo_path or "."), AGENT_SECTION_MAP[ToolAgentKind.DEVOPS]) or ""

        prompt = DEVOPS_FIX_SINGLE_ISSUE_PROMPT.format(
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
                agent_name="DevOps_FixSingleIssue",
            )
            raw = parse_fix_output(raw_text)
            updated_content = raw.get("updated_content", "")
            fix_desc = raw.get("fix_description", "")
            resolved = raw.get("resolved", False)
            file_updates = raw.get("file_updates") or {}
            devops_path = planning_asset_path("devops.md")
            if file_updates.get(devops_path):
                updated_content = file_updates[devops_path]
            elif not updated_content and file_updates:
                updated_content = next(iter(file_updates.values()), "")

            files: Dict[str, str] = {}
            if updated_content and isinstance(updated_content, str) and updated_content.strip():
                if looks_like_truncated_file_content(updated_content):
                    continued = attempt_fix_output_continuation(
                        self.llm,
                        prompt,
                        raw_text,
                        "DevOps_FixSingleIssue",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    updated_content = (
                        fu.get(devops_path)
                        or raw.get("updated_content", "")
                        or next(iter(fu.values()), "")
                    )
                    if updated_content and not looks_like_truncated_file_content(updated_content):
                        files[planning_asset_path("devops.md")] = updated_content
                        logger.info("DevOps: fix applied after continuation (single-issue).")
                    else:
                        logger.warning(
                            "DevOps: fix output still truncated after continuation; not writing.",
                        )
                else:
                    files[planning_asset_path("devops.md")] = updated_content
                    logger.info("DevOps: fix applied (single-issue) — %s", fix_desc[:120])
            elif file_updates:
                for path, content in file_updates.items():
                    if (
                        content
                        and isinstance(content, str)
                        and content.strip()
                        and not looks_like_truncated_file_content(content)
                    ):
                        files[path] = content
                        logger.info("DevOps: fix applied (single-issue) — %s", fix_desc[:120])
                        break
                else:
                    continued = attempt_fix_output_continuation(
                        self.llm,
                        prompt,
                        raw_text,
                        "DevOps_FixSingleIssue",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    uc = (
                        fu.get(devops_path)
                        or raw.get("updated_content", "")
                        or next(iter(fu.values()), "")
                    )
                    if uc and not looks_like_truncated_file_content(uc):
                        files[planning_asset_path("devops.md")] = uc
                        logger.info("DevOps: fix applied after continuation (single-issue).")
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
                                    "DevOps: fix applied after continuation (single-issue)."
                                )
                                break
                        else:
                            logger.warning(
                                "DevOps: fix output still truncated after continuation; not writing.",
                            )

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

    def fix_all_issues(self, issues: List[str], inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix all listed DevOps issues in one LLM call."""
        if not issues:
            return ToolAgentPhaseOutput(
                summary="No DevOps issues to fix.",
                resolved=True,
            )
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="DevOps fix skipped (no LLM).",
                resolved=False,
            )

        current_artifact = ""
        if inp.current_files:
            current_artifact = inp.current_files.get(planning_asset_path("devops.md"), "")
            if not current_artifact:
                for path, content in inp.current_files.items():
                    if "devops" in path.lower():
                        current_artifact = content
                        break
        if not current_artifact:
            current_artifact = read_section(Path(inp.repo_path or "."), AGENT_SECTION_MAP[ToolAgentKind.DEVOPS]) or ""

        issues_list = "\n".join(f"{i + 1}. {issue}" for i, issue in enumerate(issues))
        prompt = DEVOPS_FIX_ALL_ISSUES_PROMPT.format(
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
                agent_name="DevOps_FixAllIssues",
            )
            raw = parse_fix_output(raw_text)
            updated_content = raw.get("updated_content", "")
            fix_desc = raw.get("fix_description", "")
            resolved = raw.get("resolved", False)
            file_updates = raw.get("file_updates") or {}
            devops_path = planning_asset_path("devops.md")
            if file_updates.get(devops_path):
                updated_content = file_updates[devops_path]
            elif not updated_content and file_updates:
                updated_content = next(iter(file_updates.values()), "")

            files: Dict[str, str] = {}
            if updated_content and isinstance(updated_content, str) and updated_content.strip():
                if looks_like_truncated_file_content(updated_content):
                    continued = attempt_fix_output_continuation(
                        self.llm,
                        prompt,
                        raw_text,
                        "DevOps_FixAllIssues",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    updated_content = (
                        fu.get(devops_path)
                        or raw.get("updated_content", "")
                        or next(iter(fu.values()), "")
                    )
                    if updated_content and not looks_like_truncated_file_content(updated_content):
                        files[planning_asset_path("devops.md")] = updated_content
                    else:
                        logger.warning(
                            "DevOps: fix_all_issues output still truncated after continuation; not writing.",
                        )
                else:
                    files[planning_asset_path("devops.md")] = updated_content
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
                        "DevOps_FixAllIssues",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    uc = (
                        fu.get(devops_path)
                        or raw.get("updated_content", "")
                        or next(iter(fu.values()), "")
                    )
                    if uc and not looks_like_truncated_file_content(uc):
                        files[planning_asset_path("devops.md")] = uc
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
                                "DevOps: fix_all_issues output still truncated after continuation; not writing.",
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
            logger.warning("DevOps fix_all_issues failed: %s", e)
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
