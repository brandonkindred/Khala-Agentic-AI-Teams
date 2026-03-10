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
from typing import Any, Callable, Dict, List, Optional

from software_engineering_team.shared.llm import LLMClient

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

    logger.info(
        "Planning-v2 Problem-solving: starting to fix %d review issue(s) (will process each issue and apply fixes to planning artifacts).",
        total_issues,
    )

    for issue_idx, issue in enumerate(review_issues):
        remaining = total_issues - issue_idx - resolved_count
        issue_short = issue[:80]

        logger.info(
            "Planning-v2 Problem-solving: fixing issue %d/%d (%d resolved so far, %d remaining) — current issue: %s",
            issue_idx + 1,
            total_issues,
            resolved_count,
            remaining,
            issue_short,
        )

        if detail_callback:
            detail_callback(
                f"Fixing issue {issue_idx + 1}/{total_issues}: {issue_short[:50]}..."
            )

        agent_kind = _classify_issue(issue)
        agent = tool_agents.get(agent_kind) if tool_agents else None

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

                issue_resolved = getattr(fix_result, "resolved", False) or bool(fix_result.files)
                fix_summary = fix_result.summary or f"Fixed by {agent_kind.value}"

            except Exception as e:
                logger.warning(
                    "Planning-v2 Problem-solving: %s fix_single_issue failed for issue %d: %s",
                    agent_kind.value,
                    issue_idx + 1,
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
                    agent_name=f"PlanningV2_ProblemSolving_Issue{issue_idx + 1}",
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
                    "Planning-v2 Problem-solving: LLM fix failed for issue %d: %s",
                    issue_idx + 1,
                    e,
                )

        if issue_resolved:
            resolved_count += 1
            fixes_applied.append(fix_summary)
            logger.info(
                "Planning-v2 Problem-solving: issue %d/%d RESOLVED (fix applied successfully) — summary: %s",
                issue_idx + 1,
                total_issues,
                fix_summary[:120],
            )
        else:
            unresolved_issues.append(issue)
            logger.warning(
                "Planning-v2 Problem-solving: issue %d/%d UNRESOLVED (no fix applied or fix failed) — issue description: %s",
                issue_idx + 1,
                total_issues,
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
