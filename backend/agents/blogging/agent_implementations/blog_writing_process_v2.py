"""
Brand-aligned blog writing pipeline with artifact persistence and gates.

Runs research -> planning -> draft -> copy-editor loop. When work_dir is provided,
persists artifacts and runs validators, fact-check, and compliance. On FAIL,
enters closed-loop rewrite until PASS or max_rewrite_iterations.

Supports job_updater callback for UI phase tracking.
"""

import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, List, Literal, Optional, Tuple, Union

from blog_compliance_agent import BlogComplianceAgent
from blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorInput
from blog_copy_editor_agent.models import FeedbackItem
from blog_draft_agent import BlogDraftAgent, DraftInput, ReviseDraftInput
from blog_fact_check_agent import BlogFactCheckAgent
from blog_planning_agent import BlogPlanningAgent
from blog_publication_agent.models import PublishingPack
from blog_research_agent.agent import ResearchAgent
from blog_research_agent.allowed_claims import extract_allowed_claims
from blog_research_agent.models import ResearchBriefInput
from shared.artifacts import read_artifact, write_artifact
from shared.brand_spec import load_brand_spec_prompt
from shared.content_plan import (
    PlanningInput,
    PlanningPhaseResult,
    build_research_digest,
    content_plan_to_content_brief_markdown,
    content_plan_to_markdown_doc,
    content_plan_to_outline_markdown,
)
from shared.content_profile import (
    ContentProfile,
    LengthPolicy,
    SeriesContext,
    build_draft_length_instruction,
    build_planning_length_context,
    resolve_length_policy,
    series_context_block,
)
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
from shared.style_loader import load_style_file
from temporalio.exceptions import CancelledError
from validators.runner import run_validators_from_work_dir

from llm_service import OllamaLLMClient, get_client
from llm_service.interface import LLMClient

from . import _path_setup  # noqa: F401

logger = logging.getLogger(__name__)

_blogging_docs = Path(__file__).resolve().parent.parent / "docs"
STYLE_GUIDE_PATH = _blogging_docs / "writing_guidelines.md"
BRAND_SPEC_PROMPT_PATH = _blogging_docs / "brand_spec_prompt.md"
DRAFT_EDITOR_ITERATIONS = 500
MAX_REWRITE_ITERATIONS = 100

# Default model - use environment variable or this default
DEFAULT_MODEL = "qwen3.5:397b-cloud"

PipelineStatus = Literal["PASS", "FAIL", "NEEDS_HUMAN_REVIEW"]

# Type alias for job updater callback
JobUpdater = Callable[..., None]


def _is_external_cancellation(exc: BaseException) -> bool:
    """True when exception chain indicates runtime cancellation (e.g., Temporal)."""
    cur: Optional[BaseException] = exc
    for _ in range(8):
        if cur is None:
            break
        cls = cur.__class__
        if cls.__name__ == "CancelledError":
            module = getattr(cls, "__module__", "")
            if module.startswith("temporalio"):
                return True
        cur = cur.__cause__ or cur.__context__
    return False


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
            except CancelledError:
                raise
            except Exception as e:
                logger.warning("Failed to update job status: %s", e)

    def _research_progress(status_text: str, sub_progress: float) -> None:
        _update(BlogPhase.RESEARCH, sub_progress=sub_progress, status_text=status_text)

    try:
        research_agent = ResearchAgent(llm_client=llm_client)
        research_result = research_agent.run(brief, progress_callback=_research_progress)
    except BloggingError:
        raise
    except CancelledError:
        raise
    except Exception as e:
        if _is_external_cancellation(e):
            raise
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
        except CancelledError:
            raise
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
        if _is_external_cancellation(e):
            raise
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


def _extract_plan_keywords(plan: Any) -> list[str]:
    """Extract searchable keywords from a content plan for story bank queries.

    Combines the overarching topic and section titles, splits on whitespace,
    and filters to words >= 4 chars to avoid noise from short stopwords.
    """
    parts: list[str] = []
    topic = getattr(plan, "overarching_topic", "") or ""
    parts.extend(topic.lower().split())
    for section in getattr(plan, "sections", []) or []:
        title = getattr(section, "title", "") or ""
        parts.extend(title.lower().split())
    # Deduplicate and filter short words (stopwords like "the", "and", "for")
    seen: set[str] = set()
    keywords: list[str] = []
    for word in parts:
        cleaned = word.strip(".,;:!?()[]\"'")
        if len(cleaned) >= 4 and cleaned not in seen:
            seen.add(cleaned)
            keywords.append(cleaned)
    return keywords


# Regex matching [Author: ...] placeholders in draft output.
_PLACEHOLDER_RE = re.compile(
    r"\[Author:\s*(?:add\s+)?(.+?)\]",
    re.IGNORECASE,
)


def _extract_story_placeholders(draft_text: str) -> List[Tuple[str, str]]:
    """Return (full_match, topic_description) pairs for each ``[Author: ...]`` placeholder."""
    results = []
    for m in _PLACEHOLDER_RE.finditer(draft_text):
        results.append((m.group(0), m.group(1).strip()))
    return results


def _fill_story_placeholders(
    *,
    draft_text: str,
    plan: Any,
    llm_client: Any,
    job_id: str,
    job_updater: Callable,
    elicited_stories_text: Optional[str],
    draft_agent: Any,
    draft_input_kwargs: dict,
    work_dir: Optional[Union[str, Path]],
    iteration: int,
) -> Tuple[Any, Optional[str]]:
    """Scan draft for ``[Author: ...]`` placeholders and interview the user for each.

    For each placeholder the ghost writer conducts an interview.  If the user
    indicates they have no relevant experience the placeholder is removed and
    the section is rewritten without a personal story.  Otherwise the collected
    narrative replaces the placeholder.

    Returns ``(updated_draft_result, updated_elicited_stories_text)``.
    """
    from blog_draft_agent.models import DraftInput, DraftOutput
    from ghost_writer_agent import GhostWriterElicitationAgent
    from ghost_writer_agent.agent import MAX_ROUNDS_POST_DRAFT
    from ghost_writer_agent.models import StoryGap
    from shared.blog_job_store import (
        add_story_agent_message,
        get_blog_job,
        update_blog_job,
    )

    placeholders = _extract_story_placeholders(draft_text)
    if not placeholders:
        return DraftOutput(draft=draft_text), elicited_stories_text

    logger.info("Post-draft: found %d story placeholder(s) to fill", len(placeholders))
    job_updater(
        phase="story_elicitation",
        progress=35,
        status_text=f"Draft has {len(placeholders)} story placeholder(s) — waiting for your stories...",
    )

    ghost_agent = GhostWriterElicitationAgent(llm_client=llm_client)
    new_narratives: list[str] = []
    skipped_topics: list[str] = []

    # Build story gaps from placeholders
    gaps = []
    for _full_match, topic in placeholders:
        gaps.append(
            StoryGap(
                section_title=topic[:80],
                section_context=f"The draft needs a personal story about: {topic}",
                seed_question=(
                    f"The blog post needs a personal story about: {topic}\n\n"
                    "Walk me through a specific time you experienced this. "
                    "I'll ask follow-up questions until we have a complete story "
                    "with real numbers.\n\n"
                    "If you don't have any relevant experience with this topic, "
                    'just reply "skip" or "no experience" and we\'ll rewrite that '
                    "section without a personal story."
                ),
            )
        )

    # Store gaps in the job and run interviews
    gap_dicts = [g.model_dump() for g in gaps]
    update_blog_job(
        job_id,
        story_gaps=gap_dicts,
        current_story_gap_index=0,
        waiting_for_story_input=False,
    )

    for idx, gap in enumerate(gaps):
        job_data = get_blog_job(job_id)
        if job_data and job_data.get("status") in ("failed", "cancelled"):
            break

        job_updater(
            phase="story_elicitation",
            progress=35 + idx,
            status_text=f"Waiting for your story {idx + 1}/{len(gaps)}: {gap.section_title}",
        )
        update_blog_job(job_id, current_story_gap_index=idx)

        # Post seed question — pipeline pauses here until user responds
        add_story_agent_message(job_id, gap.seed_question, idx)

        # conduct_interview waits indefinitely for each user response
        result = ghost_agent.conduct_interview(
            gap=gap,
            job_id=job_id,
            gap_index=idx,
            job_updater=job_updater,
            max_rounds=MAX_ROUNDS_POST_DRAFT,
        )

        if result.skipped:
            skipped_topics.append(gap.section_title)
            logger.info("Post-draft: user has no experience for '%s'", gap.section_title)
        elif result.narrative:
            new_narratives.append(
                f"[Story for section: {gap.section_title}]\n{result.narrative}"
            )
            # Save to story bank for reuse across future posts
            try:
                from shared.story_bank import save_story

                save_story(
                    narrative=result.narrative,
                    section_title=gap.section_title,
                    section_context=gap.section_context,
                    keywords=_extract_plan_keywords(plan),
                    source_job_id=job_id,
                )
            except Exception as e:
                logger.warning("Story bank save failed (non-fatal): %s", e)
        else:
            # No narrative and not skipped — treat as no usable material
            skipped_topics.append(gap.section_title)

    update_blog_job(
        job_id,
        waiting_for_story_input=False,
        current_story_gap_index=len(gaps),
    )

    if not new_narratives and not skipped_topics:
        return DraftOutput(draft=draft_text), elicited_stories_text

    # Merge new narratives into elicited_stories_text
    if new_narratives:
        new_text = "\n\n".join(new_narratives)
        if elicited_stories_text:
            elicited_stories_text = elicited_stories_text + "\n\n" + new_text
        else:
            elicited_stories_text = new_text

    # Re-draft with the updated stories and skip instructions
    job_updater(
        phase="draft_initial",
        progress=40,
        status_text="Re-drafting with your stories and removing unsupported story sections...",
    )

    skip_instruction = ""
    if skipped_topics:
        skip_list = "; ".join(skipped_topics)
        skip_instruction = (
            f"\n\nSECTIONS WHERE THE AUTHOR HAS NO PERSONAL EXPERIENCE (rewrite these "
            f"sections using research facts, labeled hypotheticals, or straight explanation "
            f"instead of personal stories — remove any [Author: ...] placeholders): {skip_list}"
        )

    try:
        draft_input = DraftInput(
            **draft_input_kwargs,
            elicited_stories=(elicited_stories_text or "") + skip_instruction or None,
        )
        draft_output_path = (Path(work_dir) / f"draft_v{iteration}.md") if work_dir is not None else None
        redraft_result = draft_agent.run(
            draft_input,
            on_llm_request=lambda msg: job_updater(phase="draft_initial", status_text=msg),
            draft_output_path=draft_output_path,
        )
        logger.info(
            "Post-draft re-draft complete: %d new stories, %d skipped topics, length=%s",
            len(new_narratives),
            len(skipped_topics),
            len(redraft_result.draft),
        )
        return redraft_result, elicited_stories_text
    except Exception as e:
        logger.warning("Post-draft re-draft failed (keeping original): %s", e)
        return DraftOutput(draft=draft_text), elicited_stories_text


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
            except CancelledError:
                raise
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
        except CancelledError:
            raise
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

                    # Persist each narrative to the story bank for reuse across future posts.
                    try:
                        from shared.story_bank import save_story

                        topic_keywords = _extract_plan_keywords(plan)
                        for idx2, gap2 in enumerate(story_gaps):
                            # Find the matching narrative (format: "[Story for section: ...]\n<narrative>")
                            for narr in collected_narratives:
                                if gap2.section_title in narr:
                                    raw_narrative = narr.split("\n", 1)[1] if "\n" in narr else narr
                                    save_story(
                                        narrative=raw_narrative,
                                        section_title=gap2.section_title,
                                        section_context=gap2.section_context,
                                        keywords=topic_keywords,
                                        source_job_id=job_id,
                                    )
                    except Exception as e:
                        logger.warning("Story bank save failed (non-fatal): %s", e)

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
        except CancelledError:
            raise
        except Exception as e:
            logger.warning("Story elicitation phase error (skipping): %s", e)

    # Augment stories from the story bank: retrieve previously elicited narratives that match
    # this post's topic and sections, so the draft agent has real stories even if the ghost
    # writer interview was skipped or produced fewer stories than needed.
    try:
        from shared.story_bank import find_relevant_stories

        bank_keywords = _extract_plan_keywords(plan)
        bank_results = find_relevant_stories(bank_keywords, limit=5)
        if bank_results:
            bank_stories = []
            for r in bank_results:
                # Skip stories that are already in elicited_stories_text (same job)
                if elicited_stories_text and r["narrative"] in elicited_stories_text:
                    continue
                bank_stories.append(
                    f"[Banked story for section: {r['section_title']}]\n{r['narrative']}"
                )
            if bank_stories:
                bank_text = "\n\n".join(bank_stories)
                if elicited_stories_text:
                    elicited_stories_text = elicited_stories_text + "\n\n" + bank_text
                else:
                    elicited_stories_text = bank_text
                logger.info("Story bank: augmented with %d banked story(ies)", len(bank_stories))
    except Exception as e:
        logger.warning("Story bank retrieval failed (non-fatal): %s", e)

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
            except CancelledError:
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

            # ── Post-draft story elicitation ─────────────────────────────────
            # Scan the draft for [Author: ...] placeholders left by the draft
            # agent.  For each one, offer the ghost writer interview so the user
            # can provide a real story.  Collected stories are injected and the
            # draft is regenerated.
            if job_id is not None and job_updater is not None:
                draft_result, elicited_stories_text = _fill_story_placeholders(
                    draft_text=draft_result.draft,
                    plan=plan,
                    llm_client=llm_client,
                    job_id=job_id,
                    job_updater=job_updater,
                    elicited_stories_text=elicited_stories_text,
                    draft_agent=draft_agent,
                    draft_input_kwargs=dict(
                        research_document=research_document,
                        research_references=research_result.references if research_result.references else None,
                        content_plan=plan,
                        audience=brief.audience,
                        tone_or_purpose=brief.tone_or_purpose,
                        allowed_claims=allowed_claims_data if isinstance(allowed_claims_data, dict) else None,
                        target_word_count=length_policy.target_word_count,
                        length_guidance=build_draft_length_instruction(length_policy),
                        selected_title=selected_title or None,
                    ),
                    work_dir=work_dir,
                    iteration=iteration,
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
                previous_feedback_items = previous_feedback_items + list(copy_editor_result.feedback_items)
                draft_output_path = (Path(work_dir) / f"draft_v{iteration}.md") if work_dir is not None else None
                draft_result = draft_agent.revise(
                    revise_input,
                    on_llm_request=lambda msg: _update(BlogPhase.COPY_EDIT_LOOP, status_text=msg),
                    draft_output_path=draft_output_path,
                )
            except BloggingError:
                raise
            except CancelledError:
                raise
            except Exception as e:
                if _is_external_cancellation(e):
                    raise
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
            except CancelledError:
                raise
            except Exception as e:
                if _is_external_cancellation(e):
                    raise
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
            except CancelledError:
                raise
            except Exception as e:
                if _is_external_cancellation(e):
                    raise
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
            
            # --- Build feedback from ALL gates ---
            feedback_items: list[FeedbackItem] = []

            # 1. Validator failed checks
            if validator_report.status == "FAIL":
                for check in validator_report.checks:
                    if check.status == "FAIL":
                        details_str = ""
                        if check.details:
                            if "matches" in check.details:
                                details_str = f" Found: {', '.join(str(m) for m in check.details['matches'][:3])}"
                            elif "violations" in check.details:
                                details_str = f" Violations: {', '.join(str(v) for v in check.details['violations'][:3])}"
                            elif "fk_grade" in check.details:
                                details_str = f" FK grade: {check.details['fk_grade']}"
                        feedback_items.append(
                            FeedbackItem(
                                category="validator",
                                severity="must_fix",
                                location=None,
                                issue=f"Validator check '{check.name}' failed.{details_str}",
                                suggestion=f"Fix the '{check.name}' violation identified by the deterministic validator.",
                            )
                        )

            # 2. Fact-check failures
            if fact_report.claims_status == "FAIL" or fact_report.risk_status == "FAIL":
                for flag in fact_report.risk_flags:
                    feedback_items.append(
                        FeedbackItem(
                            category="fact_check",
                            severity="must_fix",
                            location=None,
                            issue=f"Risk flag: {flag}",
                            suggestion=f"Address risk flag: {flag}",
                        )
                    )
                for disclaimer in fact_report.required_disclaimers:
                    feedback_items.append(
                        FeedbackItem(
                            category="fact_check",
                            severity="must_fix",
                            location=None,
                            issue=f"Missing required disclaimer: {disclaimer}",
                            suggestion=f"Add disclaimer: {disclaimer}",
                        )
                    )

            # 3. Compliance fixes
            for fix in compliance_report.required_fixes:
                feedback_items.append(
                    FeedbackItem(
                        category="compliance",
                        severity="must_fix",
                        location=None,
                        issue=fix,
                        suggestion=fix,
                    )
                )

            if not feedback_items:
                feedback_items = [
                    FeedbackItem(
                        category="compliance",
                        severity="must_fix",
                        location=None,
                        issue="Validator, fact-check, or compliance check failed; see reports for details.",
                        suggestion="Address all violations from validator_report.json, fact_check_report.json, and compliance_report.json.",
                    )
                ]

            # Build a summary reflecting all gate failures
            gate_failures = []
            if validator_report.status == "FAIL":
                failed_checks = [c.name for c in validator_report.checks if c.status == "FAIL"]
                gate_failures.append(f"Validator FAIL ({', '.join(failed_checks)})")
            if fact_report.claims_status == "FAIL" or fact_report.risk_status == "FAIL":
                gate_failures.append(f"Fact-check FAIL (claims={fact_report.claims_status}, risk={fact_report.risk_status})")
            if compliance_report.status == "FAIL":
                gate_failures.append(f"Compliance FAIL ({len(compliance_report.violations)} violations)")
            feedback_summary = "; ".join(gate_failures) if gate_failures else "Gates failed"

            try:
                revise_input = ReviseDraftInput(
                    draft=draft_result.draft,
                    feedback_items=feedback_items,
                    feedback_summary=feedback_summary,
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
            except CancelledError:
                raise
            except Exception as e:
                if _is_external_cancellation(e):
                    raise
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
