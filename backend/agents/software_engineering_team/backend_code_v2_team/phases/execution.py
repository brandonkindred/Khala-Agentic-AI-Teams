"""
Execution phase: run each microtask via tool agents or general code gen.

No code from ``backend_agent`` is used.
Uses template-based output (not JSON) so parsing works across model providers.
Supports per-microtask review gates with configurable retry behavior.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from llm_service import LLMClient
from software_engineering_team.shared.models import SystemArchitecture, Task

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
from ..prompts import EXECUTION_PROMPT, JAVA_CONVENTIONS, PYTHON_CONVENTIONS

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


def _language_conventions(language: str) -> str:
    return JAVA_CONVENTIONS if language == "java" else PYTHON_CONVENTIONS


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
        language_conventions=_language_conventions(language),
        microtask_description=microtask.description or microtask.title,
        requirements=task.requirements or task.description,
        existing_code=existing_code[:8000] if existing_code else "(none)",
        architecture_context=arch_ctx or "(none)",
    )
    raw = llm.complete_text(prompt)
    data = parse_files_and_summary_template(raw)
    files = data.get("files") or {}

    return files


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
    """
    Execute microtasks in dependency order.

    If ``only_microtask_ids`` is set, only those microtasks are run (e.g. fix
    microtasks from plan_fixes_for_unresolved_issues). Otherwise all microtasks
    are run.

    ``tool_runners`` maps ToolAgentKind → callable(ToolAgentInput) → ToolAgentOutput.
    For microtasks whose tool_agent has no runner, fall back to general LLM code gen.
    ``progress_callback(current_index, completed, total, title, microtask_phase, phase_detail)`` is called during execution.
    ``current_index`` is the 1-based index of the currently executing microtask.
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
            logger.warning(
                "[%s] Microtask %s has unmet deps %s — running anyway",
                task.id,
                mt.id,
                mt.depends_on,
            )

        mt.status = MicrotaskStatus.IN_PROGRESS
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
            progress_callback(
                idx + 1, len(completed_ids), total, mt.title or mt.id, "completed", ""
            )

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
    from .review import (
        run_code_review_phase,
        run_documentation_self_review,
        run_qa_testing_phase,
        run_security_testing_phase,
    )

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
        "[%s] Starting execution with batch review flow: %d microtasks, on_failure=%s",
        task_id,
        total,
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
            _write_microtask_files(repo_path, microtask_files)
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
        max_total_cycles = (
            config.code_review_max_retries + config.qa_max_retries + config.security_max_retries
        )

        # ── Sequential Review Gates with Batch Fixes ──────────────────────────
        # Flow: Code Review -> QA -> Security -> Documentation
        # After QA/Security fixes, restart from Code Review

        while not phase_failed and total_cycles < max_total_cycles:
            total_cycles += 1

            # ── Code Review Phase ─────────────────────────────────────────────
            mt.status = MicrotaskStatus.IN_CODE_REVIEW
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

            cr_result = run_code_review_phase(
                llm=llm,
                task=task,
                microtask=mt,
                repo_path=repo_path,
                files=microtask_files,
                build_verifier=deps.build_verifier,
                code_review_agent=deps.code_review_agent,
                linting_tool_agent=deps.linting_tool_agent,
                detail_callback=lambda d: _detail_cb(d, current_idx, "code_review"),
            )

            cr_retry = 0
            while not cr_result.passed and cr_retry < config.code_review_max_retries:
                cr_retry += 1
                logger.info(
                    "[%s] Microtask %s: Code review failed with %d issues. Batch fixing (attempt %d/%d)",
                    task_id,
                    mt.id,
                    len(cr_result.issues),
                    cr_retry,
                    config.code_review_max_retries,
                )

                if progress_callback:
                    progress_callback(
                        current_idx,
                        len(completed_ids),
                        total,
                        mt.title or mt.id,
                        "code_review",
                        f"Batch fixing {len(cr_result.issues)} issues (attempt {cr_retry})...",
                    )

                ps_result = run_batch_coding_fixes(
                    llm=llm,
                    microtask=mt,
                    issues=cr_result.issues,
                    current_files=microtask_files,
                    language=planning_result.language,
                    repo_path=str(repo_path),
                    task_id=task_id,
                    phase_name="code_review",
                    detail_callback=lambda d: _detail_cb(d, current_idx, "code_review"),
                )

                microtask_files = ps_result.files
                _write_microtask_files(repo_path, microtask_files)
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

                cr_result = run_code_review_phase(
                    llm=llm,
                    task=task,
                    microtask=mt,
                    repo_path=repo_path,
                    files=microtask_files,
                    build_verifier=deps.build_verifier,
                    code_review_agent=deps.code_review_agent,
                    linting_tool_agent=deps.linting_tool_agent,
                    detail_callback=lambda d: _detail_cb(d, current_idx, "code_review"),
                )

            if not cr_result.passed:
                phase_failed = True
                mt.status = MicrotaskStatus.REVIEW_FAILED
                review_failed_ids.add(mt.id)
                mt.notes = f"Code review failed after {config.code_review_max_retries} batch fix attempts: {cr_result.summary}"
                logger.warning(
                    "[%s] Microtask %s: CODE_REVIEW_FAILED after %d batch fix attempts. Issues: %s",
                    task_id,
                    mt.id,
                    config.code_review_max_retries,
                    cr_result.summary[:200],
                )
                if config.on_failure == "stop":
                    raise MicrotaskReviewFailedError(
                        mt,
                        ReviewResult(
                            passed=False, issues=cr_result.issues, summary=cr_result.summary
                        ),
                    )
                break

            # ── QA Testing Phase ──────────────────────────────────────────────
            mt.status = MicrotaskStatus.IN_QA_TESTING
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

            qa_result = run_qa_testing_phase(
                task=task,
                microtask=mt,
                files=microtask_files,
                qa_agent=deps.qa_agent,
                tool_agents=deps.tool_agents,
                repo_path=repo_path,
                detail_callback=lambda d: _detail_cb(d, current_idx, "qa_testing"),
            )

            if not qa_result.passed:
                logger.info(
                    "[%s] Microtask %s: QA testing failed with %d issues. Batch fixing and restarting from code review.",
                    task_id,
                    mt.id,
                    len(qa_result.issues),
                )

                if progress_callback:
                    progress_callback(
                        current_idx,
                        len(completed_ids),
                        total,
                        mt.title or mt.id,
                        "qa_testing",
                        f"Batch fixing {len(qa_result.issues)} QA issues...",
                    )

                ps_result = run_batch_coding_fixes(
                    llm=llm,
                    microtask=mt,
                    issues=qa_result.issues,
                    current_files=microtask_files,
                    language=planning_result.language,
                    repo_path=str(repo_path),
                    task_id=task_id,
                    phase_name="qa",
                    detail_callback=lambda d: _detail_cb(d, current_idx, "qa_testing"),
                )

                microtask_files = ps_result.files
                _write_microtask_files(repo_path, microtask_files)
                mt.output_files = microtask_files
                all_files.update(microtask_files)

                # Restart from code review
                continue

            # ── Security Testing Phase ────────────────────────────────────────
            mt.status = MicrotaskStatus.IN_SECURITY_TESTING
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

            sec_result = run_security_testing_phase(
                task=task,
                microtask=mt,
                files=microtask_files,
                security_agent=deps.security_agent,
                tool_agents=deps.tool_agents,
                repo_path=repo_path,
                detail_callback=lambda d: _detail_cb(d, current_idx, "security_testing"),
            )

            if not sec_result.passed:
                logger.info(
                    "[%s] Microtask %s: Security testing failed with %d issues. Batch fixing and restarting from code review.",
                    task_id,
                    mt.id,
                    len(sec_result.issues),
                )

                if progress_callback:
                    progress_callback(
                        current_idx,
                        len(completed_ids),
                        total,
                        mt.title or mt.id,
                        "security_testing",
                        f"Batch fixing {len(sec_result.issues)} security issues...",
                    )

                ps_result = run_batch_coding_fixes(
                    llm=llm,
                    microtask=mt,
                    issues=sec_result.issues,
                    current_files=microtask_files,
                    language=planning_result.language,
                    repo_path=str(repo_path),
                    task_id=task_id,
                    phase_name="security",
                    detail_callback=lambda d: _detail_cb(d, current_idx, "security_testing"),
                )

                microtask_files = ps_result.files
                _write_microtask_files(repo_path, microtask_files)
                mt.output_files = microtask_files
                all_files.update(microtask_files)

                # Restart from code review
                continue

            # All review phases passed - proceed to documentation
            break

        # Check if we exceeded max cycles
        if total_cycles >= max_total_cycles and not phase_failed:
            # Check if we're still failing any phase
            if not cr_result.passed or not qa_result.passed or not sec_result.passed:
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
                if config.on_failure == "stop":
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
                min_iterations=3,
                max_iterations=5,
                quality_threshold=0.9,
                detail_callback=lambda d: _detail_cb(d, current_idx, "documentation"),
            )

            # Update files with refined documentation
            if self_review_result.documentation:
                microtask_files.update(self_review_result.documentation)
                _write_microtask_files(repo_path, self_review_result.documentation)
                mt.output_files = microtask_files
                all_files.update(self_review_result.documentation)

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
