"""
Shared Execution-phase leaf helpers for the code-v2 teams.

Holds the pieces that were byte-identical between the backend and frontend
execution phases — issue dedup, the review-dependency container, the
microtask-file writer, the general (non-specialist) microtask coder, and the
non-gated ``run_execution`` loop. The stack-specific ``EXECUTION_PROMPT``
divergence (backend injects ``{language_conventions}``, frontend does not) is
handled via the team's
:class:`~software_engineering_team.shared.stack_profile.StackProfile`.

The gated ``run_execution_with_review_gates`` orchestration stays per-team: its
review-gate loop interlocks with each team's ``review.py`` (out of scope).
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_service import LLMClient
from software_engineering_team.shared.models import SystemArchitecture, Task
from software_engineering_team.shared.repo_writer import write_repo_text_files
from software_engineering_team.shared.stack_profile import StackProfile
from software_engineering_team.shared.strands_model import LlmRunner

logger = logging.getLogger(__name__)


def _dedup_issues(issues: List[Any], seen: set[tuple[str, str]]) -> List[Any]:
    """Remove duplicate issues across review cycles based on (file_path, description).

    Preconditions:
        ``seen`` accumulates ``(file_path, description)`` keys across calls.
    Postconditions:
        Returns issues whose key was not already in ``seen``; mutates ``seen``.
    """
    unique: List[Any] = []
    for issue in issues:
        key = (issue.file_path or "", issue.description or "")
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


class ReviewDependencies:
    """Container for all review-related agents and callbacks.

    Invariants:
        ``tool_agents`` is always a dict (never ``None``).
    """

    def __init__(
        self,
        *,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        linting_tool_agent: Any = None,
        tool_agents: Optional[Dict[Any, Any]] = None,
    ) -> None:
        self.build_verifier = build_verifier
        self.qa_agent = qa_agent
        self.security_agent = security_agent
        self.code_review_agent = code_review_agent
        self.linting_tool_agent = linting_tool_agent
        self.tool_agents = tool_agents or {}


# Writing microtask output files is the same guarded operation as the
# documentation phase's writer — both delegate to the one shared implementation.
_write_microtask_files = write_repo_text_files


def _run_general_microtask_impl(
    *,
    llm: LLMClient,
    microtask: Any,
    task: Task,
    language: str,
    existing_code: str,
    architecture: Optional[SystemArchitecture],
    execution_prompt: str,
    parse_files_and_summary: Callable[[str], Dict[str, Any]],
    profile: StackProfile,
    runner: LlmRunner,
) -> Dict[str, str]:
    """Use the LLM to implement a general (non-specialist) microtask.

    Preconditions:
        ``execution_prompt`` carries a ``{language_conventions}`` slot iff
        ``profile.execution_has_language_conventions``.
    Postconditions:
        Returns the parsed ``{path: content}`` map (possibly empty).
    """
    arch_ctx = ""
    if architecture:
        arch_ctx = architecture.overview

    fmt: Dict[str, Any] = dict(
        microtask_description=microtask.description or microtask.title,
        requirements=task.requirements or task.description,
        existing_code=existing_code[:8000] if existing_code else "(none)",
        architecture_context=arch_ctx or "(none)",
    )
    if profile.execution_has_language_conventions:
        fmt["language_conventions"] = profile.conventions_for(language)
    prompt = execution_prompt.format(**fmt)
    raw = runner.run(llm, prompt)
    data = parse_files_and_summary(raw)
    files = data.get("files") or {}

    return files


def run_execution_impl(
    *,
    llm: LLMClient,
    task: Task,
    planning_result: Any,
    repo_path: Path,
    architecture: Optional[SystemArchitecture],
    existing_code: str,
    tool_runners: Optional[Dict[Any, Any]],
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]],
    only_microtask_ids: Optional[List[str]],
    models: ModuleType,
    run_general_microtask: Callable[..., Dict[str, str]],
) -> Any:
    """Execute microtasks in dependency order (non-gated).

    If ``only_microtask_ids`` is set, only those microtasks are run (e.g. fix
    microtasks from ``plan_fixes_for_unresolved_issues``). ``tool_runners`` maps
    ToolAgentKind → callable(ToolAgentInput) → ToolAgentOutput; microtasks whose
    tool_agent has no runner fall back to ``run_general_microtask``.

    Preconditions:
        ``models`` exposes ``MicrotaskStatus``, ``ExecutionResult``, ``ToolAgentInput``;
        ``run_general_microtask`` is the team's general coder (the monkeypatch
        boundary for its LLM ``Agent``).
    Postconditions:
        Returns an ``ExecutionResult``; a failed microtask is marked FAILED and
        execution continues with the rest.
    """
    microtask_status_enum = models.MicrotaskStatus
    execution_result_cls = models.ExecutionResult
    tool_agent_input_cls = models.ToolAgentInput

    runners = tool_runners or {}
    all_files: Dict[str, str] = {}
    microtasks = list(planning_result.microtasks)
    if only_microtask_ids is not None:
        id_set = set(only_microtask_ids)
        microtasks = [mt for mt in microtasks if mt.id in id_set]
    completed_ids: set[str] = set()
    total = len(microtasks)

    for idx, mt in enumerate(microtasks):
        deps_met = all(d in completed_ids for d in mt.depends_on)
        if not deps_met:
            logger.warning(
                "[%s] Microtask %s has unmet deps %s — running anyway",
                task.id,
                mt.id,
                mt.depends_on,
            )

        mt.status = microtask_status_enum.IN_PROGRESS
        logger.info(
            "[%s] Execution: microtask %d/%d — %s (%s)",
            task.id,
            idx + 1,
            total,
            mt.id,
            mt.tool_agent.value,
        )

        if progress_callback:
            progress_callback(
                idx + 1,
                len(completed_ids),
                total,
                mt.title or mt.id,
                "coding",
                "Generating code...",
            )

        try:
            runner = runners.get(mt.tool_agent)
            if runner is not None:
                inp = tool_agent_input_cls(
                    microtask=mt,
                    repo_path=str(repo_path),
                    existing_code=existing_code[:6000] if existing_code else "",
                    language=planning_result.language,
                )
                out = runner(inp)
                mt.output_files = out.files
                mt.notes = out.summary
            else:
                files = run_general_microtask(
                    llm=llm,
                    microtask=mt,
                    task=task,
                    language=planning_result.language,
                    existing_code=existing_code,
                    architecture=architecture,
                )
                mt.output_files = files

            all_files.update(mt.output_files)
            mt.status = microtask_status_enum.COMPLETED
            completed_ids.add(mt.id)
        except Exception as exc:
            logger.error("[%s] Microtask %s failed: %s", task.id, mt.id, exc)
            mt.status = microtask_status_enum.FAILED
            mt.notes = str(exc)

        if progress_callback:
            progress_callback(
                idx + 1, len(completed_ids), total, mt.title or mt.id, "completed", ""
            )

    summary = f"Executed {len(completed_ids)}/{total} microtasks; {len(all_files)} files produced."
    return execution_result_cls(files=all_files, microtasks=microtasks, summary=summary)
