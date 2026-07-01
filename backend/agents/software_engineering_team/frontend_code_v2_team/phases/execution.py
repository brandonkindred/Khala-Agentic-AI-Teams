"""
Execution phase: run each microtask via tool agents or general code gen.

No code from frontend_team is used. Uses template-based output parsing.
Supports per-microtask review gates with configurable retry behavior.

The non-gated helpers (issue dedup, review-dependency container, file writer,
general microtask coder, and ``run_execution``) are shared across the code-v2
teams (see ``shared/phases/execution.py``); this module wires in the frontend
team's models/prompt/profile and keeps the gated
``run_execution_with_review_gates`` orchestration, which interlocks with the
frontend ``review.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional

from strands import Agent

from llm_service import LLMClient
from software_engineering_team.shared.models import SystemArchitecture, Task
from software_engineering_team.shared.phases.execution import (
    ReviewDependencies,
    _run_general_microtask_impl,
    _write_microtask_files,
    run_execution_impl,
    write_microtask_output_or_fail,
)
from software_engineering_team.shared.repo_writer import UnsafeRepoPathError
from software_engineering_team.shared.strands_model import (
    LlmRunner,
    resolve_text_mode_strands_model,
)

from .. import models as _models
from ..models import (
    ExecutionResult,
    Microtask,
    MicrotaskReviewConfig,
    MicrotaskReviewFailedError,
    MicrotaskStatus,
    PlanningResult,
    ReviewResult,
    ToolAgentInput,
    ToolAgentKind,
    ToolAgentOutput,
)
from ..output_templates import parse_files_and_summary_template
from ..prompts import EXECUTION_PROMPT
from ._profile import PROFILE

logger = logging.getLogger(__name__)

ToolAgentRunner = Callable[[ToolAgentInput], ToolAgentOutput]


def _llm_runner() -> LlmRunner:
    """Build the LLM runner from this module's globals so tests can monkeypatch them."""
    return LlmRunner(agent_factory=Agent, resolve_model=resolve_text_mode_strands_model)


__all__ = [
    "ReviewDependencies",
    "ToolAgentRunner",
    "run_execution",
    "run_execution_with_review_gates",
]


def _run_general_microtask(
    *,
    llm: LLMClient,
    microtask: Microtask,
    task: Task,
    language: str,
    existing_code: str,
    architecture: Optional[SystemArchitecture],
) -> Dict[str, str]:
    """Use the LLM to implement a general (non-specialist) microtask (frontend models).

    Delegates to the shared implementation; keeps ``Agent`` /
    ``resolve_text_mode_strands_model`` as this module's LLM boundary.
    """
    return _run_general_microtask_impl(
        llm=llm,
        microtask=microtask,
        task=task,
        language=language,
        existing_code=existing_code,
        architecture=architecture,
        execution_prompt=EXECUTION_PROMPT,
        parse_files_and_summary=parse_files_and_summary_template,
        profile=PROFILE,
        runner=_llm_runner(),
    )


def run_execution(
    *,
    llm: LLMClient,
    task: Task,
    planning_result: PlanningResult,
    repo_path: Path,
    architecture: Optional[SystemArchitecture] = None,
    existing_code: str = "",
    tool_runners: Optional[Dict[ToolAgentKind, ToolAgentRunner]] = None,
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]] = None,
    only_microtask_ids: Optional[List[str]] = None,
) -> ExecutionResult:
    """Execute microtasks in dependency order (frontend models).

    Delegates the non-gated loop to ``run_execution_impl``; see that shared
    implementation for the full contract.
    """
    return run_execution_impl(
        llm=llm,
        task=task,
        planning_result=planning_result,
        repo_path=repo_path,
        architecture=architecture,
        existing_code=existing_code,
        tool_runners=tool_runners,
        progress_callback=progress_callback,
        only_microtask_ids=only_microtask_ids,
        models=_models,
        run_general_microtask=_run_general_microtask,
    )


def run_execution_with_review_gates(
    *,
    llm: LLMClient,
    task: Task,
    planning_result: PlanningResult,
    repo_path: Path,
    architecture: Optional[SystemArchitecture] = None,
    existing_code: str = "",
    tool_runners: Optional[Dict[ToolAgentKind, ToolAgentRunner]] = None,
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]] = None,
    only_microtask_ids: Optional[List[str]] = None,
    review_config: Optional[MicrotaskReviewConfig] = None,
    review_deps: Optional[ReviewDependencies] = None,
) -> ExecutionResult:
    """
    Execute microtasks with batch-based review cycles.

    After each microtask is coded, it must pass through sequential review phases:
    1. Code Review (build + lint + code review) - batch fix all issues
    2. QA Testing - batch fix all issues, then restart from Code Review
    3. Security Testing - batch fix all issues, then restart from Code Review
    4. Documentation - self-review loop (3-5 iterations, never fails)

    Key behavior:
    - Each review phase collects ALL issues and sends them to the coding agent at once
    - After QA or Security fixes, the flow restarts from Code Review
    - Documentation uses self-review iterations (no failure mode)

    ``progress_callback(current_index, completed, total, title, microtask_phase, phase_detail)`` is called during execution.
    ``current_index`` is the 1-based index of the currently executing microtask.
    ``microtask_phase`` is one of: "coding", "code_review", "qa_testing", "security_testing", "documentation", "completed".
    ``phase_detail`` provides human-readable detail about the current action.
    """
    from .problem_solving import run_batch_coding_fixes
    from .review import run_documentation_self_review, run_microtask_review

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
    logger.info(
        "[%s] Starting execution with batch review flow: %d microtasks, max_retries=%d, on_failure=%s",
        task_id,
        total,
        config.max_retries,
        config.on_failure,
    )

    for idx, mt in enumerate(microtasks):
        deps_met = all(d in completed_ids for d in mt.depends_on)
        if not deps_met:
            unmet = [d for d in mt.depends_on if d not in completed_ids]
            if any(d in review_failed_ids for d in unmet):
                logger.warning(
                    "[%s] Microtask %s depends on review-failed microtasks %s — skipping",
                    task_id,
                    mt.id,
                    unmet,
                )
                mt.status = MicrotaskStatus.SKIPPED
                mt.notes = f"Skipped: depends on review-failed microtasks {unmet}"
                continue
            logger.warning(
                "[%s] Microtask %s has unmet deps %s — running anyway", task_id, mt.id, unmet
            )

        mt.status = MicrotaskStatus.IN_PROGRESS
        logger.info(
            "[%s] Execution: microtask %d/%d — %s (%s)",
            task_id,
            idx + 1,
            total,
            mt.id,
            mt.tool_agent.value,
        )

        current_idx = idx + 1
        current_phase = "coding"

        if progress_callback:
            progress_callback(
                current_idx,
                len(completed_ids),
                total,
                mt.title or mt.id,
                "coding",
                "Generating code...",
            )

        def _detail_cb(detail: str, _idx: int = current_idx, _phase: str = current_phase) -> None:
            """Forward phase detail to progress callback."""
            if progress_callback:
                progress_callback(
                    _idx, len(completed_ids), total, mt.title or mt.id, _phase, detail
                )

        # ── Phase 1: Coding ───────────────────────────────────────────────────
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
            # Track files this microtask introduced for rollback on failure.
            microtask_file_keys = set(microtask_files.keys())
            # Route the initial write through the same guarded helper the review
            # cycles use, so an unsafe path in the first emission is a handled
            # REVIEW_FAILED (rolled back + recorded in review_failed_ids so
            # dependents SKIP) rather than a bare FAILED that skips that bookkeeping.
            if not write_microtask_output_or_fail(
                repo_path,
                microtask_files,
                mt=mt,
                task_id=task_id,
                review_failed_ids=review_failed_ids,
                all_files=all_files,
                microtask_file_keys=microtask_file_keys,
                review_failed_status=MicrotaskStatus.REVIEW_FAILED,
            ):
                continue
            all_files.update(microtask_files)

        except Exception as exc:
            logger.error("[%s] Microtask %s execution failed: %s", task_id, mt.id, exc)
            mt.status = MicrotaskStatus.FAILED
            mt.notes = str(exc)
            if progress_callback:
                progress_callback(
                    current_idx, len(completed_ids), total, mt.title or mt.id, "completed", ""
                )
            continue

        phase_failed = False
        total_cycles = 0
        max_total_cycles = config.max_retries * 3
        # Initialize review results so they're always defined for max-cycles check
        sec_review = ReviewResult(passed=True, summary="")

        # ── Sequential Review Gates with Batch Fixes ──────────────────────────
        # Flow: Code Review -> QA -> Security -> Documentation
        # After QA/Security fixes, restart from Code Review

        while not phase_failed and total_cycles < max_total_cycles:
            total_cycles += 1

            # ── Code Review Phase ─────────────────────────────────────────────
            mt.status = MicrotaskStatus.IN_REVIEW
            current_phase = "code_review"
            logger.info(
                "[%s] Microtask %s: Cycle %d - Running code review phase",
                task_id,
                mt.id,
                total_cycles,
            )

            if progress_callback:
                progress_callback(
                    current_idx,
                    len(completed_ids),
                    total,
                    mt.title or mt.id,
                    "code_review",
                    f"Code review (cycle {total_cycles})...",
                )

            review_result = run_microtask_review(
                llm=llm,
                task=task,
                microtask=mt,
                repo_path=repo_path,
                files=microtask_files,
                build_verifier=deps.build_verifier,
                qa_agent=None,
                security_agent=None,
                code_review_agent=deps.code_review_agent,
                linting_tool_agent=deps.linting_tool_agent,
                tool_agents=deps.tool_agents,
                detail_callback=lambda d: _detail_cb(d, current_idx, "code_review"),
            )

            cr_retry = 0
            while not review_result.passed and cr_retry < config.max_retries:
                cr_retry += 1
                logger.info(
                    "[%s] Microtask %s: Code review failed with %d issues. Batch fixing (attempt %d/%d)",
                    task_id,
                    mt.id,
                    len(review_result.issues),
                    cr_retry,
                    config.max_retries,
                )

                if progress_callback:
                    progress_callback(
                        current_idx,
                        len(completed_ids),
                        total,
                        mt.title or mt.id,
                        "code_review",
                        f"Batch fixing {len(review_result.issues)} issues (attempt {cr_retry})...",
                    )

                ps_result = run_batch_coding_fixes(
                    llm=llm,
                    microtask=mt,
                    issues=review_result.issues,
                    current_files=microtask_files,
                    language=planning_result.language,
                    repo_path=str(repo_path),
                    task_id=task_id,
                    phase_name="code_review",
                    detail_callback=lambda d: _detail_cb(d, current_idx, "code_review"),
                )

                microtask_files = ps_result.files
                if not write_microtask_output_or_fail(
                    repo_path,
                    microtask_files,
                    mt=mt,
                    task_id=task_id,
                    review_failed_ids=review_failed_ids,
                    all_files=all_files,
                    microtask_file_keys=microtask_file_keys,
                    review_failed_status=MicrotaskStatus.REVIEW_FAILED,
                ):
                    phase_failed = True
                    break
                mt.output_files = microtask_files
                all_files.update(microtask_files)

                if progress_callback:
                    progress_callback(
                        current_idx,
                        len(completed_ids),
                        total,
                        mt.title or mt.id,
                        "code_review",
                        "Re-running code review...",
                    )

                review_result = run_microtask_review(
                    llm=llm,
                    task=task,
                    microtask=mt,
                    repo_path=repo_path,
                    files=microtask_files,
                    build_verifier=deps.build_verifier,
                    qa_agent=None,
                    security_agent=None,
                    code_review_agent=deps.code_review_agent,
                    linting_tool_agent=deps.linting_tool_agent,
                    tool_agents=deps.tool_agents,
                    detail_callback=lambda d: _detail_cb(d, current_idx, "code_review"),
                )

            if not review_result.passed:
                phase_failed = True
                mt.status = MicrotaskStatus.REVIEW_FAILED
                review_failed_ids.add(mt.id)
                mt.notes = f"Code review failed after {config.max_retries} batch fix attempts: {review_result.summary}"
                logger.warning(
                    "[%s] Microtask %s: CODE_REVIEW_FAILED after %d batch fix attempts. Issues: %s",
                    task_id,
                    mt.id,
                    config.max_retries,
                    review_result.summary,
                )
                # Rollback: remove this microtask's files from all_files
                for fk in microtask_file_keys:
                    all_files.pop(fk, None)
                if config.on_failure == "stop":
                    raise MicrotaskReviewFailedError(mt, review_result)
                break

            # ── QA Testing Phase ──────────────────────────────────────────────
            current_phase = "qa_testing"
            logger.info(
                "[%s] Microtask %s: Cycle %d - Running QA testing phase",
                task_id,
                mt.id,
                total_cycles,
            )

            if progress_callback:
                progress_callback(
                    current_idx,
                    len(completed_ids),
                    total,
                    mt.title or mt.id,
                    "qa_testing",
                    f"QA testing (cycle {total_cycles})...",
                )

            qa_review = run_microtask_review(
                llm=llm,
                task=task,
                microtask=mt,
                repo_path=repo_path,
                files=microtask_files,
                build_verifier=None,
                qa_agent=deps.qa_agent,
                security_agent=None,
                code_review_agent=None,
                linting_tool_agent=None,
                tool_agents=deps.tool_agents,
                detail_callback=lambda d: _detail_cb(d, current_idx, "qa_testing"),
            )

            qa_issues = [i for i in qa_review.issues if i.source == "qa"]
            if qa_issues:
                logger.info(
                    "[%s] Microtask %s: QA testing found %d issues. Batch fixing and restarting from code review.",
                    task_id,
                    mt.id,
                    len(qa_issues),
                )

                if progress_callback:
                    progress_callback(
                        current_idx,
                        len(completed_ids),
                        total,
                        mt.title or mt.id,
                        "qa_testing",
                        f"Batch fixing {len(qa_issues)} QA issues...",
                    )

                ps_result = run_batch_coding_fixes(
                    llm=llm,
                    microtask=mt,
                    issues=qa_issues,
                    current_files=microtask_files,
                    language=planning_result.language,
                    repo_path=str(repo_path),
                    task_id=task_id,
                    phase_name="qa",
                    detail_callback=lambda d: _detail_cb(d, current_idx, "qa_testing"),
                )

                microtask_files = ps_result.files
                if not write_microtask_output_or_fail(
                    repo_path,
                    microtask_files,
                    mt=mt,
                    task_id=task_id,
                    review_failed_ids=review_failed_ids,
                    all_files=all_files,
                    microtask_file_keys=microtask_file_keys,
                    review_failed_status=MicrotaskStatus.REVIEW_FAILED,
                ):
                    phase_failed = True
                    break
                mt.output_files = microtask_files
                all_files.update(microtask_files)

                # Restart from code review
                continue

            # ── Security Testing Phase ────────────────────────────────────────
            current_phase = "security_testing"
            logger.info(
                "[%s] Microtask %s: Cycle %d - Running security testing phase",
                task_id,
                mt.id,
                total_cycles,
            )

            if progress_callback:
                progress_callback(
                    current_idx,
                    len(completed_ids),
                    total,
                    mt.title or mt.id,
                    "security_testing",
                    f"Security testing (cycle {total_cycles})...",
                )

            sec_review = run_microtask_review(
                llm=llm,
                task=task,
                microtask=mt,
                repo_path=repo_path,
                files=microtask_files,
                build_verifier=None,
                qa_agent=None,
                security_agent=deps.security_agent,
                code_review_agent=None,
                linting_tool_agent=None,
                tool_agents=deps.tool_agents,
                detail_callback=lambda d: _detail_cb(d, current_idx, "security_testing"),
            )

            sec_issues = [i for i in sec_review.issues if i.source == "security"]
            if sec_issues:
                logger.info(
                    "[%s] Microtask %s: Security testing found %d issues. Batch fixing and restarting from code review.",
                    task_id,
                    mt.id,
                    len(sec_issues),
                )

                if progress_callback:
                    progress_callback(
                        current_idx,
                        len(completed_ids),
                        total,
                        mt.title or mt.id,
                        "security_testing",
                        f"Batch fixing {len(sec_issues)} security issues...",
                    )

                ps_result = run_batch_coding_fixes(
                    llm=llm,
                    microtask=mt,
                    issues=sec_issues,
                    current_files=microtask_files,
                    language=planning_result.language,
                    repo_path=str(repo_path),
                    task_id=task_id,
                    phase_name="security",
                    detail_callback=lambda d: _detail_cb(d, current_idx, "security_testing"),
                )

                microtask_files = ps_result.files
                if not write_microtask_output_or_fail(
                    repo_path,
                    microtask_files,
                    mt=mt,
                    task_id=task_id,
                    review_failed_ids=review_failed_ids,
                    all_files=all_files,
                    microtask_file_keys=microtask_file_keys,
                    review_failed_status=MicrotaskStatus.REVIEW_FAILED,
                ):
                    phase_failed = True
                    break
                mt.output_files = microtask_files
                all_files.update(microtask_files)

                # Restart from code review
                continue

            # All review phases passed - proceed to documentation
            break

        # Check if we exceeded max cycles
        if total_cycles >= max_total_cycles and not phase_failed:
            phase_failed = True
            mt.status = MicrotaskStatus.REVIEW_FAILED
            review_failed_ids.add(mt.id)
            mt.notes = f"Review cycles exhausted after {total_cycles} iterations"
            logger.warning(
                "[%s] Microtask %s: REVIEW_FAILED - exhausted %d total cycles",
                task_id,
                mt.id,
                total_cycles,
            )
            # Rollback: remove this microtask's files from all_files
            for fk in microtask_file_keys:
                all_files.pop(fk, None)
            # Security failures always stop regardless of on_failure setting
            _force_stop = config.on_failure == "stop" or (
                getattr(config, "security_failure_always_stops", True) and not sec_review.passed
            )
            if _force_stop:
                raise MicrotaskReviewFailedError(
                    mt, ReviewResult(passed=False, issues=[], summary="Max cycles exceeded")
                )

        # ── Phase 5: Documentation (Self-Review, Never Fails) ─────────────────
        if not phase_failed:
            mt.status = MicrotaskStatus.IN_DOCUMENTATION
            current_phase = "documentation"
            logger.info(
                "[%s] Microtask %s: Running documentation self-review (3-5 iterations)",
                task_id,
                mt.id,
            )

            if progress_callback:
                progress_callback(
                    current_idx,
                    len(completed_ids),
                    total,
                    mt.title or mt.id,
                    "documentation",
                    "Starting documentation self-review...",
                )

            # Generate initial documentation
            doc_agent = (
                deps.tool_agents.get(ToolAgentKind.DOCUMENTATION) if deps.tool_agents else None
            )
            doc_files: Dict[str, str] = {}
            if doc_agent and hasattr(doc_agent, "document_microtask"):
                try:
                    doc_result = doc_agent.document_microtask(
                        microtask=mt,
                        files=microtask_files,
                        task_description=task.description or "",
                    )
                    if doc_result.files:
                        doc_files = doc_result.files
                        logger.info(
                            "[%s] Microtask %s: initial documentation generated %d file(s)",
                            task_id,
                            mt.id,
                            len(doc_files),
                        )
                except Exception as e:
                    logger.warning(
                        "[%s] Microtask %s: initial documentation generation failed: %s",
                        task_id,
                        mt.id,
                        e,
                    )

            # Run self-review iterations
            self_review_result = run_documentation_self_review(
                llm=llm,
                documentation=doc_files,
                code_files=microtask_files,
                task_description=task.description or "",
                min_iterations=1,
                max_iterations=2,
                quality_threshold=0.9,
                detail_callback=lambda d: _detail_cb(d, current_idx, "documentation"),
            )

            # Update files with refined documentation
            # A rejected (unsafe) doc path is best-effort: log and skip it — the
            # microtask still completes.
            if self_review_result.documentation:
                try:
                    _write_microtask_files(repo_path, self_review_result.documentation)
                    microtask_files.update(self_review_result.documentation)
                    mt.output_files = microtask_files
                    all_files.update(self_review_result.documentation)
                except UnsafeRepoPathError as exc:
                    logger.warning(
                        "[%s] Microtask %s: unsafe documentation path rejected, skipping: %s",
                        task_id,
                        mt.id,
                        exc,
                    )

            logger.info(
                "[%s] Microtask %s: documentation self-review complete after %d iterations (score: %.2f)",
                task_id,
                mt.id,
                self_review_result.iterations,
                self_review_result.final_quality_score,
            )

            mt.status = MicrotaskStatus.COMPLETED
            completed_ids.add(mt.id)
            logger.info(
                "[%s] Microtask %s: COMPLETED (passed all review phases in %d cycles)",
                task_id,
                mt.id,
                total_cycles,
            )

        if progress_callback:
            progress_callback(
                current_idx, len(completed_ids), total, mt.title or mt.id, "completed", ""
            )

    completed_count = len(completed_ids)
    failed_count = len(review_failed_ids)
    summary = f"Executed {completed_count}/{total} microtasks successfully; {failed_count} review-failed; {len(all_files)} files produced."
    logger.info("[%s] Execution with batch review flow complete: %s", task_id, summary)

    return ExecutionResult(files=all_files, microtasks=microtasks, summary=summary)
