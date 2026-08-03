"""Discovery-phase collaborator for run_orchestrator: resolves the spec source
(sprint_id / spec_content_override / newest-spec-on-disk) and runs (or skips) the
Product Requirements Analysis step.

Extracted from ``software_engineering_team/orchestrator.py`` (issue: decompose the
orchestrator god-file into named collaborators) — pure structural move, no behavior
change. The three previously-scattered branches on ``sprint_id``/
``spec_content_override``/default (spec source selection, the LLM spec-parse skip,
and the PRA skip) are consolidated here into two functions that ``run_orchestrator``
calls in sequence.

``update_job_fn`` is threaded through as an explicit parameter (the caller's own
``update_job`` reference), not imported here, so that a test's
``patch("orchestrator.update_job", ...)`` — which only intercepts calls made by code
still physically defined in orchestrator.py — continues to observe every write made
on this path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

from llm_service import OLLAMA_WEEKLY_LIMIT_MESSAGE, LLMRateLimitError, get_client
from shared.observability import current_trace_id
from software_engineering_team.shared.job_store import (
    JOB_STATUS_FAILED,
    JOB_STATUS_PAUSED_LLM_LIMIT,
)

# Extracted to shared/sprint_scope.py so a future V2 activity can call the same
# sprint-scope spec synthesis without duplicating the product_delivery-read
# logic; re-exported here under its original private name so resolve_spec_source
# (below) and orchestrator.py's existing re-export both keep working unchanged.
from software_engineering_team.shared.sprint_scope import (
    load_requirements_from_sprint as _load_requirements_from_sprint,
)

logger = logging.getLogger(__name__)


@dataclass
class SpecSourceResult:
    """Resolved Discovery output: the parsed requirements and the spec text/context
    behind them, ready for Design/Planning."""

    requirements: Any
    spec_content: str
    initial_spec_path: Optional[Path]
    context_files: List[Any] = field(default_factory=list)


def resolve_spec_source(
    job_id: str,
    path: Path,
    *,
    sprint_id: Optional[str],
    spec_content_override: Optional[str],
    update_job_fn: Callable[..., None],
) -> Optional[SpecSourceResult]:
    """Resolve the spec source and (unless ``sprint_id`` is set) parse it via the LLM.

    Consolidates the three former ``sprint_id``/``spec_content_override``/default
    branches: (1) spec source selection — a sprint's synthesized spec, an explicit
    override, or the newest spec on disk; (2) gathering repo context files
    (unconditional); (3) the LLM spec-parse, skipped when ``sprint_id`` is set (the
    sprint path's spec is already structured and validated).

    Preconditions:
        - ``sprint_id`` and ``spec_content_override`` are not both set (checked here;
          treated as a caller error, not silently resolved).
    Postconditions:
        - On success, returns a ``SpecSourceResult`` with ``requirements`` set (either
          synthesized from the sprint or parsed via the LLM).
        - On any failure, calls ``update_job_fn(job_id, status=..., error=..., ...)``
          with the exact original status/error/phase shape and returns ``None``. The
          caller's contract is ``result = resolve_spec_source(...); if result is None:
          return``.
    """
    from software_engineering_team.spec_parser import (
        gather_context_files,
        get_newest_spec_content,
        get_newest_spec_path,
        parse_spec_with_llm,
    )

    initial_spec_path = None
    requirements: Any = None
    # Sprint path: when sprint_id is set, the synthesized spec comes from the
    # product_delivery sprint's planned stories. Both the LLM spec-parse and the PRA
    # agent are skipped — the spec is already structured (per-story user_story + ACs)
    # and validated by the upstream Sprint Planner.
    if sprint_id is not None:
        from product_delivery import UnknownProductDeliveryEntity  # noqa: PLC0415

        if spec_content_override is not None:
            err = (
                "run_orchestrator received both sprint_id and spec_content_override; "
                "they are mutually exclusive."
            )
            logger.error(err, extra={"trace_id": current_trace_id()})
            update_job_fn(job_id, status=JOB_STATUS_FAILED, error=err, phase="completed")
            return None
        try:
            requirements, spec_content = _load_requirements_from_sprint(sprint_id)
        except UnknownProductDeliveryEntity as e:
            logger.error(
                "Sprint %s not found: %s", sprint_id, e, extra={"trace_id": current_trace_id()}
            )
            update_job_fn(
                job_id,
                status=JOB_STATUS_FAILED,
                error=f"Sprint scope load failed: {e}",
                phase="completed",
            )
            return None
        except ValueError as e:
            logger.error(
                "Sprint %s scope is empty: %s", sprint_id, e, extra={"trace_id": current_trace_id()}
            )
            update_job_fn(
                job_id,
                status=JOB_STATUS_FAILED,
                error=f"Sprint scope load failed: {e}",
                phase="completed",
            )
            return None
    elif spec_content_override is not None:
        spec_content = spec_content_override
    else:
        initial_spec_path = get_newest_spec_path(path)
        spec_content = get_newest_spec_content(path)

    # Gather all context files from the repo for PRA agent
    context_files = gather_context_files(path)
    if context_files:
        logger.info(
            "Gathered %d context files for PRA agent",
            len(context_files),
            extra={"trace_id": current_trace_id()},
        )

    if sprint_id is None:
        try:
            requirements = parse_spec_with_llm(spec_content, get_client("spec_intake"))
        except LLMRateLimitError:
            logger.warning(
                "Ollama LLM usage limit exceeded for week. Job %s paused.",
                job_id,
                extra={"trace_id": current_trace_id()},
            )
            update_job_fn(
                job_id, status=JOB_STATUS_PAUSED_LLM_LIMIT, error=OLLAMA_WEEKLY_LIMIT_MESSAGE
            )
            return None
        except Exception as e:
            logger.error(
                "Spec parsing failed (LLM unavailable or returned invalid output): %s",
                e,
                extra={"trace_id": current_trace_id()},
            )
            update_job_fn(
                job_id,
                status=JOB_STATUS_FAILED,
                error=f"Spec parsing failed: {e}",
                phase="completed",
            )
            return None

    return SpecSourceResult(
        requirements=requirements,
        spec_content=spec_content,
        initial_spec_path=initial_spec_path,
        context_files=context_files,
    )


def run_product_requirements_analysis(
    job_id: str,
    path: Path,
    source: SpecSourceResult,
    *,
    sprint_id: Optional[str],
    pra_job_updater: Callable[..., None],
    update_job_fn: Callable[..., None],
) -> Optional[str]:
    """Run (or skip) Product Requirements Analysis and return the validated spec content.

    Sprint path: PRA's review/communicate/update/cleanup loop has nothing to do (the
    spec is already structured and validated by the Sprint Planner), so the
    synthesized spec is used directly. Default path: runs
    ``ProductRequirementsAnalysisAgent.run_workflow``.

    Postconditions:
        - Returns the validated spec content on success.
        - On PRA failure, calls ``update_job_fn(job_id, status=JOB_STATUS_FAILED,
          error=..., phase="completed")`` and returns ``None`` — same caller contract
          as ``resolve_spec_source``.
    """
    if sprint_id is not None:
        logger.info(
            "Sprint %s: skipped Product Requirements Analysis; using synthesized spec",
            sprint_id,
            extra={"trace_id": current_trace_id()},
        )
        return source.spec_content

    # ── Product Requirements Analysis Agent ────────────────────────────────
    # Validates spec, asks user questions, produces validated_spec.md
    from software_engineering_team.product_requirements_analysis_agent import (
        ProductRequirementsAnalysisAgent,
    )

    update_job_fn(
        job_id,
        phase="product_analysis",
        message="Starting product requirements analysis...",
        status_text="Starting product requirements analysis",
    )
    logger.info(
        "Next step -> Running Product Requirements Analysis agent to validate spec and "
        "gather clarifications",
        extra={"trace_id": current_trace_id()},
    )
    pra_agent = ProductRequirementsAnalysisAgent(get_client("product_analysis"))
    pra_result = pra_agent.run_workflow(
        spec_content=source.spec_content,
        repo_path=path,
        job_id=job_id,
        job_updater=pra_job_updater,
        context_files=source.context_files,
        initial_spec_path=source.initial_spec_path,
    )
    if not pra_result.success:
        err = (
            pra_result.failure_reason
            or "Product Requirements Analysis did not complete successfully."
        )
        logger.error(
            "Product Requirements Analysis failed: %s", err, extra={"trace_id": current_trace_id()}
        )
        update_job_fn(job_id, status=JOB_STATUS_FAILED, error=err, phase="completed")
        return None

    logger.info(
        "Product Requirements Analysis complete: %d iterations, validated spec ready",
        pra_result.iterations,
        extra={"trace_id": current_trace_id()},
    )
    return pra_result.final_spec_content or source.spec_content
