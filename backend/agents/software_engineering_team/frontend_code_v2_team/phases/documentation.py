"""
Documentation phase: review all documentation and iterate until issues are resolved.

This phase runs after Execution completes and before Deliver.
It performs a comprehensive documentation review and fix cycle.

The review/fix loop is shared across the code-v2 teams; see
``shared/phases/documentation.py``. This module wires in the frontend team's
models.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from llm_service import LLMClient
from software_engineering_team.shared.models import Task
from software_engineering_team.shared.phases.documentation import (
    _write_files,
    run_documentation_phase_impl,
)

from .. import models as _models
from ..models import DocumentationPhaseResult, ExecutionResult, PlanningResult, ToolAgentKind

__all__ = ["MAX_DOCUMENTATION_ITERATIONS", "run_documentation_phase", "_write_files"]

MAX_DOCUMENTATION_ITERATIONS = 100


def run_documentation_phase(
    llm: LLMClient,
    task: Task,
    repo_path: Path,
    execution_result: ExecutionResult,
    planning_result: PlanningResult,
    tool_agents: Dict[ToolAgentKind, Any],
    max_iterations: int = MAX_DOCUMENTATION_ITERATIONS,
) -> DocumentationPhaseResult:
    """Review all documentation and iterate until no issues remain.

    Preconditions:
        ``max_iterations`` >= 1; ``tool_agents`` may or may not contain a
        documentation agent.
    Postconditions:
        Returns a ``DocumentationPhaseResult``; delegates the loop to
        ``run_documentation_phase_impl`` with the frontend team's models.
    """
    return run_documentation_phase_impl(
        llm,
        task,
        repo_path,
        execution_result,
        planning_result,
        tool_agents,
        max_iterations,
        models=_models,
    )
