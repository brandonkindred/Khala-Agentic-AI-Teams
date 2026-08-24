"""
Problem-solving phase: root-cause analysis and fix loop.

Processes one issue at a time to keep LLM prompts and responses small.
Each issue gets up to MAX_ITERATIONS_PER_ISSUE attempts; unresolved issues
are returned for the backend v2 agent to turn into fix microtasks.

The formatting, batch-fix, single-issue loop, top-level orchestration, and
phase-specific fix functions are all shared across both stacks (see
``shared/phases/problem_solving.py``); this module wires in the backend
stack's models/prompts/profile and its ``code_review``/``documentation``
tool-agent map, which interlock with the backend review-gate phase
functions.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from strands import Agent

from llm_service import LLMClient
from llm_service.strands_model import (
    LlmRunner,
    resolve_text_mode_strands_model,
)
from shared.dev_models.models import Task
from software_engineering_team.codegen_team import models as _models
from software_engineering_team.codegen_team.models import (
    Microtask,
    ProblemSolvingResult,
    ReviewIssue,
    ReviewResult,
    ToolAgentKind,
)
from software_engineering_team.codegen_team.stacks.backend.prompts import (
    BATCH_FIX_PROMPT,
    PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT,
)
from software_engineering_team.shared.phases.problem_solving import (
    MAX_ITERATIONS_PER_ISSUE,
    _format_all_code,
    _format_issues_for_batch,
    _relevant_code_for_issue,
    make_phase_fix_functions,
    run_batch_coding_fixes_impl,
    run_problem_solving_for_microtask_impl,
    run_problem_solving_impl,
)

from .profile import (
    PROFILE,
    parse_batch_fix_template,
    parse_problem_solving_single_issue_template,
)

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
    """Fix ALL issues from a review phase in a single batch, bound to backend's
    models/prompts/profile.

    Thin wrapper over :func:`~software_engineering_team.shared.phases.problem_solving.run_batch_coding_fixes_impl`
    (see its docstring for the full Preconditions/Postconditions contract);
    this call only supplies the backend stack's ``PROFILE``, ``models``
    module, ``BATCH_FIX_PROMPT``, and template parser.

    Args:
        llm: LLM client used for the batch-fix call.
        microtask: The microtask whose files/issues are being fixed.
        issues: Review issues to fix; only ``critical``/``high``/``medium``
            severities are actionable (see the wrapped function).
        current_files: The microtask's current file contents, keyed by path.
        language: Language label used to select conventions text.
        task_id: Task id, used for logging only.
        phase_name: Which review phase produced ``issues`` (for logging/telemetry).
        detail_callback: Optional progress callback.

    Returns:
        A ``ProblemSolvingResult`` (backend's ``models.ProblemSolvingResult``).
    """
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
    """Analyse review issues and produce fixes, one issue at a time, bound to
    backend's models/prompts/profile.

    Thin wrapper over :func:`~software_engineering_team.shared.phases.problem_solving.run_problem_solving_impl`
    (see its docstring for the full Preconditions/Postconditions contract);
    this call only supplies the backend stack's ``PROFILE``, ``models``
    module, single-issue prompt, and template parser.

    Args:
        llm: LLM client used for each per-issue fix call.
        task: The task whose review issues are being resolved.
        review_result: The review-phase result carrying the issues to fix.
        current_files: The task's current file contents, keyed by path.
        language: Language label used to select conventions text.
        repo_path: Repo path, forwarded to any tool-agent fix pass.
        tool_agents: Optional tool-agent map for kinds with a dedicated fix
            pass (e.g. Build Specialist, Documentation); ``None`` skips them.

    Returns:
        A ``ProblemSolvingResult`` with unresolved issues surfaced for the
        caller to escalate into fix microtasks.
    """
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
    """Fix issues for a single microtask, one issue at a time, bound to
    backend's models/prompts/profile.

    Thin wrapper over :func:`~software_engineering_team.shared.phases.problem_solving.run_problem_solving_for_microtask_impl`
    (see its docstring, and :func:`run_problem_solving`'s, for the full
    Preconditions/Postconditions contract); scoped to a single microtask's
    files within the per-microtask review loop rather than the whole task.

    Args:
        llm: LLM client used for each per-issue fix call.
        microtask: The microtask whose files/issues are being fixed.
        review_result: The review-phase result carrying the issues to fix.
        current_files: The microtask's current file contents, keyed by path.
        language: Language label used to select conventions text.
        repo_path: Repo path, forwarded to any tool-agent fix pass.
        tool_agents: Optional tool-agent map for kinds with a dedicated fix
            pass (e.g. Build Specialist, Documentation); ``None`` skips them.
        task_id: Task id, used for logging only.
        detail_callback: Optional progress callback.

    Returns:
        A ``ProblemSolvingResult`` with unresolved issues surfaced for the
        caller to escalate into fix microtasks.
    """
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
# Phase-specific fix functions (backend-only tool-agent map; the four
# functions themselves are the generic ``shared.phases.problem_solving``
# implementation, bound here with backend's profile/models/prompt).
# ---------------------------------------------------------------------------

# "qa" and "security" are intentionally absent: TestingQAToolAgent and
# SecurityToolAgent are review-only (they no longer implement problem_solve) —
# fixing their findings is the coding agent's job, done entirely by the
# generic per-issue fix loop. Only Build Specialist (whose "fix" is
# mechanically re-running the build) and Documentation (which fixes the same
# prose/docs artifacts it authors itself) still get a second,
# tool-agent-driven fix pass.
_PHASE_FIX_TOOL_AGENT: Dict[str, ToolAgentKind] = {
    "code_review": ToolAgentKind.BUILD_SPECIALIST,
    "documentation": ToolAgentKind.DOCUMENTATION,
}

_phase_fix_functions = make_phase_fix_functions(
    profile=PROFILE,
    models=_models,
    single_issue_prompt=PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT,
    parse_single=parse_problem_solving_single_issue_template,
    runner_factory=_llm_runner,
    phase_fix_tool_agent=_PHASE_FIX_TOOL_AGENT,
)

run_code_review_fixes = _phase_fix_functions.run_code_review_fixes
run_qa_fixes = _phase_fix_functions.run_qa_fixes
run_security_fixes = _phase_fix_functions.run_security_fixes
run_documentation_fixes = _phase_fix_functions.run_documentation_fixes
