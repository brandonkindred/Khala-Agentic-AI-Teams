"""
System Design tool agent for planning-v2.

Participates in all 6 phases: Spec Review, Planning, Implementation, Review, Problem Solving, Deliver.
Focuses on component layout, system boundaries, and integration points.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from ...models import ToolAgentKind, ToolAgentPhaseInput, ToolAgentPhaseOutput, planning_asset_path
from ...output_templates import (
    looks_like_truncated_file_content,
    parse_fix_output,
    parse_planning_tool_output,
    parse_review_output,
    parse_spec_review_output,
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


SYSTEM_DESIGN_SPEC_REVIEW_PROMPT = """You are a System Design expert. Review this specification and identify:
1. Component boundaries and responsibilities
2. System integration points
3. Critical design gaps or ambiguities
4. Scalability considerations

Specification:
---
{spec_content}
---

Respond using this EXACT format:

## COMPONENTS ##
- Component 1
- Component 2
## END COMPONENTS ##

## INTEGRATION_POINTS ##
- Point 1
- Point 2
## END INTEGRATION_POINTS ##

## GAPS ##
- Gap 1
- Gap 2
## END GAPS ##

## SCALABILITY_NOTES ##
Scalability considerations (one short paragraph).
## END SCALABILITY_NOTES ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##
"""

SYSTEM_DESIGN_PLANNING_PROMPT = """You are a System Design expert. Create a system design plan for:

Specification:
---
{spec_content}
---

Prior analysis: {prior_analysis}

Respond using this EXACT format:

## COMPONENT_DESIGN ##
ComponentName: responsibility and dependencies
AnotherComponent: what it does
## END COMPONENT_DESIGN ##

## DATA_FLOW ##
Description of data flow between components.
## END DATA_FLOW ##

## INTEGRATION_STRATEGY ##
How components integrate.
## END INTEGRATION_STRATEGY ##

## RECOMMENDATIONS ##
- Recommendation 1
- Recommendation 2
## END RECOMMENDATIONS ##

## SUMMARY ##
Brief summary.
## END SUMMARY ##
"""

SYSTEM_DESIGN_REVIEW_PROMPT = """You are a System Design expert. Review these planning artifacts for design coherence:

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

SYSTEM_DESIGN_FIX_SINGLE_ISSUE_PROMPT = """You are a System Design expert. Fix this specific issue in the planning artifacts.

ISSUE TO FIX:
---
{issue}
---

CURRENT SYSTEM DESIGN ARTIFACT:
---
{current_artifact}
---

SPECIFICATION CONTEXT:
---
{spec_excerpt}
---

Analyze and fix this issue. Provide the complete updated file content using the format below.
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
### plan/planning_team/system_design.md ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##
"""

SYSTEM_DESIGN_FIX_ALL_ISSUES_PROMPT = """You are a System Design expert. Address ALL of the following issues in the planning artifacts in ONE coherent update.

ISSUES TO FIX (address every one):
---
{issues_list}
---

CURRENT SYSTEM DESIGN ARTIFACT:
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
### plan/planning_team/system_design.md ###
Complete updated file content here.
### END FILE ###
## END FILE_UPDATES ##
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

        spec_content = inp.spec_content or ""
        prompt = SYSTEM_DESIGN_PLANNING_PROMPT.format(
            spec_content=spec_content[:8000],
            prior_analysis=prior_analysis[:2000],
        )
        raw_text = complete_text_with_continuation(
            self.llm,
            prompt,
            agent_name="SystemDesign",
        )
        data = parse_planning_tool_output(raw_text)
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
        """Implementation phase: generate system design artifacts and fix review issues.
        Writes to disk as fixes are applied; returns files_written so implementation phase does not overwrite.
        """
        if not self.llm:
            return ToolAgentPhaseOutput(summary="System Design execute skipped (no LLM).")

        fixes_applied: List[str] = []
        files_written: List[str] = []
        current_files: Dict[str, str] = dict(inp.current_files or {})

        design_issues = [
            i
            for i in inp.review_issues
            if any(
                kw in i.lower()
                for kw in ["design", "system", "diagram", "flow", "interface", "component"]
            )
        ]

        if design_issues:
            logger.info(
                "SystemDesign: handling %d review issue(s) (will apply fixes in one update and write to disk).",
                len(design_issues),
            )
            fix_inp = inp.model_copy(update={"current_files": current_files})
            result = self.fix_all_issues(design_issues, fix_inp)
            if result.files:
                for rel_path, content in result.files.items():
                    repo = Path(inp.repo_path or ".")
                    write_section(repo, AGENT_SECTION_MAP[ToolAgentKind.SYSTEM_DESIGN], content)
                    logger.info(
                        "SystemDesign: applied fix — writing to shared doc (%d chars)",
                        len(content),
                    )
                    if shared_doc_asset_path() not in files_written:
                        files_written.append(shared_doc_asset_path())
                    current_files[rel_path] = content
                fixes_applied.append(result.summary)
            logger.info(
                "SystemDesign: fixed %d review issue(s) in one update (all fixes written to planning artifacts).",
                len(design_issues),
            )

        existing_design = (inp.current_files or {}).get(planning_asset_path("system_design.md")) or read_section(
            Path(inp.repo_path or "."), AGENT_SECTION_MAP[ToolAgentKind.SYSTEM_DESIGN]
        )
        if existing_design and not design_issues:
            return ToolAgentPhaseOutput(
                summary="System design artifacts unchanged (file exists, no review issues).",
                files={},
                recommendations=fixes_applied if fixes_applied else [],
                files_written=[],
            )

        component_design = inp.metadata.get("component_design", [])
        data_flow = inp.metadata.get("data_flow", "")
        integration_strategy = inp.metadata.get("integration_strategy", "")

        # Blackboard: read other agents' sections for cross-referencing
        blackboard_context = read_other_sections(
            Path(inp.repo_path or "."), AGENT_SECTION_MAP[ToolAgentKind.SYSTEM_DESIGN]
        )
        if blackboard_context:
            logger.info("SystemDesign: read %d chars of cross-agent context from blackboard", len(blackboard_context))

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

        if (component_design or data_flow) and shared_doc_asset_path() not in files_written:
            content = "".join(content_parts)
            repo = Path(inp.repo_path or ".")
            write_section(repo, AGENT_SECTION_MAP[ToolAgentKind.SYSTEM_DESIGN], content)
            files_written.append(shared_doc_asset_path())

        summary = "System design artifacts generated."
        if fixes_applied:
            summary = f"System design artifacts generated. Fixed {len(design_issues)} review issue(s) in one update."

        return ToolAgentPhaseOutput(
            summary=summary,
            files={},
            recommendations=fixes_applied if fixes_applied else [],
            files_written=files_written,
        )

    def review(self, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Review phase: check system design coherence."""
        if not self.llm:
            return ToolAgentPhaseOutput(summary="System Design review skipped (no LLM).")

        artifacts = "\n".join(
            f"--- {path} ---\n{content}" for path, content in list(inp.current_files.items())[:10]
        )[:8000]

        if not artifacts.strip():
            return ToolAgentPhaseOutput(
                summary="System Design review skipped (no artifacts).",
                issues=[],
            )

        prompt = SYSTEM_DESIGN_REVIEW_PROMPT.format(artifacts=artifacts)
        raw_text = complete_text_with_continuation(
            self.llm,
            prompt,
            agent_name="SystemDesign",
        )
        data = parse_review_output(raw_text)
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

        design_issues = [
            i for i in inp.review_issues if "design" in i.lower() or "system" in i.lower()
        ]
        if not design_issues:
            return ToolAgentPhaseOutput(summary="No system design issues to resolve.")

        result = self.fix_all_issues(design_issues, inp)
        return ToolAgentPhaseOutput(
            summary=result.summary
            or f"System design: addressed {len(design_issues)} issue(s) in one update.",
            recommendations=[result.summary] if result.summary else [],
            files=result.files or {},
            resolved=result.resolved or bool(result.files),
        )

    def fix_single_issue(self, issue: str, inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix a single system design issue.

        Args:
            issue: The issue description to fix.
            inp: Tool agent phase input with context.

        Returns:
            ToolAgentPhaseOutput with updated files if fix was applied.
        """
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="System Design fix skipped (no LLM).",
                resolved=False,
            )

        current_artifact = inp.current_files.get(planning_asset_path("system_design.md"), "") or read_section(
            Path(inp.repo_path or "."), AGENT_SECTION_MAP[ToolAgentKind.SYSTEM_DESIGN]
        ) or ""
        if not current_artifact:
            for path, content in inp.current_files.items():
                if "system" in path.lower() or "design" in path.lower():
                    current_artifact = content
                    break

        prompt = SYSTEM_DESIGN_FIX_SINGLE_ISSUE_PROMPT.format(
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
                agent_name="SystemDesign_FixSingleIssue",
            )
            raw = parse_fix_output(raw_text)
            updated_content = raw.get("updated_content", "")
            fix_desc = raw.get("fix_description", "")
            resolved = raw.get("resolved", False)
            file_updates = raw.get("file_updates") or {}
            system_design_path = planning_asset_path("system_design.md")
            if file_updates.get(system_design_path):
                updated_content = file_updates[system_design_path]
            elif not updated_content and file_updates:
                updated_content = next(iter(file_updates.values()), "")

            files: Dict[str, str] = {}
            if updated_content and isinstance(updated_content, str) and updated_content.strip():
                if looks_like_truncated_file_content(updated_content):
                    continued = attempt_fix_output_continuation(
                        self.llm,
                        prompt,
                        raw_text,
                        "SystemDesign_FixSingleIssue",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    updated_content = (
                        fu.get(system_design_path)
                        or raw.get("updated_content", "")
                        or next(iter(fu.values()), "")
                    )
                    if updated_content and not looks_like_truncated_file_content(updated_content):
                        files[planning_asset_path("system_design.md")] = updated_content
                        logger.info("SystemDesign: fix applied after continuation (single-issue).")
                    else:
                        logger.warning(
                            "SystemDesign: fix output still truncated after continuation; not writing.",
                        )
                else:
                    files[planning_asset_path("system_design.md")] = updated_content
                    logger.info("SystemDesign: fix applied (single-issue) — %s", fix_desc[:120])
            elif file_updates:
                for path, content in file_updates.items():
                    if (
                        content
                        and isinstance(content, str)
                        and content.strip()
                        and not looks_like_truncated_file_content(content)
                    ):
                        files[path] = content
                        logger.info("SystemDesign: fix applied (single-issue) — %s", fix_desc[:120])
                        break
                else:
                    continued = attempt_fix_output_continuation(
                        self.llm,
                        prompt,
                        raw_text,
                        "SystemDesign_FixSingleIssue",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    uc = (
                        fu.get(system_design_path)
                        or raw.get("updated_content", "")
                        or next(iter(fu.values()), "")
                    )
                    if uc and not looks_like_truncated_file_content(uc):
                        files[planning_asset_path("system_design.md")] = uc
                        logger.info("SystemDesign: fix applied after continuation (single-issue).")
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
                                    "SystemDesign: fix applied after continuation (single-issue)."
                                )
                                break
                        else:
                            logger.warning(
                                "SystemDesign: fix output still truncated after continuation; not writing.",
                            )

            return ToolAgentPhaseOutput(
                summary=fix_desc or f"System design issue addressed: {issue[:50]}",
                files=files,
                resolved=resolved or bool(files),
                metadata={"root_cause": raw.get("root_cause", "")},
            )

        except Exception as e:
            logger.warning("SystemDesign fix_single_issue failed: %s", e)
            return ToolAgentPhaseOutput(
                summary=f"Fix failed: {str(e)[:50]}",
                resolved=False,
            )

    def fix_all_issues(self, issues: List[str], inp: ToolAgentPhaseInput) -> ToolAgentPhaseOutput:
        """Fix all listed system design issues in one LLM call."""
        if not issues:
            return ToolAgentPhaseOutput(
                summary="No system design issues to fix.",
                resolved=True,
            )
        if not self.llm:
            return ToolAgentPhaseOutput(
                summary="System Design fix skipped (no LLM).",
                resolved=False,
            )

        current_artifact = inp.current_files.get(planning_asset_path("system_design.md"), "") or read_section(
            Path(inp.repo_path or "."), AGENT_SECTION_MAP[ToolAgentKind.SYSTEM_DESIGN]
        ) or ""
        if not current_artifact:
            for path, content in inp.current_files.items():
                if "system" in path.lower() or "design" in path.lower():
                    current_artifact = content
                    break

        issues_list = "\n".join(f"{i + 1}. {issue}" for i, issue in enumerate(issues))
        prompt = SYSTEM_DESIGN_FIX_ALL_ISSUES_PROMPT.format(
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
                agent_name="SystemDesign_FixAllIssues",
            )
            raw = parse_fix_output(raw_text)
            updated_content = raw.get("updated_content", "")
            fix_desc = raw.get("fix_description", "")
            resolved = raw.get("resolved", False)
            file_updates = raw.get("file_updates") or {}
            system_design_path = planning_asset_path("system_design.md")
            if file_updates.get(system_design_path):
                updated_content = file_updates[system_design_path]
            elif not updated_content and file_updates:
                updated_content = next(iter(file_updates.values()), "")

            files: Dict[str, str] = {}
            if updated_content and isinstance(updated_content, str) and updated_content.strip():
                if looks_like_truncated_file_content(updated_content):
                    continued = attempt_fix_output_continuation(
                        self.llm,
                        prompt,
                        raw_text,
                        "SystemDesign_FixAllIssues",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    updated_content = (
                        fu.get(system_design_path)
                        or raw.get("updated_content", "")
                        or next(iter(fu.values()), "")
                    )
                    if updated_content and not looks_like_truncated_file_content(updated_content):
                        files[planning_asset_path("system_design.md")] = updated_content
                    else:
                        logger.warning(
                            "SystemDesign: fix_all_issues output still truncated after continuation; not writing.",
                        )
                else:
                    files[planning_asset_path("system_design.md")] = updated_content
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
                        "SystemDesign_FixAllIssues",
                    )
                    raw = parse_fix_output(continued)
                    fu = raw.get("file_updates") or {}
                    uc = (
                        fu.get(system_design_path)
                        or raw.get("updated_content", "")
                        or next(iter(fu.values()), "")
                    )
                    if uc and not looks_like_truncated_file_content(uc):
                        files[planning_asset_path("system_design.md")] = uc
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
                                "SystemDesign: fix_all_issues output still truncated after continuation; not writing.",
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
            logger.warning("SystemDesign fix_all_issues failed: %s", e)
            return ToolAgentPhaseOutput(
                summary=f"Fix failed: {str(e)[:50]}",
                resolved=False,
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
        raw_text = complete_text_with_continuation(
            self.llm,
            prompt,
            agent_name="SystemDesign",
        )
        data = parse_spec_review_output(raw_text)
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
