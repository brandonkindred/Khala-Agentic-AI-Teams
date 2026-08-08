"""
Shared Planning-phase implementation for the code-v2 teams.

The planning phase decomposes a task into microtasks and assigns tool agents.
The backend and frontend versions were near-identical; the only differences are
the language-detection callable, the language label in the prompt context, and
the progress-log token — all supplied by the team's
:class:`~software_engineering_team.shared.stack_profile.StackProfile`. Team-local
models (``Microtask``, ``MicrotaskStatus``, ``ToolAgentKind``, ``PlanningResult``,
``Phase``, ``ToolAgentPhaseInput``) are injected via the team's ``models`` module.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from llm_service import LLMClient
from llm_service.strands_model import LlmRunner
from shared.dev_models.models import SystemArchitecture, Task
from software_engineering_team.shared.stack_profile import PhaseModels, StackProfile

logger = logging.getLogger(__name__)


def build_context(
    task: Task,
    architecture: Optional[SystemArchitecture],
    existing_code: str,
    language: str,
    *,
    planning_prompt: str,
    language_label: str,
) -> str:
    """Build the full prompt context for the planning LLM call.

    Preconditions:
        ``planning_prompt`` is the team's PLANNING_PROMPT; ``language_label`` is
        the profile's language label (``"Language"`` / ``"Language/stack"``).
    Postconditions:
        Returns the prompt followed by the task/context block. Pure; no side
        effects. Architecture/code excerpts are truncated as before.
    """
    parts: List[str] = [
        planning_prompt,
        "",
        "---",
        "",
        f"**Task title:** {task.title or task.id}",
        f"**Task description:** {task.description}",
        f"**Requirements:** {task.requirements or 'N/A'}",
        f"**Acceptance criteria:** {', '.join(task.acceptance_criteria) if task.acceptance_criteria else 'N/A'}",
        f"**{language_label}:** {language}",
    ]
    if architecture:
        parts.extend(["", "**Architecture overview:**", architecture.overview])
    if existing_code and existing_code != "# No code files found":
        parts.extend(["", "**Existing codebase (excerpt):**", existing_code])
    return "\n".join(parts)


def parse_planning_output(raw: Dict[str, Any], language: str, *, models: PhaseModels) -> Any:
    """Convert the parsed LLM response into a PlanningResult.

    Preconditions:
        ``models`` exposes ``Microtask``, ``MicrotaskStatus``, ``ToolAgentKind``,
        and ``PlanningResult``. ``raw`` is the parsed template dict.
    Postconditions:
        Returns a ``PlanningResult``; microtasks lacking an ``id`` are skipped
        and an unknown ``tool_agent`` falls back to ``ToolAgentKind.GENERAL``.
    """
    microtask_cls = models.Microtask
    microtask_status_cls = models.MicrotaskStatus
    tool_agent_kind_enum = models.ToolAgentKind
    planning_result_cls = models.PlanningResult

    microtasks: List[Any] = []
    for mt in raw.get("microtasks") or []:
        if not isinstance(mt, dict) or not mt.get("id"):
            continue
        try:
            kind = tool_agent_kind_enum(mt.get("tool_agent", "general"))
        except ValueError:
            kind = tool_agent_kind_enum.GENERAL
        microtasks.append(
            microtask_cls(
                id=mt["id"],
                title=mt.get("title", ""),
                description=mt.get("description", ""),
                tool_agent=kind,
                status=microtask_status_cls.PENDING,
                depends_on=mt.get("depends_on") or [],
            )
        )
    return planning_result_cls(
        microtasks=microtasks,
        language=raw.get("language") or language,
        summary=raw.get("summary", ""),
    )


def run_planning_impl(
    *,
    llm: LLMClient,
    task: Task,
    repo_path: Path,
    architecture: Optional[SystemArchitecture],
    existing_code: str,
    tool_agents: Optional[Dict[Any, Any]],
    profile: StackProfile,
    planning_prompt: str,
    parse_planning_template: Callable[[str], Dict[str, Any]],
    models: PhaseModels,
    runner: LlmRunner,
) -> Any:
    """Execute the Planning phase and return a PlanningResult.

    If tool_agents is provided, each tool agent's plan() is called after LLM planning
    to enrich microtask recommendations (appended to result summary).

    Preconditions:
        ``profile`` is the team's stack profile; ``parse_planning_template`` is
        the team's template parser; ``models`` exposes the team-local model set.
    Postconditions:
        Returns a ``PlanningResult`` with at least one microtask (a single
        fallback microtask is synthesized when the LLM produced none).
    """
    tool_agent_kind_enum = models.ToolAgentKind
    microtask_cls = models.Microtask
    phase_enum = models.Phase
    phase_input_cls = models.ToolAgentPhaseInput

    language = profile.detect_language(repo_path, task)
    prompt = build_context(
        task,
        architecture,
        existing_code,
        language,
        planning_prompt=planning_prompt,
        language_label=profile.planning_language_label,
    )

    logger.info(
        "[%s] Planning phase: generating microtasks (%s=%s)",
        task.id,
        profile.planning_progress_label,
        language,
    )
    raw = runner.run(llm, prompt)
    raw_parsed = parse_planning_template(raw)
    result = parse_planning_output(raw_parsed, language, models=models)
    logger.info(
        "[%s] Planning phase: produced %d microtasks — %s",
        task.id,
        len(result.microtasks),
        result.summary[:120] if result.summary else "",
    )

    if tool_agents:
        phase_inp = phase_input_cls(
            phase=phase_enum.PLANNING,
            repo_path=str(repo_path),
            language=language,
            task_title=task.title or "",
            task_description=task.description or "",
        )
        for kind, agent in tool_agents.items():
            if not hasattr(agent, "plan"):
                continue
            try:
                out = agent.plan(phase_inp)
                if out.recommendations:
                    result.summary = (
                        (result.summary or "").rstrip() + "\n" + " ".join(out.recommendations)
                    )
            except Exception as e:
                logger.warning("[%s] Tool agent %s plan() failed: %s", task.id, kind.value, e)

    if not result.microtasks:
        result.microtasks = [
            microtask_cls(
                id="mt-implement-task",
                title=task.title or "Implement task",
                description=task.description or "Implement the full task as described.",
                tool_agent=tool_agent_kind_enum.GENERAL,
            )
        ]
        result.summary = result.summary or "Single-microtask fallback."
    return result


def plan_fixes_impl(  # pragma: no cover  # integration-only: LLM-driven re-plan for escalated issues
    *,
    llm: LLMClient,
    task: Task,
    unresolved_issues: List[Any],
    current_files: Dict[str, str],
    language: str,
    planning_fixes_prompt: str,
    parse_planning_template: Callable[[str], Dict[str, Any]],
    models: PhaseModels,
    runner: LlmRunner,
) -> List[Any]:
    """Create microtasks to fix unresolved review issues (escalation from problem-solving).

    Called when the problem-solving phase could not resolve issues after
    MAX_ITERATIONS_PER_ISSUE attempts per issue. Returns new microtasks that
    the execution phase can run to implement the fixes.

    Preconditions:
        ``planning_fixes_prompt`` has ``{issues_text}``/``{existing_code}``/
        ``{language}`` slots; ``models`` exposes the team-local model set.
    Postconditions:
        Returns a list of microtasks (empty when ``unresolved_issues`` is empty).
    """
    if not unresolved_issues:
        return []
    issues_text = "\n".join(
        f"- [{i.severity}] {i.description} (file: {i.file_path or 'N/A'}) → {i.recommendation}"
        for i in unresolved_issues
    )
    code_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in list(current_files.items())[:15])
    prompt = planning_fixes_prompt.format(
        issues_text=issues_text,
        existing_code=code_text or "(no code)",
        language=language,
    )
    logger.info(
        "[%s] Planning fix microtasks for %d unresolved issues", task.id, len(unresolved_issues)
    )
    raw = runner.run(llm, prompt)
    raw_parsed = parse_planning_template(raw)
    result = parse_planning_output(raw_parsed, language, models=models)
    return result.microtasks
