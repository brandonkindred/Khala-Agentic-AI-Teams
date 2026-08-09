"""Documentation self-review phase for the gated execution loop in ``execution.py``.

The gated per-microtask loop (``run_gated_execution_impl``) runs a documentation
self-review as its final phase, once a microtask has passed the code review / QA /
security gate cycles. This module holds that phase (``_run_documentation_phase``)
and its tuning constants — factored out of ``execution.py`` so it can be read and
tested independently of the coding/review phases that precede it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from llm_service import LLMClient
from shared.dev_models.models import Task
from software_engineering_team.shared.repo_writer import (
    UnsafeRepoPathError,
    write_repo_text_files,
)

if TYPE_CHECKING:
    from software_engineering_team.shared.phases.execution import (
        GatedExecutionConfig,
        ReviewDependencies,
    )

logger = logging.getLogger(__name__)

# Iteration budget for the gated loop's own final documentation self-review pass.
# Deliberately its own (lower) constants rather than reusing
# review_utils.MIN/MAX_DOC_SELF_REVIEW_ITERATIONS (3/3): this pass runs once per
# microtask on top of the code/QA/security review cycles already spent, so a
# smaller budget here is intentional, not an oversight -- kept separate so tuning
# one never silently changes the other.
_GATED_DOC_SELF_REVIEW_MIN_ITERS = 1
_GATED_DOC_SELF_REVIEW_MAX_ITERS = 2
_GATED_DOC_SELF_REVIEW_QUALITY_THRESHOLD = 0.9


def _run_documentation_phase(
    *,
    gate_config: "GatedExecutionConfig",
    llm: LLMClient,
    task: Task,
    task_id: str,
    mt: Any,
    microtask_files: Dict[str, str],
    repo_path: Path,
    deps: "ReviewDependencies",
    tool_agent_kind: Any,
    all_files: Dict[str, str],
    microtask_status: Any,
    completed_ids: set,
    total_cycles: int,
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]],
    current_idx: int,
    total: int,
    detail_cb: Callable[[str, int, str], None],
) -> None:
    """Run a microtask's documentation self-review phase (Phase 5, never fails).

    Split out of :func:`run_gated_execution_impl`; called only once the review-
    gate cycles (Phases 2-4) have not failed for ``mt``.

    Preconditions:
        ``microtask_files`` reflects the last review-gate-accepted write.
    Postconditions:
        ``mt.status`` becomes ``COMPLETED`` and ``mt.id`` is added to
        ``completed_ids``. ``microtask_files``, ``all_files``, and
        ``mt.output_files`` gain any refined documentation the self-review
        produced. A documentation-agent exception or an unsafe documentation
        write path is logged and skipped rather than propagated — this phase
        never fails the microtask.
    """
    mt.status = microtask_status.IN_DOCUMENTATION
    logger.info(
        "[%s] Microtask %s: Running documentation self-review (%d-%d iterations)",
        task_id,
        mt.id,
        _GATED_DOC_SELF_REVIEW_MIN_ITERS,
        _GATED_DOC_SELF_REVIEW_MAX_ITERS,
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
    doc_agent = deps.tool_agents.get(tool_agent_kind.DOCUMENTATION) if deps.tool_agents else None
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

    # Run self-review iterations (capped to avoid excessive LLM calls)
    self_review_result = gate_config.run_documentation_self_review(
        llm=llm,
        documentation=doc_files,
        code_files=microtask_files,
        task_description=task.description or "",
        min_iterations=_GATED_DOC_SELF_REVIEW_MIN_ITERS,
        max_iterations=_GATED_DOC_SELF_REVIEW_MAX_ITERS,
        quality_threshold=_GATED_DOC_SELF_REVIEW_QUALITY_THRESHOLD,
        detail_callback=lambda d: detail_cb(d, current_idx, "documentation"),
    )

    # Update files with refined documentation. A rejected (unsafe) doc
    # path is best-effort: log and skip it — the microtask still completes.
    if self_review_result.documentation:
        try:
            write_repo_text_files(repo_path, self_review_result.documentation)
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

    mt.status = microtask_status.COMPLETED
    completed_ids.add(mt.id)
    logger.info(
        "[%s] Microtask %s: COMPLETED (passed all review phases in %d cycles)",
        task_id,
        mt.id,
        total_cycles,
    )
