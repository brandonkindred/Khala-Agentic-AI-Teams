"""
Brand-aligned blog writing pipeline with artifact persistence and gates.

Runs research -> planning -> draft -> copy-editor loop. When work_dir is provided,
persists artifacts and runs validators, fact-check, and compliance. On FAIL,
enters closed-loop rewrite until PASS or max_rewrite_iterations.

Supports job_updater callback for UI phase tracking.
"""

from . import _path_setup  # noqa: F401

import logging
import time
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Tuple, Union

from blog_compliance_agent import BlogComplianceAgent
from blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorInput
from blog_copy_editor_agent.models import FeedbackItem
from blog_draft_agent import BlogDraftAgent, DraftInput, ReviseDraftInput
from blog_fact_check_agent import BlogFactCheckAgent
from blog_research_agent.agent import ResearchAgent
from blog_research_agent.allowed_claims import extract_allowed_claims
from llm_service import get_client, OllamaLLMClient
from llm_service.interface import LLMClient
from blog_research_agent.models import ResearchBriefInput
from blog_publication_agent.models import PublishingPack
from blog_planning_agent import BlogPlanningAgent
from shared.artifacts import read_artifact, write_artifact
from shared.content_plan import (
    PlanningInput,
    PlanningPhaseResult,
    build_research_digest,
    content_plan_to_content_brief_markdown,
    content_plan_to_markdown_doc,
    content_plan_to_outline_markdown,
)
from shared.brand_spec import load_brand_spec_prompt
from shared.content_profile import (
    ContentProfile,
    LengthPolicy,
    SeriesContext,
    build_draft_length_instruction,
    build_planning_length_context,
    resolve_length_policy,
    series_context_block,
)
from shared.style_loader import load_style_file
from shared.errors import (
    BloggingError,
    ComplianceError,
    DraftError,
    FactCheckError,
    PlanningError,
    ResearchError,
)
from shared.models import BlogPhase, get_phase_progress
from shared.planning_config import planning_model_override
from validators.runner import run_validators_from_work_dir

logger = logging.getLogger(__name__)

_blogging_docs = Path(__file__).resolve().parent.parent / "docs"
STYLE_GUIDE_PATH = _blogging_docs / "writing_guidelines.md"
BRAND_SPEC_PROMPT_PATH = _blogging_docs / "brand_spec_prompt.md"
DRAFT_EDITOR_ITERATIONS = 100
MAX_REWRITE_ITERATIONS = 100

# Default model - use environment variable or this default
DEFAULT_MODEL = "qwen3.5:397b-cloud"

PipelineStatus = Literal["PASS", "FAIL", "NEEDS_HUMAN_REVIEW"]

# Type alias for job updater callback
JobUpdater = Callable[..., None]


def planning_llm_client(base: LLMClient) -> LLMClient:
    """Use BLOG_PLANNING_MODEL for planning when set (Ollama clients only)."""
    model = planning_model_override()
    if not model:
        return base
    if isinstance(base, OllamaLLMClient):
        return OllamaLLMClient(model=model, base_url=base.base_url, timeout=base.timeout)
    return base


def run_research_and_planning(
    brief: ResearchBriefInput,
    *,
    work_dir: Optional[Union[str, Path]],
    llm_client: OllamaLLMClient,
    length_policy: LengthPolicy,
    series_context: Optional[SeriesContext],
    job_updater: Optional[JobUpdater],
) -> Tuple[Any, str, PlanningPhaseResult]:
    """
    Shared Research → Planning steps for full pipeline and POST /research-and-review.

    Returns research agent result, compiled research_document markdown, and planning phase result.
    """

    def _update(
        phase: BlogPhase,
        sub_progress: float = 0.0,
        status_text: str = "",
        **kwargs: Any,
    ) -> None:
        if job_updater:
            try:
                progress = get_phase_progress(phase, sub_progress)
                job_updater(
                    phase=phase.value,
                    progress=progress,
                    status_text=status_text,
                    **kwargs,
                )
            except Exception as e:
                logger.warning("Failed to update job status: %s", e)

    def _research_progress(status_text: str, sub_progress: float) -> None:
        _update(BlogPhase.RESEARCH, sub_progress=sub_progress, status_text=status_text)

    try:
        research_agent = ResearchAgent(llm_client=llm_client)
        research_result = research_agent.run(brief, progress_callback=_research_progress)
    except BloggingError:
        raise
    except Exception as e:
        raise ResearchError(f"Research failed: {e}", cause=e) from e

    logger.info("Research complete: %s references", len(research_result.references))
    _update(
        BlogPhase.RESEARCH,
        sub_progress=1.0,
        status_text=f"Research complete: {len(research_result.references)} sources found",
        research_sources_count=len(research_result.references),
    )

    parts = ["## Sources\n"]
    for ref in research_result.references:
        parts.append(f"- **{ref.title}** ({ref.url}): {ref.summary}")
        if ref.key_points:
            parts.append("  Key points: " + "; ".join(ref.key_points))
    research_document = "\n".join(parts)

    if work_dir is not None:
        write_artifact(work_dir, "research_packet.md", research_document)
        logger.info("Persisted research_packet.md")
        try:
            allowed = extract_allowed_claims(
                llm_client,
                research_document,
                research_result.references,
                topic=brief.brief,
            )
            write_artifact(work_dir, "allowed_claims.json", allowed.to_dict())
            logger.info("Persisted allowed_claims.json (%s claims)", len(allowed.claims))
        except Exception as e:
            logger.warning("Could not extract allowed claims: %s", e)

    _update(
        BlogPhase.PLANNING,
        sub_progress=0.0,
        status_text="Generating content plan...",
    )

    research_digest = build_research_digest(research_document)
    planning_input = PlanningInput(
        brief=brief.brief,
        audience=brief.audience,
        tone_or_purpose=brief.tone_or_purpose,
        research_digest=research_digest,
        length_policy_context=build_planning_length_context(length_policy),
        series_context_block=series_context_block(series_context),
    )

    try:
        planning_agent = BlogPlanningAgent(llm_client=planning_llm_client(llm_client))
        planning_phase_result = planning_agent.run(
            planning_input,
            length_policy=length_policy,
            on_llm_request=lambda msg: _update(BlogPhase.PLANNING, status_text=msg),
        )
    except BloggingError:
        raise
    except Exception as e:
        raise PlanningError(f"Planning failed: {e}", cause=e) from e

    plan = planning_phase_result.content_plan
    logger.info(
        "Planning complete: %s iteration(s), %s title candidates",
        planning_phase_result.planning_iterations_used,
        len(plan.title_candidates),
    )
    _update(
        BlogPhase.PLANNING,
        sub_progress=1.0,
        status_text=(
            f"Planning complete ({planning_phase_result.planning_iterations_used} iteration(s), "
            f"{len(plan.title_candidates)} titles)"
        ),
        planning_iterations_used=planning_phase_result.planning_iterations_used,
        parse_retry_count=planning_phase_result.parse_retry_count,
        planning_wall_ms_total=planning_phase_result.planning_wall_ms_total,
    )

    if work_dir is not None:
        write_artifact(work_dir, "content_plan.json", plan.model_dump(mode="json"))
        write_artifact(work_dir, "content_plan.md", content_plan_to_markdown_doc(plan))
        write_artifact(work_dir, "outline.md", content_plan_to_outline_markdown(plan))
        write_artifact(work_dir, "content_brief.md", content_plan_to_content_brief_markdown(plan))
        logger.info("Persisted content_plan.json, content_plan.md, outline.md, content_brief.md")

    return research_result, research_document, planning_phase_result


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
    Run the full blog writing pipeline: research -> planning -> draft -> copy-editor loop.

    When work_dir is provided, persists artifacts. When run_gates is True (default when
    work_dir is set), runs validators, fact-check, and compliance. On FAIL, enters
    closed-loop rewrite until PASS or max_rewrite_iterations.

    Args:
        brief: The research brief input describing the blog topic.
        work_dir: Optional directory for artifact persistence.
        llm_client: Optional LLM client (defaults to qwen3.5:397b-cloud).
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
        Tuple of (research_result, planning_phase_result, draft_result, status).
        status is PASS, FAIL, or NEEDS_HUMAN_REVIEW.

    Raises:
        ResearchError: If research phase fails.
        PlanningError: If content planning fails.
        DraftError: If draft generation fails.
        ComplianceError: If compliance check fails unrecoverably.
        FactCheckError: If fact check fails unrecoverably.
    """
    
    def _update(
        phase: BlogPhase,
        sub_progress: float = 0.0,
        status_text: str = "",
        **kwargs: Any,
    ) -> None:
        """Update job status if job_updater is provided."""
        if job_updater:
            try:
                progress = get_phase_progress(phase, sub_progress)
                job_updater(
                    phase=phase.value,
                    progress=progress,
                    status_text=status_text,
                    **kwargs,
                )
            except Exception as e:
                logger.warning("Failed to update job status: %s", e)
    
    if llm_client is None:
        llm_client = get_client("blog")

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

    research_result, research_document, planning_phase_result = run_research_and_planning(
        brief,
        work_dir=work_dir,
        llm_client=llm_client,
        length_policy=length_policy,
        series_context=series_context,
        job_updater=job_updater,
    )
    plan = planning_phase_result.content_plan

    # ------------------------------------------------------------------
    # Title selection: pause until the author picks a title
    # ------------------------------------------------------------------
    selected_title: Optional[str] = None
    if job_id is not None and job_updater is not None:
        try:
            from shared.blog_job_store import (
                get_blog_job,
                is_waiting_for_title_selection,
                update_blog_job,
            )

            title_choices = [
                {"title": tc.title, "probability_of_success": tc.probability_of_success}
                for tc in plan.title_candidates
            ]
            update_blog_job(
                job_id,
                waiting_for_title_selection=True,
                title_choices=title_choices,
            )
            job_updater(
                phase="title_selection",
                progress=25,
                status_text=f"Waiting for title selection ({len(title_choices)} candidates)...",
            )

            while is_waiting_for_title_selection(job_id):
                job_data = get_blog_job(job_id)
                if job_data and job_data.get("status") in ("failed", "cancelled"):
                    return research_result, planning_phase_result, None, "FAIL"
                time.sleep(2)

            job_data = get_blog_job(job_id)
            selected_title = (job_data or {}).get("selected_title")
            logger.info("Title selected: %r", selected_title)
            job_updater(
                phase="title_selection",
                progress=26,
                status_text=f"Title selected: {selected_title}",
            )
        except Exception as e:
            logger.warning("Title selection phase error (skipping): %s", e)

    # ------------------------------------------------------------------
    # Story elicitation: ghost writer surfaces personal anecdotes
    # ------------------------------------------------------------------
    elicited_stories_text: Optional[str] = None
    if job_id is not None and job_updater is not None:
        try:
            from ghost_writer_agent import GhostWriterElicitationAgent
            from shared.blog_job_store import (
                add_story_agent_message,
                complete_story_elicitation,
                get_blog_job,
                is_waiting_for_story_input,
                update_blog_job,
            )

            ghost_agent = GhostWriterElicitationAgent(llm_client=llm_client)
            job_updater(
                phase="story_elicitation",
                progress=27,
                status_text="Identifying story opportunities in the content plan...",
            )
            story_gaps = ghost_agent.find_story_gaps(plan)

            if story_gaps:
                gap_dicts = [g.model_dump() for g in story_gaps]
                update_blog_job(job_id, story_gaps=gap_dicts, current_story_gap_index=0)
                collected_narratives: list[str] = []

                for idx, gap in enumerate(story_gaps):
                    job_data = get_blog_job(job_id)
                    if job_data and job_data.get("status") in ("failed", "cancelled"):
                        break

                    job_updater(
                        phase="story_elicitation",
                        progress=27 + idx,
                        status_text=f"Gathering story for section: {gap.section_title} ({idx + 1}/{len(story_gaps)})",
                    )
                    update_blog_job(job_id, current_story_gap_index=idx)

                    # Post seed question and wait for first user response
                    add_story_agent_message(job_id, gap.seed_question, idx)

                    result = ghost_agent.conduct_interview(
                        gap=gap,
                        job_id=job_id,
                        gap_index=idx,
                        job_updater=job_updater,
                    )
                    if result.narrative:
                        collected_narratives.append(
                            f"[Story for section: {gap.section_title}]\n{result.narrative}"
                        )

                if collected_narratives:
                    elicited_stories_text = "\n\n".join(collected_narratives)
                    complete_story_elicitation(job_id, elicited_stories=collected_narratives)

                update_blog_job(
                    job_id,
                    waiting_for_story_input=False,
                    current_story_gap_index=len(story_gaps),
                )
                job_updater(
                    phase="story_elicitation",
                    progress=30,
                    status_text=(
                        f"Story gathering complete: {len(collected_narratives)} narrative(s) collected"
                        if collected_narratives else "Story gathering complete (no stories collected)"
                    ),
                )
            else:
                job_updater(
                    phase="story_elicitation",
                    progress=30,
                    status_text="No personal story opportunities identified — proceeding to draft",
                )
        except Exception as e:
            logger.warning("Story elicitation phase error (skipping): %s", e)

    # Draft + Copy Editor loop (load style and brand spec as raw text for draft/editor agents)
    writing_style_content = load_style_file(STYLE_GUIDE_PATH, "writing style guide")
    brand_spec_content = load_style_file(BRAND_SPEC_PROMPT_PATH, "brand spec prompt")
    allowed_claims_data = (
        read_artifact(work_dir, "allowed_claims.json") if work_dir is not None else None
    )
    draft_agent = BlogDraftAgent(
        llm_client=llm_client,
        writing_style_guide_content=writing_style_content,
        brand_spec_content=brand_spec_content,
    )
    copy_editor_agent = BlogCopyEditorAgent(
        llm_client=llm_client,
        writing_style_guide_content=writing_style_content,
        brand_spec_content=brand_spec_content,
    )

    draft_result = None
    previous_feedback_items: list[FeedbackItem] = []
    for iteration in range(1, draft_editor_iterations + 1):
        if iteration == 1:
            # Initial draft
            _update(
                BlogPhase.DRAFT_INITIAL,
                sub_progress=0.0,
                status_text="Generating initial draft...",
                draft_iterations=iteration,
            )

            try:
                draft_input = DraftInput(
                    research_document=research_document,
                    research_references=research_result.references if research_result.references else None,
                    content_plan=plan,
                    audience=brief.audience,
                    tone_or_purpose=brief.tone_or_purpose,
                    allowed_claims=allowed_claims_data if isinstance(allowed_claims_data, dict) else None,
                    target_word_count=length_policy.target_word_count,
                    length_guidance=build_draft_length_instruction(length_policy),
                    selected_title=selected_title or None,
                    elicited_stories=elicited_stories_text or None,
                )
                draft_output_path = (Path(work_dir) / f"draft_v{iteration}.md") if work_dir is not None else None
                draft_result = draft_agent.run(
                    draft_input,
                    on_llm_request=lambda msg: _update(BlogPhase.DRAFT_INITIAL, status_text=msg),
                    draft_output_path=draft_output_path,
                )
            except BloggingError:
                raise
            except Exception as e:
                raise DraftError(f"Initial draft generation failed: {e}", iteration=iteration, cause=e) from e

            logger.info("Draft iteration %s: initial draft, length=%s", iteration, len(draft_result.draft))
            _update(
                BlogPhase.DRAFT_INITIAL,
                sub_progress=1.0,
                status_text=f"Initial draft complete ({len(draft_result.draft)} chars)",
                draft_iterations=iteration,
            )
        else:
            # Copy edit loop
            copy_edit_num = iteration - 1
            sub_progress = copy_edit_num / draft_editor_iterations
            _update(
                BlogPhase.COPY_EDIT_LOOP,
                sub_progress=sub_progress,
                status_text=f"Copy edit iteration {copy_edit_num}/{draft_editor_iterations - 1}...",
                draft_iterations=iteration,
            )

            try:
                copy_editor_input = CopyEditorInput(
                    draft=draft_result.draft,
                    audience=brief.audience,
                    tone_or_purpose=brief.tone_or_purpose,
                    previous_feedback_items=previous_feedback_items if previous_feedback_items else None,
                    target_word_count=length_policy.target_word_count,
                    length_guidance=length_policy.length_guidance,
                    soft_min_words=length_policy.soft_min_words,
                    soft_max_words=length_policy.soft_max_words,
                    editor_must_fix_over_ratio=length_policy.editor_must_fix_over_ratio,
                    editor_should_fix_over_ratio=length_policy.editor_should_fix_over_ratio,
                    content_profile=length_policy.content_profile.value,
                    content_plan_context=content_plan_to_outline_markdown(plan),
                )
                feedback_path = (Path(work_dir) / f"editor_feedback_iter_{copy_edit_num}.json") if work_dir is not None else None
                copy_editor_result = copy_editor_agent.run(
                    copy_editor_input,
                    on_llm_request=lambda msg: _update(BlogPhase.COPY_EDIT_LOOP, status_text=msg),
                    feedback_output_path=feedback_path,
                )
                logger.info(
                    "Copy editor iteration %s: approved=%s, %s feedback items",
                    copy_edit_num,
                    copy_editor_result.approved,
                    len(copy_editor_result.feedback_items),
                )

                if copy_editor_result.approved:
                    logger.info("Copy editor approved draft at iteration %s, stopping loop.", copy_edit_num)
                    _update(
                        BlogPhase.COPY_EDIT_LOOP,
                        sub_progress=1.0,
                        status_text=f"Draft approved by editor after {copy_edit_num} pass(es)",
                        draft_iterations=iteration,
                    )
                    break

                revise_input = ReviseDraftInput(
                    draft=draft_result.draft,
                    feedback_items=copy_editor_result.feedback_items,
                    feedback_summary=copy_editor_result.summary,
                    previous_feedback_items=previous_feedback_items if previous_feedback_items else None,
                    research_document=research_document,
                    content_plan=plan,
                    audience=brief.audience,
                    tone_or_purpose=brief.tone_or_purpose,
                    allowed_claims=allowed_claims_data if isinstance(allowed_claims_data, dict) else None,
                    target_word_count=length_policy.target_word_count,
                    length_guidance=build_draft_length_instruction(length_policy),
                    selected_title=selected_title or None,
                    elicited_stories=elicited_stories_text or None,
                )
                previous_feedback_items = copy_editor_result.feedback_items
                draft_output_path = (Path(work_dir) / f"draft_v{iteration}.md") if work_dir is not None else None
                draft_result = draft_agent.revise(
                    revise_input,
                    on_llm_request=lambda msg: _update(BlogPhase.COPY_EDIT_LOOP, status_text=msg),
                    draft_output_path=draft_output_path,
                )
            except BloggingError:
                raise
            except Exception as e:
                raise DraftError(f"Draft revision failed: {e}", iteration=iteration, cause=e) from e

            logger.info("Draft iteration %s: revised, length=%s", iteration, len(draft_result.draft))
    else:
        _update(
            BlogPhase.COPY_EDIT_LOOP,
            sub_progress=1.0,
            status_text=f"Draft editing complete after {draft_editor_iterations} iteration(s)",
            draft_iterations=draft_editor_iterations,
        )

    status: PipelineStatus = "PASS"
    if work_dir is not None:
        write_artifact(work_dir, "final.md", draft_result.draft)
        logger.info("Persisted final.md")
        
    if work_dir is not None and run_gates:
        brand_spec_prompt_text = load_brand_spec_prompt(BRAND_SPEC_PROMPT_PATH)
        compliance_agent = BlogComplianceAgent(llm_client=llm_client)
        fact_check_agent = BlogFactCheckAgent(llm_client=llm_client)
        require_disclaimer_for = ["medical", "legal", "financial"]

        for rewrite_iter in range(max_rewrite_iterations):
            # Fact check phase
            _update(
                BlogPhase.FACT_CHECK,
                sub_progress=rewrite_iter / max_rewrite_iterations,
                status_text=f"Running fact check (iteration {rewrite_iter + 1})...",
                rewrite_iterations=rewrite_iter,
            )
            
            try:
                validator_report = run_validators_from_work_dir(work_dir)
                fact_report = fact_check_agent.run(
                    draft_result.draft,
                    allowed_claims=allowed_claims_data if isinstance(allowed_claims_data, dict) else None,
                    require_disclaimer_for=require_disclaimer_for,
                    work_dir=work_dir,
                    on_llm_request=lambda msg: _update(BlogPhase.FACT_CHECK, status_text=msg),
                )
            except BloggingError:
                raise
            except Exception as e:
                raise FactCheckError(f"Fact check failed: {e}", cause=e) from e
            
            # Compliance phase
            _update(
                BlogPhase.COMPLIANCE,
                sub_progress=rewrite_iter / max_rewrite_iterations,
                status_text=f"Running compliance check (iteration {rewrite_iter + 1})...",
                rewrite_iterations=rewrite_iter,
            )
            
            try:
                compliance_report = compliance_agent.run(
                    draft_result.draft,
                    brand_spec_prompt=brand_spec_prompt_text,
                    validator_report=validator_report.model_dump() if hasattr(validator_report, "model_dump") else None,
                    work_dir=work_dir,
                    on_llm_request=lambda msg: _update(BlogPhase.COMPLIANCE, status_text=msg),
                )
            except BloggingError:
                raise
            except Exception as e:
                raise ComplianceError(f"Compliance check failed: {e}", cause=e) from e

            all_pass = (
                validator_report.status == "PASS"
                and fact_report.claims_status == "PASS"
                and fact_report.risk_status == "PASS"
                and compliance_report.status == "PASS"
            )
            if all_pass:
                status = "PASS"
                logger.info("All gates PASS on rewrite iteration %s", rewrite_iter + 1)
                
                _update(
                    BlogPhase.FINALIZE,
                    sub_progress=0.5,
                    status_text="All checks passed, finalizing...",
                )
                
                pack = PublishingPack(
                    title_options=[tc.title for tc in plan.title_candidates[:5]],
                    meta_description=draft_result.draft[:155].strip() or None,
                    tags=[],
                )
                write_artifact(work_dir, "publishing_pack.json", pack.model_dump())
                logger.info("Wrote publishing_pack.json")
                
                _update(
                    BlogPhase.FINALIZE,
                    sub_progress=1.0,
                    status_text="Pipeline complete - all checks passed",
                )
                break

            if rewrite_iter >= max_rewrite_iterations - 1:
                status = "NEEDS_HUMAN_REVIEW"
                logger.warning(
                    "Max rewrite iterations (%s) reached; status=NEEDS_HUMAN_REVIEW",
                    max_rewrite_iterations,
                )
                _update(
                    BlogPhase.FINALIZE,
                    sub_progress=1.0,
                    status_text=f"Needs human review after {max_rewrite_iterations} rewrite attempts",
                )
                break

            # Rewrite loop
            _update(
                BlogPhase.REWRITE_LOOP,
                sub_progress=(rewrite_iter + 1) / max_rewrite_iterations,
                status_text=f"Rewriting to address issues (iteration {rewrite_iter + 1}/{max_rewrite_iterations})...",
                rewrite_iterations=rewrite_iter + 1,
            )
            
            feedback_items = [
                FeedbackItem(
                    category="compliance",
                    severity="must_fix",
                    location=None,
                    issue=fix,
                    suggestion=fix,
                )
                for fix in compliance_report.required_fixes
            ]
            if not feedback_items:
                feedback_items = [
                    FeedbackItem(
                        category="compliance",
                        severity="must_fix",
                        location=None,
                        issue="Validator or compliance check failed; see validator_report.json and compliance_report.json",
                        suggestion="Address all violations and re-run.",
                    )
                ]
            
            try:
                revise_input = ReviseDraftInput(
                    draft=draft_result.draft,
                    feedback_items=feedback_items,
                    feedback_summary=f"Compliance FAIL: {len(compliance_report.violations)} violations. Apply required_fixes.",
                    research_document=research_document,
                    content_plan=plan,
                    audience=brief.audience,
                    tone_or_purpose=brief.tone_or_purpose,
                    allowed_claims=allowed_claims_data if isinstance(allowed_claims_data, dict) else None,
                    target_word_count=length_policy.target_word_count,
                    length_guidance=build_draft_length_instruction(length_policy),
                    selected_title=selected_title or None,
                    elicited_stories=elicited_stories_text or None,
                )
                draft_output_path = Path(work_dir) / f"draft_rewrite_{rewrite_iter + 1}.md"
                draft_result = draft_agent.revise(
                    revise_input,
                    on_llm_request=lambda msg: _update(BlogPhase.REWRITE_LOOP, status_text=msg),
                    draft_output_path=draft_output_path,
                )
            except BloggingError:
                raise
            except Exception as e:
                raise DraftError(f"Rewrite revision failed: {e}", iteration=rewrite_iter + 1, cause=e) from e

            write_artifact(work_dir, "final.md", draft_result.draft)
            logger.info("Rewrite iteration %s: applied fixes, re-running gates", rewrite_iter + 1)
    else:
        # No gates - mark as finalized
        _update(
            BlogPhase.FINALIZE,
            sub_progress=1.0,
            status_text="Pipeline complete (gates skipped)",
        )

    return research_result, planning_phase_result, draft_result, status


def main() -> None:
    """CLI entrypoint: run pipeline with optional work_dir."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    brief = ResearchBriefInput(
        brief="LLM observability best practices for large enterprises",
        audience="CTOs and platform teams",
        tone_or_purpose="technical deep-dive",
        max_results=20,
    )

    work_dir = Path(__file__).resolve().parent / "run_dir"
    research_result, planning_phase_result, draft_result, status = run_pipeline(brief, work_dir=work_dir)
    plan = planning_phase_result.content_plan

    print("\n--- Title choices ---")
    for i, tc in enumerate(plan.title_candidates, 1):
        print(f"{i}. {tc.title}  [{tc.probability_of_success:.0%}]")
    print("\n--- Outline ---\n")
    print(content_plan_to_outline_markdown(plan))
    print("\n--- Draft ---\n")
    print(draft_result.draft[:2000] + ("..." if len(draft_result.draft) > 2000 else ""))
    print(f"\nStatus: {status}")
    print(f"Artifacts written to {work_dir}")


if __name__ == "__main__":
    main()
