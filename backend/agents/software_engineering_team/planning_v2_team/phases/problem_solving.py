"""
DEPRECATED: Problem-solving phase has been removed from the workflow.

Review issues are now passed directly to the Implementation phase, which handles
fixes however it sees fit (batch, one-by-one, or all at once).

This module is kept for backward compatibility only. Do not use in new code.

Previous purpose:
- Identify root causes and fix issues one at a time
- Tool agents: System Design, Architecture, User Story
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_service import LLMClient

from ..models import (
    ImplementationPhaseResult,
    PLAN_PLANNING_TEAM_DIR,
    PlanningPhaseResult,
    ProblemSolvingPhaseResult,
    ReviewPhaseResult,
    SpecReviewResult,
    ToolAgentKind,
    ToolAgentPhaseInput,
)
from ..output_templates import parse_fix_output
from ..prompts import PROBLEM_SOLVING_PROMPT, PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT
from ..tool_agents.json_utils import complete_with_continuation

logger = logging.getLogger(__name__)

MAX_FIX_ATTEMPTS_PER_ISSUE = 3


def _classify_issue(issue: str) -> ToolAgentKind:
    """Classify an issue to determine which tool agent should handle it.

    Args:
        issue: The issue description string.

    Returns:
        The ToolAgentKind best suited to handle this issue.
    """
    issue_lower = issue.lower()

    if any(kw in issue_lower for kw in ["architect", "layer", "module", "component", "integration"]):
        return ToolAgentKind.ARCHITECTURE
    # Check task classification before user story so "task team assignment" maps to classification
    if any(kw in issue_lower for kw in ["classification", "team assignment", "task team", "frontend", "backend", "devops", "qa"]):
        return ToolAgentKind.TASK_CLASSIFICATION
    if any(kw in issue_lower for kw in ["story", "task", "epic", "user", "acceptance", "criteria"]):
        return ToolAgentKind.USER_STORY
    if any(kw in issue_lower for kw in ["design", "system", "diagram", "flow", "interface"]):
        return ToolAgentKind.SYSTEM_DESIGN
    if any(kw in issue_lower for kw in ["deploy", "ci", "cd", "docker", "kubernetes", "infra"]):
        return ToolAgentKind.DEVOPS
    if any(kw in issue_lower for kw in ["ui", "visual", "layout", "style", "css"]):
        return ToolAgentKind.UI_DESIGN
    if any(kw in issue_lower for kw in ["ux", "usability", "accessibility", "navigation"]):
        return ToolAgentKind.UX_DESIGN

    return ToolAgentKind.SYSTEM_DESIGN


def group_issues_by_agent(issues: List[str]) -> Dict[ToolAgentKind, List[str]]:
    """Group review issues by the tool agent that would handle them.

    Uses _classify_issue for each issue. Useful for status breakdown and synopsis.

    Returns:
        Dict mapping each ToolAgentKind to the list of issues classified to that agent.
    """
    grouped: Dict[ToolAgentKind, List[str]] = {}
    for issue in issues:
        kind = _classify_issue(issue)
        grouped.setdefault(kind, []).append(issue)
    return grouped


# Human-readable labels for status text (e.g. user_story -> "user story")
TOOL_AGENT_LABELS: Dict[ToolAgentKind, str] = {
    ToolAgentKind.SYSTEM_DESIGN: "system design",
    ToolAgentKind.ARCHITECTURE: "architecture",
    ToolAgentKind.USER_STORY: "user story",
    ToolAgentKind.DEVOPS: "DevOps",
    ToolAgentKind.UI_DESIGN: "UI design",
    ToolAgentKind.UX_DESIGN: "UX design",
    ToolAgentKind.TASK_CLASSIFICATION: "task classification",
    ToolAgentKind.TASK_DEPENDENCY: "task dependency",
}

# Stable order for breakdown display (Implementation-phase agents first)
ISSUE_BREAKDOWN_DISPLAY_ORDER: List[ToolAgentKind] = [
    ToolAgentKind.SYSTEM_DESIGN,
    ToolAgentKind.ARCHITECTURE,
    ToolAgentKind.USER_STORY,
    ToolAgentKind.DEVOPS,
    ToolAgentKind.UI_DESIGN,
    ToolAgentKind.UX_DESIGN,
    ToolAgentKind.TASK_CLASSIFICATION,
    ToolAgentKind.TASK_DEPENDENCY,
]

SYNOPSIS_MAX_LEN = 55


def format_issues_breakdown_and_synopsis(
    grouped: Dict[ToolAgentKind, List[str]],
) -> Tuple[str, str]:
    """Build counts segment and optional synopsis segment for status text.

    Returns:
        (counts_segment, synopsis_segment)
        - counts_segment: e.g. "6 user story, 2 architecture, 1 system design"
        - synopsis_segment: e.g. "User story: missing acceptance criteria. Architecture: ..."
    """
    parts: List[str] = []
    for kind in ISSUE_BREAKDOWN_DISPLAY_ORDER:
        issues_list = grouped.get(kind, [])
        if not issues_list:
            continue
        label = TOOL_AGENT_LABELS.get(kind, kind.value.replace("_", " "))
        parts.append(f"{len(issues_list)} {label}")
    counts_segment = ", ".join(parts) if parts else "0"

    synopsis_parts: List[str] = []
    for kind in ISSUE_BREAKDOWN_DISPLAY_ORDER:
        issues_list = grouped.get(kind, [])
        if not issues_list:
            continue
        label = TOOL_AGENT_LABELS.get(kind, kind.value.replace("_", " "))
        first_issue = issues_list[0].strip()
        if len(first_issue) > SYNOPSIS_MAX_LEN:
            first_issue = first_issue[: SYNOPSIS_MAX_LEN - 3].rstrip() + "..."
        display_label = label[0].upper() + label[1:] if len(label) > 1 else label.upper()
        synopsis_parts.append(f"{display_label}: {first_issue}")
    synopsis_segment = ". ".join(synopsis_parts) if synopsis_parts else ""

    return counts_segment, synopsis_segment


def _read_planning_artifacts(repo_path: Path) -> Dict[str, str]:
    """Read planning artifacts from plan/planning_team for problem-solving context."""
    files: Dict[str, str] = {}
    plan_dir = repo_path / PLAN_PLANNING_TEAM_DIR
    if plan_dir.exists():
        for f in plan_dir.glob("*.md"):
            try:
                content = f.read_text(encoding="utf-8")
                files[str(f.relative_to(repo_path))] = content
            except Exception:
                pass
    return files


def run_problem_solving(
    llm: LLMClient,
    spec_content: str,
    repo_path: Path,
    spec_review_result: Optional[SpecReviewResult] = None,
    planning_result: Optional[PlanningPhaseResult] = None,
    implementation_result: Optional[ImplementationPhaseResult] = None,
    review_result: Optional[ReviewPhaseResult] = None,
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
) -> ProblemSolvingPhaseResult:
    """
    Run Problem-solving phase, fixing issues one at a time with progress tracking.

    This function processes each review issue individually, logging progress
    in the format: "fixing issue X/Y (Z resolved, W remaining) — issue description"

    Tool agents: System Design, Architecture, User Story.

    Args:
        llm: LLM client for completions.
        spec_content: The specification content.
        repo_path: Path to the repository.
        spec_review_result: Optional spec review results.
        planning_result: Optional planning phase results.
        implementation_result: Optional implementation phase results.
        review_result: Optional review phase results containing issues to fix.
        tool_agents: Dict of tool agent instances.
        detail_callback: Optional callback to report detailed status messages.

    Returns:
        ProblemSolvingPhaseResult with fixes applied and resolution status.
    """
    review_issues = review_result.issues if review_result else []

    if not review_issues:
        logger.info("Planning-v2 Problem-solving: no issues to fix")
        return ProblemSolvingPhaseResult(
            fixes_applied=[],
            resolved=True,
            unresolved_issues=[],
            summary="No issues to fix.",
        )

    total_issues = len(review_issues)
    resolved_count = 0
    unresolved_issues: List[str] = []
    fixes_applied: List[str] = []

    current_files = _read_planning_artifacts(repo_path)
    grouped = group_issues_by_agent(review_issues)

    logger.info(
        "Planning-v2 Problem-solving: starting to fix %d review issue(s) (grouped by agent; will apply fixes in batch per agent when available).",
        total_issues,
    )

    for agent_kind in ISSUE_BREAKDOWN_DISPLAY_ORDER:
        agent_issues = grouped.get(agent_kind, [])
        if not agent_issues:
            continue

        agent = tool_agents.get(agent_kind) if tool_agents else None
        single_issue_input = ToolAgentPhaseInput(
            spec_content=spec_content,
            repo_path=str(repo_path),
            spec_review_result=spec_review_result,
            planning_result=planning_result,
            implementation_result=implementation_result,
            review_result=review_result,
            review_issues=agent_issues,
            current_files=current_files,
        )

        batch_resolved = False
        fix_summary = ""

        if agent and hasattr(agent, "fix_all_issues"):
            try:
                fix_result = agent.fix_all_issues(agent_issues, single_issue_input)
                if fix_result.files:
                    for rel_path, content in fix_result.files.items():
                        full_path = repo_path / rel_path
                        full_path.parent.mkdir(parents=True, exist_ok=True)
                        full_path.write_text(content, encoding="utf-8")
                        current_files[rel_path] = content
                        file_name = Path(rel_path).name
                        logger.info(
                            "Planning-v2 Problem-solving: applied batch fix via %s — writing to file: %s (%d chars)",
                            agent_kind.value,
                            file_name,
                            len(content),
                        )
                    batch_resolved = getattr(fix_result, "resolved", False) or True
                    fix_summary = fix_result.summary or f"Addressed {len(agent_issues)} issue(s) via {agent_kind.value}"
            except Exception as e:
                logger.warning(
                    "Planning-v2 Problem-solving: %s fix_all_issues failed: %s",
                    agent_kind.value,
                    e,
                )

        if batch_resolved:
            resolved_count += len(agent_issues)
            fixes_applied.append(fix_summary)
            logger.info(
                "Planning-v2 Problem-solving: %s batch RESOLVED (%d issue(s)) — %s",
                agent_kind.value,
                len(agent_issues),
                fix_summary[:120],
            )
            continue

        for issue_idx, issue in enumerate(agent_issues):
            issue_short = issue[:80]

            logger.info(
                "Planning-v2 Problem-solving: fixing issue via %s (%d/%d for this agent) — %s",
                agent_kind.value,
                issue_idx + 1,
                len(agent_issues),
                issue_short,
            )

            if detail_callback:
                detail_callback(
                    f"Fixing issue via {agent_kind.value}: {issue_short[:50]}..."
                )

            issue_resolved = False
            fix_summary = ""

            if agent and hasattr(agent, "fix_single_issue"):
                try:
                    single_issue_input = ToolAgentPhaseInput(
                        spec_content=spec_content,
                        repo_path=str(repo_path),
                        spec_review_result=spec_review_result,
                        planning_result=planning_result,
                        implementation_result=implementation_result,
                        review_result=review_result,
                        review_issues=[issue],
                        current_files=current_files,
                    )

                    fix_result = agent.fix_single_issue(issue, single_issue_input)

                    if fix_result.files:
                        for rel_path, content in fix_result.files.items():
                            full_path = repo_path / rel_path
                            full_path.parent.mkdir(parents=True, exist_ok=True)
                            full_path.write_text(content, encoding="utf-8")
                            current_files[rel_path] = content
                            file_name = Path(rel_path).name
                            logger.info(
                                "Planning-v2 Problem-solving: applied fix via %s — writing to file: %s (%d chars)",
                                agent_kind.value,
                                file_name,
                                len(content),
                            )

                        issue_resolved = getattr(fix_result, "resolved", False) or True
                        fix_summary = fix_result.summary or f"Fixed by {agent_kind.value}"

                except Exception as e:
                    logger.warning(
                        "Planning-v2 Problem-solving: %s fix_single_issue failed: %s",
                        agent_kind.value,
                        e,
                    )

            if not issue_resolved:
                try:
                    prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT.format(
                        issue=issue,
                        spec_excerpt=spec_content[:3000] if spec_content else "",
                        current_artifacts=_format_artifacts_for_prompt(current_files),
                    )

                    raw_text = complete_with_continuation(
                        llm=llm,
                        prompt=prompt,
                        agent_name=f"PlanningV2_ProblemSolving_{agent_kind.value}_Issue{issue_idx + 1}",
                    )
                    raw = parse_fix_output(raw_text)
                    fix_desc = raw.get("fix_description", "")
                    file_updates = raw.get("file_updates") or {}

                    if file_updates and isinstance(file_updates, dict):
                        for rel_path, content in file_updates.items():
                            if isinstance(content, str) and content.strip():
                                full_path = repo_path / rel_path
                                full_path.parent.mkdir(parents=True, exist_ok=True)
                                full_path.write_text(content, encoding="utf-8")
                                current_files[rel_path] = content
                                file_name = Path(rel_path).name
                                logger.info(
                                    "Planning-v2 Problem-solving: applied fix via LLM — writing to file: %s (%d chars)",
                                    file_name,
                                    len(content),
                                )
                        issue_resolved = True
                        fix_summary = fix_desc or f"LLM fix applied for: {issue_short}"
                    elif raw.get("resolved"):
                        issue_resolved = True
                        fix_summary = fix_desc or "Issue marked as resolved by LLM"

                except Exception as e:
                    logger.warning(
                        "Planning-v2 Problem-solving: LLM fix failed: %s",
                        e,
                    )

            if issue_resolved:
                resolved_count += 1
                fixes_applied.append(fix_summary)
                logger.info(
                    "Planning-v2 Problem-solving: issue RESOLVED via %s — %s",
                    agent_kind.value,
                    fix_summary[:120],
                )
            else:
                unresolved_issues.append(issue)
                logger.warning(
                    "Planning-v2 Problem-solving: issue UNRESOLVED — %s",
                    issue[:160] if len(issue) > 160 else issue,
                )

    all_resolved = len(unresolved_issues) == 0

    logger.info(
        "Planning-v2 Problem-solving phase complete: %d out of %d issues resolved successfully, %d issue(s) remain unresolved.",
        resolved_count,
        total_issues,
        len(unresolved_issues),
    )

    if detail_callback:
        detail_callback(
            f"Problem-solving complete: {resolved_count}/{total_issues} issues resolved"
        )

    return ProblemSolvingPhaseResult(
        fixes_applied=fixes_applied,
        resolved=all_resolved,
        unresolved_issues=unresolved_issues,
        summary=f"Resolved {resolved_count}/{total_issues} issues."
        + (f" {len(unresolved_issues)} issue(s) remain unresolved." if unresolved_issues else ""),
    )


def _format_artifacts_for_prompt(files: Dict[str, str], max_chars: int = 6000) -> str:
    """Format planning artifacts for inclusion in a prompt."""
    parts: List[str] = []
    total = 0

    for path, content in list(files.items())[:5]:
        chunk = f"--- {path} ---\n{content[:2000]}\n"
        if total + len(chunk) > max_chars:
            break
        parts.append(chunk)
        total += len(chunk)

    return "\n".join(parts) if parts else "(no artifacts)"
