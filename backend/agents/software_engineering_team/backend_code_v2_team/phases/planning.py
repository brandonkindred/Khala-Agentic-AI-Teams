"""
Planning phase: decompose a task into microtasks and assign tool agents.

No code from ``backend_agent`` is used.
Uses template-based output (not JSON) so parsing works across model providers.

The planning logic is shared across the code-v2 teams (see
``shared/phases/planning.py``); this module wires in the backend team's models,
prompts, and stack profile. ``_detect_language`` / ``_parse_planning_output``
are re-exported here for callers and tests.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


def _llm_runner() -> LlmRunner:
    """Build the LLM runner from this module's globals so tests can monkeypatch them."""
    return LlmRunner(agent_factory=Agent, resolve_model=resolve_text_mode_strands_model)


__all__ = [
    "run_planning",
    "plan_fixes_for_unresolved_issues",
    "_detect_language",
    "_parse_planning_output",
]


def _parse_planning_output(raw: Dict[str, Any], language: str) -> PlanningResult:
    """Convert the parsed LLM response into a PlanningResult (backend models).

    Preconditions:
        ``raw`` is the parsed planning template; ``language`` is the detected language.
    Postconditions:
        Returns a backend ``PlanningResult``; see the shared implementation.
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

    If tool_agents is provided, each tool agent's plan() is called after LLM planning
    to enrich microtask recommendations (appended to result summary).

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
    language: str = "python",
) -> List[Microtask]:
    """
    Create microtasks to fix unresolved review issues (escalation from problem-solving).

    Called when the problem-solving phase could not resolve issues after
    MAX_ITERATIONS_PER_ISSUE attempts per issue. Returns new microtasks that
    the execution phase can run to implement the fixes.
    """
    microtasks = plan_fixes_impl(
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
    if unresolved_issues:
        logger.info("[%s] Planned %d fix microtasks", task.id, len(microtasks))
    return microtasks
