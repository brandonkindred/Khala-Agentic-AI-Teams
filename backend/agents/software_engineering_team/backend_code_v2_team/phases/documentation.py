"""
Documentation phase: review all documentation and iterate until issues are resolved.

This phase runs after Execution completes and before Deliver.
It performs a comprehensive documentation review and fix cycle.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from software_engineering_team.shared.llm import LLMClient
from software_engineering_team.shared.models import Task

from ..models import (
    DocumentationPhaseResult,
    ExecutionResult,
    Phase,
    PlanningResult,
    ReviewIssue,
    ToolAgentKind,
    ToolAgentPhaseInput,
)

logger = logging.getLogger(__name__)

MAX_DOCUMENTATION_ITERATIONS = 100


def _write_files(repo_path: Path, files: Dict[str, str]) -> None:
    """Write files to disk."""
    for rel_path, content in files.items():
        safe_rel_path = rel_path.lstrip("/")
        full_path = repo_path / safe_rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")


def run_documentation_phase(
    llm: LLMClient,
    task: Task,
    repo_path: Path,
    execution_result: ExecutionResult,
    planning_result: PlanningResult,
    tool_agents: Dict[ToolAgentKind, Any],
    max_iterations: int = MAX_DOCUMENTATION_ITERATIONS,
) -> DocumentationPhaseResult:
    """
    Review all documentation and iterate until no issues remain.
    
    This phase:
    1. Calls the documentation tool agent's review() to find issues
    2. If issues found, calls problem_solve() to fix them
    3. Repeats until no issues or max_iterations reached
    
    Args:
        llm: LLM client for the documentation agent
        task: The task being executed
        repo_path: Path to the repository
        execution_result: Result from the execution phase
        planning_result: Result from the planning phase
        tool_agents: Dictionary of tool agents
        max_iterations: Maximum review/fix cycles
        
    Returns:
        DocumentationPhaseResult with updated files and summary
    """
    task_id = task.id or "unknown"
    logger.info("[%s] Documentation phase starting", task_id)
    
    doc_agent = tool_agents.get(ToolAgentKind.DOCUMENTATION)
    if not doc_agent:
        logger.warning("[%s] No documentation agent available, skipping documentation phase", task_id)
        return DocumentationPhaseResult(
            summary="Documentation phase skipped (no documentation agent)."
        )
    
    if not hasattr(doc_agent, "review") or not hasattr(doc_agent, "problem_solve"):
        logger.warning("[%s] Documentation agent missing review/problem_solve methods", task_id)
        return DocumentationPhaseResult(
            summary="Documentation phase skipped (agent missing required methods)."
        )
    
    current_files = dict(execution_result.files)
    total_issues_fixed = 0
    iteration = 0
    
    while iteration < max_iterations:
        iteration += 1
        logger.info("[%s] Documentation review iteration %d/%d", task_id, iteration, max_iterations)
        
        phase_input = ToolAgentPhaseInput(
            phase=Phase.DOCUMENTATION,
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
            task_id, len(issues),
        )
        
        problem_solve_input = ToolAgentPhaseInput(
            phase=Phase.DOCUMENTATION,
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
            current_files.update(fix_result.files)
            _write_files(repo_path, fix_result.files)
            total_issues_fixed += len(issues)
            logger.info("[%s] Documentation fixed %d issue(s), updated %d file(s)", 
                        task_id, len(issues), len(fix_result.files))
        else:
            logger.warning(
                "[%s] Documentation problem_solve returned no files. Recovery summary: "
                "1) Review found issues, 2) Problem-solve completed without file changes. Stopping.",
                task_id,
            )
            break
    
    summary = f"Documentation phase completed: {iteration} iteration(s), {total_issues_fixed} issue(s) fixed."
    logger.info("[%s] %s", task_id, summary)
    
    return DocumentationPhaseResult(
        files=current_files,
        iterations=iteration,
        issues_fixed=total_issues_fixed,
        summary=summary,
    )
