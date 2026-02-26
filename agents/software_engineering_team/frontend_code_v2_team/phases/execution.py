"""
Execution phase: run each microtask via tool agents or general code gen.

No code from frontend_team is used. Uses template-based output parsing.
Supports per-microtask review gates with configurable retry behavior.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from shared.llm import LLMClient
from shared.models import SystemArchitecture, Task

from ..models import (
    ExecutionResult,
    Microtask,
    MicrotaskReviewConfig,
    MicrotaskReviewFailedError,
    MicrotaskStatus,
    PlanningResult,
    ReviewResult,
    ToolAgentKind,
    ToolAgentInput,
    ToolAgentOutput,
)
from ..output_templates import parse_files_and_summary_template
from ..prompts import EXECUTION_PROMPT

logger = logging.getLogger(__name__)

ToolAgentRunner = Callable[[ToolAgentInput], ToolAgentOutput]


class ReviewDependencies:
    """Container for all review-related agents and callbacks."""

    def __init__(
        self,
        *,
        build_verifier: Optional[Callable[..., Tuple[bool, str]]] = None,
        qa_agent: Any = None,
        security_agent: Any = None,
        code_review_agent: Any = None,
        linting_tool_agent: Any = None,
        tool_agents: Optional[Dict[ToolAgentKind, Any]] = None,
    ) -> None:
        self.build_verifier = build_verifier
        self.qa_agent = qa_agent
        self.security_agent = security_agent
        self.code_review_agent = code_review_agent
        self.linting_tool_agent = linting_tool_agent
        self.tool_agents = tool_agents or {}


def _run_general_microtask(
    *,
    llm: LLMClient,
    microtask: Microtask,
    task: Task,
    language: str,
    existing_code: str,
    architecture: Optional[SystemArchitecture],
) -> Dict[str, str]:
    """Use the LLM to implement a general (non-specialist) microtask."""
    arch_ctx = ""
    if architecture:
        arch_ctx = architecture.overview[:2000]
    prompt = EXECUTION_PROMPT.format(
        microtask_description=microtask.description or microtask.title,
        requirements=task.requirements or task.description,
        existing_code=existing_code[:8000] if existing_code else "(none)",
        architecture_context=arch_ctx or "(none)",
    )
    raw = llm.complete_text(prompt)
    data = parse_files_and_summary_template(raw)
    return data.get("files") or {}


def run_execution(
    *,
    llm: LLMClient,
    task: Task,
    planning_result: PlanningResult,
    repo_path: Path,
    architecture: Optional[SystemArchitecture] = None,
    existing_code: str = "",
    tool_runners: Optional[Dict[ToolAgentKind, ToolAgentRunner]] = None,
    progress_callback: Optional[Callable[[int, int, str, str, str], None]] = None,
    only_microtask_ids: Optional[List[str]] = None,
) -> ExecutionResult:
    """
    Execute microtasks in dependency order.

    tool_runners maps ToolAgentKind to callable(ToolAgentInput) -> ToolAgentOutput.
    For microtasks whose tool_agent has no runner, fall back to general LLM code gen.
    ``progress_callback(completed, total, title, microtask_phase, phase_detail)`` is called during execution.
    ``microtask_phase`` is one of: "coding", "review", "problem_solving", "completed".
    ``phase_detail`` provides human-readable detail about the current action.
    """
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
            logger.warning("[%s] Microtask %s has unmet deps %s — running anyway", task.id, mt.id, mt.depends_on)

        mt.status = MicrotaskStatus.IN_PROGRESS
        logger.info("[%s] Execution: microtask %d/%d — %s (%s)", task.id, idx + 1, total, mt.id, mt.tool_agent.value)

        if progress_callback:
            progress_callback(len(completed_ids), total, mt.title or mt.id, "coding", "Generating code...")

        try:
            runner = runners.get(mt.tool_agent)
            if runner is not None:
                inp = ToolAgentInput(
                    microtask=mt,
                    repo_path=str(repo_path),
                    existing_code=existing_code[:6000] if existing_code else "",
                    language=planning_result.language,
                )
                out = runner(inp)
                mt.output_files = out.files
                mt.notes = out.summary
            else:
                files = _run_general_microtask(
                    llm=llm,
                    microtask=mt,
                    task=task,
                    language=planning_result.language,
                    existing_code=existing_code,
                    architecture=architecture,
                )
                mt.output_files = files

            all_files.update(mt.output_files)
            mt.status = MicrotaskStatus.COMPLETED
            completed_ids.add(mt.id)
        except Exception as exc:
            logger.error("[%s] Microtask %s failed: %s", task.id, mt.id, exc)
            mt.status = MicrotaskStatus.FAILED
            mt.notes = str(exc)

        if progress_callback:
            progress_callback(len(completed_ids), total, mt.title or mt.id, "completed", "")

    summary = f"Executed {len(completed_ids)}/{total} microtasks; {len(all_files)} files produced."
    return ExecutionResult(files=all_files, microtasks=microtasks, summary=summary)


def _write_microtask_files(repo_path: Path, files: Dict[str, str]) -> None:
    """Write microtask output files to the repository."""
    for rel_path, content in files.items():
        safe_rel_path = rel_path.lstrip("/")
        file_path = repo_path / safe_rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")


def run_execution_with_review_gates(
    *,
    llm: LLMClient,
    task: Task,
    planning_result: PlanningResult,
    repo_path: Path,
    architecture: Optional[SystemArchitecture] = None,
    existing_code: str = "",
    tool_runners: Optional[Dict[ToolAgentKind, ToolAgentRunner]] = None,
    progress_callback: Optional[Callable[[int, int, str, str, str], None]] = None,
    only_microtask_ids: Optional[List[str]] = None,
    review_config: Optional[MicrotaskReviewConfig] = None,
    review_deps: Optional[ReviewDependencies] = None,
) -> ExecutionResult:
    """
    Execute microtasks sequentially with per-microtask review gates.

    After each microtask is coded, it must pass a full review (code quality,
    QA, security, build, lint) before the next microtask can begin.

    If review fails, the problem-solving loop runs up to max_retries times.
    If still failing after retries, behavior depends on review_config.on_failure:
    - "stop": raises MicrotaskReviewFailedError
    - "skip_continue": marks microtask as REVIEW_FAILED and continues

    ``progress_callback(completed, total, title, microtask_phase, phase_detail)`` is called during execution.
    ``microtask_phase`` is one of: "coding", "review", "problem_solving", "completed".
    ``phase_detail`` provides human-readable detail about the current action.
    """
    from .review import run_microtask_review
    from .problem_solving import run_problem_solving_for_microtask

    config = review_config or MicrotaskReviewConfig()
    deps = review_deps or ReviewDependencies()
    runners = tool_runners or {}

    all_files: Dict[str, str] = {}
    microtasks = list(planning_result.microtasks)
    if only_microtask_ids is not None:
        id_set = set(only_microtask_ids)
        microtasks = [mt for mt in microtasks if mt.id in id_set]
    completed_ids: set[str] = set()
    review_failed_ids: set[str] = set()
    total = len(microtasks)

    task_id = task.id
    logger.info("[%s] Starting execution with review gates: %d microtasks, max_retries=%d, on_failure=%s",
                task_id, total, config.max_retries, config.on_failure)

    for idx, mt in enumerate(microtasks):
        deps_met = all(d in completed_ids for d in mt.depends_on)
        if not deps_met:
            unmet = [d for d in mt.depends_on if d not in completed_ids]
            if any(d in review_failed_ids for d in unmet):
                logger.warning("[%s] Microtask %s depends on review-failed microtasks %s — skipping",
                               task_id, mt.id, unmet)
                mt.status = MicrotaskStatus.SKIPPED
                mt.notes = f"Skipped: depends on review-failed microtasks {unmet}"
                continue
            logger.warning("[%s] Microtask %s has unmet deps %s — running anyway", task_id, mt.id, unmet)

        mt.status = MicrotaskStatus.IN_PROGRESS
        logger.info("[%s] Execution: microtask %d/%d — %s (%s)", task_id, idx + 1, total, mt.id, mt.tool_agent.value)

        if progress_callback:
            progress_callback(len(completed_ids), total, mt.title or mt.id, "coding", "Generating code...")

        def _detail_cb(detail: str) -> None:
            """Forward phase detail to progress callback."""
            if progress_callback:
                progress_callback(len(completed_ids), total, mt.title or mt.id, current_phase, detail)

        current_phase = "coding"

        try:
            runner = runners.get(mt.tool_agent)
            if runner is not None:
                inp = ToolAgentInput(
                    microtask=mt,
                    repo_path=str(repo_path),
                    existing_code=existing_code[:6000] if existing_code else "",
                    language=planning_result.language,
                )
                out = runner(inp)
                mt.output_files = out.files
                mt.notes = out.summary
            else:
                files = _run_general_microtask(
                    llm=llm,
                    microtask=mt,
                    task=task,
                    language=planning_result.language,
                    existing_code=existing_code,
                    architecture=architecture,
                )
                mt.output_files = files

            microtask_files = dict(mt.output_files)
            _write_microtask_files(repo_path, microtask_files)
            all_files.update(microtask_files)

        except Exception as exc:
            logger.error("[%s] Microtask %s execution failed: %s", task_id, mt.id, exc)
            mt.status = MicrotaskStatus.FAILED
            mt.notes = str(exc)
            if progress_callback:
                progress_callback(len(completed_ids), total, mt.title or mt.id, "completed", "")
            continue

        mt.status = MicrotaskStatus.IN_REVIEW
        current_phase = "review"
        logger.info("[%s] Microtask %s: starting review", task_id, mt.id)

        if progress_callback:
            progress_callback(len(completed_ids), total, mt.title or mt.id, "review", "Starting review...")

        review_result = run_microtask_review(
            llm=llm,
            task=task,
            microtask=mt,
            repo_path=repo_path,
            files=microtask_files,
            build_verifier=deps.build_verifier,
            qa_agent=deps.qa_agent,
            security_agent=deps.security_agent,
            code_review_agent=deps.code_review_agent,
            linting_tool_agent=deps.linting_tool_agent,
            tool_agents=deps.tool_agents,
            detail_callback=_detail_cb,
        )

        retry_count = 0
        while not review_result.passed and retry_count < config.max_retries:
            retry_count += 1
            logger.info("[%s] Microtask %s: review failed, problem-solving attempt %d/%d",
                        task_id, mt.id, retry_count, config.max_retries)

            current_phase = "problem_solving"
            if progress_callback:
                progress_callback(len(completed_ids), total, mt.title or mt.id, "problem_solving", "Analyzing issues...")

            ps_result = run_problem_solving_for_microtask(
                llm=llm,
                microtask=mt,
                review_result=review_result,
                current_files=microtask_files,
                language=planning_result.language,
                repo_path=str(repo_path),
                tool_agents=deps.tool_agents,
                task_id=task_id,
                detail_callback=_detail_cb,
            )

            microtask_files = ps_result.files
            _write_microtask_files(repo_path, microtask_files)
            mt.output_files = microtask_files
            all_files.update(microtask_files)

            current_phase = "review"
            if progress_callback:
                progress_callback(len(completed_ids), total, mt.title or mt.id, "review", "Re-running review...")

            review_result = run_microtask_review(
                llm=llm,
                task=task,
                microtask=mt,
                repo_path=repo_path,
                files=microtask_files,
                build_verifier=deps.build_verifier,
                qa_agent=deps.qa_agent,
                security_agent=deps.security_agent,
                code_review_agent=deps.code_review_agent,
                linting_tool_agent=deps.linting_tool_agent,
                tool_agents=deps.tool_agents,
                detail_callback=_detail_cb,
            )

        if review_result.passed:
            mt.status = MicrotaskStatus.IN_DOCUMENTATION
            logger.info("[%s] Microtask %s: starting documentation update", task_id, mt.id)
            
            doc_agent = deps.tool_agents.get(ToolAgentKind.DOCUMENTATION) if deps.tool_agents else None
            if doc_agent and hasattr(doc_agent, "document_microtask"):
                try:
                    doc_result = doc_agent.document_microtask(
                        microtask=mt,
                        files=microtask_files,
                        task_description=task.description or "",
                    )
                    if doc_result.files:
                        microtask_files.update(doc_result.files)
                        _write_microtask_files(repo_path, doc_result.files)
                        mt.output_files = microtask_files
                        all_files.update(doc_result.files)
                        logger.info("[%s] Microtask %s: documentation updated %d file(s)", 
                                    task_id, mt.id, len(doc_result.files))
                except Exception as e:
                    logger.warning("[%s] Microtask %s: documentation update failed: %s", task_id, mt.id, e)
            
            mt.status = MicrotaskStatus.COMPLETED
            completed_ids.add(mt.id)
            logger.info("[%s] Microtask %s: COMPLETED (passed review after %d retries)", task_id, mt.id, retry_count)
        else:
            mt.status = MicrotaskStatus.REVIEW_FAILED
            review_failed_ids.add(mt.id)
            mt.notes = f"Review failed after {config.max_retries} retries: {review_result.summary}"
            logger.warning("[%s] Microtask %s: REVIEW_FAILED after %d retries", task_id, mt.id, config.max_retries)

            if config.on_failure == "stop":
                raise MicrotaskReviewFailedError(mt, review_result)

        if progress_callback:
            progress_callback(len(completed_ids), total, mt.title or mt.id, "completed", "")

    completed_count = len(completed_ids)
    failed_count = len(review_failed_ids)
    summary = f"Executed {completed_count}/{total} microtasks successfully; {failed_count} review-failed; {len(all_files)} files produced."
    logger.info("[%s] Execution with review gates complete: %s", task_id, summary)

    return ExecutionResult(files=all_files, microtasks=microtasks, summary=summary)
