"""
Planning phase: decompose a task into microtasks and assign tool agents.

No code from frontend_team is used. Uses template-based output parsing.

The planning logic is shared across the code-v2 teams (see
``shared/phases/planning.py``); this module wires in the frontend team's models,
prompts, and stack profile. ``_detect_language`` / ``_parse_planning_output``
are re-exported here for callers and tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from strands import Agent

from llm_service import LLMClient
from llm_service.strands_model import (
    LlmRunner,
    resolve_text_mode_strands_model,
)
from shared.dev_models.models import SystemArchitecture, Task
from software_engineering_team.shared.phases.planning import (
    parse_planning_output,
    plan_fixes_impl,
    run_planning_impl,
)

from .. import models as _models
from ..models import Microtask, PlanningResult, ReviewIssue, ToolAgentKind
from ..output_templates import parse_planning_template
from ..prompts import PLANNING_FIXES_FOR_ISSUES_PROMPT, PLANNING_PROMPT
from ._profile import PROFILE, _detect_language

__all__ = [
    "run_planning",
    "plan_fixes_for_unresolved_issues",
    "_detect_language",
    "_parse_planning_output",
]


def _llm_runner() -> LlmRunner:
    """Build the LLM runner from this module's globals so tests can monkeypatch them."""
    return LlmRunner(agent_factory=Agent, resolve_model=resolve_text_mode_strands_model)


def _parse_planning_output(raw: Dict[str, Any], language: str) -> PlanningResult:
    """Convert the parsed LLM response into a PlanningResult (frontend models).

    Preconditions:
        ``raw`` is the parsed planning template; ``language`` is the detected stack.
    Postconditions:
        Returns a frontend ``PlanningResult``; see the shared implementation.
    """
    return parse_planning_output(raw, language, models=_models)


def run_planning(
    *,
    llm: LLMClient,
    task: Task,
    repo_path: Path,
    architecture: Optional[SystemArchitecture] = None,
    existing_code: str = "",
    tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
) -> PlanningResult:
    """
    Execute the Planning phase and return a PlanningResult.

    If tool_agents is provided, each tool agent's plan() is called after LLM planning.

    Preconditions:
        ``repo_path`` is a filesystem path; ``task`` is the assigned task.
    Postconditions:
        Returns a ``PlanningResult`` with at least one microtask.
    """
    return run_planning_impl(
        llm=llm,
        task=task,
        repo_path=repo_path,
        architecture=architecture,
        existing_code=existing_code,
        tool_agents=tool_agents,
        profile=PROFILE,
        planning_prompt=PLANNING_PROMPT,
        parse_planning_template=parse_planning_template,
        models=_models,
        runner=_llm_runner(),
    )


def plan_fixes_for_unresolved_issues(  # pragma: no cover  # integration-only: LLM-driven re-plan for escalated issues
    *,
    llm: LLMClient,
    task: Task,
    unresolved_issues: List[ReviewIssue],
    current_files: Dict[str, str],
    language: str = "typescript",
) -> List[Microtask]:
    """Create microtasks to fix unresolved review issues (escalation from problem-solving)."""
    return plan_fixes_impl(
        llm=llm,
        task=task,
        unresolved_issues=unresolved_issues,
        current_files=current_files,
        language=language,
        planning_fixes_prompt=PLANNING_FIXES_FOR_ISSUES_PROMPT,
        parse_planning_template=parse_planning_template,
        models=_models,
        runner=_llm_runner(),
    )
