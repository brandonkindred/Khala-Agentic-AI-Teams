"""Thin thread-mode sequencer over the three pipeline stages, plus the CLI entrypoint."""

import logging
from pathlib import Path
from typing import Optional, Union

from agents.blogging.blog_research_agent.models import ResearchBriefInput
from agents.blogging.shared.content_plan import content_plan_to_outline_markdown
from agents.blogging.shared.content_profile import (
    ContentProfile,
    LengthPolicy,
    SeriesContext,
    resolve_length_policy,
)

from llm_service import OllamaLLMClient, get_strands_model

from .constants import DRAFT_EDITOR_ITERATIONS, MAX_REWRITE_ITERATIONS
from .context import JobUpdater, PipelineContext

logger = logging.getLogger(__name__)


def run_pipeline(
    brief: ResearchBriefInput,
    *,
    work_dir: Optional[Union[str, Path]] = None,
    llm_client: Optional[OllamaLLMClient] = None,
    draft_editor_iterations: int = DRAFT_EDITOR_ITERATIONS,
    max_rewrite_iterations: int = MAX_REWRITE_ITERATIONS,
    run_gates: bool = True,
    job_updater: Optional[JobUpdater] = None,
    job_id: Optional[str] = None,
    length_policy: Optional[LengthPolicy] = None,
    content_profile: Optional[ContentProfile] = None,
    series_context: Optional[SeriesContext] = None,
    length_notes: Optional[str] = None,
    target_word_count: Optional[int] = None,
):
    """
    Run the full blog writing pipeline: planning -> draft -> copy-editor loop.

    When work_dir is provided, persists artifacts. When run_gates is True (default when
    work_dir is set), runs validators, fact-check, and compliance. On FAIL, enters
    closed-loop rewrite until PASS or max_rewrite_iterations.

    Internally the pipeline is decomposed into three stages — ``run_planning_stage``,
    ``run_draft_stage``, ``run_gates_stage`` — that operate on a shared
    ``PipelineContext``. This function is a thin thread-mode sequencer over them;
    the same stage functions run as independent Temporal activities when orchestrated
    by ``BlogFullPipelineWorkflow``. The signature and return contract are unchanged.

    Preconditions:
        - ``brief`` is a valid ``ResearchBriefInput``.
        - ``llm_client``/``length_policy`` may be None; each is resolved here before
          the shared ``PipelineContext`` is built (default Strands model; policy
          derived from content_profile/series_context/length_notes/target_word_count).
    Postconditions:
        - Runs the three stages in order over one ``PipelineContext`` and returns
          ``(planning_phase_result, draft_result, status)`` (see Returns).
        - Short-circuits and forwards a stage's abort result unchanged when a stage
          aborts (planning/draft) — the later stages do not run.
    Invariants:
        - Each stage's preconditions are met by the previous stage's postconditions:
          planning populates ``plan``/``planning_phase_result`` before draft reads
          them; draft populates ``draft_result`` before gates reads it. The
          ``PipelineContext`` is the single shared carrier of that state.

    Args:
        brief: The research brief input describing the blog topic.
        work_dir: Optional directory for artifact persistence.
        llm_client: Optional LLM client (defaults to the resolved "blog" model
            via get_strands_model("blog")).
        draft_editor_iterations: Number of draft/copy-edit iterations.
        max_rewrite_iterations: Max compliance rewrite attempts.
        run_gates: Whether to run validators/compliance gates.
        job_updater: Optional callback for UI phase tracking updates.
            Called with (phase, progress, status_text, **kwargs).
        length_policy: Pre-resolved length/format policy. When omitted, built from
            content_profile, series_context, length_notes, and optional target_word_count.
        content_profile: Semantic writing format (used if length_policy not passed).
        series_context: Optional series instalment scope.
        length_notes: Optional author notes merged into length guidance.
        target_word_count: Optional override for numeric target (100–10_000).

    Returns:
        Tuple of (planning_phase_result, draft_result, status).
        status is PASS, FAIL, or NEEDS_HUMAN_REVIEW. On an abort during planning,
        draft_result is None and status is FAIL (the planning stage returns its
        abort tuple, which this sequencer forwards unchanged).

    Raises:
        PlanningError: If content planning fails.
        DraftError: If draft generation fails.
        ComplianceError: If compliance check fails unrecoverably.
        FactCheckError: If fact check fails unrecoverably.
    """
    # Deferred import: see agents.blogging.agent_implementations.pipeline._common's
    # module docstring — keeps monkeypatch.setattr(shim, "run_planning_stage", ...) /
    # ("run_draft_stage", ...) / ("run_gates_stage", ...) effective now that this
    # sequencer lives outside the shim.
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        run_draft_stage,
        run_gates_stage,
        run_planning_stage,
    )

    if llm_client is None:
        llm_client = get_strands_model("blog")

    if length_policy is None:
        length_policy = resolve_length_policy(
            content_profile=content_profile,
            explicit_target_word_count=target_word_count,
            length_notes=length_notes,
            series_context=series_context,
        )

    if work_dir is not None:
        work_path = Path(work_dir).resolve()
        work_path.mkdir(parents=True, exist_ok=True)
        logger.info("Artifact work_dir: %s", work_path)

    ctx = PipelineContext(
        brief=brief,
        work_dir=work_dir,
        llm_client=llm_client,
        length_policy=length_policy,
        series_context=series_context,
        job_id=job_id,
        job_updater=job_updater,
        draft_editor_iterations=draft_editor_iterations,
        max_rewrite_iterations=max_rewrite_iterations,
        run_gates=run_gates,
    )

    planning_abort = run_planning_stage(ctx)
    if planning_abort is not None:
        return planning_abort
    draft_abort = run_draft_stage(ctx)
    if draft_abort is not None:
        return draft_abort
    run_gates_stage(ctx)
    return ctx.planning_phase_result, ctx.draft_result, ctx.status


def main() -> None:  # pragma: no cover - manual CLI demo; drives a live LLM end-to-end.
    """CLI entrypoint: run pipeline with optional work_dir."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    brief = ResearchBriefInput(
        brief="LLM observability best practices for large enterprises",
        audience="CTOs and platform teams",
        tone_or_purpose="technical deep-dive",
        max_results=20,
    )

    work_dir = Path(__file__).resolve().parent.parent / "run_dir"
    planning_phase_result, draft_result, status = run_pipeline(brief, work_dir=work_dir)
    plan = planning_phase_result.content_plan

    print("\n--- Title choices ---")
    for i, tc in enumerate(plan.title_candidates, 1):
        print(f"{i}. {tc.title}  [{tc.probability_of_success:.0%}]")
    print("\n--- Outline ---\n")
    print(content_plan_to_outline_markdown(plan))
    print("\n--- Draft ---\n")
    print(draft_result.draft)
    print(f"\nStatus: {status}")
    print(f"Artifacts written to {work_dir}")


if __name__ == "__main__":
    main()
