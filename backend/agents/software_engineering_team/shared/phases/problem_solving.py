"""
Shared Problem-solving-phase leaf helpers for the code-v2 teams.

Holds the code that was common (byte-identical or divergent only in logging /
language-conventions injection) between the backend and frontend problem-solving
phases: the code/issue formatters, the batch-fix pass, the one-issue-at-a-time
fix loop, the tool-agent problem-solve application, and the two top-level
``run_problem_solving`` / ``run_problem_solving_for_microtask`` orchestrations.

Team-local models are injected via the team's ``models`` module and the
stack-specific conventions / prompt-slot behavior via its
:class:`~software_engineering_team.shared.stack_profile.StackProfile`. Backend's
phase-specific fix functions (which interlock with the out-of-scope backend
``review.py``) stay in the backend team module and reuse
:func:`_fix_issues_one_at_a_time_impl` directly.
"""

from __future__ import annotations

import logging
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional

from llm_service import LLMClient
from software_engineering_team.shared.models import Task
from software_engineering_team.shared.stack_profile import StackProfile

logger = logging.getLogger(__name__)

MAX_ITERATIONS_PER_ISSUE = 5

MAX_BATCH_FIX_CODE_CHARS = 60_000  # Cap context to avoid blowing up the LLM context window


def _format_all_code(
    current_files: Dict[str, str], max_chars: int = MAX_BATCH_FIX_CODE_CHARS
) -> str:
    """Format current files for batch fix prompt, truncating to stay within budget.

    Preconditions:
        ``current_files`` maps paths to content; ``max_chars`` > 0.
    Postconditions:
        Returns a code block within ``max_chars`` (a trailing file is marked
        truncated); ``"(no code)"`` when empty. Pure.
    """
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


def _format_issues_for_batch(issues: List[Any]) -> str:
    """Format all issues into a numbered list for the batch fix prompt.

    Preconditions:
        ``issues`` is a list of review issues with the usual attributes.
    Postconditions:
        Returns a numbered, human-readable block. Pure.
    """
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


def _relevant_code_for_issue(issue: Any, current_files: Dict[str, str]) -> str:
    """Return code context for a single issue: prefer issue's file, else first files.

    Preconditions:
        ``issue`` has a ``file_path``; ``current_files`` maps paths to content.
    Postconditions:
        Returns the issue's file when known, else the first ≤10 files, else
        ``"(no code)"``. Pure.
    """
    if issue.file_path and issue.file_path in current_files:
        content = current_files[issue.file_path]
        return f"--- {issue.file_path} ---\n{content}"
    # Fallback: include first files
    parts: List[str] = []
    for path, content in list(current_files.items())[:10]:
        parts.append(f"--- {path} ---\n{content}\n")
    return "\n".join(parts) if parts else "(no code)"


def run_batch_coding_fixes_impl(
    *,
    llm: LLMClient,
    microtask: Any,
    issues: List[Any],
    current_files: Dict[str, str],
    language: str,
    repo_path: str,
    task_id: str,
    phase_name: str,
    detail_callback: Optional[Callable[[str], None]],
    profile: StackProfile,
    models: ModuleType,
    batch_fix_prompt: str,
    parse_batch_fix_template: Callable[[str], Dict[str, Any]],
    agent_factory: Callable[..., Any],
    resolve_model: Callable[[LLMClient], Any],
) -> Any:
    """Fix ALL issues from a review phase in a single batch.

    Instead of fixing issues one at a time, this sends all issues to the coding
    agent at once, allowing it to decide how to organize the fixes internally.

    Preconditions:
        ``batch_fix_prompt`` has a ``{language_conventions}`` slot (both stacks
        do); ``models`` exposes ``ProblemSolvingResult``; ``profile`` supplies the
        language conventions.
    Postconditions:
        Returns a ``ProblemSolvingResult``; on LLM failure returns an unresolved
        result carrying the actionable issues.
    """
    problem_solving_result_cls = models.ProblemSolvingResult

    microtask_id = microtask.id
    actionable = [i for i in issues if i.severity in ("critical", "high", "medium")]

    if not actionable:
        logger.info("[%s] Batch fix for %s: no actionable issues.", task_id, phase_name)
        return problem_solving_result_cls(
            resolved=True,
            files=current_files,
            summary=f"No actionable {phase_name} issues to fix.",
        )

    lang_conv = profile.conventions_for(language)

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

    prompt = batch_fix_prompt.format(
        language_conventions=lang_conv,
        issue_count=len(actionable),
        phase_name=phase_name,
        formatted_issues=formatted_issues,
        current_code=current_code,
    )

    try:
        raw = (lambda _r: str(_r))(agent_factory(model=resolve_model(llm))(prompt)).strip()
    except Exception as exc:
        logger.error(
            "[%s] Microtask %s: batch fix LLM call failed: %s",
            task_id,
            microtask_id,
            exc,
        )
        return problem_solving_result_cls(
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

    unresolved_issues: List[Any] = []
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

    return problem_solving_result_cls(
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


def _fix_issues_one_at_a_time_impl(
    *,
    llm: LLMClient,
    actionable: List[Any],
    current_files: Dict[str, str],
    lang_conv: str,
    task_id: str,
    single_issue_prompt: str,
    parse_single: Callable[[str], Dict[str, Any]],
    has_language_conventions: bool,
    agent_factory: Callable[..., Any],
    resolve_model: Callable[[LLMClient], Any],
    microtask_id: str = "",
    phase_name: str = "",
    detail_callback: Optional[Callable[[str], None]] = None,
) -> tuple[Dict[str, str], List[Dict[str, Any]], List[Any]]:
    """Fix each actionable issue in isolation, up to MAX_ITERATIONS_PER_ISSUE attempts.

    Shared inner loop behind ``run_problem_solving``,
    ``run_problem_solving_for_microtask`` and the backend phase-fix functions. The
    callers differ only in logging labels and which keys each ``fixes_applied``
    entry carries (``microtask``/``phase``).

    Preconditions:
        ``actionable`` is already filtered to actionable severities;
        ``single_issue_prompt`` carries a ``{language_conventions}`` slot iff
        ``has_language_conventions``.
    Postconditions:
        Returns ``(merged_files, fixes_applied, unresolved_issues)``;
        ``merged_files`` is a fresh dict (never the caller's input object).
    """
    merged = dict(current_files)
    fixes_applied: List[Dict[str, Any]] = []
    unresolved_issues: List[Any] = []

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
            fmt: Dict[str, Any] = dict(
                source=issue.source or default_source,
                severity=issue.severity or "medium",
                description=issue.description or "",
                file_path=issue.file_path or "N/A",
                recommendation=issue.recommendation or default_recommendation,
                current_code=relevant_code,
            )
            if has_language_conventions:
                fmt["language_conventions"] = lang_conv
            prompt = single_issue_prompt.format(**fmt)
            try:
                raw = str(agent_factory(model=resolve_model(llm))(prompt)).strip()
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

            parsed = parse_single(raw)
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
    tool_agents: Dict[Any, Any],
    phase_inp: Any,
    merged: Dict[str, str],
    fixes_applied: List[Dict[str, Any]],
    summary_parts: List[str],
    task_id: str,
    microtask_id: str = "",
) -> None:
    """Run every tool agent's ``problem_solve``; merge files, record recommendations.

    Preconditions:
        ``tool_agents`` maps kinds to agents; ``phase_inp`` is a
        ``ToolAgentPhaseInput``.
    Postconditions:
        Mutates ``merged``, ``fixes_applied`` and ``summary_parts`` in place; a
        failing agent is logged and skipped.
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


def run_problem_solving_impl(
    *,
    llm: LLMClient,
    task: Task,
    review_result: Any,
    current_files: Dict[str, str],
    language: str,
    repo_path: str,
    tool_agents: Optional[Dict[Any, Any]],
    profile: StackProfile,
    models: ModuleType,
    single_issue_prompt: str,
    parse_single: Callable[[str], Dict[str, Any]],
    agent_factory: Callable[..., Any],
    resolve_model: Callable[[LLMClient], Any],
) -> Any:
    """Analyse review issues and produce fixes, one issue at a time.

    Preconditions:
        ``models`` exposes ``ProblemSolvingResult``, ``Phase``,
        ``ToolAgentPhaseInput``; ``profile`` supplies conventions and the
        prompt-slot flag.
    Postconditions:
        Returns a ``ProblemSolvingResult``; unresolved issues are surfaced for
        the caller to escalate into fix microtasks.
    """
    problem_solving_result_cls = models.ProblemSolvingResult
    phase_enum = models.Phase
    phase_input_cls = models.ToolAgentPhaseInput

    task_id = task.id
    actionable = [i for i in review_result.issues if i.severity in ("critical", "high", "medium")]
    if not actionable:
        logger.info("[%s] Problem-solving: no actionable issues.", task_id)
        return problem_solving_result_cls(
            resolved=True, files=current_files, summary="No actionable issues."
        )

    merged, fixes_applied, unresolved_issues = _fix_issues_one_at_a_time_impl(
        llm=llm,
        actionable=actionable,
        current_files=current_files,
        lang_conv=profile.conventions_for(language),
        task_id=task_id,
        single_issue_prompt=single_issue_prompt,
        parse_single=parse_single,
        has_language_conventions=profile.problem_solving_has_language_conventions,
        agent_factory=agent_factory,
        resolve_model=resolve_model,
    )
    summary_parts: List[str] = []

    if tool_agents:
        phase_inp = phase_input_cls(
            phase=phase_enum.PROBLEM_SOLVING,
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
    return problem_solving_result_cls(
        fixes_applied=fixes_applied,
        files=merged,
        summary=summary,
        resolved=resolved,
        unresolved_issues=unresolved_issues,
    )


def run_problem_solving_for_microtask_impl(
    *,
    llm: LLMClient,
    microtask: Any,
    review_result: Any,
    current_files: Dict[str, str],
    language: str,
    repo_path: str,
    tool_agents: Optional[Dict[Any, Any]],
    task_id: str,
    detail_callback: Optional[Callable[[str], None]],
    profile: StackProfile,
    models: ModuleType,
    single_issue_prompt: str,
    parse_single: Callable[[str], Dict[str, Any]],
    agent_factory: Callable[..., Any],
    resolve_model: Callable[[LLMClient], Any],
) -> Any:
    """Fix issues for a single microtask, one issue at a time.

    Similar to :func:`run_problem_solving_impl` but scoped to a single microtask's
    files, enabling per-microtask problem-solving within the review loop.

    Preconditions / Postconditions: see :func:`run_problem_solving_impl`.
    """
    problem_solving_result_cls = models.ProblemSolvingResult
    phase_enum = models.Phase
    phase_input_cls = models.ToolAgentPhaseInput

    microtask_id = microtask.id
    actionable = [i for i in review_result.issues if i.severity in ("critical", "high", "medium")]
    if not actionable:
        return problem_solving_result_cls(
            resolved=True, files=current_files, summary="No actionable issues."
        )

    logger.info(
        "[%s] Problem-solving for microtask %s: %d actionable issues",
        task_id,
        microtask_id,
        len(actionable),
    )

    merged, fixes_applied, unresolved_issues = _fix_issues_one_at_a_time_impl(
        llm=llm,
        actionable=actionable,
        current_files=current_files,
        lang_conv=profile.conventions_for(language),
        task_id=task_id,
        single_issue_prompt=single_issue_prompt,
        parse_single=parse_single,
        has_language_conventions=profile.problem_solving_has_language_conventions,
        agent_factory=agent_factory,
        resolve_model=resolve_model,
        microtask_id=microtask_id,
        detail_callback=detail_callback,
    )
    summary_parts: List[str] = []

    if tool_agents:
        phase_inp = phase_input_cls(
            phase=phase_enum.PROBLEM_SOLVING,
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

    return problem_solving_result_cls(
        fixes_applied=fixes_applied,
        files=merged,
        summary=summary,
        resolved=resolved,
        unresolved_issues=unresolved_issues,
    )
