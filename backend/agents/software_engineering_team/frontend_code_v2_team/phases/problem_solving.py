"""
Problem-solving phase: root-cause analysis and fix loop.

Processes one issue at a time. Unresolved issues can be turned into fix microtasks.
No code from frontend_team is used.

The formatting, batch-fix, single-issue loop, and orchestration are shared across
the code-v2 teams (see ``shared/phases/problem_solving.py``); this module wires in
the frontend team's models, prompts, and stack profile.
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
from software_engineering_team.shared.phases.problem_solving import (
    MAX_ITERATIONS_PER_ISSUE,
    _format_all_code,
    _format_issues_for_batch,
    _relevant_code_for_issue,
    run_batch_coding_fixes_impl,
    run_problem_solving_for_microtask_impl,
    run_problem_solving_impl,
)

from .. import models as _models
from ..models import Microtask, ProblemSolvingResult, ReviewIssue, ReviewResult, ToolAgentKind
from ..output_templates import (
    parse_batch_fix_template,
    parse_problem_solving_single_issue_template,
)
from ..prompts import BATCH_FIX_PROMPT, PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT
from ._profile import PROFILE

__all__ = [
    "MAX_ITERATIONS_PER_ISSUE",
    "run_batch_coding_fixes",
    "run_problem_solving",
    "run_problem_solving_for_microtask",
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
