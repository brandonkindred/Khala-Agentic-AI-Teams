"""
Problem-solving phase: root-cause analysis and fix loop.

Processes one issue at a time. Unresolved issues can be turned into fix microtasks.
No code from frontend_team is used.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from software_engineering_team.shared.llm import LLMClient
from software_engineering_team.shared.models import Task

from ..models import (
    Microtask,
    Phase,
    ProblemSolvingResult,
    ReviewIssue,
    ReviewResult,
    ToolAgentKind,
    ToolAgentPhaseInput,
)
from ..output_templates import parse_problem_solving_single_issue_template, parse_batch_fix_template
from ..prompts import PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT, BATCH_FIX_PROMPT, TYPESCRIPT_CONVENTIONS

logger = logging.getLogger(__name__)

MAX_ITERATIONS_PER_ISSUE = 100
MAX_RELEVANT_CODE_CHARS = 8000
MAX_BATCH_CODE_CHARS = 24000


def _format_all_code(current_files: Dict[str, str], max_chars: int = MAX_BATCH_CODE_CHARS) -> str:
    """Format all current files for batch fix prompt, respecting character limits."""
    parts: List[str] = []
    total = 0
    for path, content in current_files.items():
        chunk = f"--- {path} ---\n{content}\n"
        if total + len(chunk) > max_chars:
            remaining = max_chars - total
            if remaining > 200:
                chunk = f"--- {path} ---\n{content[:remaining]}\n... [truncated]"
                parts.append(chunk)
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts) if parts else "(no code)"


def _format_issues_for_batch(issues: List[ReviewIssue]) -> str:
    """Format all issues into a numbered list for the batch fix prompt."""
    lines: List[str] = []
    for idx, issue in enumerate(issues, 1):
        lines.append(f"### Issue {idx}")
        lines.append(f"- **Source:** {issue.source or 'review'}")
        lines.append(f"- **Severity:** {issue.severity or 'medium'}")
        lines.append(f"- **File:** {issue.file_path or 'N/A'}")
        lines.append(f"- **Description:** {issue.description or 'No description'}")
        lines.append(f"- **Recommendation:** {issue.recommendation or 'Fix the issue.'}")
        lines.append("")
    return "\n".join(lines)


def run_batch_coding_fixes(
    *,
    llm: LLMClient,
    microtask: Microtask,
    issues: List[ReviewIssue],
    current_files: Dict[str, str],
    language: str = "typescript",
    repo_path: str = "",
    task_id: str = "",
    phase_name: str = "review",
    detail_callback: Optional[Callable[[str], None]] = None,
) -> ProblemSolvingResult:
    """
    Fix ALL issues from a review phase in a single batch.

    Instead of fixing issues one at a time, this function sends all issues
    to the coding agent at once, allowing it to decide how to organize
    the fixes internally.

    Args:
        llm: LLM client for code generation
        microtask: The microtask being fixed
        issues: Complete list of issues from the review phase
        current_files: Current state of all files
        language: Programming language (typescript/javascript)
        repo_path: Path to repository
        task_id: Task identifier for logging
        phase_name: Name of the review phase (code_review, qa, security)
        detail_callback: Optional callback for status updates

    Returns:
        ProblemSolvingResult with updated files and summary
    """
    microtask_id = microtask.id
    actionable = [i for i in issues if i.severity in ("critical", "high", "medium")]
    
    if not actionable:
        logger.info("[%s] Batch fix for %s: no actionable issues.", task_id, phase_name)
        return ProblemSolvingResult(
            resolved=True,
            files=current_files,
            summary=f"No actionable {phase_name} issues to fix.",
        )

    lang_conv = TYPESCRIPT_CONVENTIONS
    
    logger.info(
        "[%s] Microtask %s: batch fixing %d %s issues. Sending all issues to coding agent.",
        task_id, microtask_id, len(actionable), phase_name,
    )

    if detail_callback:
        detail_callback(f"Fixing all {len(actionable)} {phase_name} issues in batch...")

    formatted_issues = _format_issues_for_batch(actionable)
    current_code = _format_all_code(current_files)

    prompt = BATCH_FIX_PROMPT.format(
        language_conventions=lang_conv,
        issue_count=len(actionable),
        phase_name=phase_name,
        formatted_issues=formatted_issues,
        current_code=current_code,
    )

    try:
        raw = llm.complete_text(prompt)
    except Exception as exc:
        logger.error(
            "[%s] Microtask %s: batch fix LLM call failed: %s",
            task_id, microtask_id, exc,
        )
        return ProblemSolvingResult(
            resolved=False,
            files=current_files,
            summary=f"Batch fix failed: {exc}",
            unresolved_issues=actionable,
        )

    parsed = parse_batch_fix_template(raw)
    fixed_files = parsed.get("files") or {}
    issues_addressed = parsed.get("issues_addressed") or []
    summary = parsed.get("summary") or f"Batch fixed {len(fixed_files)} file(s)"

    merged = dict(current_files)
    merged.update(fixed_files)

    addressed_count = len(issues_addressed)
    
    unresolved_issues: List[ReviewIssue] = []
    if addressed_count < len(actionable):
        addressed_indices = set()
        for item in issues_addressed:
            try:
                idx = int(item.get("issue_index", 0)) - 1
                if 0 <= idx < len(actionable):
                    addressed_indices.add(idx)
            except (ValueError, TypeError):
                pass
        for idx, issue in enumerate(actionable):
            if idx not in addressed_indices:
                unresolved_issues.append(issue)

    resolved = len(unresolved_issues) == 0

    logger.info(
        "[%s] Microtask %s: batch fix complete. %d files updated, %d/%d issues addressed.",
        task_id, microtask_id, len(fixed_files), addressed_count, len(actionable),
    )

    if detail_callback:
        detail_callback(f"Batch fix complete: {addressed_count}/{len(actionable)} issues addressed")

    return ProblemSolvingResult(
        resolved=resolved,
        files=merged,
        summary=summary,
        fixes_applied=[
            {
                "microtask": microtask_id,
                "phase": phase_name,
                "batch_size": len(actionable),
                "addressed": addressed_count,
            }
        ],
        unresolved_issues=unresolved_issues,
    )


def _relevant_code_for_issue(
    issue: ReviewIssue,
    current_files: Dict[str, str],
) -> str:
    if issue.file_path and issue.file_path in current_files:
        content = current_files[issue.file_path]
        if len(content) <= MAX_RELEVANT_CODE_CHARS:
            return f"--- {issue.file_path} ---\n{content}"
        return f"--- {issue.file_path} ---\n{content[:MAX_RELEVANT_CODE_CHARS]}\n... [truncated]"
    parts: List[str] = []
    total = 0
    for path, content in list(current_files.items())[:10]:
        chunk = f"--- {path} ---\n{content}\n"
        if total + len(chunk) > MAX_RELEVANT_CODE_CHARS:
            remaining = MAX_RELEVANT_CODE_CHARS - total
            if remaining > 200:
                chunk = f"--- {path} ---\n{content[:remaining]}\n... [truncated]"
                parts.append(chunk)
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts) if parts else "(no code)"


def run_problem_solving(
    *,
    llm: LLMClient,
    task: Task,
    review_result: ReviewResult,
    current_files: Dict[str, str],
    language: str = "typescript",
    repo_path: str = "",
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
) -> ProblemSolvingResult:
    """Analyse review issues and produce fixes, one issue at a time."""
    task_id = task.id
    actionable = [i for i in review_result.issues if i.severity in ("critical", "high", "medium")]
    if not actionable:
        return ProblemSolvingResult(resolved=True, files=current_files, summary="No actionable issues.")

    merged = dict(current_files)
    fixes_applied: List[Dict[str, Any]] = []
    summary_parts: List[str] = []
    unresolved_issues: List[ReviewIssue] = []

    for issue_idx, issue in enumerate(actionable):
        desc_short = (issue.description or "")[:80]
        logger.info("[%s] Problem-solving: issue %d/%d — %s", task_id, issue_idx + 1, len(actionable), desc_short)
        working = dict(merged)
        resolved_this = False
        for attempt in range(1, MAX_ITERATIONS_PER_ISSUE + 1):
            relevant_code = _relevant_code_for_issue(issue, working)
            prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT.format(
                source=issue.source or "review",
                severity=issue.severity or "medium",
                description=issue.description or "",
                file_path=issue.file_path or "N/A",
                recommendation=issue.recommendation or "Fix the issue.",
                current_code=relevant_code,
            )
            try:
                raw = llm.complete_text(prompt)
            except Exception as exc:
                logger.warning("[%s] Problem-solving LLM call failed (issue %d, attempt %d): %s", task_id, issue_idx + 1, attempt, exc)
                break
            parsed = parse_problem_solving_single_issue_template(raw)
            fixed_files = parsed.get("files") or {}
            if not fixed_files:
                if parsed.get("resolved"):
                    resolved_this = True
                break

            working.update(fixed_files)
            merged.update(fixed_files)
            fixes_applied.append({
                "issue": desc_short,
                "fix": parsed.get("summary", "updated file(s)"),
                "root_cause": parsed.get("root_cause", ""),
            })
            if parsed.get("resolved"):
                resolved_this = True
                break
        if not resolved_this:
            unresolved_issues.append(issue)

    if tool_agents:
        phase_inp = ToolAgentPhaseInput(
            phase=Phase.PROBLEM_SOLVING,
            repo_path=repo_path,
            spec_context=task.description or "",
            language=language,
            current_files=merged,
            review_issues=review_result.issues,
            task_title=task.title or "",
            task_description=task.description or "",
        )
        for kind, agent in tool_agents.items():
            if not hasattr(agent, "problem_solve"):
                continue
            try:
                out = agent.problem_solve(phase_inp)
                if out.files:
                    merged.update(out.files)
                if out.recommendations:
                    fixes_applied.extend([{"source": kind.value, "recommendation": r} for r in out.recommendations])
                    summary_parts.append(f"Tool {kind.value}: {out.summary or 'suggestions applied.'}")
            except Exception as exc:
                logger.warning("[%s] Tool agent %s problem_solve() failed: %s", task_id, kind.value, exc)

    resolved = len(unresolved_issues) == 0
    summary = " ".join(summary_parts) if summary_parts else f"Applied {len(fixes_applied)} fix(s); {len(unresolved_issues)} unresolved."
    return ProblemSolvingResult(
        fixes_applied=fixes_applied,
        files=merged,
        summary=summary,
        resolved=resolved,
        unresolved_issues=unresolved_issues,
    )


def run_problem_solving_for_microtask(
    *,
    llm: LLMClient,
    microtask: Microtask,
    review_result: ReviewResult,
    current_files: Dict[str, str],
    language: str = "typescript",
    repo_path: str = "",
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    task_id: str = "",
    detail_callback: Optional[Callable[[str], None]] = None,
) -> ProblemSolvingResult:
    """
    Fix issues for a single microtask, one issue at a time.

    This function is similar to run_problem_solving() but is scoped to a single
    microtask's files, enabling per-microtask problem-solving within the review loop.

    Args:
        detail_callback: Optional callback to report detailed status messages
            (e.g., "Fixing issue 2/5: Missing null check...").
    """
    microtask_id = microtask.id
    actionable = [i for i in review_result.issues if i.severity in ("critical", "high", "medium")]
    if not actionable:
        return ProblemSolvingResult(resolved=True, files=current_files, summary="No actionable issues.")

    merged = dict(current_files)
    fixes_applied: List[Dict[str, Any]] = []
    summary_parts: List[str] = []
    unresolved_issues: List[ReviewIssue] = []

    logger.info("[%s] Problem-solving for microtask %s: %d actionable issues", task_id, microtask_id, len(actionable))

    for issue_idx, issue in enumerate(actionable):
        desc_short = (issue.description or "")[:80]
        if detail_callback:
            detail_callback(f"Fixing issue {issue_idx + 1}/{len(actionable)}: {desc_short[:50]}...")
        logger.info("[%s] Microtask %s: fixing issue %d/%d — %s", task_id, microtask_id, issue_idx + 1, len(actionable), desc_short)
        working = dict(merged)
        resolved_this = False

        for attempt in range(1, MAX_ITERATIONS_PER_ISSUE + 1):
            relevant_code = _relevant_code_for_issue(issue, working)
            prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT.format(
                source=issue.source or "review",
                severity=issue.severity or "medium",
                description=issue.description or "",
                file_path=issue.file_path or "N/A",
                recommendation=issue.recommendation or "Fix the issue.",
                current_code=relevant_code,
            )
            try:
                raw = llm.complete_text(prompt)
            except Exception as exc:
                logger.warning("[%s] Microtask %s: problem-solving LLM call failed (issue %d, attempt %d): %s",
                               task_id, microtask_id, issue_idx + 1, attempt, exc)
                break

            parsed = parse_problem_solving_single_issue_template(raw)
            fixed_files = parsed.get("files") or {}
            if not fixed_files:
                if parsed.get("resolved"):
                    resolved_this = True
                break

            working.update(fixed_files)
            merged.update(fixed_files)
            fixes_applied.append({
                "microtask": microtask_id,
                "issue": desc_short,
                "fix": parsed.get("summary", "updated file(s)"),
                "root_cause": parsed.get("root_cause", ""),
            })
            if parsed.get("resolved"):
                resolved_this = True
                break

        if not resolved_this:
            unresolved_issues.append(issue)

    if tool_agents:
        phase_inp = ToolAgentPhaseInput(
            phase=Phase.PROBLEM_SOLVING,
            microtask=microtask,
            repo_path=repo_path,
            spec_context=microtask.description or "",
            language=language,
            current_files=merged,
            review_issues=review_result.issues,
            task_title=microtask.title or "",
            task_description=microtask.description or "",
            task_id=task_id,
        )
        for kind, agent in tool_agents.items():
            if not hasattr(agent, "problem_solve"):
                continue
            try:
                out = agent.problem_solve(phase_inp)
                if out.files:
                    merged.update(out.files)
                if out.recommendations:
                    fixes_applied.extend([{"source": kind.value, "microtask": microtask_id, "recommendation": r} for r in out.recommendations])
                    summary_parts.append(f"Tool {kind.value}: {out.summary or 'suggestions applied.'}")
            except Exception as exc:
                logger.warning("[%s] Microtask %s: tool agent %s problem_solve() failed: %s", task_id, microtask_id, kind.value, exc)

    resolved = len(unresolved_issues) == 0
    summary = " ".join(summary_parts) if summary_parts else f"Microtask {microtask_id}: applied {len(fixes_applied)} fix(s); {len(unresolved_issues)} unresolved."
    logger.info("[%s] %s", task_id, summary)

    return ProblemSolvingResult(
        fixes_applied=fixes_applied,
        files=merged,
        summary=summary,
        resolved=resolved,
        unresolved_issues=unresolved_issues,
    )
