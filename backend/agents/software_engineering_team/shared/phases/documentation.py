"""
Shared Documentation-phase implementation for the code-v2 teams.

The backend and frontend documentation phases were byte-for-byte identical; the
only team-local dependencies are the ``DocumentationPhaseResult`` /
``ToolAgentPhaseInput`` models and the ``Phase`` / ``ToolAgentKind`` enums,
which are injected via the team's ``models`` module.

``make_run_documentation_phase`` binds those models into the team-facing
``run_documentation_phase`` entry point so each team module stays a thin
re-export / patch surface.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict

from llm_service import LLMClient
from shared.dev_models.models import Task
from software_engineering_team.shared.repo_writer import UnsafeRepoPathError
from software_engineering_team.shared.repo_writer import write_repo_text_files as _write_files
from software_engineering_team.shared.stack_profile import PhaseModels

logger = logging.getLogger(__name__)

MAX_DOCUMENTATION_ITERATIONS = 100


def run_documentation_phase_impl(
    llm: LLMClient,
    task: Task,
    repo_path: Path,
    execution_result: Any,
    planning_result: Any,
    tool_agents: Dict[Any, Any],
    max_iterations: int,
    *,
    models: PhaseModels,
) -> Any:
    """Review all documentation and iterate until no issues remain.

    This phase:
    1. Calls the documentation tool agent's review() to find issues
    2. If issues found, calls problem_solve() to fix them
    3. Repeats until no issues or max_iterations reached

    Preconditions:
        ``models`` exposes ``DocumentationPhaseResult``, ``Phase``,
        ``ToolAgentKind``, and ``ToolAgentPhaseInput``. ``max_iterations`` >= 1.
        ``execution_result``/``planning_result`` are the team's phase results.
    Postconditions:
        Returns a ``DocumentationPhaseResult``. Never raises for a missing or
        malformed documentation agent — it returns a skip result instead.
    """
    doc_result_cls = models.DocumentationPhaseResult
    phase_enum = models.Phase
    tool_agent_kind_enum = models.ToolAgentKind
    phase_input_cls = models.ToolAgentPhaseInput

    task_id = task.id or "unknown"
    logger.info("[%s] Documentation phase starting", task_id)

    doc_agent = tool_agents.get(tool_agent_kind_enum.DOCUMENTATION)
    if not doc_agent:
        logger.warning(
            "[%s] No documentation agent available, skipping documentation phase", task_id
        )
        return doc_result_cls(summary="Documentation phase skipped (no documentation agent).")

    if not hasattr(doc_agent, "review") or not hasattr(doc_agent, "problem_solve"):
        logger.warning("[%s] Documentation agent missing review/problem_solve methods", task_id)
        return doc_result_cls(
            summary="Documentation phase skipped (agent missing required methods)."
        )

    current_files = dict(execution_result.files)
    total_issues_fixed = 0
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        logger.info("[%s] Documentation review iteration %d/%d", task_id, iteration, max_iterations)

        phase_input = phase_input_cls(
            phase=phase_enum.DOCUMENTATION,
            repo_path=str(repo_path),
            current_files=current_files,
            review_issues=[],
            task_title=task.title or "",
            task_description=task.description or "",
            task_id=task_id,
            language=planning_result.language,
        )

        try:
            review_result = doc_agent.review(phase_input)
        except Exception as e:
            logger.error("[%s] Documentation review failed: %s", task_id, e)
            break

        issues = review_result.issues or []
        if not issues:
            logger.info("[%s] Documentation review passed - no issues found", task_id)
            break

        logger.info(
            "[%s] Documentation review found %d issue(s). Next step -> Applying fixes",
            task_id,
            len(issues),
        )

        problem_solve_input = phase_input_cls(
            phase=phase_enum.DOCUMENTATION,
            repo_path=str(repo_path),
            current_files=current_files,
            review_issues=issues,
            task_title=task.title or "",
            task_description=task.description or "",
            task_id=task_id,
            language=planning_result.language,
        )

        try:
            fix_result = doc_agent.problem_solve(problem_solve_input)
        except Exception as e:
            logger.error("[%s] Documentation problem_solve failed: %s", task_id, e)
            break

        if fix_result.files:
            try:
                _write_files(repo_path, fix_result.files)
            except UnsafeRepoPathError as exc:
                logger.warning(
                    "[%s] Documentation fix produced an unsafe file path; stopping: %s",
                    task_id,
                    exc,
                )
                break
            current_files.update(fix_result.files)
            total_issues_fixed += len(issues)
            logger.info(
                "[%s] Documentation fixed %d issue(s), updated %d file(s)",
                task_id,
                len(issues),
                len(fix_result.files),
            )
        else:
            logger.warning(
                "[%s] Documentation problem_solve returned no files. Recovery summary: "
                "1) Review found issues, 2) Problem-solve completed without file changes. Stopping.",
                task_id,
            )
            break

    summary = f"Documentation phase completed: {iteration} iteration(s), {total_issues_fixed} issue(s) fixed."
    logger.info("[%s] %s", task_id, summary)

    return doc_result_cls(
        files=current_files,
        iterations=iteration,
        issues_fixed=total_issues_fixed,
        summary=summary,
    )


def make_run_documentation_phase(*, models: PhaseModels) -> Callable[..., Any]:
    """Bind a team-module ``run_documentation_phase`` that injects ``models``.

    Preconditions:
        ``models`` satisfies ``PhaseModels`` (exposes ``DocumentationPhaseResult``,
        ``Phase``, ``ToolAgentKind``, and ``ToolAgentPhaseInput``).
    Postconditions:
        Returns a ``run_documentation_phase`` matching the code-v2 team public
        signature. Each call delegates entirely to ``run_documentation_phase_impl``
        with the bound ``models``.
    """

    def run_documentation_phase(
        llm: LLMClient,
        task: Task,
        repo_path: Path,
        execution_result: Any,
        planning_result: Any,
        tool_agents: Dict[Any, Any],
        max_iterations: int = MAX_DOCUMENTATION_ITERATIONS,
    ) -> Any:
        """Review all documentation and iterate until no issues remain.

        Preconditions:
            ``max_iterations`` >= 1; ``tool_agents`` may or may not contain a
            documentation agent.
        Postconditions:
            Returns a ``DocumentationPhaseResult``; delegates the loop to
            ``run_documentation_phase_impl`` with the bound team's models.
        """
        return run_documentation_phase_impl(
            llm,
            task,
            repo_path,
            execution_result,
            planning_result,
            tool_agents,
            max_iterations,
            models=models,
        )

    return run_documentation_phase
