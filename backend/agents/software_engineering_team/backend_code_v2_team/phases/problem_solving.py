"""
Problem-solving phase: root-cause analysis and fix loop.

Processes one issue at a time to keep LLM prompts and responses small.
Each issue gets up to MAX_ITERATIONS_PER_ISSUE attempts; unresolved issues
are returned for the backend v2 agent to turn into fix microtasks.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from strands import Agent

from llm_service import LLMClient
from software_engineering_team.shared.models import Task
from software_engineering_team.shared.strands_model import resolve_text_mode_strands_model

from ..models import (
    Microtask,
    Phase,
    PhaseReviewResult,
    ProblemSolvingResult,
    ReviewIssue,
    ReviewResult,
    ToolAgentKind,
    ToolAgentPhaseInput,
)
from ..output_templates import (
    parse_batch_fix_template,
    parse_problem_solving_single_issue_template,
)
from ..prompts import (
    BATCH_FIX_PROMPT,
    JAVA_CONVENTIONS,
    PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT,
    PYTHON_CONVENTIONS,
)

logger = logging.getLogger(__name__)

MAX_ITERATIONS_PER_ISSUE = 5

MAX_BATCH_FIX_CODE_CHARS = 60_000  # Cap context to avoid blowing up the LLM context window


def _lang_conventions(language: str) -> str:
    """Return the language-convention block injected into fix prompts."""
    return JAVA_CONVENTIONS if language == "java" else PYTHON_CONVENTIONS


def _format_all_code(
    current_files: Dict[str, str], max_chars: int = MAX_BATCH_FIX_CODE_CHARS
) -> str:
    """Format current files for batch fix prompt, truncating to stay within budget."""
    parts: List[str] = []
    total = 0
    for path, content in current_files.items():
        chunk = f"--- {path} ---\n{content}\n"
        if total + len(chunk) > max_chars:
            parts.append(f"--- {path} --- (truncated, {len(content)} chars omitted)\n")
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
    language: str = "python",
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
        language: Programming language (python/java)
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

    lang_conv = _lang_conventions(language)

    logger.info(
        "[%s] Microtask %s: batch fixing %d %s issues. Sending all issues to coding agent.",
        task_id,
        microtask_id,
        len(actionable),
        phase_name,
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
        raw = (lambda _r: str(_r))(
            Agent(model=resolve_text_mode_strands_model(llm))(prompt)
        ).strip()
    except Exception as exc:
        logger.error(
            "[%s] Microtask %s: batch fix LLM call failed: %s",
            task_id,
            microtask_id,
            exc,
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
        task_id,
        microtask_id,
        len(fixed_files),
        addressed_count,
        len(actionable),
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
    """Return code context for a single issue: prefer issue's file, else first files."""
    if issue.file_path and issue.file_path in current_files:
        content = current_files[issue.file_path]
        return f"--- {issue.file_path} ---\n{content}"
    # Fallback: include first files
    parts: List[str] = []
    for path, content in list(current_files.items())[:10]:
        parts.append(f"--- {path} ---\n{content}\n")
    return "\n".join(parts) if parts else "(no code)"


def _fix_issues_one_at_a_time(
    *,
    llm: LLMClient,
    actionable: List[ReviewIssue],
    current_files: Dict[str, str],
    lang_conv: str,
    task_id: str,
    microtask_id: str = "",
    phase_name: str = "",
    detail_callback: Optional[Callable[[str], None]] = None,
) -> tuple[Dict[str, str], List[Dict[str, Any]], List[ReviewIssue]]:
    """Fix each actionable issue in isolation, up to MAX_ITERATIONS_PER_ISSUE attempts.

    Shared inner loop behind :func:`run_problem_solving`,
    :func:`run_problem_solving_for_microtask` and :func:`_run_phase_fixes`. The
    three callers differ only in logging labels and which keys each
    ``fixes_applied`` entry carries (``microtask``/``phase``).

    Preconditions: ``actionable`` is already filtered to actionable severities.
    Postconditions: returns ``(merged_files, fixes_applied, unresolved_issues)``;
    ``merged_files`` is a fresh dict (never the caller's input object).
    """
    merged = dict(current_files)
    fixes_applied: List[Dict[str, Any]] = []
    unresolved_issues: List[ReviewIssue] = []

    default_source = phase_name or "review"
    default_recommendation = f"Fix the {phase_name} issue." if phase_name else "Fix the issue."
    mt_ctx = f"Microtask {microtask_id}: " if microtask_id else ""
    phase_ctx = f"{phase_name} " if phase_name else ""

    for issue_idx, issue in enumerate(actionable):
        desc_short = (issue.description or "")[:80]
        if detail_callback:
            detail_callback(
                f"Fixing {phase_ctx}issue {issue_idx + 1}/{len(actionable)}: {desc_short[:50]}..."
            )
        logger.info(
            "[%s] %sfixing %sissue %d/%d — %s. Next step -> Attempting fix (up to %d iterations)",
            task_id,
            mt_ctx,
            phase_ctx,
            issue_idx + 1,
            len(actionable),
            desc_short,
            MAX_ITERATIONS_PER_ISSUE,
        )
        working = dict(merged)
        resolved_this = False

        for attempt in range(1, MAX_ITERATIONS_PER_ISSUE + 1):
            relevant_code = _relevant_code_for_issue(issue, working)
            prompt = PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT.format(
                language_conventions=lang_conv,
                source=issue.source or default_source,
                severity=issue.severity or "medium",
                description=issue.description or "",
                file_path=issue.file_path or "N/A",
                recommendation=issue.recommendation or default_recommendation,
                current_code=relevant_code,
            )
            try:
                raw = str(Agent(model=resolve_text_mode_strands_model(llm))(prompt)).strip()
            except Exception as exc:
                logger.warning(
                    "[%s] %s%sfix LLM call failed (issue %d, attempt %d): %s",
                    task_id,
                    mt_ctx,
                    phase_ctx,
                    issue_idx + 1,
                    attempt,
                    exc,
                )
                break

            parsed = parse_problem_solving_single_issue_template(raw)
            fixed_files = parsed.get("files") or {}
            if not fixed_files:
                if parsed.get("resolved"):
                    resolved_this = True
                break

            working.update(fixed_files)
            merged.update(fixed_files)
            entry: Dict[str, Any] = {}
            if microtask_id:
                entry["microtask"] = microtask_id
            if phase_name:
                entry["phase"] = phase_name
            entry["issue"] = desc_short
            entry["fix"] = parsed.get("summary", "updated file(s)")
            entry["root_cause"] = parsed.get("root_cause", "")
            fixes_applied.append(entry)
            if parsed.get("resolved"):
                resolved_this = True
                break

        if not resolved_this:
            unresolved_issues.append(issue)
            logger.warning(
                "[%s] %s%sissue unresolved. Recovery summary: "
                "1) Attempted %d fix iterations, 2) No successful resolution. Issue: %s",
                task_id,
                mt_ctx,
                phase_ctx,
                MAX_ITERATIONS_PER_ISSUE,
                desc_short,
            )

    return merged, fixes_applied, unresolved_issues


def _apply_tool_agents_problem_solve(
    *,
    tool_agents: Dict[ToolAgentKind, Any],
    phase_inp: ToolAgentPhaseInput,
    merged: Dict[str, str],
    fixes_applied: List[Dict[str, Any]],
    summary_parts: List[str],
    task_id: str,
    microtask_id: str = "",
) -> None:
    """Run every tool agent's ``problem_solve``; merge files, record recommendations.

    Mutates ``merged``, ``fixes_applied`` and ``summary_parts`` in place.
    """
    for kind, agent in tool_agents.items():
        if not hasattr(agent, "problem_solve"):
            continue
        try:
            out = agent.problem_solve(phase_inp)
            if out.files:
                merged.update(out.files)
            if out.recommendations:
                for r in out.recommendations:
                    entry: Dict[str, Any] = {"source": kind.value, "recommendation": r}
                    if microtask_id:
                        entry["microtask"] = microtask_id
                    fixes_applied.append(entry)
                summary_parts.append(f"Tool {kind.value}: {out.summary or 'suggestions applied.'}")
        except Exception as exc:
            mt_ctx = f"Microtask {microtask_id}: " if microtask_id else ""
            logger.warning(
                "[%s] %stool agent %s problem_solve() failed: %s",
                task_id,
                mt_ctx,
                kind.value,
                exc,
            )


def run_problem_solving(
    *,
    llm: LLMClient,
    task: Task,
    review_result: ReviewResult,
    current_files: Dict[str, str],
    language: str = "python",
    repo_path: str = "",
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
) -> ProblemSolvingResult:
    """
    Analyse review issues and produce fixes, one issue at a time.

    For each actionable issue: identify root cause, implement fix (up to
    MAX_ITERATIONS_PER_ISSUE attempts). Unresolved issues are returned for
    the backend v2 agent to turn into fix microtasks.
    """
    task_id = task.id
    actionable = [i for i in review_result.issues if i.severity in ("critical", "high", "medium")]
    if not actionable:
        logger.info("[%s] Problem-solving: no actionable issues.", task_id)
        return ProblemSolvingResult(
            resolved=True, files=current_files, summary="No actionable issues."
        )

    merged, fixes_applied, unresolved_issues = _fix_issues_one_at_a_time(
        llm=llm,
        actionable=actionable,
        current_files=current_files,
        lang_conv=_lang_conventions(language),
        task_id=task_id,
    )
    summary_parts: List[str] = []

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
        _apply_tool_agents_problem_solve(
            tool_agents=tool_agents,
            phase_inp=phase_inp,
            merged=merged,
            fixes_applied=fixes_applied,
            summary_parts=summary_parts,
            task_id=task_id,
        )

    resolved = len(unresolved_issues) == 0
    summary = (
        " ".join(summary_parts)
        if summary_parts
        else f"Applied {len(fixes_applied)} fix(s); {len(unresolved_issues)} unresolved."
    )
    logger.info(
        "[%s] Problem-solving: %s — %s (%d unresolved)",
        task_id,
        "resolved" if resolved else "partial",
        summary[:120],
        len(unresolved_issues),
    )
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
    language: str = "python",
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
        return ProblemSolvingResult(
            resolved=True, files=current_files, summary="No actionable issues."
        )

    logger.info(
        "[%s] Problem-solving for microtask %s: %d actionable issues",
        task_id,
        microtask_id,
        len(actionable),
    )

    merged, fixes_applied, unresolved_issues = _fix_issues_one_at_a_time(
        llm=llm,
        actionable=actionable,
        current_files=current_files,
        lang_conv=_lang_conventions(language),
        task_id=task_id,
        microtask_id=microtask_id,
        detail_callback=detail_callback,
    )
    summary_parts: List[str] = []

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
        _apply_tool_agents_problem_solve(
            tool_agents=tool_agents,
            phase_inp=phase_inp,
            merged=merged,
            fixes_applied=fixes_applied,
            summary_parts=summary_parts,
            task_id=task_id,
            microtask_id=microtask_id,
        )

    resolved = len(unresolved_issues) == 0
    summary = (
        " ".join(summary_parts)
        if summary_parts
        else f"Microtask {microtask_id}: applied {len(fixes_applied)} fix(s); {len(unresolved_issues)} unresolved."
    )
    logger.info("[%s] %s", task_id, summary)

    return ProblemSolvingResult(
        fixes_applied=fixes_applied,
        files=merged,
        summary=summary,
        resolved=resolved,
        unresolved_issues=unresolved_issues,
    )


# ---------------------------------------------------------------------------
# Phase-specific fix functions
# ---------------------------------------------------------------------------


def _run_phase_fixes(
    *,
    llm: LLMClient,
    microtask: Microtask,
    phase_result: PhaseReviewResult,
    current_files: Dict[str, str],
    language: str = "python",
    repo_path: str = "",
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    task_id: str = "",
    phase_name: str = "",
    detail_callback: Optional[Callable[[str], None]] = None,
) -> ProblemSolvingResult:
    """
    Common implementation for phase-specific fixes.

    Processes issues from a specific phase review and applies fixes.
    """
    microtask_id = microtask.id
    actionable = [i for i in phase_result.issues if i.severity in ("critical", "high", "medium")]
    if not actionable:
        return ProblemSolvingResult(
            resolved=True, files=current_files, summary=f"No actionable {phase_name} issues."
        )

    logger.info(
        "[%s] %s fixes for microtask %s: %d actionable issues",
        task_id,
        phase_name.title(),
        microtask_id,
        len(actionable),
    )

    merged, fixes_applied, unresolved_issues = _fix_issues_one_at_a_time(
        llm=llm,
        actionable=actionable,
        current_files=current_files,
        lang_conv=_lang_conventions(language),
        task_id=task_id,
        microtask_id=microtask_id,
        phase_name=phase_name,
        detail_callback=detail_callback,
    )

    resolved = len(unresolved_issues) == 0
    summary = f"Microtask {microtask_id} {phase_name}: applied {len(fixes_applied)} fix(s); {len(unresolved_issues)} unresolved."
    logger.info("[%s] %s", task_id, summary)

    return ProblemSolvingResult(
        fixes_applied=fixes_applied,
        files=merged,
        summary=summary,
        resolved=resolved,
        unresolved_issues=unresolved_issues,
    )


_PHASE_FIX_TOOL_AGENT: Dict[str, ToolAgentKind] = {
    "code_review": ToolAgentKind.BUILD_SPECIALIST,
    "qa": ToolAgentKind.TESTING_QA,
    "security": ToolAgentKind.SECURITY,
    "documentation": ToolAgentKind.DOCUMENTATION,
}


def _run_phase_fixes_with_tool_agent(
    *,
    phase_name: str,
    llm: LLMClient,
    microtask: Microtask,
    phase_result: PhaseReviewResult,
    current_files: Dict[str, str],
    language: str = "python",
    repo_path: str = "",
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    task_id: str = "",
    detail_callback: Optional[Callable[[str], None]] = None,
) -> ProblemSolvingResult:
    """Run the generic phase fixes, then let the phase's dedicated tool agent take a pass.

    Preconditions: ``phase_name`` is a key of :data:`_PHASE_FIX_TOOL_AGENT`.
    Postconditions: returns the phase fix result, with the tool agent's file
    updates merged in when that agent is wired and supports ``problem_solve``.
    """
    result = _run_phase_fixes(
        llm=llm,
        microtask=microtask,
        phase_result=phase_result,
        current_files=current_files,
        language=language,
        repo_path=repo_path,
        tool_agents=tool_agents,
        task_id=task_id,
        phase_name=phase_name,
        detail_callback=detail_callback,
    )

    kind = _PHASE_FIX_TOOL_AGENT[phase_name]
    if tool_agents and kind in tool_agents:
        agent = tool_agents[kind]
        if hasattr(agent, "problem_solve"):
            try:
                phase_inp = ToolAgentPhaseInput(
                    phase=Phase.PROBLEM_SOLVING,
                    microtask=microtask,
                    repo_path=repo_path,
                    spec_context=microtask.description or "",
                    language=language,
                    current_files=result.files,
                    review_issues=phase_result.issues,
                    task_title=microtask.title or "",
                    task_description=microtask.description or "",
                    task_id=task_id,
                )
                out = agent.problem_solve(phase_inp)
                if out.files:
                    result.files.update(out.files)
            except Exception as exc:
                logger.warning(
                    "[%s] %s tool agent problem_solve failed: %s", task_id, phase_name, exc
                )

    return result


def run_code_review_fixes(
    *,
    llm: LLMClient,
    microtask: Microtask,
    phase_result: PhaseReviewResult,
    current_files: Dict[str, str],
    language: str = "python",
    repo_path: str = "",
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    task_id: str = "",
    detail_callback: Optional[Callable[[str], None]] = None,
) -> ProblemSolvingResult:
    """Fix issues from code review phase (build errors, lint issues, code quality)."""
    return _run_phase_fixes_with_tool_agent(
        phase_name="code_review",
        llm=llm,
        microtask=microtask,
        phase_result=phase_result,
        current_files=current_files,
        language=language,
        repo_path=repo_path,
        tool_agents=tool_agents,
        task_id=task_id,
        detail_callback=detail_callback,
    )


def run_qa_fixes(
    *,
    llm: LLMClient,
    microtask: Microtask,
    phase_result: PhaseReviewResult,
    current_files: Dict[str, str],
    language: str = "python",
    repo_path: str = "",
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    task_id: str = "",
    detail_callback: Optional[Callable[[str], None]] = None,
) -> ProblemSolvingResult:
    """Fix issues from QA testing phase (bugs, missing tests, quality issues)."""
    return _run_phase_fixes_with_tool_agent(
        phase_name="qa",
        llm=llm,
        microtask=microtask,
        phase_result=phase_result,
        current_files=current_files,
        language=language,
        repo_path=repo_path,
        tool_agents=tool_agents,
        task_id=task_id,
        detail_callback=detail_callback,
    )


def run_security_fixes(
    *,
    llm: LLMClient,
    microtask: Microtask,
    phase_result: PhaseReviewResult,
    current_files: Dict[str, str],
    language: str = "python",
    repo_path: str = "",
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    task_id: str = "",
    detail_callback: Optional[Callable[[str], None]] = None,
) -> ProblemSolvingResult:
    """Fix issues from security testing phase (vulnerabilities, security best practices)."""
    return _run_phase_fixes_with_tool_agent(
        phase_name="security",
        llm=llm,
        microtask=microtask,
        phase_result=phase_result,
        current_files=current_files,
        language=language,
        repo_path=repo_path,
        tool_agents=tool_agents,
        task_id=task_id,
        detail_callback=detail_callback,
    )


def run_documentation_fixes(
    *,
    llm: LLMClient,
    microtask: Microtask,
    phase_result: PhaseReviewResult,
    current_files: Dict[str, str],
    language: str = "python",
    repo_path: str = "",
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    task_id: str = "",
    detail_callback: Optional[Callable[[str], None]] = None,
) -> ProblemSolvingResult:
    """Fix issues from documentation review phase (missing docs, incomplete comments)."""
    return _run_phase_fixes_with_tool_agent(
        phase_name="documentation",
        llm=llm,
        microtask=microtask,
        phase_result=phase_result,
        current_files=current_files,
        language=language,
        repo_path=repo_path,
        tool_agents=tool_agents,
        task_id=task_id,
        detail_callback=detail_callback,
    )
