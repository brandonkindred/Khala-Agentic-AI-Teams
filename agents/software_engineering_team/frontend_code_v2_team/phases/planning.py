"""
Planning phase: decompose a task into microtasks and assign tool agents.

No code from frontend_team is used. Uses template-based output parsing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from shared.llm import LLMClient
from shared.models import SystemArchitecture, Task

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
from ..prompts import PLANNING_PROMPT, PLANNING_FIXES_FOR_ISSUES_PROMPT

logger = logging.getLogger(__name__)


def _detect_language(repo_path: Path, task: Task) -> str:
    """Infer frontend stack from repo or task."""
    if repo_path.is_dir():
        if (repo_path / "angular.json").exists():
            return "angular"
        pkg = repo_path / "package.json"
        if pkg.exists():
            try:
                content = pkg.read_text(encoding="utf-8")
                if "@angular/core" in content or "@angular/common" in content:
                    return "angular"
                if '"react"' in content or "'react'" in content:
                    return "react"
            except Exception:
                pass
        if any(repo_path.rglob("tsconfig.json")):
            return "typescript"
        if any(repo_path.rglob("*.tsx")) or any(repo_path.rglob("*.ts")):
            return "typescript"
    desc = (task.description or "").lower() + " " + (task.requirements or "").lower()
    if "angular" in desc:
        return "angular"
    if "react" in desc:
        return "react"
    if "typescript" in desc or "ts " in desc:
        return "typescript"
    return "typescript"


def _build_context(
    task: Task,
    architecture: Optional[SystemArchitecture],
    existing_code: str,
    language: str,
) -> str:
    parts: List[str] = [
        PLANNING_PROMPT,
        "",
        "---",
        "",
        f"**Task title:** {task.title or task.id}",
        f"**Task description:** {task.description}",
        f"**Requirements:** {task.requirements or 'N/A'}",
        f"**Acceptance criteria:** {', '.join(task.acceptance_criteria) if task.acceptance_criteria else 'N/A'}",
        f"**Language/stack:** {language}",
    ]
    if architecture:
        parts.extend(["", "**Architecture overview:**", architecture.overview[:3000]])
    if existing_code and existing_code != "# No code files found":
        parts.extend(["", "**Existing codebase (excerpt):**", existing_code[:6000]])
    return "\n".join(parts)


def _parse_planning_output(raw: Dict[str, Any], language: str) -> PlanningResult:
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

    If tool_agents is provided, each tool agent's plan() is called after LLM planning.
    """
    language = _detect_language(repo_path, task)
    prompt = _build_context(task, architecture, existing_code, language)

    logger.info("[%s] Planning phase: generating microtasks (stack=%s)", task.id, language)
    raw = llm.complete_text(prompt)
    raw_parsed = parse_planning_template(raw)
    result = _parse_planning_output(raw_parsed, language)
    logger.info(
        "[%s] Planning phase: produced %d microtasks — %s",
        task.id, len(result.microtasks), result.summary[:120] if result.summary else "",
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
                    result.summary = (result.summary or "").rstrip() + "\n" + " ".join(out.recommendations)
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
    language: str = "typescript",
) -> List[Microtask]:
    """Create microtasks to fix unresolved review issues (escalation from problem-solving)."""
    if not unresolved_issues:
        return []
    issues_text = "\n".join(
        f"- [{i.severity}] {i.description} (file: {i.file_path or 'N/A'}) → {i.recommendation}"
        for i in unresolved_issues
    )
    code_text = "\n\n".join(
        f"--- {p} ---\n{c}" for p, c in list(current_files.items())[:15]
    )[:8000]
    prompt = PLANNING_FIXES_FOR_ISSUES_PROMPT.format(
        issues_text=issues_text,
        existing_code=code_text or "(no code)",
        language=language,
    )
    logger.info("[%s] Planning fix microtasks for %d unresolved issues", task.id, len(unresolved_issues))
    raw = llm.complete_text(prompt)
    raw_parsed = parse_planning_template(raw)
    result = _parse_planning_output(raw_parsed, language)
    return result.microtasks
