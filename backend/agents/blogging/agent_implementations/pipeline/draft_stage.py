"""Draft stage: initial draft, interactive review, and the copy-edit loop."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from agents.blogging.blog_writer_agent.models import WriterOutput

from agents.blogging.blog_copy_editor_agent import CopyEditorInput
from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
from agents.blogging.blog_writer_agent import ReviseWriterInput, WriterInput
from agents.blogging.shared.artifacts import read_artifact
from agents.blogging.shared.blog_job_store import (
    add_blog_pending_questions,
    is_waiting_for_blog_answers,
    record_guideline_updates,
)
from agents.blogging.shared.content_plan import (
    PlanningPhaseResult,
    content_plan_to_outline_markdown,
)
from agents.blogging.shared.content_profile import build_draft_length_instruction
from agents.blogging.shared.errors import BloggingError, DraftError
from agents.blogging.shared.models import BlogPhase
from agents.blogging.shared.run_pipeline_job import _is_external_cancellation
from agents.blogging.shared.style_loader import append_guidelines
from temporalio.exceptions import CancelledError

from llm_service.interface import LLMRateLimitError, LLMTemporaryError

from ._common import (
    _fill_story_placeholders,
    _load_required_guidelines,
    _make_update,
    _wait_for_hitl,
)
from .constants import COPY_EDIT_ESCALATION_THRESHOLD, STYLE_GUIDE_PATH
from .context import PipelineContext, PipelineStatus

logger = logging.getLogger(__name__)


def run_draft_stage(
    ctx: "PipelineContext",
) -> Optional[Tuple[PlanningPhaseResult, Optional["WriterOutput"], PipelineStatus]]:
    """Draft stage: initial draft, interactive review, and the copy-edit loop.

    Also reads an optional ``allowed_claims.json`` artifact from ``ctx.work_dir``
    and threads it into the writer/revision prompts so factual claims can be
    tagged with ``[CLAIM:id]``.

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
        - An optional ``allowed_claims.json`` artifact may exist in ``ctx.work_dir``.
          If present and a dict, its contents are passed to the draft writer and
          subsequent revision calls (including ``revise_from_user_feedback``) as
          ``allowed_claims``; a missing or non-dict artifact (or no ``work_dir`` at
          all) is a no-op (matching the fact-check/validator gates' handling of the
          same artifact).
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
    # Deferred import: see agents.blogging.agent_implementations.pipeline._common's
    # module docstring — keeps monkeypatch.setattr(shim, "BlogWriterAgent", ...) /
    # ("BlogCopyEditorAgent", ...) / ("load_style_file", ...) effective now that this
    # code lives outside the shim.
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        BlogCopyEditorAgent,
        BlogWriterAgent,
        load_style_file,
    )

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

    # Load allowed_claims.json (if a prior pipeline step has persisted one — see
    # ARTIFACT_PRODUCER in shared/artifacts.py) so the writer can tag factual claims
    # with [CLAIM:id]; a missing/non-dict artifact is a no-op, matching the
    # fact-check/validator gates' handling of the same artifact.
    allowed_claims = (
        read_artifact(work_dir, "allowed_claims.json", default=None) if work_dir else None
    )
    if not isinstance(allowed_claims, dict):
        allowed_claims = None

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
    from agents.blogging.blog_writer_agent.feedback_tracker import (
        MAX_PREVIOUS_FEEDBACK_ITEMS,
        FeedbackTracker,
    )

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
                    allowed_claims=allowed_claims,
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
                        allowed_claims=allowed_claims,
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
                                allowed_claims=allowed_claims,
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
                        allowed_claims=allowed_claims,
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
                if copy_editor_result.feedback_file_written is False:
                    logger.warning(
                        "Copy editor feedback file failed to write for iteration %s (path=%s)",
                        copy_edit_num,
                        feedback_path,
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
                            allowed_claims=allowed_claims,
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
                        max_items=MAX_PREVIOUS_FEEDBACK_ITEMS
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
                    allowed_claims=allowed_claims,
                )
                previous_feedback_items = feedback_tracker.get_capped_previous_feedback(
                    max_items=MAX_PREVIOUS_FEEDBACK_ITEMS
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
