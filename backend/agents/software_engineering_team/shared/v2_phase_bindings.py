"""
Shared phase-bindings factory for the code-v2 teams.

``backend_code_v2_team`` and ``frontend_code_v2_team`` each used to carry a
twin ``phases/documentation.py``, ``phases/planning.py``, and
``output_templates.py`` that bound the same shared implementations
(``shared/phases/documentation.py``, ``shared/phases/planning.py``,
``shared/v2_output_templates.py``) to that team's ``models`` module, prompts,
and :class:`~software_engineering_team.shared.v2_team_config.V2TeamConfig`.
``build_phase_bindings`` performs that binding once, here, driven entirely by
the team's ``V2TeamConfig`` (plus its ``models`` module and planning
prompts) — each team's ``phases/_profile.py`` calls it and re-exports the
resulting :class:`PhaseBindings` attributes under their existing public
names, so no team-local wrapper module remains.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from strands import Agent

from llm_service import LLMClient
from llm_service.strands_model import LlmRunner, resolve_text_mode_strands_model
from shared.dev_models.models import SystemArchitecture, Task
from software_engineering_team.shared.phases.documentation import (
    make_run_documentation_phase,
)
from software_engineering_team.shared.phases.planning import (
    parse_planning_output as _parse_planning_output_impl,
)
from software_engineering_team.shared.phases.planning import (
    plan_fixes_impl,
    run_planning_impl,
)
from software_engineering_team.shared.stack_profile import PhaseModels
from software_engineering_team.shared.v2_output_templates import make_output_templates
from software_engineering_team.shared.v2_team_config import V2TeamConfig


def _llm_runner() -> LlmRunner:
    """Build the LLM runner from this module's globals so tests can monkeypatch them."""
    return LlmRunner(agent_factory=Agent, resolve_model=resolve_text_mode_strands_model)


@dataclass(frozen=True)
class PhaseBindings:
    """A team's documentation/planning/output-template entry points, bound.

    Invariants:
        Every attribute is a plain callable matching the code-v2 team public
        signature that used to live in that team's ``phases/documentation.py``,
        ``phases/planning.py``, or ``output_templates.py``.
    """

    run_documentation_phase: Callable[..., Any]
    run_planning: Callable[..., Any]
    plan_fixes_for_unresolved_issues: Callable[..., List[Any]]
    parse_planning_output: Callable[[Dict[str, Any], str], Any]
    parse_files_and_summary_template: Callable[[str], Dict[str, Any]]
    parse_planning_template: Callable[[str], Dict[str, Any]]
    parse_review_template: Callable[[str], Dict[str, Any]]
    parse_problem_solving_template: Callable[[str], Dict[str, Any]]
    parse_problem_solving_single_issue_template: Callable[[str], Dict[str, Any]]
    parse_batch_fix_template: Callable[[str], Dict[str, Any]]
    parse_documentation_self_review_template: Callable[[str], Dict[str, Any]]


def build_phase_bindings(
    *,
    models: PhaseModels,
    config: V2TeamConfig,
    planning_prompt: str,
    planning_fixes_prompt: str,
) -> PhaseBindings:
    """Bind a team's documentation/planning/output-template entry points.

    Preconditions:
        ``models`` satisfies ``PhaseModels``; ``config`` is the team's
        ``V2TeamConfig`` (its ``stack_profile`` supplies the language
        default, and its ``output_template_*`` fields supply the
        output-template knobs); ``planning_prompt`` /
        ``planning_fixes_prompt`` are the team's prompt templates.
    Postconditions:
        Returns a ``PhaseBindings`` whose entry points delegate entirely to
        the shared ``run_documentation_phase_impl`` / ``run_planning_impl`` /
        ``plan_fixes_impl`` / ``make_output_templates`` implementations, with
        this team's ``models``/``config``/prompts closed over.
    """
    templates = make_output_templates(
        path_prefixes=config.output_template_path_prefixes,
        allowed_languages=config.output_template_allowed_languages,
        default_language=config.stack_profile.default_language,
        coerce_unknown=config.output_template_coerce_unknown,
    )
    run_documentation_phase = make_run_documentation_phase(models=models)

    def parse_planning_output(raw: Dict[str, Any], language: str) -> Any:
        return _parse_planning_output_impl(raw, language, models=models)

    def run_planning(
        *,
        llm: LLMClient,
        task: Task,
        repo_path: Any,
        architecture: Optional[SystemArchitecture] = None,
        existing_code: str = "",
        tool_agents: Optional[Dict[Any, Any]] = None,
    ) -> Any:
        return run_planning_impl(
            llm=llm,
            task=task,
            repo_path=repo_path,
            architecture=architecture,
            existing_code=existing_code,
            tool_agents=tool_agents,
            profile=config.stack_profile,
            planning_prompt=planning_prompt,
            parse_planning_template=templates.parse_planning_template,
            models=models,
            runner=_llm_runner(),
        )

    def plan_fixes_for_unresolved_issues(  # pragma: no cover  # integration-only: LLM-driven re-plan for escalated issues
        *,
        llm: LLMClient,
        task: Task,
        unresolved_issues: List[Any],
        current_files: Dict[str, str],
        language: Optional[str] = None,
    ) -> List[Any]:
        return plan_fixes_impl(
            llm=llm,
            task=task,
            unresolved_issues=unresolved_issues,
            current_files=current_files,
            language=language or config.stack_profile.default_language,
            planning_fixes_prompt=planning_fixes_prompt,
            parse_planning_template=templates.parse_planning_template,
            models=models,
            runner=_llm_runner(),
        )

    return PhaseBindings(
        run_documentation_phase=run_documentation_phase,
        run_planning=run_planning,
        plan_fixes_for_unresolved_issues=plan_fixes_for_unresolved_issues,
        parse_planning_output=parse_planning_output,
        parse_files_and_summary_template=templates.parse_files_and_summary_template,
        parse_planning_template=templates.parse_planning_template,
        parse_review_template=templates.parse_review_template,
        parse_problem_solving_template=templates.parse_problem_solving_template,
        parse_problem_solving_single_issue_template=(
            templates.parse_problem_solving_single_issue_template
        ),
        parse_batch_fix_template=templates.parse_batch_fix_template,
        parse_documentation_self_review_template=(
            templates.parse_documentation_self_review_template
        ),
    )
