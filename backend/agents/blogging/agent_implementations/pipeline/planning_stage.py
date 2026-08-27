"""Planning stage: content planning, story elicitation, and outline approval."""

import logging
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from agents.blogging.blog_writer_agent.models import WriterOutput

from agents.blogging.shared.content_plan import (
    PlanningInput,
    PlanningPhaseResult,
    content_plan_to_markdown_doc,
    content_plan_to_outline_markdown,
)
from agents.blogging.shared.content_profile import (
    build_planning_length_context,
    series_context_block,
)
from agents.blogging.shared.errors import PlanningError
from agents.blogging.shared.models import BlogPhase
from temporalio.exceptions import CancelledError

from llm_service.interface import LLMRateLimitError, LLMTemporaryError

from ._common import (
    _extract_plan_keywords,
    _make_update,
    _persist_content_plan_artifacts,
    _save_narratives_to_story_bank,
    _wait_for_hitl,
    planning_llm_client,
)
from .context import PipelineContext, PipelineStatus

logger = logging.getLogger(__name__)


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
          HITL wait ends without a human response — either the job was
          cancelled/failed while awaiting outline approval, or the job record
          disappeared from the store mid-wait. This tuple sentinel mirrors
          ``run_pipeline``'s return shape so the sequencer forwards it unchanged
          (see ``run_draft_stage`` for the rationale).
        - Each outline-feedback re-plan round refreshes ``content_plan.json``,
          ``content_plan.md``, ``outline.md``, ``content_brief.md``, and
          ``allowed_claims.json`` together (via the same
          ``_persist_content_plan_artifacts`` helper ``run_planning`` uses),
          so ``allowed_claims.json`` never goes stale relative to the plan the
          user is currently reviewing.
    Raises:
        PlanningError: when content planning fails (e.g. max parse retries).
        BloggingError: any other blogging-domain failure from the planning agent
            propagates unchanged.
        LLMRateLimitError / LLMTemporaryError: transient LLM-transport failures
            from ``run_planning`` (including the plan critic) propagate unwrapped
            for Temporal retry.
        CancelledError: a Temporal-native cancellation propagates (never swallowed);
            a cancellation surfaced *while awaiting outline approval* instead
            short-circuits to the FAIL tuple above.
    """
    # Deferred import: see agents.blogging.agent_implementations.pipeline._common's
    # module docstring — keeps monkeypatch.setattr(shim, "run_planning", ...) /
    # ("BlogWriterAgent", ...) effective now that this code lives outside the shim.
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        BlogWriterAgent,
    )
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        run_planning as _run_planning,
    )

    brief = ctx.brief
    work_dir = ctx.work_dir
    llm_client = ctx.llm_client
    length_policy = ctx.length_policy
    series_context = ctx.series_context
    job_id = ctx.job_id
    job_updater = ctx.job_updater
    _update = _make_update(job_updater)

    planning_phase_result = _run_planning(
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
                except (LLMRateLimitError, LLMTemporaryError, PlanningError):
                    raise
                except Exception as e:
                    logger.warning("Re-planning with feedback failed: %s; keeping current plan", e)

                outline_text = content_plan_to_outline_markdown(plan)

                # Persist updated artifacts (including a refreshed allowed_claims.json,
                # via the same helper run_planning uses — see its docstring).
                content_plan_markdown = None
                if work_dir is not None:
                    content_plan_markdown = _persist_content_plan_artifacts(
                        work_dir, plan, llm_client=llm_client, topic=brief.brief
                    )
                if content_plan_markdown is None:
                    content_plan_markdown = content_plan_to_markdown_doc(plan)

                # Present revised outline for another round
                _update(
                    BlogPhase.PLANNING,
                    sub_progress=0.8,
                    status_text="Waiting for approval of revised outline...",
                    content_plan_detail=content_plan_markdown,
                )
                request_draft_feedback(
                    job_id,
                    draft=outline_text,
                    revision=outline_revision,
                )

        except CancelledError:
            raise
        except (LLMRateLimitError, LLMTemporaryError, PlanningError):
            raise
        except Exception as e:
            logger.warning("Outline approval phase error (skipping): %s", e)

    ctx.planning_phase_result = planning_phase_result
    ctx.plan = plan
    ctx.elicited_stories_text = elicited_stories_text
    return None
