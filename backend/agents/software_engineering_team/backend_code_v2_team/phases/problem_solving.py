"""
Problem-solving phase: root-cause analysis and fix loop.

Processes one issue at a time to keep LLM prompts and responses small.
Each issue gets up to MAX_ITERATIONS_PER_ISSUE attempts; unresolved issues
are returned for the backend v2 agent to turn into fix microtasks.

The formatting, batch-fix, single-issue loop, and top-level orchestration are
shared across the code-v2 teams (see ``shared/phases/problem_solving.py``); this
module wires in the backend team's models/prompts/profile and keeps the
backend-only phase-specific fix functions, which interlock with the backend
``review.py`` phase functions.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from strands import Agent

from llm_service import LLMClient
from software_engineering_team.shared.models import Task
from software_engineering_team.shared.phases.problem_solving import (
    MAX_ITERATIONS_PER_ISSUE,
    _fix_issues_one_at_a_time_impl,
    _format_all_code,
    _format_issues_for_batch,
    _relevant_code_for_issue,
    run_batch_coding_fixes_impl,
    run_problem_solving_for_microtask_impl,
    run_problem_solving_impl,
)
from software_engineering_team.shared.strands_model import (
    LlmRunner,
    resolve_text_mode_strands_model,
)

from .. import models as _models
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
from ..prompts import BATCH_FIX_PROMPT, PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT
from ._profile import PROFILE

logger = logging.getLogger(__name__)


def _llm_runner() -> LlmRunner:
    """Build the LLM runner from this module's globals so tests can monkeypatch them."""
    return LlmRunner(agent_factory=Agent, resolve_model=resolve_text_mode_strands_model)


__all__ = [
    "MAX_ITERATIONS_PER_ISSUE",
    "run_batch_coding_fixes",
    "run_problem_solving",
    "run_problem_solving_for_microtask",
    "run_code_review_fixes",
    "run_qa_fixes",
    "run_security_fixes",
    "run_documentation_fixes",
    "_format_all_code",
    "_format_issues_for_batch",
    "_relevant_code_for_issue",
]


def run_batch_coding_fixes(
    *,
    llm: LLMClient,
    microtask: Microtask,
    issues: List[ReviewIssue],
    current_files: Dict[str, str],
    language: str = "python",
    task_id: str = "",
    phase_name: str = "review",
    detail_callback: Optional[Callable[[str], None]] = None,
) -> ProblemSolvingResult:
    """Fix ALL issues from a review phase in a single batch (backend models)."""
    return run_batch_coding_fixes_impl(
        llm=llm,
        microtask=microtask,
        issues=issues,
        current_files=current_files,
        language=language,
        task_id=task_id,
        phase_name=phase_name,
        detail_callback=detail_callback,
        profile=PROFILE,
        models=_models,
        batch_fix_prompt=BATCH_FIX_PROMPT,
        parse_batch_fix_template=parse_batch_fix_template,
        runner=_llm_runner(),
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
    """Analyse review issues and produce fixes, one issue at a time (backend models)."""
    return run_problem_solving_impl(
        llm=llm,
        task=task,
        review_result=review_result,
        current_files=current_files,
        language=language,
        repo_path=repo_path,
        tool_agents=tool_agents,
        profile=PROFILE,
        models=_models,
        single_issue_prompt=PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT,
        parse_single=parse_problem_solving_single_issue_template,
        runner=_llm_runner(),
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
    """Fix issues for a single microtask, one issue at a time (backend models)."""
    return run_problem_solving_for_microtask_impl(
        llm=llm,
        microtask=microtask,
        review_result=review_result,
        current_files=current_files,
        language=language,
        repo_path=repo_path,
        tool_agents=tool_agents,
        task_id=task_id,
        detail_callback=detail_callback,
        profile=PROFILE,
        models=_models,
        single_issue_prompt=PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT,
        parse_single=parse_problem_solving_single_issue_template,
        runner=_llm_runner(),
    )


# ---------------------------------------------------------------------------
# Phase-specific fix functions (backend-only; interlock with backend review.py)
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

    merged, fixes_applied, unresolved_issues = _fix_issues_one_at_a_time_impl(
        llm=llm,
        actionable=actionable,
        current_files=current_files,
        lang_conv=PROFILE.conventions_for(language),
        task_id=task_id,
        single_issue_prompt=PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT,
        parse_single=parse_problem_solving_single_issue_template,
        has_language_conventions=PROFILE.has_language_conventions,
        runner=_llm_runner(),
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


# "qa" and "security" are intentionally absent: TestingQAToolAgent and
# SecurityToolAgent are review-only (they no longer implement problem_solve) —
# fixing their findings is the coding agent's job, done entirely by the
# generic per-issue fix loop in _run_phase_fixes. Only Build Specialist (whose
# "fix" is mechanically re-running the build) and Documentation (which fixes
# the same prose/docs artifacts it authors itself) still get a second,
# tool-agent-driven fix pass.
_PHASE_FIX_TOOL_AGENT: Dict[str, ToolAgentKind] = {
    "code_review": ToolAgentKind.BUILD_SPECIALIST,
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
    Postconditions: returns the phase fix result from the generic per-issue fix
    loop, with the tool agent's file updates merged in when that agent is
    wired, supports ``problem_solve``, and the call succeeds. If the tool
    agent's ``problem_solve`` raises, the exception is logged (with
    traceback), the generic loop's file updates already in ``result.files``
    are preserved unchanged, and ``result.resolved`` is forced to ``False``
    with a note appended to ``result.summary`` describing the failure.
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
                logger.exception(
                    "[%s] %s tool agent problem_solve failed: %s", task_id, phase_name, exc
                )
                result.resolved = False
                result.summary += f" (tool-agent fix pass failed: {exc})"

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
    """Fix issues from QA testing phase (bugs, missing tests, quality issues).

    The QA tool agent only reports findings (see ``TestingQAToolAgent``) — all
    fixing here is done by the generic per-issue coding-agent loop; there is no
    second, tool-agent-driven fix pass. ``tool_agents`` is accepted for
    signature parity with the other phase-fix functions but unused.
    """
    del tool_agents  # QA is review-only; fixing is the coding agent's job.
    return _run_phase_fixes(
        llm=llm,
        microtask=microtask,
        phase_result=phase_result,
        current_files=current_files,
        language=language,
        repo_path=repo_path,
        tool_agents=None,
        task_id=task_id,
        phase_name="qa",
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
    """Fix issues from security testing phase (vulnerabilities, security best practices).

    The security tool agent only reports findings (see ``SecurityToolAgent``) —
    all fixing here is done by the generic per-issue coding-agent loop; there is
    no second, tool-agent-driven fix pass. ``tool_agents`` is accepted for
    signature parity with the other phase-fix functions but unused.
    """
    del tool_agents  # Security is review-only; fixing is the coding agent's job.
    return _run_phase_fixes(
        llm=llm,
        microtask=microtask,
        phase_result=phase_result,
        current_files=current_files,
        language=language,
        repo_path=repo_path,
        tool_agents=None,
        task_id=task_id,
        phase_name="security",
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
