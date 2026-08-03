"""Quality gate tool functions for the Software Engineering pipeline.

Each function is a self-contained tool that can be called by any agent or
orchestrator.  They instantiate their own agent/LLM when needed (no shared
mutable state), making them safe for concurrent and cross-activity use.

Usage from an implementation worker, orchestrator, or Temporal activity::

    from software_engineering_team.quality_gate_tools import (
        run_build_verification,
        run_code_review,
        run_linting,
    )
    build_ok, build_err = run_build_verification(repo_path, "backend", task_id)
    review = run_code_review(code, spec, task_desc, language="python")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


def _default_llm_getter(agent_key: str) -> Any:
    from llm_service import get_strands_model

    return get_strands_model(agent_key)


def _normalize_task_requirements(
    task_requirements: Optional[Union[str, List[str]]],
) -> str:
    """Coerce ``task_requirements`` to the string ``CodeReviewInput`` expects.

    Preconditions:
        - ``task_requirements`` is None, a str, or a list of strings.
    Postconditions:
        - Returns ``""`` when None or an empty list.
        - Returns a newline-joined string when given a list.
        - Returns the input unchanged when given a str.
    """
    if task_requirements is None:
        return ""
    if isinstance(task_requirements, list):
        return "\n".join(task_requirements)
    return task_requirements


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CodeReviewResult:
    approved: bool = False
    issues: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    spec_compliance_notes: str = ""


@dataclass
class BuildResult:
    success: bool = True
    error: str = ""
    is_env_failure: bool = False


@dataclass
class LintResult:
    passed: bool = True
    issues: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


def run_code_review(
    code: str,
    spec_content: str,
    task_description: str,
    language: str,
    *,
    files: Optional[Dict[str, str]] = None,
    task_requirements: Optional[Union[str, List[str]]] = None,
    acceptance_criteria: Optional[List[str]] = None,
    architecture: Any = None,
    existing_codebase: Optional[str] = None,
    user_decisions: Optional[List[str]] = None,
    repo_path: Optional[str] = None,
    llm_getter: Callable[[str], Any] = _default_llm_getter,
    progress_callback: Optional[Callable[[str, str, float], None]] = None,
) -> CodeReviewResult:
    """Run the code review agent and return structured results.

    Preconditions:
        - ``progress_callback`` is None or a non-raising callable accepting
          ``(step, detail, fraction)`` (see code_review_agent.models.ReviewProgressCallback).
        - ``user_decisions`` is None or a list of human-readable 'question → answer' lines the
          user has already answered; the reviewer treats them as settled (never flags them as open
          questions). Empty/None changes nothing about the review.
        - ``repo_path`` is None or the path of the materialized workspace the
          changed files live in; when set, the false-positive verifier is given
          read access to the whole repository so it can confirm existing files a
          finding claims are missing. Forwarded both as a live ``repo_reader``
          (used on the in-process path) and as ``repo_root`` on the review input
          (used to reconstruct the same access worker-side when the agent
          dispatches to Temporal, which cannot carry the live reader object
          across the workflow boundary).

    Postconditions:
        - When ``files`` (a ``{path: content}`` mapping of the task's changed
          files) is provided it takes precedence over ``code``; the agent
          bounds its own per-call prompts either way.
        - ``progress_callback`` is forwarded to the agent so review sub-steps
          (context prep, per-chunk review, parsing, approval) are reported live.
    """
    try:
        from software_engineering_team.code_review_agent import CodeReviewAgent
        from software_engineering_team.code_review_agent.models import build_code_review_input
        from software_engineering_team.code_review_agent.repo_reader import DiskRepoReader

        llm = llm_getter("code_review")
        agent = CodeReviewAgent(llm)

        # No pre-truncation: the coordinator bounds its own per-call prompts,
        # and its full-coverage guarantee only holds when it sees all the code.
        review_input = build_code_review_input(
            files=files,
            code=None if files is not None else code,
            spec_content=spec_content,
            task_description=task_description,
            task_requirements=_normalize_task_requirements(task_requirements),
            acceptance_criteria=acceptance_criteria or [],
            language=language,
            architecture=architecture,
            existing_codebase=existing_codebase,
            user_decisions=user_decisions or None,
            repo_root=repo_path,
        )
        run_kwargs: Dict[str, Any] = {"progress_callback": progress_callback}
        # Forward the reader only when a workspace path was supplied: passing
        # ``repo_reader=None`` is a no-op for the real agent, and omitting it keeps
        # duck-typed reviewer stubs (which may not declare the kwarg) working.
        if repo_path:
            run_kwargs["repo_reader"] = DiskRepoReader(repo_path)
        result = agent.run(review_input, **run_kwargs)
        issues = []
        for i in result.issues or []:
            issues.append(i.model_dump() if hasattr(i, "model_dump") else vars(i))
        return CodeReviewResult(
            approved=result.approved,
            issues=issues,
            summary=result.summary or "",
            spec_compliance_notes=result.spec_compliance_notes or "",
        )
    except Exception as e:
        logger.warning("Code review tool failed: %s", e)
        return CodeReviewResult(approved=False, summary=f"Review failed: {e}")


def run_build_verification(
    repo_path: Path,
    agent_type: str,
    task_id: str,
) -> BuildResult:
    """Run build verification (syntax check, compilation, tests).

    Delegates to ``_run_build_verification`` in :mod:`software_engineering_team.build_fix`
    which handles frontend (ng build), backend (python syntax + pytest), and
    devops (YAML + docker build) paths.
    """
    try:
        from software_engineering_team.build_fix import _run_build_verification

        success, error = _run_build_verification(repo_path, agent_type, task_id)
        is_env = error.startswith("ENV:") if error else False
        return BuildResult(success=success, error=error, is_env_failure=is_env)
    except Exception as e:
        logger.warning("Build verification tool failed: %s", e)
        return BuildResult(success=False, error=str(e))


def run_linting(
    repo_path: Path,
    task_id: str,
    *,
    llm_getter: Callable[[str], Any] = _default_llm_getter,
) -> LintResult:
    """Run the linting tool agent on the repo."""
    try:
        from software_engineering_team.linting_tool_agent import LintingToolAgent

        llm = llm_getter("linting_tool_agent")
        agent = LintingToolAgent(llm)
        result = agent.run(str(repo_path))
        issues = []
        if hasattr(result, "issues"):
            issues = [
                i.model_dump() if hasattr(i, "model_dump") else vars(i)
                for i in (result.issues or [])
            ]
        passed = getattr(result, "passed", True) if result else True
        return LintResult(passed=passed, issues=issues)
    except Exception as e:
        logger.warning("[%s] Linting tool failed: %s", task_id, e)
        return LintResult(passed=True)  # non-blocking
