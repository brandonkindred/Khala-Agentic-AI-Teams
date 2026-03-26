"""
Planning phase: decompose a task into microtasks and assign tool agents.

No code from ``backend_agent`` is used.
Uses template-based output (not JSON) so parsing works across model providers.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from llm_service import LLMClient
from software_engineering_team.shared.models import SystemArchitecture, Task

from ..models import (
    Microtask,
    MicrotaskStatus,
    Phase,
    PlanningResult,
    ReviewIssue,
    ToolAgentKind,
    ToolAgentPhaseInput,
)
from ..output_templates import parse_planning_template
from ..prompts import PLANNING_FIXES_FOR_ISSUES_PROMPT, PLANNING_PROMPT

logger = logging.getLogger(__name__)


def _detect_language(repo_path: Path, task: Task) -> str:
    """Infer whether the project is Python or Java from the repo."""
    if repo_path.is_dir():
        if any(repo_path.rglob("pom.xml")) or any(repo_path.rglob("build.gradle")):
            return "java"
        if any(repo_path.rglob("requirements.txt")) or any(repo_path.rglob("pyproject.toml")):
            return "python"
        if any(repo_path.rglob("*.java")):
            return "java"
    desc = (task.description or "").lower() + " " + (task.requirements or "").lower()
    if "spring" in desc or "java" in desc or "maven" in desc or "gradle" in desc:
        return "java"
    return "python"


def _build_context(
    task: Task,
    architecture: Optional[SystemArchitecture],
    existing_code: str,
    language: str,
) -> str:
    """Build the full prompt context for the planning LLM call."""
    parts: List[str] = [
        PLANNING_PROMPT,
        "",
        "---",
        "",
        f"**Task title:** {task.title or task.id}",
        f"**Task description:** {task.description}",
        f"**Requirements:** {task.requirements or 'N/A'}",
        f"**Acceptance criteria:** {', '.join(task.acceptance_criteria) if task.acceptance_criteria else 'N/A'}",
        f"**Language:** {language}",
    ]
    if architecture:
        parts.extend(["", "**Architecture overview:**", architecture.overview[:3000]])
    if existing_code and existing_code != "# No code files found":
        parts.extend(["", "**Existing codebase (excerpt):**", existing_code[:6000]])
    return "\n".join(parts)


def _parse_planning_output(raw: Dict[str, Any], language: str) -> PlanningResult:
    """Convert the LLM JSON response into a PlanningResult."""
    microtasks: List[Microtask] = []
    for mt in raw.get("microtasks") or []:
        if not isinstance(mt, dict) or not mt.get("id"):
            continue
        try:
            kind = ToolAgentKind(mt.get("tool_agent", "general"))
        except ValueError:
            kind = ToolAgentKind.GENERAL
        microtasks.append(
            Microtask(
                id=mt["id"],
                title=mt.get("title", ""),
                description=mt.get("description", ""),
                tool_agent=kind,
                status=MicrotaskStatus.PENDING,
                depends_on=mt.get("depends_on") or [],
            )
        )
    return PlanningResult(
        microtasks=microtasks,
        language=raw.get("language") or language,
        summary=raw.get("summary", ""),
    )


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
    """
    language = _detect_language(repo_path, task)
    prompt = _build_context(task, architecture, existing_code, language)

    logger.info("[%s] Planning phase: generating microtasks (language=%s)", task.id, language)
    raw = llm.complete_text(prompt, think=True)
    raw_parsed = parse_planning_template(raw)
    result = _parse_planning_output(raw_parsed, language)
    logger.info(
        "[%s] Planning phase: produced %d microtasks — %s",
        task.id,
        len(result.microtasks),
        result.summary[:120] if result.summary else "",
    )

    if tool_agents:
        phase_inp = ToolAgentPhaseInput(
            phase=Phase.PLANNING,
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
            Microtask(
                id="mt-implement-task",
                title=task.title or "Implement task",
                description=task.description or "Implement the full task as described.",
                tool_agent=ToolAgentKind.GENERAL,
            )
        ]
        result.summary = result.summary or "Single-microtask fallback."
    return result


def plan_fixes_for_unresolved_issues(
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
    if not unresolved_issues:
        return []
    task_id = task.id
    issues_text = "\n".join(
        f"- [{i.severity}] {i.description} (file: {i.file_path or 'N/A'}) → {i.recommendation}"
        for i in unresolved_issues
    )
    code_text = "\n\n".join(f"--- {p} ---\n{c}" for p, c in list(current_files.items())[:15])[:8000]
    prompt = PLANNING_FIXES_FOR_ISSUES_PROMPT.format(
        issues_text=issues_text,
        existing_code=code_text or "(no code)",
        language=language,
    )
    logger.info(
        "[%s] Planning fix microtasks for %d unresolved issues", task_id, len(unresolved_issues)
    )
    raw = llm.complete_text(prompt, think=True)
    raw_parsed = parse_planning_template(raw)
    result = _parse_planning_output(raw_parsed, language)
    logger.info("[%s] Planned %d fix microtasks", task_id, len(result.microtasks))
    return result.microtasks
