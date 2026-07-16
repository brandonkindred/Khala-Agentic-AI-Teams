"""
Brand-aligned blog writing pipeline with artifact persistence and gates.

Runs planning -> draft -> interactive user review -> copy-editor loop.
When work_dir is provided, persists artifacts and runs validators, fact-check, and
compliance. On FAIL, enters closed-loop rewrite until PASS or max_rewrite_iterations.

Supports job_updater callback for UI phase tracking.
"""

import logging
import os
import re
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, List, Literal, Optional, Tuple, Union

if TYPE_CHECKING:
    from agents.blogging.blog_writer_agent.models import WriterOutput
    from agents.blogging.ghost_writer_agent.models import StoryGap

from agents.blogging.blog_compliance_agent import BlogComplianceAgent
from agents.blogging.blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorInput
from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
from agents.blogging.blog_fact_check_agent import BlogFactCheckAgent
from agents.blogging.blog_plan_critic_agent import BlogPlanCriticAgent
from agents.blogging.blog_publication_agent.models import PublishingPack
from agents.blogging.blog_research_agent.models import ResearchBriefInput
from agents.blogging.blog_writer_agent import BlogWriterAgent, ReviseWriterInput, WriterInput
from agents.blogging.shared.artifacts import write_artifact
from agents.blogging.shared.blog_job_store import (
    add_blog_pending_questions,
    get_blog_job,
    is_waiting_for_blog_answers,
    record_guideline_updates,
)
from agents.blogging.shared.brand_spec import load_brand_spec_prompt
from agents.blogging.shared.content_plan import (
    ContentPlan,
    PlanningInput,
    PlanningPhaseResult,
    content_plan_to_content_brief_markdown,
    content_plan_to_markdown_doc,
    content_plan_to_outline_markdown,
)
from agents.blogging.shared.content_profile import (
    ContentProfile,
    LengthPolicy,
    SeriesContext,
    build_draft_length_instruction,
    build_planning_length_context,
    resolve_length_policy,
    series_context_block,
)
from agents.blogging.shared.errors import (
    BloggingError,
    ComplianceError,
    DraftError,
    FactCheckError,
    PlanningError,
)
from agents.blogging.shared.models import BlogPhase, get_phase_progress
from agents.blogging.shared.planning_config import (
    plan_critic_enabled,
    plan_critic_max_iterations,
    plan_critic_model_override,
    planning_model_override,
)
from agents.blogging.shared.style_loader import append_guidelines, load_style_file
from agents.blogging.validators.runner import run_validators_from_work_dir
from temporalio.exceptions import CancelledError

from llm_service import (
    LLMClientModel,
    OllamaLLMClient,
    get_strands_model,
    with_model_override,
)
from llm_service.interface import LLMClient, LLMRateLimitError, LLMTemporaryError
from shared_concurrency import parallel_map

from . import _path_setup  # noqa: F401

logger = logging.getLogger(__name__)

_blogging_docs = Path(__file__).resolve().parent.parent / "docs"
STYLE_GUIDE_PATH = _blogging_docs / "writing_guidelines.md"
BRAND_SPEC_PROMPT_PATH = _blogging_docs / "brand_spec_prompt.md"
# Hard upper bound on the draft/copy-edit loop iterations (the `for iteration in
# range(1, draft_editor_iterations + 1)` cap in run_draft_stage). The loop normally
# exits *early* when the copy editor approves the draft, or escalates to the author
# every COPY_EDIT_ESCALATION_THRESHOLD iterations — 30 is a runaway-safety ceiling
# (3x the escalation threshold), not an expected iteration count.
DRAFT_EDITOR_ITERATIONS = 30
MAX_REWRITE_ITERATIONS = 10
# After this many copy-edit revisions without editor approval, escalate to the user
COPY_EDIT_ESCALATION_THRESHOLD = 10

# Poll cadence (seconds) for every human-in-the-loop wait loop (draft feedback,
# uncertainty answers, title selection). One value keeps the loops consistent and
# configurable in one place. This is independent of the Temporal activity heartbeat:
# ``start_pipeline_heartbeat`` runs a background thread that heartbeats on its own
# schedule, so these blocking sleeps never risk a heartbeat timeout.
HITL_POLL_INTERVAL_S = int(os.getenv("BLOGGING_HITL_POLL_INTERVAL_S", "10"))

# A human-in-the-loop wait can poll the job store for up to ~1h; a single transient
# job-store read blip should not fail the whole job. Tolerate this many CONSECUTIVE
# read failures (sleeping a poll interval between each) before giving up and letting the
# error propagate — a persistent outage still surfaces, a momentary one is ridden out.
HITL_MAX_CONSECUTIVE_READ_ERRORS = 5

# Default model - use environment variable or this default
DEFAULT_MODEL = "deepseek-v4-pro:cloud"

PipelineStatus = Literal["PASS", "FAIL", "NEEDS_HUMAN_REVIEW"]

# Type alias for job updater callback
JobUpdater = Callable[..., None]


def _is_external_cancellation(exc: BaseException) -> bool:
    """True when the exception chain indicates a Temporal runtime cancellation.

    Walks the ``__cause__``/``__context__`` chain (bounded by a ``seen`` id-set so a
    self-referential chain can't loop forever) and tests each link with ``isinstance``
    against ``temporalio.exceptions.CancelledError`` — robust to subclasses and free
    of the class-name/module string matching that a Temporal exception-hierarchy
    change could silently break.
    """
    cur: Optional[BaseException] = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, CancelledError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _wait_for_hitl(
    job_id: str,
    is_waiting: Callable[[str], bool],
    *,
    on_poll: Optional[Callable[[str], bool]] = None,
) -> bool:
    """Block until a human-in-the-loop wait clears or the job goes terminal.

    Single home for the pipeline's HITL poll loops (title selection, outline/draft
    feedback, uncertainty answers): the poll cadence (``HITL_POLL_INTERVAL_S``), the
    terminal-status check, and the blocking sleep live here instead of being copied
    at every wait site.

    Args:
        job_id: The job being waited on.
        is_waiting: Predicate ``(job_id) -> bool`` — True while a human response is
            still outstanding.
        on_poll: Optional ``(job_id) -> bool`` invoked once per iteration before
            sleeping. Return True to re-poll immediately without sleeping (e.g. after
            handling incremental feedback); a falsy return sleeps.

    Preconditions:
        - ``is_waiting`` (and ``on_poll`` when provided) are callables accepting a
          ``job_id`` string.
    Postconditions:
        - Returns True iff the job reached a terminal state while waiting — either a
          "failed"/"cancelled" status, or the job disappeared from the store
          (``get_blog_job`` is None). The caller aborts with its own FAIL result.
        - Returns False once ``is_waiting`` became False without a terminal state
          (a human responded) — the caller reads the response.
        - A transient job-store read failure (``is_waiting``/``get_blog_job`` raising)
          is ridden out: it is logged and retried on the next poll, up to
          ``HITL_MAX_CONSECUTIVE_READ_ERRORS`` CONSECUTIVE failures, after which the
          error propagates (a persistent outage still fails the job). ``on_poll`` errors
          are not caught — they propagate immediately.
        - Does not mutate job state; ``on_poll`` may.
    """
    consecutive_read_errors = 0
    while True:
        # Wrap only the job-store reads: a transient blip during a long HITL wait should
        # retry next poll, not fail the whole job. on_poll (below) stays outside so its
        # errors surface immediately.
        try:
            if not is_waiting(job_id):
                return False
            job_data = get_blog_job(job_id)
        except Exception as e:
            consecutive_read_errors += 1
            if consecutive_read_errors > HITL_MAX_CONSECUTIVE_READ_ERRORS:
                logger.warning(
                    "HITL wait for job %s: %d consecutive job-store read failures; giving up",
                    job_id,
                    consecutive_read_errors,
                )
                raise
            logger.warning(
                "HITL wait for job %s: transient job-store read failure (%d/%d), retrying: %s",
                job_id,
                consecutive_read_errors,
                HITL_MAX_CONSECUTIVE_READ_ERRORS,
                e,
            )
            time.sleep(HITL_POLL_INTERVAL_S)
            continue
        consecutive_read_errors = 0
        if job_data is None:
            # The job was deleted from the store mid-wait. ``get_blog_job`` only
            # returns None for a genuinely-absent job (transient/HTTP errors raise),
            # so treat it as terminal and stop polling a job that no longer exists.
            logger.warning("Job %s not found during HITL wait — treating as terminal", job_id)
            return True
        if job_data.get("status") in ("failed", "cancelled"):
            return True
        if on_poll is not None and on_poll(job_id):
            continue
        time.sleep(HITL_POLL_INTERVAL_S)


def _apply_stage_model_override(base: LLMClient, model: Optional[str]) -> LLMClient:
    """Return a variant of ``base`` pinning Ollama fallback candidates to ``model``.

    ``base`` may be a Strands :class:`LLMClientModel` (what the pipeline actually
    passes — ``get_strands_model`` wraps the failover client) or a raw failover
    client. In both cases the override reaches the backing :class:`FailoverLLMClient`
    via :func:`with_model_override`, so an Ollama candidate uses ``model`` while a
    non-Ollama candidate keeps its configured model — multi-provider failover is
    preserved. A backing with no failover client (e.g. a ``DummyLLMClient``) or a
    falsy ``model`` returns ``base`` unchanged.

    Preconditions: ``model`` is a non-empty model name or falsy. Postconditions:
        returns a client ready to use; ``base`` is never mutated (a Strands model is
        rebuilt over the pinned backing, preserving its response format and config).
    """
    if not model:
        return base
    if isinstance(base, LLMClientModel):
        pinned_backing = with_model_override(base.client, model)
        if pinned_backing is base.client:
            # No failover client underneath (e.g. Dummy) — nothing to pin.
            return base
        return LLMClientModel(pinned_backing, **base.get_config())
    return with_model_override(base, model)


def planning_llm_client(base: LLMClient) -> LLMClient:
    """Return the LLM client to use for blog planning.

    When ``BLOG_PLANNING_MODEL`` is set, returns a variant of ``base`` whose Ollama
    fallback candidates are pinned to that model; otherwise returns ``base`` unchanged.
    The override is applied per call (via :func:`_apply_stage_model_override` →
    :func:`with_model_override`), so multi-provider failover is preserved — an Ollama
    provider uses the planning model while a non-Ollama fallback keeps its configured
    model — and ``base``'s agent attribution and reasoning hook carry across. Works
    whether ``base`` is a raw failover client or the Strands model the pipeline passes.

    :param base: The default client the blog pipeline would otherwise use.
    :returns: ``base``, or a failover-preserving variant pinning Ollama candidates to
        ``BLOG_PLANNING_MODEL``.
    """
    return _apply_stage_model_override(base, planning_model_override())


def plan_critic_llm_client(base: LLMClient) -> LLMClient:
    """Return the LLM client to use for the plan critic.

    When ``BLOG_PLAN_CRITIC_MODEL`` is set, returns a variant of ``base`` whose Ollama
    fallback candidates are pinned to that model (via :func:`with_model_override`, so
    multi-provider failover is preserved); otherwise returns ``base`` unchanged. The
    override preserves ``base``'s agent attribution (see :func:`planning_llm_client`).

    Per the architectural tenet, the critic runs on the same model as the writer
    by default. This hook exists so per-role model diversification can be flipped
    on later without further code changes.

    :param base: The default client the blog pipeline would otherwise use.
    :returns: ``base``, or a failover-preserving variant pinning Ollama candidates to
        ``BLOG_PLAN_CRITIC_MODEL``.
    """
    return _apply_stage_model_override(base, plan_critic_model_override())


def build_plan_critic_agent(base: LLMClient) -> Optional[BlogPlanCriticAgent]:
    """Construct the plan-critic agent when enabled, else return None."""
    if not plan_critic_enabled():
        return None
    return BlogPlanCriticAgent(llm_client=plan_critic_llm_client(base))


def run_planning(
    brief: ResearchBriefInput,
    *,
    work_dir: Optional[Union[str, Path]],
    llm_client: OllamaLLMClient,
    length_policy: LengthPolicy,
    series_context: Optional[SeriesContext],
    job_updater: Optional[JobUpdater],
) -> PlanningPhaseResult:
    """
    Planning step for the full pipeline: build the content plan for ``brief``.

    Args:
        brief: The research brief describing the blog topic.
        work_dir: Optional directory for artifact persistence (planning artifacts
            are written when set).
        llm_client: Resolved LLM client used for planning.
        length_policy: Resolved length/format policy for the plan.
        series_context: Optional series-instalment scope.
        job_updater: Optional UI progress callback.

    Preconditions:
        - ``brief`` is a valid ``ResearchBriefInput``.
        - ``llm_client`` and ``length_policy`` are resolved (non-None).
    Postconditions:
        - Returns a ``PlanningPhaseResult`` (content plan with title candidates,
          sections, requirements analysis, and planning telemetry).
    Raises:
        PlanningError: If content planning fails.
    """

    # Same progress-callback as the stage functions use; _make_update is the single
    # source of the swallow-but-reraise-CancelledError update logic.
    _update = _make_update(job_updater)

    _update(
        BlogPhase.PLANNING,
        sub_progress=0.0,
        status_text="Generating content plan...",
    )

    planning_input = PlanningInput(
        brief=brief.brief,
        audience=brief.audience,
        tone_or_purpose=brief.tone_or_purpose,
        length_policy_context=build_planning_length_context(length_policy),
        series_context_block=series_context_block(series_context),
    )

    # Load the author's brand spec + writing guidelines so the plan critic can
    # evaluate against the author-owned sources of truth. These are safe to load
    # even when the critic is disabled — the BlogWriterAgent used for drafting
    # wants them too, but planning uses an empty-style instance by design.
    try:
        brand_spec_for_critic = load_brand_spec_prompt(BRAND_SPEC_PROMPT_PATH)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Could not load brand spec for plan critic: %s", e)
        brand_spec_for_critic = ""
    try:
        writing_guidelines_for_critic = load_style_file(STYLE_GUIDE_PATH)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Could not load writing guidelines for plan critic: %s", e)
        writing_guidelines_for_critic = ""

    plan_critic = build_plan_critic_agent(llm_client)

    # Planning convergence cap: honour the critic's max iterations when the
    # critic is enabled, since the critic can reject plans the planner would
    # otherwise accept. Fall back to the planner's own iteration cap otherwise.
    planning_max_iter = plan_critic_max_iterations() if plan_critic is not None else 5

    try:
        planning_draft_agent = BlogWriterAgent(
            llm_client=planning_llm_client(llm_client),
            writing_style_guide_content=writing_guidelines_for_critic,
            brand_spec_content=brand_spec_for_critic,
        )
        planning_phase_result = planning_draft_agent.plan_content(
            planning_input,
            length_policy=length_policy,
            on_llm_request=lambda msg: _update(BlogPhase.PLANNING, status_text=msg),
            plan_critic=plan_critic,
            work_dir=work_dir,
            max_iterations=planning_max_iter,
        )
    except BloggingError:
        raise
    except Exception as e:
        if _is_external_cancellation(e):
            raise
        raise PlanningError(f"Planning failed: {e}", cause=e) from e

    plan = planning_phase_result.content_plan
    plan_brief_md = content_plan_to_content_brief_markdown(plan)
    logger.info(
        "Planning complete: %s iteration(s), %s title candidates\n%s",
        planning_phase_result.planning_iterations_used,
        len(plan.title_candidates),
        plan_brief_md,
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
        content_plan_detail=content_plan_to_markdown_doc(plan),
    )

    if work_dir is not None:
        write_artifact(work_dir, "content_plan.json", plan.model_dump(mode="json"))
        write_artifact(work_dir, "content_plan.md", content_plan_to_markdown_doc(plan))
        write_artifact(work_dir, "outline.md", content_plan_to_outline_markdown(plan))
        write_artifact(work_dir, "content_brief.md", content_plan_to_content_brief_markdown(plan))
        logger.info("Persisted content_plan.json, content_plan.md, outline.md, content_brief.md")
        # Persist the critic's final verdict under a stable filename for easy inspection;
        # per-iteration reports (plan_critic_report_v{N}.json) remain in work_dir too.
        if planning_phase_result.plan_critic_report is not None:
            write_artifact(
                work_dir,
                "plan_critic_report.json",
                planning_phase_result.plan_critic_report,
            )
            logger.info(
                "Persisted plan_critic_report.json (status=%s)",
                planning_phase_result.plan_critic_report.get("status"),
            )

    return planning_phase_result


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

    Raises:
        CancelledError: a Temporal-native (or otherwise external) cancellation
            propagates unchanged — the non-fatal story-bank-save guard below
            never swallows it.
    """
    from agents.blogging.blog_writer_agent.models import WriterInput, WriterOutput
    from agents.blogging.ghost_writer_agent import GhostWriterElicitationAgent
    from agents.blogging.ghost_writer_agent.agent import MAX_ROUNDS_POST_DRAFT
    from agents.blogging.ghost_writer_agent.models import StoryGap
    from agents.blogging.shared.blog_job_store import (
        add_story_agent_message,
        update_blog_job,
    )

    placeholders = _extract_story_placeholders(draft_text)
    if not placeholders:
        return WriterOutput(draft=draft_text), elicited_stories_text

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
                    f"Hey, there's a spot in the post where a personal story about {topic} "
                    f"would really bring it to life. Have you ever had a moment like that? "
                    f"I'd love to hear about it."
                ),
            )
        )

    for idx, gap in enumerate(gaps):
        job_data = get_blog_job(job_id)
        if job_data and job_data.get("status") in ("failed", "cancelled"):
            break

        # Expose only the current gap — one at a time.
        # Use gap_round tagging so the frontend filters by round.
        update_blog_job(
            job_id,
            story_gaps=[gap.model_dump()],
            current_story_gap_index=0,
            current_gap_round=idx,
            waiting_for_story_input=False,
        )
        job_updater(
            phase="story_elicitation",
            progress=35 + idx,
            status_text=f"Chatting about your experience with: {gap.section_title}",
        )

        # Post seed question — pipeline pauses here until user responds
        add_story_agent_message(job_id, gap.seed_question, 0)

        # conduct_interview waits indefinitely for each user response
        result = ghost_agent.conduct_interview(
            gap=gap,
            job_id=job_id,
            gap_index=0,
            job_updater=job_updater,
            max_rounds=MAX_ROUNDS_POST_DRAFT,
        )

        if result.skipped:
            skipped_topics.append(gap.section_title)
            logger.info("Post-draft: user has no experience for '%s'", gap.section_title)
        elif result.narrative:
            new_narratives.append(f"[Story for section: {gap.section_title}]\n{result.narrative}")
            # Save to story bank for reuse across future posts
            try:
                from agents.blogging.shared.story_bank import save_story

                save_story(
                    narrative=result.narrative,
                    section_title=gap.section_title,
                    section_context=gap.section_context,
                    keywords=_extract_plan_keywords(plan),
                    source_job_id=job_id,
                    llm_client=llm_client,
                )
            except CancelledError:
                raise
            except Exception as e:
                if _is_external_cancellation(e):
                    raise
                logger.warning("Story bank save failed (non-fatal): %s", e)
        else:
            # No narrative and not skipped — treat as no usable material
            skipped_topics.append(gap.section_title)

    update_blog_job(
        job_id,
        waiting_for_story_input=False,
        story_gaps=[],
        current_story_gap_index=0,
    )

    if not new_narratives and not skipped_topics:
        return WriterOutput(draft=draft_text), elicited_stories_text

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
        draft_input = WriterInput(
            **draft_input_kwargs,
            elicited_stories=(elicited_stories_text or "") + skip_instruction or None,
        )
        draft_output_path = (
            (Path(work_dir) / f"draft_v{iteration}.md") if work_dir is not None else None
        )
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
        return WriterOutput(draft=draft_text), elicited_stories_text


def _run_title_selection(
    plan: Any,
    llm_client: Any,
    job_id: Optional[str],
    job_updater: Optional[JobUpdater],
    _update: Callable,
) -> Optional[str]:
    """Run the title selection phase: present candidates, process feedback, return loved title.

    Args:
        plan: The content plan; its ``title_candidates`` drive the selection UI.
        llm_client: Resolved LLM client (used to regenerate candidates on feedback).
        job_id: Job identifier, or None to skip title selection.
        job_updater: UI progress callback, or None to skip title selection.
        _update: The phase-progress callback bound to ``job_updater``.

    Preconditions:
        - When title selection runs, both ``job_id`` and ``job_updater`` are non-None
          (either being None short-circuits to a no-op returning None).
    Postconditions:
        - Returns the author-selected title string, or None when title selection is
          skipped (missing job context) or no title is chosen.
    """
    if job_id is None or job_updater is None:
        return None

    try:
        from agents.blogging.shared.blog_job_store import (
            clear_pending_title_feedback,
            get_blog_job,
            get_pending_title_feedback,
            is_waiting_for_title_selection,
        )

        title_choices = [
            {"title": tc.title, "probability_of_success": tc.probability_of_success}
            for tc in plan.title_candidates
        ]

        all_ratings: list[dict] = []
        title_round = 0

        def _process_title_feedback(poll_job_id: str) -> bool:
            """Consume a pending like/dislike rating during a title-selection wait.

            Regenerates (or drops) the rated candidate via the LLM and re-presents
            the list. Returns True when a rating was handled so the poll loop
            re-checks immediately without sleeping; False when nothing was pending.
            """
            nonlocal title_choices, title_round
            pending = get_pending_title_feedback(poll_job_id)
            if not pending:
                return False
            clear_pending_title_feedback(poll_job_id)
            for fb in pending:
                all_ratings.append(fb)

            rated_title = pending[0].get("title", "")
            rating_type = pending[0].get("rating", "like")
            all_liked = [r["title"] for r in all_ratings if r.get("rating") == "like"]
            all_disliked = [r["title"] for r in all_ratings if r.get("rating") == "dislike"]
            all_previous = [r["title"] for r in all_ratings]

            logger.info(
                "Title feedback (round %s): %r rated %r — generating replacement",
                title_round,
                rated_title,
                rating_type,
            )

            feedback_prompt = (
                "Generate exactly 1 new blog post title candidate to replace one that was rated.\n\n"
                f"TOPIC (the article's core argument — the title MUST align with this): {plan.overarching_topic}\n\n"
            )
            if plan.target_reader:
                feedback_prompt += f"TARGET READER: {plan.target_reader}\n\n"
            section_titles = [sec.title for sec in sorted(plan.sections, key=lambda s: s.order)]
            if section_titles:
                feedback_prompt += "ARTICLE SECTIONS:\n"
                feedback_prompt += "\n".join(f"- {t}" for t in section_titles) + "\n\n"
            feedback_prompt += (
                "REQUIREMENTS:\n"
                "- The title MUST accurately reflect the topic above.\n"
                "- The title should promise the reader something concrete and valuable.\n"
                "- Be specific about what the reader will gain.\n\n"
            )
            if all_liked:
                feedback_prompt += (
                    "Titles the user LIKED (generate a title with a similar style/angle):\n"
                )
                feedback_prompt += "\n".join(f"- {t}" for t in all_liked) + "\n\n"
            if all_disliked:
                feedback_prompt += "Titles the user DISLIKED (avoid this style/angle):\n"
                feedback_prompt += "\n".join(f"- {t}" for t in all_disliked) + "\n\n"
            if all_previous:
                feedback_prompt += "DO NOT repeat any of these previous titles:\n"
                feedback_prompt += "\n".join(f"- {t}" for t in all_previous) + "\n\n"
            feedback_prompt += (
                "Return a JSON object with exactly one key: "
                '"titles": [{"title": "...", "probability_of_success": 0.0-1.0}]'
            )

            replacement = None
            try:
                data = llm_client.complete_json(
                    feedback_prompt, temperature=0.7, objective="regenerate blog titles"
                )
                new_titles = data.get("titles", []) if data else []
                if new_titles and isinstance(new_titles, list):
                    t = new_titles[0]
                    if isinstance(t, dict) and t.get("title"):
                        replacement = {
                            "title": t["title"],
                            "probability_of_success": float(t.get("probability_of_success", 0.5)),
                        }
            except Exception as e:
                logger.warning("Failed to generate replacement title: %s", e)

            if replacement:
                title_choices = [
                    replacement if tc.get("title") == rated_title else tc for tc in title_choices
                ]
            else:
                title_choices = [tc for tc in title_choices if tc.get("title") != rated_title]

            title_round += 1
            job_updater(
                phase="title_selection",
                progress=get_phase_progress(BlogPhase.TITLE_SELECTION, 0.0),
                status_text=f"Rate titles (round {title_round}, {len(title_choices)} candidates)...",
                waiting_for_title_selection=True,
                title_choices=title_choices,
            )
            return True

        while True:
            title_round += 1
            _update(
                BlogPhase.TITLE_SELECTION,
                sub_progress=0.0,
                status_text=f"Rate titles (round {title_round}, {len(title_choices)} candidates)...",
                waiting_for_title_selection=True,
                title_choices=title_choices,
            )

            if _wait_for_hitl(
                job_id,
                is_waiting_for_title_selection,
                on_poll=_process_title_feedback,
            ):
                return None

            job_data = get_blog_job(job_id) or {}
            selected_title = job_data.get("selected_title")

            if selected_title:
                logger.info("Title loved (round %s): %r", title_round, selected_title)
                _update(
                    BlogPhase.TITLE_SELECTION,
                    sub_progress=1.0,
                    status_text=f"Title selected: {selected_title}",
                )
                return selected_title

    except CancelledError:
        raise
    except Exception as e:
        logger.warning("Title selection phase error (skipping): %s", e)
    return None


def _load_required_guidelines(action: str, *, phase: str = "draft") -> Tuple[str, str]:
    """Load the writing-style and brand-spec guideline files, failing loudly if absent.

    Preconditions:
        - ``action`` is a short phrase for the error message (e.g. "start drafting").
        - ``phase`` names the pipeline stage the failure should be attributed to.
    Postconditions:
        - Returns ``(writing_style_content, brand_spec_content)``, both non-empty.
        - Raises ``DraftError(phase=phase)`` naming each missing file when either
          cannot be loaded — agents must never run with silently-empty guidelines —
          so the job store's ``failed_phase`` points at the stage that actually failed.
    """
    writing_style_content = load_style_file(STYLE_GUIDE_PATH, "writing style guide")
    brand_spec_content = load_style_file(BRAND_SPEC_PROMPT_PATH, "brand spec prompt")
    if not writing_style_content or not brand_spec_content:
        missing_parts: list[str] = []
        if not writing_style_content:
            missing_parts.append(f"writing guidelines ({STYLE_GUIDE_PATH})")
        if not brand_spec_content:
            missing_parts.append(f"brand guidelines ({BRAND_SPEC_PROMPT_PATH})")
        missing_msg = ", ".join(missing_parts)
        raise DraftError(
            f"Cannot {action} without required guideline inputs. Missing: {missing_msg}.",
            cause=ValueError(missing_msg),
            phase=phase,
        )
    return writing_style_content, brand_spec_content


def _make_update(job_updater: Optional[JobUpdater]) -> Callable[..., None]:
    """Build the phase-progress ``_update`` callback bound to a job_updater.

    Preconditions:
        - ``job_updater`` is either a callable ``(**kwargs) -> None`` or None.
    Postconditions:
        - Returns a callable ``(phase, sub_progress=0.0, status_text="", **kwargs)``
          that forwards a computed overall progress to ``job_updater`` (no-op when
          ``job_updater`` is None). Re-raises CancelledError; swallows other
          job-update failures (identical to the pipeline's former inline closure).
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

    return _update


@dataclass
class PipelineContext:
    """Mutable state threaded across the blogging pipeline stages.

    Split out so each stage (planning -> draft -> gates) can run as its own Temporal
    activity: the activity seeds a context from the previous stage's serialized DTO,
    runs the stage, and serializes the produced fields. In thread mode a single
    context is threaded through all three stages in-process.

    Invariants:
        - ``llm_client`` and ``length_policy`` are resolved (non-None) before any
          stage runs.
        - ``planning_phase_result``/``plan``/``elicited_stories_text`` are populated
          by the planning stage before the draft stage reads them.
        - ``draft_result`` is populated by the draft stage before the gates stage
          reads it.
    """

    brief: ResearchBriefInput
    work_dir: Optional[Union[str, Path]]
    # ``Any`` is deliberate: the LLM client is one of several unrelated concrete
    # types (a Strands model wrapper, a FailoverLLMClient, a DummyLLMClient) with no
    # shared base. ``Optional`` because it may be None at construction — __post_init__
    # rejects that, so every stage that runs sees a resolved client.
    llm_client: Any
    length_policy: Optional[LengthPolicy]
    series_context: Optional[SeriesContext]
    job_id: Optional[str]
    job_updater: Optional[JobUpdater]
    draft_editor_iterations: int
    max_rewrite_iterations: int
    run_gates: bool
    planning_phase_result: Optional[PlanningPhaseResult] = None
    plan: Optional[ContentPlan] = None
    elicited_stories_text: Optional[str] = None
    draft_result: Optional["WriterOutput"] = None
    status: PipelineStatus = "PASS"

    def __post_init__(self) -> None:
        # Enforce the resolved-inputs invariant at construction so the Temporal
        # activity path (which builds a context directly) fails loudly here rather
        # than with an opaque error deep inside a stage. Explicit raise (not assert)
        # so the check survives ``python -O``.
        if self.llm_client is None:
            raise ValueError("PipelineContext.llm_client must be resolved before running a stage")
        if self.length_policy is None:
            raise ValueError(
                "PipelineContext.length_policy must be resolved before running a stage"
            )


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
        llm_client: Optional LLM client (defaults to deepseek-v4-pro:cloud).
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


def _save_narratives_to_story_bank(
    collected_story_pairs: List[Tuple["StoryGap", str]],
    *,
    topic_keywords: List[str],
    job_id: Optional[str],
    llm_client: Any,
) -> int:
    """Persist each elicited narrative to the story bank under its own story gap.

    The gap→narrative pairing is captured at collection time (see ``run_planning_stage``),
    so each narrative is stored against the exact gap it was elicited for — no substring
    re-matching, which was O(n*m) and could mis-associate a narrative with a gap whose
    ``section_title`` merely appeared as a substring of another section's story.

    Preconditions:
        - Each entry in ``collected_story_pairs`` is ``(gap, raw_narrative)`` where
          ``raw_narrative`` is the unformatted narrative text (no
          ``"[Story for section: ...]"`` prefix).
        - ``topic_keywords`` is the keyword list to tag every saved story with.

    Postconditions:
        - ``save_story`` is attempted exactly once per pair, using that pair's own gap
          ``section_title`` and ``section_context``.
        - A ``save_story`` failure for one pair is caught and logged (non-fatal); the batch
          continues so one bad story never loses the remaining saves.
        - Returns the count of narratives *successfully* persisted (0 ..
          ``len(collected_story_pairs)``).

    Raises:
        CancelledError: a Temporal-native (or otherwise external) cancellation propagates
            unchanged — it is never swallowed by the non-fatal per-pair guard.
    """
    from agents.blogging.shared.story_bank import save_story

    saved = 0
    for story_gap, raw_narrative in collected_story_pairs:
        try:
            save_story(
                narrative=raw_narrative,
                section_title=story_gap.section_title,
                section_context=story_gap.section_context,
                keywords=topic_keywords,
                source_job_id=job_id,
                llm_client=llm_client,
            )
            saved += 1
        except CancelledError:
            raise
        except Exception as e:  # non-fatal: one bad story must not lose the rest
            if _is_external_cancellation(e):
                raise
            logger.warning(
                "Story bank save failed for section %r (non-fatal): %s",
                story_gap.section_title,
                e,
            )
    return saved


def run_planning_stage(
    ctx: "PipelineContext",
) -> Optional[Tuple[PlanningPhaseResult, Optional["WriterOutput"], PipelineStatus]]:
    """Planning stage: content planning, story elicitation, and outline approval.

    Args:
        ctx: The shared ``PipelineContext``. Reads ``brief``, ``work_dir``,
            ``llm_client``, ``length_policy``, ``series_context``, ``job_id``, and
            ``job_updater``; writes ``planning_phase_result``/``plan``/
            ``elicited_stories_text`` (and ``status`` on abort).
    Preconditions:
        - ``ctx.llm_client`` and ``ctx.length_policy`` are resolved.
    Postconditions:
        - On success sets ``ctx.planning_phase_result``/``ctx.plan``/
          ``ctx.elicited_stories_text`` and returns None.
        - Returns a terminal ``(planning_phase_result, None, "FAIL")`` tuple if the
          job was cancelled/failed while awaiting outline approval. This tuple
          sentinel mirrors ``run_pipeline``'s return shape so the sequencer forwards
          it unchanged (see ``run_draft_stage`` for the rationale).
    Raises:
        PlanningError: when content planning fails (e.g. max parse retries).
        BloggingError: any other blogging-domain failure from the planning agent
            propagates unchanged.
        CancelledError: a Temporal-native cancellation propagates (never swallowed);
            a cancellation surfaced *while awaiting outline approval* instead
            short-circuits to the FAIL tuple above.
    """
    brief = ctx.brief
    work_dir = ctx.work_dir
    llm_client = ctx.llm_client
    length_policy = ctx.length_policy
    series_context = ctx.series_context
    job_id = ctx.job_id
    job_updater = ctx.job_updater
    _update = _make_update(job_updater)

    planning_phase_result = run_planning(
        brief,
        work_dir=work_dir,
        llm_client=llm_client,
        length_policy=length_policy,
        series_context=series_context,
        job_updater=job_updater,
    )
    plan = planning_phase_result.content_plan

    # ------------------------------------------------------------------
    # Story elicitation: ghost writer surfaces personal anecdotes
    # ------------------------------------------------------------------
    elicited_stories_text: Optional[str] = None
    if job_id is not None and job_updater is not None:
        try:
            from agents.blogging.ghost_writer_agent import GhostWriterElicitationAgent, StoryGap
            from agents.blogging.shared.blog_job_store import (
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
                collected_narratives: list[str] = []
                # Preserve the gap→narrative pairing at collection time so the story-bank
                # save loop below doesn't have to re-derive it by fragile substring matching.
                collected_story_pairs: list[tuple[StoryGap, str]] = []

                for idx, gap in enumerate(story_gaps):
                    job_data = get_blog_job(job_id)
                    if job_data and job_data.get("status") in ("failed", "cancelled"):
                        break

                    # Expose only the current gap — don't reveal how many stories are needed.
                    # Use gap_round tagging so the frontend filters by round, not gap_index.
                    # Full chat history is preserved for cross-gap LLM context.
                    update_blog_job(
                        job_id,
                        story_gaps=[gap.model_dump()],
                        current_story_gap_index=0,
                        current_gap_round=idx,
                    )
                    job_updater(
                        phase="story_elicitation",
                        progress=27 + idx,
                        status_text=f"Chatting about your experience with: {gap.section_title}",
                    )

                    # Post seed question and wait for first user response
                    # gap_index is always 0 since we expose one gap at a time
                    add_story_agent_message(job_id, gap.seed_question, 0)

                    result = ghost_agent.conduct_interview(
                        gap=gap,
                        job_id=job_id,
                        gap_index=0,
                        job_updater=job_updater,
                    )
                    # Guard against empty/whitespace-only narratives — never persist a
                    # blank story to the bank or emit one into the draft.
                    if result.narrative and result.narrative.strip():
                        collected_narratives.append(
                            f"[Story for section: {gap.section_title}]\n{result.narrative}"
                        )
                        collected_story_pairs.append((gap, result.narrative))

                if collected_narratives:
                    elicited_stories_text = "\n\n".join(collected_narratives)
                    complete_story_elicitation(job_id, elicited_stories=collected_narratives)

                    # Persist each narrative to the story bank for reuse across future posts.
                    try:
                        topic_keywords = _extract_plan_keywords(plan)
                        saved_count = _save_narratives_to_story_bank(
                            collected_story_pairs,
                            topic_keywords=topic_keywords,
                            job_id=job_id,
                            llm_client=llm_client,
                        )
                        logger.info(
                            "Story bank: persisted %d of %d elicited narrative(s)",
                            saved_count,
                            len(collected_story_pairs),
                        )
                    except CancelledError:
                        raise
                    except Exception as e:
                        logger.warning("Story bank save failed (non-fatal): %s", e)

                update_blog_job(
                    job_id,
                    waiting_for_story_input=False,
                    story_gaps=[],
                    current_story_gap_index=0,
                )
                job_updater(
                    phase="story_elicitation",
                    progress=30,
                    status_text=(
                        f"Story gathering complete — {len(collected_narratives)} story(ies) collected"
                        if collected_narratives
                        else "Story gathering complete"
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
        from agents.blogging.shared.story_bank import find_relevant_stories

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

    # ------------------------------------------------------------------
    # Outline approval: block until the user approves the outline
    # ------------------------------------------------------------------
    if job_id is not None and job_updater is not None:
        try:
            from agents.blogging.shared.blog_job_store import (
                get_blog_job,
                get_user_draft_feedback,
                is_waiting_for_draft_feedback,
                request_draft_feedback,
                update_blog_job,
            )

            outline_text = content_plan_to_outline_markdown(plan)
            outline_revision = 0

            # Present outline for approval
            _update(
                BlogPhase.PLANNING,
                sub_progress=0.8,
                status_text="Waiting for outline approval...",
            )
            request_draft_feedback(
                job_id,
                draft=outline_text,
                revision=outline_revision,
            )

            while True:
                # Poll until user submits feedback
                if _wait_for_hitl(job_id, is_waiting_for_draft_feedback):
                    return planning_phase_result, None, "FAIL"

                feedback_data = get_user_draft_feedback(job_id)
                if not feedback_data:
                    logger.warning("No outline feedback found; proceeding with current outline.")
                    break

                if feedback_data.get("approved"):
                    logger.info("User approved outline at revision %s", outline_revision)
                    _update(
                        BlogPhase.PLANNING,
                        sub_progress=0.95,
                        status_text=f"Outline approved (revision {outline_revision})",
                    )
                    break

                # User provided feedback — re-plan with their input
                user_feedback_text = feedback_data.get("feedback", "")
                logger.info(
                    "Outline feedback (revision %s): %s chars",
                    outline_revision,
                    len(user_feedback_text),
                )
                outline_revision += 1

                _update(
                    BlogPhase.PLANNING,
                    sub_progress=0.85,
                    status_text=f"Revising outline based on feedback (revision {outline_revision})...",
                )

                # Re-run planning with the user's feedback folded into the brief.
                # plan_content has no dedicated feedback parameter, so the author's
                # outline feedback is appended to the brief text — otherwise the
                # re-plan would run with the original input and silently ignore it.
                refine_brief = brief.brief
                if user_feedback_text:
                    refine_brief = (
                        f"{brief.brief}\n\nAuthor feedback on the previous outline "
                        f"(revision {outline_revision}): {user_feedback_text}"
                    )
                planning_input_for_refine = PlanningInput(
                    brief=refine_brief,
                    audience=brief.audience,
                    tone_or_purpose=brief.tone_or_purpose,
                    length_policy_context=build_planning_length_context(length_policy),
                    series_context_block=series_context_block(series_context),
                )
                planning_draft_agent = BlogWriterAgent(
                    llm_client=planning_llm_client(llm_client),
                    writing_style_guide_content="",
                    brand_spec_content="",
                )
                try:
                    refined_result = planning_draft_agent.plan_content(
                        planning_input_for_refine,
                        length_policy=length_policy,
                        on_llm_request=lambda msg: _update(BlogPhase.PLANNING, status_text=msg),
                    )
                    plan = refined_result.content_plan
                    planning_phase_result = refined_result
                except Exception as e:
                    logger.warning("Re-planning with feedback failed: %s; keeping current plan", e)

                outline_text = content_plan_to_outline_markdown(plan)

                # Persist updated artifacts
                if work_dir is not None:
                    write_artifact(work_dir, "content_plan.json", plan.model_dump(mode="json"))
                    write_artifact(work_dir, "content_plan.md", content_plan_to_markdown_doc(plan))
                    write_artifact(work_dir, "outline.md", outline_text)
                    write_artifact(
                        work_dir, "content_brief.md", content_plan_to_content_brief_markdown(plan)
                    )

                # Present revised outline for another round
                _update(
                    BlogPhase.PLANNING,
                    sub_progress=0.8,
                    status_text="Waiting for approval of revised outline...",
                    content_plan_detail=content_plan_to_markdown_doc(plan),
                )
                request_draft_feedback(
                    job_id,
                    draft=outline_text,
                    revision=outline_revision,
                )

        except CancelledError:
            raise
        except Exception as e:
            logger.warning("Outline approval phase error (skipping): %s", e)

    ctx.planning_phase_result = planning_phase_result
    ctx.plan = plan
    ctx.elicited_stories_text = elicited_stories_text
    return None


def run_draft_stage(
    ctx: "PipelineContext",
) -> Optional[Tuple[PlanningPhaseResult, Optional["WriterOutput"], PipelineStatus]]:
    """Draft stage: initial draft, interactive review, and the copy-edit loop.

    Preconditions:
        - The planning stage populated ``ctx.plan``/``ctx.planning_phase_result``/
          ``ctx.elicited_stories_text``.
        - The human-in-the-loop steps (story-placeholder filling and the interactive
          draft-review loop with uncertainty questions / author feedback / guideline
          updates) require a job store: they run only when BOTH ``ctx.job_id`` and
          ``ctx.job_updater`` are non-None. In thread-mode / CLI / test runs without a
          job store they are skipped and the draft proceeds straight to the automated
          copy-edit loop (the story-placeholder skip is logged, since unfilled
          placeholders visibly degrade the output).
    Postconditions:
        - On success sets ``ctx.draft_result`` (and the possibly-updated
          ``ctx.elicited_stories_text``) and returns None.
        - Returns a terminal ``(planning_phase_result, draft_result, "FAIL")`` tuple
          if the job was cancelled/failed while awaiting user review. This tuple
          *sentinel* (rather than a dedicated ``PipelineAbortedError``) is a
          deliberate design choice: it keeps the abort shape identical to
          ``run_pipeline``'s ``(planning, draft, status)`` return so the thin
          sequencer can forward it unchanged, and avoids exception-based control flow
          across the Temporal activity boundary where state crosses as serialized
          DTOs, not live exceptions. ``run_gates_stage`` (terminal, no abort) returns
          ``None``; only the two stages that can abort use this sentinel.
    Raises:
        DraftError: when the required guideline files cannot be loaded
            (via ``_load_required_guidelines``, phase="draft") or when draft /
            copy-edit generation fails.
        BloggingError: any other blogging-domain failure raised by the draft or
            copy-edit agents propagates unchanged.
        CancelledError: a Temporal-native cancellation propagates (never swallowed).
    """
    assert ctx.plan is not None, "run_draft_stage requires ctx.plan (set by the planning stage)"
    brief = ctx.brief
    work_dir = ctx.work_dir
    llm_client = ctx.llm_client
    length_policy = ctx.length_policy
    job_id = ctx.job_id
    job_updater = ctx.job_updater
    draft_editor_iterations = ctx.draft_editor_iterations
    planning_phase_result = ctx.planning_phase_result
    plan = ctx.plan
    elicited_stories_text = ctx.elicited_stories_text
    _update = _make_update(job_updater)

    # Draft + Copy Editor loop (load style and brand spec as raw text for draft/editor agents)
    writing_style_content, brand_spec_content = _load_required_guidelines("start drafting")
    draft_agent = BlogWriterAgent(
        llm_client=llm_client,
        writing_style_guide_content=writing_style_content,
        brand_spec_content=brand_spec_content,
    )
    copy_editor_agent = BlogCopyEditorAgent(
        llm_client=llm_client,
        writing_style_guide_content=writing_style_content,
        brand_spec_content=brand_spec_content,
    )

    # Deferred imports (here and elsewhere in the stage bodies) keep this module's
    # import-time cheap and avoid pulling the full blog_writer_agent / job-store graph
    # when the Temporal worker imports this file to register activities.
    from agents.blogging.blog_writer_agent.feedback_tracker import FeedbackTracker

    draft_result = None
    previous_feedback_items: list[FeedbackItem] = []
    feedback_tracker = FeedbackTracker(window_size=3)
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
                draft_input = WriterInput(
                    content_plan=plan,
                    audience=brief.audience,
                    tone_or_purpose=brief.tone_or_purpose,
                    target_word_count=length_policy.target_word_count,
                    length_guidance=build_draft_length_instruction(length_policy),
                    selected_title=None,
                    elicited_stories=elicited_stories_text or None,
                )
                draft_output_path = (
                    (Path(work_dir) / f"draft_v{iteration}.md") if work_dir is not None else None
                )
                draft_result = draft_agent.run(
                    draft_input,
                    on_llm_request=lambda msg: _update(BlogPhase.DRAFT_INITIAL, status_text=msg),
                    draft_output_path=draft_output_path,
                )
            except (BloggingError, CancelledError, LLMRateLimitError, LLMTemporaryError):
                # Transient LLM-transport errors propagate unwrapped so the Temporal
                # activity funnel can retry the whole stage rather than masking them
                # as a terminal DraftError (see temporal.activities._run_stage).
                raise
            except Exception as e:
                # A Temporal runtime cancellation can surface as a non-CancelledError
                # type; let it propagate as cancellation instead of masking it as a
                # terminal DraftError — matching every other stage's handler (draft
                # revision, planning, gates, validators).
                if _is_external_cancellation(e):
                    raise
                raise DraftError(
                    f"Initial draft generation failed: {e}", iteration=iteration, cause=e
                ) from e

            logger.info(
                "Draft iteration %s: initial draft, length=%s", iteration, len(draft_result.draft)
            )
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
                        content_plan=plan,
                        audience=brief.audience,
                        tone_or_purpose=brief.tone_or_purpose,
                        target_word_count=length_policy.target_word_count,
                        length_guidance=build_draft_length_instruction(length_policy),
                        selected_title=None,
                    ),
                    work_dir=work_dir,
                    iteration=iteration,
                )
            else:
                logger.info(
                    "No job store (job_id/job_updater is None) — skipping story-placeholder "
                    "elicitation; any [Author: ...] placeholders remain unfilled in the draft."
                )

            # ── Interactive draft review (user-as-editor) ──────────────────
            # After the initial draft:
            #   1. Check for uncertainty questions → block for answers
            #   2. Revise draft with answers if any
            #   3. Present draft for editor review → block for feedback
            # This loop continues until the user approves a draft.
            if job_id is not None and job_updater is not None:
                from agents.blogging.shared.blog_job_store import (
                    get_blog_job,
                    get_user_draft_feedback,
                    is_waiting_for_draft_feedback,
                    request_draft_feedback,
                )

                content_plan_text = content_plan_to_outline_markdown(plan)
                user_review_revision = 1

                # ── Step 1: Identify and block on uncertainty questions ───
                _update(
                    BlogPhase.DRAFT_REVIEW,
                    sub_progress=0.0,
                    status_text="Checking draft for areas of uncertainty...",
                )
                uncertainty_questions = draft_agent.identify_uncertainty_questions(
                    draft_result.draft, content_plan_text
                )

                if uncertainty_questions:
                    q_dicts = [
                        {
                            "id": q.question_id,
                            "question_text": q.question,
                            "context": q.context,
                            "required": True,
                        }
                        for q in uncertainty_questions
                    ]
                    _update(
                        BlogPhase.DRAFT_REVIEW,
                        sub_progress=0.05,
                        status_text=f"Waiting for answers to {len(q_dicts)} question(s)...",
                    )
                    add_blog_pending_questions(job_id, q_dicts)

                    # Block until user answers
                    if _wait_for_hitl(job_id, is_waiting_for_blog_answers):
                        return planning_phase_result, draft_result, "FAIL"

                    # ── Step 2: Revise draft with the user's answers ──────
                    job_data = get_blog_job(job_id)
                    submitted_answers = (job_data or {}).get("submitted_answers", [])
                    if submitted_answers:
                        # Build feedback text from answers for revision
                        answer_lines = []
                        for ans in submitted_answers:
                            qid = ans.get("question_id", "")
                            text = ans.get("selected_answer", "")
                            if text:
                                answer_lines.append(f"Q ({qid}): {text}")
                        if answer_lines:
                            answer_feedback = (
                                "The author answered the following uncertainty questions. "
                                "Incorporate these answers into the draft:\n\n"
                                + "\n".join(answer_lines)
                            )
                            _update(
                                BlogPhase.DRAFT_REVIEW,
                                sub_progress=0.08,
                                status_text="Incorporating answers into draft...",
                            )
                            draft_output_path = (
                                (Path(work_dir) / "draft_v1_answered.md")
                                if work_dir is not None
                                else None
                            )
                            draft_result = draft_agent.revise_from_user_feedback(
                                draft=draft_result.draft,
                                user_feedback=answer_feedback,
                                content_plan_text=content_plan_text,
                                audience=brief.audience,
                                tone_or_purpose=brief.tone_or_purpose,
                                selected_title=None,
                                elicited_stories=elicited_stories_text or None,
                                target_word_count=length_policy.target_word_count,
                                length_guidance=build_draft_length_instruction(length_policy),
                                on_llm_request=lambda msg: _update(
                                    BlogPhase.DRAFT_REVIEW, status_text=msg
                                ),
                                draft_output_path=draft_output_path,
                            )

                # ── Step 3: Present draft for editor review ───────────────
                _update(
                    BlogPhase.DRAFT_REVIEW,
                    sub_progress=0.1,
                    status_text="Waiting for editor review of draft...",
                )
                request_draft_feedback(
                    job_id,
                    draft=draft_result.draft,
                    revision=user_review_revision,
                )

                # Poll until user submits feedback
                if _wait_for_hitl(job_id, is_waiting_for_draft_feedback):
                    return planning_phase_result, draft_result, "FAIL"

                # Process user feedback in a loop until approved
                while True:
                    feedback_data = get_user_draft_feedback(job_id)
                    if not feedback_data:
                        logger.warning(
                            "No user draft feedback found; proceeding with current draft."
                        )
                        break

                    if feedback_data.get("approved"):
                        logger.info("User approved draft at revision %s", user_review_revision)
                        _update(
                            BlogPhase.DRAFT_REVIEW,
                            sub_progress=1.0,
                            status_text=f"Draft approved by editor (revision {user_review_revision})",
                        )
                        break

                    user_feedback_text = feedback_data.get("feedback", "")
                    logger.info(
                        "User feedback received (revision %s): %s chars",
                        user_review_revision,
                        len(user_feedback_text),
                    )

                    # Analyze feedback for writing guideline updates
                    if user_feedback_text:
                        _update(
                            BlogPhase.DRAFT_REVIEW,
                            status_text="Analyzing feedback for guideline updates...",
                        )
                        guideline_updates = draft_agent.analyze_user_feedback_for_guideline_updates(
                            user_feedback_text, writing_style_content
                        )
                        if guideline_updates:
                            update_dicts = [u.model_dump() for u in guideline_updates]
                            if append_guidelines(STYLE_GUIDE_PATH, update_dicts):
                                logger.info(
                                    "Applied %s guideline update(s) from user feedback",
                                    len(guideline_updates),
                                )
                                # Reload the updated style guide
                                writing_style_content = load_style_file(
                                    STYLE_GUIDE_PATH, "writing style guide"
                                )
                                # Rebuild agent with updated guidelines
                                draft_agent = BlogWriterAgent(
                                    llm_client=llm_client,
                                    writing_style_guide_content=writing_style_content,
                                    brand_spec_content=brand_spec_content,
                                )
                                copy_editor_agent = BlogCopyEditorAgent(
                                    llm_client=llm_client,
                                    writing_style_guide_content=writing_style_content,
                                    brand_spec_content=brand_spec_content,
                                )
                                record_guideline_updates(job_id, update_dicts)

                    # Revise draft based on user feedback
                    user_review_revision += 1
                    _update(
                        BlogPhase.DRAFT_REVIEW,
                        sub_progress=min(0.9, user_review_revision * 0.1),
                        status_text=f"Revising draft (revision {user_review_revision})...",
                    )
                    draft_output_path = (
                        (Path(work_dir) / f"draft_user_rev_{user_review_revision}.md")
                        if work_dir is not None
                        else None
                    )
                    draft_result = draft_agent.revise_from_user_feedback(
                        draft=draft_result.draft,
                        user_feedback=user_feedback_text,
                        content_plan_text=content_plan_text,
                        audience=brief.audience,
                        tone_or_purpose=brief.tone_or_purpose,
                        selected_title=None,
                        elicited_stories=elicited_stories_text or None,
                        target_word_count=length_policy.target_word_count,
                        length_guidance=build_draft_length_instruction(length_policy),
                        on_llm_request=lambda msg: _update(BlogPhase.DRAFT_REVIEW, status_text=msg),
                        draft_output_path=draft_output_path,
                    )

                    # Present revised draft for another round of review
                    _update(
                        BlogPhase.DRAFT_REVIEW,
                        status_text="Waiting for editor review of revised draft...",
                    )
                    request_draft_feedback(
                        job_id,
                        draft=draft_result.draft,
                        revision=user_review_revision,
                    )

                    # Poll until user submits feedback
                    if _wait_for_hitl(job_id, is_waiting_for_draft_feedback):
                        return planning_phase_result, draft_result, "FAIL"

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
                    previous_feedback_items=previous_feedback_items
                    if previous_feedback_items
                    else None,
                    target_word_count=length_policy.target_word_count,
                    length_guidance=length_policy.length_guidance,
                    soft_min_words=length_policy.soft_min_words,
                    soft_max_words=length_policy.soft_max_words,
                    editor_must_fix_over_ratio=length_policy.editor_must_fix_over_ratio,
                    editor_should_fix_over_ratio=length_policy.editor_should_fix_over_ratio,
                    content_profile=length_policy.content_profile.value,
                    content_plan_context=content_plan_to_outline_markdown(plan),
                )
                feedback_path = (
                    (Path(work_dir) / f"editor_feedback_iter_{copy_edit_num}.json")
                    if work_dir is not None
                    else None
                )
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

                # Track feedback for staleness detection and persistent issue escalation
                feedback_tracker.record_iteration(
                    iteration, list(copy_editor_result.feedback_items)
                )

                if copy_editor_result.approved:
                    logger.info(
                        "Copy editor approved draft at iteration %s, stopping loop.", copy_edit_num
                    )
                    _update(
                        BlogPhase.COPY_EDIT_LOOP,
                        sub_progress=1.0,
                        status_text=f"Draft approved by editor after {copy_edit_num} pass(es)",
                        draft_iterations=iteration,
                    )
                    break

                # Detect stalled loop — same issues repeating without resolution
                if iteration > 3 and feedback_tracker.is_stalled():
                    logger.warning(
                        "Copy-edit loop stalled at iteration %s (same issues repeating); accepting draft.",
                        iteration,
                    )
                    _update(
                        BlogPhase.COPY_EDIT_LOOP,
                        sub_progress=1.0,
                        status_text=f"Draft accepted after {copy_edit_num} pass(es) (editor loop converged)",
                        draft_iterations=iteration,
                    )
                    break

                # ── Escalation to user after N revisions without approval ──
                # When the copy-editor has iterated COPY_EDIT_ESCALATION_THRESHOLD
                # times without approving, pause the pipeline and ask the user
                # (human editor) for feedback or explicit approval.
                if (
                    copy_edit_num > 0
                    and copy_edit_num % COPY_EDIT_ESCALATION_THRESHOLD == 0
                    and job_id is not None
                    and job_updater is not None
                ):
                    persistent_issues_for_esc = feedback_tracker.get_persistent_issues(
                        min_occurrences=2
                    )
                    logger.warning(
                        "Copy-edit loop reached %s iterations without approval; escalating to user.",
                        copy_edit_num,
                    )
                    _update(
                        BlogPhase.COPY_EDIT_LOOP,
                        status_text=(
                            f"Draft has been through {copy_edit_num} automated revisions "
                            "without approval. Requesting editor feedback..."
                        ),
                    )

                    escalation_summary = draft_agent.generate_escalation_summary(
                        revision_count=copy_edit_num,
                        latest_feedback_items=list(copy_editor_result.feedback_items),
                        persistent_issues=persistent_issues_for_esc,
                    )

                    request_draft_feedback(
                        job_id,
                        draft=draft_result.draft,
                        revision=copy_edit_num,
                        escalation_summary=escalation_summary,
                    )

                    # Poll until user submits feedback
                    if _wait_for_hitl(job_id, is_waiting_for_draft_feedback):
                        return planning_phase_result, draft_result, "FAIL"

                    esc_feedback = get_user_draft_feedback(job_id)
                    if esc_feedback and esc_feedback.get("approved"):
                        logger.info(
                            "User approved draft during escalation at iteration %s",
                            copy_edit_num,
                        )
                        _update(
                            BlogPhase.COPY_EDIT_LOOP,
                            sub_progress=1.0,
                            status_text=f"Draft approved by editor after {copy_edit_num} pass(es)",
                            draft_iterations=iteration,
                        )
                        break

                    esc_feedback_text = (esc_feedback or {}).get("feedback", "")
                    if esc_feedback_text:
                        # Analyze for guideline updates
                        guideline_updates = draft_agent.analyze_user_feedback_for_guideline_updates(
                            esc_feedback_text, writing_style_content
                        )
                        if guideline_updates:
                            update_dicts = [u.model_dump() for u in guideline_updates]
                            if append_guidelines(STYLE_GUIDE_PATH, update_dicts):
                                writing_style_content = load_style_file(
                                    STYLE_GUIDE_PATH, "writing style guide"
                                )
                                draft_agent = BlogWriterAgent(
                                    llm_client=llm_client,
                                    writing_style_guide_content=writing_style_content,
                                    brand_spec_content=brand_spec_content,
                                )
                                copy_editor_agent = BlogCopyEditorAgent(
                                    llm_client=llm_client,
                                    writing_style_guide_content=writing_style_content,
                                    brand_spec_content=brand_spec_content,
                                )
                                record_guideline_updates(job_id, update_dicts)

                        # Revise based on user feedback before continuing the loop
                        content_plan_text = content_plan_to_outline_markdown(plan)
                        draft_output_path = (
                            (Path(work_dir) / f"draft_v{iteration}_esc.md")
                            if work_dir is not None
                            else None
                        )
                        draft_result = draft_agent.revise_from_user_feedback(
                            draft=draft_result.draft,
                            user_feedback=esc_feedback_text,
                            content_plan_text=content_plan_text,
                            audience=brief.audience,
                            tone_or_purpose=brief.tone_or_purpose,
                            selected_title=None,
                            elicited_stories=elicited_stories_text or None,
                            target_word_count=length_policy.target_word_count,
                            length_guidance=build_draft_length_instruction(length_policy),
                            on_llm_request=lambda msg: _update(
                                BlogPhase.COPY_EDIT_LOOP, status_text=msg
                            ),
                            draft_output_path=draft_output_path,
                        )
                        # Continue copy-edit loop with revised draft
                        continue

                persistent_issues = feedback_tracker.get_persistent_issues(min_occurrences=2)
                if persistent_issues:
                    logger.info(
                        "Escalating %s persistent issue(s) to revision prompt",
                        len(persistent_issues),
                    )

                revise_input = ReviseWriterInput(
                    draft=draft_result.draft,
                    feedback_items=copy_editor_result.feedback_items,
                    feedback_summary=copy_editor_result.summary,
                    previous_feedback_items=feedback_tracker.get_capped_previous_feedback(
                        max_items=15
                    )
                    or None,
                    persistent_issues=persistent_issues or None,
                    content_plan=plan,
                    audience=brief.audience,
                    tone_or_purpose=brief.tone_or_purpose,
                    target_word_count=length_policy.target_word_count,
                    length_guidance=build_draft_length_instruction(length_policy),
                    selected_title=None,
                    elicited_stories=elicited_stories_text or None,
                )
                previous_feedback_items = feedback_tracker.get_capped_previous_feedback(
                    max_items=15
                )
                draft_output_path = (
                    (Path(work_dir) / f"draft_v{iteration}.md") if work_dir is not None else None
                )
                draft_result = draft_agent.revise(
                    revise_input,
                    on_llm_request=lambda msg: _update(BlogPhase.COPY_EDIT_LOOP, status_text=msg),
                    draft_output_path=draft_output_path,
                    work_dir=work_dir,
                    iteration=iteration,
                )
            except (BloggingError, CancelledError, LLMRateLimitError, LLMTemporaryError):
                # Transient LLM-transport errors propagate unwrapped for Temporal retry.
                raise
            except Exception as e:
                if _is_external_cancellation(e):
                    raise
                raise DraftError(f"Draft revision failed: {e}", iteration=iteration, cause=e) from e

            logger.info(
                "Draft iteration %s: revised, length=%s", iteration, len(draft_result.draft)
            )
    else:
        _update(
            BlogPhase.COPY_EDIT_LOOP,
            sub_progress=1.0,
            status_text=f"Draft editing complete after {draft_editor_iterations} iteration(s)",
            draft_iterations=draft_editor_iterations,
        )

    ctx.draft_result = draft_result
    ctx.elicited_stories_text = elicited_stories_text
    return None


def run_gates_stage(ctx: "PipelineContext") -> None:
    """Gates stage: validators, fact-check, compliance, rewrite loop, and finalize.

    Args:
        ctx: The shared ``PipelineContext``. Reads ``brief``, ``work_dir``,
            ``llm_client``, ``length_policy``, ``job_id``, ``job_updater``,
            ``max_rewrite_iterations``, ``run_gates``, ``plan``,
            ``elicited_stories_text``, and ``draft_result``; writes the final
            ``draft_result`` and ``status``.
    Preconditions:
        - The draft stage populated ``ctx.draft_result``/``ctx.plan``/
          ``ctx.elicited_stories_text``.
    Postconditions:
        - Sets ``ctx.draft_result`` (final) and ``ctx.status`` (PASS or
          NEEDS_HUMAN_REVIEW). Always returns None (no early aborts).
        - When ``run_gates`` is True but ``work_dir`` is None the gates cannot run
          (they persist artifacts under ``work_dir``): they are skipped with a
          ``logger.info`` and ``ctx.status`` stays PASS — a "gates requested but not
          executable" result rather than "gates passed". Callers that require gates
          to actually run must supply a ``work_dir``.
    Raises:
        DraftError: when gates are enabled but the guideline files required for
            gate-driven rewrites cannot be loaded, or when a rewrite iteration
            fails (phase="gates"/"draft").
        FactCheckError: when the fact-check gate fails unrecoverably.
        ComplianceError: when the compliance gate fails unrecoverably.
        BloggingError: any other blogging-domain gate failure (base class of the
            above) propagates unchanged.
        CancelledError: a Temporal-native cancellation propagates for the worker
            to observe (never swallowed here).
        LLMRateLimitError / LLMTemporaryError: a transient LLM-transport failure
            propagates unwrapped so the Temporal activity funnel can retry the
            stage instead of masking it as a domain gate failure.

    Note:
        The fact-check and compliance gates are independent given the draft and the
        deterministic validator report, so they run concurrently via ``parallel_map``
        (which copies the caller's LLM attribution/request-id contextvars into each
        worker). Validators run first because the compliance gate consumes their report.
    """
    assert ctx.draft_result is not None, (
        "run_gates_stage requires ctx.draft_result (set by the draft stage)"
    )
    brief = ctx.brief
    work_dir = ctx.work_dir
    llm_client = ctx.llm_client
    length_policy = ctx.length_policy
    job_id = ctx.job_id
    job_updater = ctx.job_updater
    max_rewrite_iterations = ctx.max_rewrite_iterations
    run_gates = ctx.run_gates
    plan = ctx.plan
    elicited_stories_text = ctx.elicited_stories_text
    draft_result = ctx.draft_result
    _update = _make_update(job_updater)

    status: PipelineStatus = "PASS"
    if work_dir is not None:
        write_artifact(work_dir, "final.md", draft_result.draft)
        logger.info("Persisted final.md")

    # Gates require a work_dir: they persist validator/fact-check/compliance
    # artifacts and drive the closed-loop rewrite off them. When gates are
    # requested without a work_dir (e.g. an in-memory run), skip them but say so
    # rather than finalizing silently as PASS.
    if run_gates and work_dir is None:
        logger.info(
            "Blog gates requested (run_gates=True) but skipped: no work_dir to "
            "persist gate artifacts. Provide work_dir to enable quality gates."
        )

    if work_dir is not None and run_gates:
        brand_spec_prompt_text = load_brand_spec_prompt(BRAND_SPEC_PROMPT_PATH)
        compliance_agent = BlogComplianceAgent(llm_client=llm_client)
        fact_check_agent = BlogFactCheckAgent(llm_client=llm_client)
        require_disclaimer_for = ["medical", "legal", "financial"]

        # Reconstruct the draft agent for gate-driven rewrites. Guideline edits made
        # during the draft stage are persisted to STYLE_GUIDE_PATH, so re-loading here
        # picks them up — this also makes the gates stage self-contained when it runs
        # as its own Temporal activity (a fresh process with no in-memory draft agent).
        writing_style_content, brand_spec_content = _load_required_guidelines(
            "run gate-driven rewrites", phase="gates"
        )
        draft_agent = BlogWriterAgent(
            llm_client=llm_client,
            writing_style_guide_content=writing_style_content,
            brand_spec_content=brand_spec_content,
        )

        # The fact-check and compliance gates are independent given the draft (and,
        # for compliance, the deterministic validator report), so they run
        # concurrently below. Each returns ``(report, error)`` — CAPTURING (not
        # raising) any failure it would otherwise raise — so that parallel_map runs
        # BOTH gates to completion before the stage propagates a failure. That drain
        # matters because both gates persist artifacts (fact_check_report.json /
        # compliance_report.json) into the same work_dir: if one raised while the
        # other was still running, parallel_map's fast-fail would abandon the running
        # worker, which could later overwrite the report from a subsequent
        # retry/rewrite. The captured error is Temporal cancellation, BloggingError,
        # or a transient LLM-transport error (propagated unwrapped so the Temporal
        # activity funnel can retry the stage — see temporal.activities._run_stage),
        # or any other failure mapped to the gate's domain error type.
        #
        # These are nested (not module-level) deliberately: they take only the
        # per-iteration draft/validator report as parameters — so they never close
        # over the `rewrite_iter` loop variable — and intentionally close over the
        # loop-INVARIANT collaborators built once above (the agents,
        # require_disclaimer_for, work_dir, brand_spec_prompt_text, _update). That
        # closure is accepted for conciseness; the gates' behavior is covered
        # end-to-end via run_pipeline in test_run_pipeline_gates.py.
        def _fact_check_gate(draft: str):
            """Run the fact-check gate, capturing (not raising) its outcome.

            Preconditions:
                - ``draft`` is the current draft text to check.
            Postconditions:
                - Returns ``(FactCheckReport, None)`` on success, or ``(None, error)`` on
                  failure — CAPTURING every failure so ``parallel_map`` runs the sibling
                  gate to completion instead of fast-failing (see the block comment above).
                - The captured ``error`` preserves its class: ``BloggingError``,
                  ``CancelledError``, and transient ``LLMRateLimitError``/``LLMTemporaryError``
                  pass through unwrapped (for cancellation/Temporal-retry handling); an
                  external cancellation surfacing as another type is passed through too;
                  any other exception is wrapped in ``FactCheckError``.
            """
            # Both gates report progress under BlogPhase.FACT_CHECK — the umbrella phase
            # for this concurrent step — so the two callbacks don't flip the UI phase
            # back and forth between FACT_CHECK and COMPLIANCE while they run together.
            try:
                report = fact_check_agent.run(
                    draft,
                    require_disclaimer_for=require_disclaimer_for,
                    work_dir=work_dir,
                    on_llm_request=lambda msg: _update(BlogPhase.FACT_CHECK, status_text=msg),
                )
                return report, None
            except (BloggingError, CancelledError, LLMRateLimitError, LLMTemporaryError) as e:
                return None, e
            except Exception as e:
                if _is_external_cancellation(e):
                    return None, e
                return None, FactCheckError(f"Fact check failed: {e}", cause=e)

        def _compliance_gate(draft: str, validator_report):
            """Run the compliance gate, capturing (not raising) its outcome.

            Preconditions:
                - ``draft`` is the current draft text; ``validator_report`` is the
                  deterministic validator result (a Pydantic model, or a stand-in that
                  the ``model_dump`` guard tolerates) that compliance consumes.
            Postconditions:
                - Returns ``(ComplianceReport, None)`` on success, or ``(None, error)`` on
                  failure — capturing every failure (same rationale as ``_fact_check_gate``).
                - The captured ``error`` preserves its class: ``BloggingError``,
                  ``CancelledError``, and transient LLM errors pass through unwrapped, an
                  external cancellation surfacing as another type is passed through, and
                  any other exception is wrapped in ``ComplianceError``.
            """
            # Reports progress under BlogPhase.FACT_CHECK too — see _fact_check_gate; the
            # umbrella phase keeps the concurrent gates from flip-flopping the UI phase.
            try:
                report = compliance_agent.run(
                    draft,
                    brand_spec_prompt=brand_spec_prompt_text,
                    # validator_report is normally a Pydantic model, but the
                    # hasattr guard tolerates plain-object stand-ins from test
                    # doubles / legacy validator paths (passes None if absent).
                    validator_report=validator_report.model_dump()
                    if hasattr(validator_report, "model_dump")
                    else None,
                    work_dir=work_dir,
                    on_llm_request=lambda msg: _update(BlogPhase.FACT_CHECK, status_text=msg),
                )
                return report, None
            except (BloggingError, CancelledError, LLMRateLimitError, LLMTemporaryError) as e:
                return None, e
            except Exception as e:
                if _is_external_cancellation(e):
                    return None, e
                return None, ComplianceError(f"Compliance check failed: {e}", cause=e)

        for rewrite_iter in range(max_rewrite_iterations):
            _update(
                BlogPhase.FACT_CHECK,
                sub_progress=rewrite_iter / max_rewrite_iterations,
                status_text=f"Running fact-check + compliance (iteration {rewrite_iter + 1})...",
                rewrite_iterations=rewrite_iter,
            )

            # Deterministic validators run first (non-LLM, and the compliance gate
            # consumes their report). Their failures map to FactCheckError as before.
            try:
                validator_report = run_validators_from_work_dir(work_dir)
            except BloggingError:
                raise
            except CancelledError:
                raise
            except Exception as e:
                if _is_external_cancellation(e):
                    raise
                raise FactCheckError(f"Fact check failed: {e}", cause=e) from e

            # Fan the two independent LLM gates out concurrently. parallel_map copies
            # this thread's context into each worker so the LLM attribution /
            # request-id contextvars propagate (a raw ThreadPoolExecutor would not;
            # see llm_service.attribution). partial() binds the current draft /
            # validator report eagerly; preserve_order keeps [fact, compliance]
            # positional; skip_none=False because each gate always returns a
            # (report, error) tuple. Because the gates capture rather than raise,
            # parallel_map never fast-fails — both run to completion before we
            # propagate any failure (no abandoned worker; see the gate comment above).
            (fact_report, fact_error), (compliance_report, compliance_error) = parallel_map(
                [
                    partial(_fact_check_gate, draft_result.draft),
                    partial(_compliance_gate, draft_result.draft, validator_report),
                ],
                lambda gate: gate(),
                max_workers=2,
                preserve_order=True,
                skip_none=False,
            )

            # Both gates have finished; propagate a failure (if any) with a fixed
            # precedence: cancellation first, then a transient LLM-transport error
            # (prefer a Temporal stage retry over a terminal domain failure), then the
            # fact-check domain error, then the compliance one (input order).
            #
            # The three passes are deliberate: each pass scans BOTH gates for a
            # higher-priority error class before falling through, so cancellation from
            # *either* gate wins over a transient from the other, which in turn wins over
            # any domain error — a single positional pass could not express that ordering.
            gate_errors = [e for e in (fact_error, compliance_error) if e is not None]
            # Only one error is raised (by the precedence below), so when BOTH gates
            # failed, log every error first — otherwise the lower-precedence failure
            # would be silently discarded and never reach the logs.
            if len(gate_errors) > 1:
                logger.error(
                    "Both gates failed on rewrite iteration %s; raising by precedence, "
                    "all gate errors: %s",
                    rewrite_iter + 1,
                    [f"{type(e).__name__}: {e}" for e in gate_errors],
                )
            for gate_error in gate_errors:
                if isinstance(gate_error, CancelledError) or _is_external_cancellation(gate_error):
                    raise gate_error
            for gate_error in gate_errors:
                if isinstance(gate_error, (LLMRateLimitError, LLMTemporaryError)):
                    raise gate_error
            if gate_errors:
                raise gate_errors[0]

            all_pass = (
                validator_report.status == "PASS"
                and fact_report.claims_status == "PASS"
                and fact_report.risk_status == "PASS"
                and compliance_report.status == "PASS"
            )
            if all_pass:
                status = "PASS"
                logger.info("All gates PASS on rewrite iteration %s", rewrite_iter + 1)

                # ── Title selection: user picks the final title ─────────
                selected_title = _run_title_selection(
                    plan=plan,
                    llm_client=llm_client,
                    job_id=job_id,
                    job_updater=job_updater,
                    _update=_update,
                )

                _update(
                    BlogPhase.FINALIZE,
                    sub_progress=0.5,
                    status_text="Finalizing...",
                )

                title_options = (
                    [selected_title]
                    if selected_title
                    else [tc.title for tc in plan.title_candidates[:5]]
                )
                pack = PublishingPack(
                    title_options=title_options,
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
                                details_str = f" Found: {', '.join(str(m) for m in check.details['matches'])}"
                            elif "violations" in check.details:
                                details_str = f" Violations: {', '.join(str(v) for v in check.details['violations'])}"
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
                gate_failures.append(
                    f"Fact-check FAIL (claims={fact_report.claims_status}, risk={fact_report.risk_status})"
                )
            if compliance_report.status == "FAIL":
                gate_failures.append(
                    f"Compliance FAIL ({len(compliance_report.violations)} violations)"
                )
            feedback_summary = "; ".join(gate_failures) if gate_failures else "Gates failed"

            try:
                revise_input = ReviseWriterInput(
                    draft=draft_result.draft,
                    feedback_items=feedback_items,
                    feedback_summary=feedback_summary,
                    content_plan=plan,
                    audience=brief.audience,
                    tone_or_purpose=brief.tone_or_purpose,
                    target_word_count=length_policy.target_word_count,
                    length_guidance=build_draft_length_instruction(length_policy),
                    selected_title=None,
                    elicited_stories=elicited_stories_text or None,
                )
                draft_output_path = Path(work_dir) / f"draft_rewrite_{rewrite_iter + 1}.md"
                draft_result = draft_agent.revise(
                    revise_input,
                    on_llm_request=lambda msg: _update(BlogPhase.REWRITE_LOOP, status_text=msg),
                    draft_output_path=draft_output_path,
                    work_dir=work_dir,
                    iteration=rewrite_iter + 1,
                )
            except (BloggingError, CancelledError, LLMRateLimitError, LLMTemporaryError):
                # Transient LLM-transport errors propagate unwrapped for Temporal retry.
                raise
            except Exception as e:
                if _is_external_cancellation(e):
                    raise
                raise DraftError(
                    f"Rewrite revision failed: {e}", iteration=rewrite_iter + 1, cause=e
                ) from e

            write_artifact(work_dir, "final.md", draft_result.draft)
            logger.info("Rewrite iteration %s: applied fixes, re-running gates", rewrite_iter + 1)
    else:
        # No gates — run title selection before finalizing
        selected_title = _run_title_selection(
            plan=plan,
            llm_client=llm_client,
            job_id=job_id,
            job_updater=job_updater,
            _update=_update,
        )
        _update(
            BlogPhase.FINALIZE,
            sub_progress=1.0,
            status_text="Pipeline complete (gates skipped)",
        )

    ctx.draft_result = draft_result
    ctx.status = status
    return None


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
