"""
Problem-solving phase: root-cause analysis and fix loop.

Processes one issue at a time. Unresolved issues can be turned into fix microtasks.
No code from frontend_team is used.

The formatting, batch-fix, single-issue loop, orchestration, and
phase-specific fix functions are all shared across both stacks (see
``shared/phases/problem_solving.py``); this module wires in the frontend
stack's models, prompts, stack profile, and its ``code_review``/
``documentation`` tool-agent map, which interlock with the frontend
review-gate phase functions.
"""

from __future__ import annotations

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
from software_engineering_team.codegen_team.stacks.frontend.prompts import (
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


def _llm_runner() -> LlmRunner:
    """Build the LLM runner from this module's globals so tests can monkeypatch them."""
    return LlmRunner(agent_factory=Agent, resolve_model=resolve_text_mode_strands_model)


def run_batch_coding_fixes(
    *,
    llm: LLMClient,
    microtask: Microtask,
    issues: List[ReviewIssue],
    current_files: Dict[str, str],
    language: str = "typescript",
    task_id: str = "",
    phase_name: str = "review",
    detail_callback: Optional[Callable[[str], None]] = None,
) -> ProblemSolvingResult:
    """Fix ALL issues from a review phase in a single batch (frontend models)."""
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
    language: str = "typescript",
    repo_path: str = "",
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
) -> ProblemSolvingResult:
    """Analyse review issues and produce fixes, one issue at a time (frontend models)."""
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
    language: str = "typescript",
    repo_path: str = "",
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    task_id: str = "",
    detail_callback: Optional[Callable[[str], None]] = None,
) -> ProblemSolvingResult:
    """Fix issues for a single microtask, one issue at a time (frontend models)."""
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
# Phase-specific fix functions (frontend-only tool-agent map; the four
# functions themselves are the generic ``shared.phases.problem_solving``
# implementation, bound here with frontend's profile/models/prompt).
# ---------------------------------------------------------------------------

# "qa" and "security" are intentionally absent: TestingQAToolAgent and
# SecurityToolAgent are review-only (they report findings; fixing them is the
# generic per-issue coding-agent loop's job). Only Build Specialist (whose
# "fix" is mechanically re-running the build) and Documentation (which fixes
# the same prose/docs artifacts it authors itself) get a second,
# tool-agent-driven fix pass — matching the backend stack's mapping.
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
