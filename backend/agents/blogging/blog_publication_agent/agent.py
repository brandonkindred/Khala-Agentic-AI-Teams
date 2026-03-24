"""
Blog publication agent: receives final drafts, writes to blog_posts, handles
approval/rejection, and generates platform-specific versions.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from blog_copy_editor_agent import BlogCopyEditorAgent
    from blog_draft_agent import BlogDraftAgent

from shared.content_plan import (
    ContentPlan,
    ContentPlanSection,
    RequirementsAnalysis,
    TitleCandidate,
)

from llm_service import LLMClient

from .models import (
    ApprovalResult,
    PublicationMetadata,
    PublicationSubmission,
    RejectionResponse,
    RevisionLoopResult,
    SubmitDraftInput,
    _slugify,
)
from .platform_formatters import (
    _extract_title_from_draft,
    format_for_devto,
    format_for_medium,
    format_for_substack,
)
from .prompts import CONVERT_FEEDBACK_TO_EDITOR_PROMPT, REJECTION_FOLLOW_UP_PROMPT

logger = logging.getLogger(__name__)


def _content_plan_from_outline(outline: str) -> ContentPlan:
    """Build a minimal content plan when only a flat outline string exists (e.g. legacy submissions)."""
    body = (outline or "").strip() or "Article body"
    return ContentPlan(
        overarching_topic="Blog post",
        narrative_flow="Follow the outline/coverage below.",
        sections=[
            ContentPlanSection(
                title="Main",
                coverage_description=body[:8000],
                order=0,
            )
        ],
        title_candidates=[TitleCandidate(title="Post", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True,
            scope_feasible=True,
            research_gaps=[],
        ),
    )


class BlogPublicationAgent:
    """
    Expert agent that receives final drafts, writes them to blog_posts, waits for human
    approval, and on approval creates platform-specific versions (Medium, dev.to,
    Substack). On rejection, collects feedback and runs the draft-editor loop.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        blog_posts_root: Optional[str | Path] = None,
        max_revision_loops: int = 500,
    ) -> None:
        """
        Preconditions:
            - llm_client is not None.
            - max_revision_loops >= 1.
        """
        assert llm_client is not None, "llm_client is required"
        assert max_revision_loops >= 1, "max_revision_loops must be >= 1"

        self.llm = llm_client
        self.blog_posts_root = Path(blog_posts_root or self._default_blog_posts_root())
        self.pending_dir = self.blog_posts_root / "pending"
        self.max_revision_loops = max_revision_loops

        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.blog_posts_root.mkdir(parents=True, exist_ok=True)

    def _default_blog_posts_root(self) -> Path:
        return Path(__file__).resolve().parent.parent / "blog_posts"

    def submit_draft(self, input: SubmitDraftInput) -> PublicationSubmission:
        """
        Write the draft to blog_posts/pending and return a submission awaiting approval.
        """
        draft = input.draft.strip()
        if not draft:
            raise ValueError("Draft cannot be empty")

        title = input.title or _extract_title_from_draft(draft)
        slug = input.slug or _slugify(title)
        submission_id = f"{slug}-{uuid.uuid4().hex[:8]}"

        meta = PublicationMetadata(
            submission_id=submission_id,
            slug=slug,
            title=title,
            draft_content=draft,
            audience=input.audience,
            tone_or_purpose=input.tone_or_purpose,
            tags=input.tags or [],
            state="awaiting_approval",
        )

        file_path = self.pending_dir / f"{submission_id}.md"
        meta_path = self.pending_dir / f"{submission_id}_meta.json"

        file_path.write_text(draft, encoding="utf-8")
        meta.save(meta_path)

        logger.info("Draft submitted: submission_id=%s, path=%s", submission_id, file_path)

        return PublicationSubmission(
            submission_id=submission_id,
            slug=slug,
            file_path=file_path,
            state="awaiting_approval",
            message="Draft written. Awaiting your approval. Call approve() when ready, or reject() with feedback.",
        )

    def approve(self, submission_id: str) -> ApprovalResult:
        """
        Approve the submission: create folder, move draft, generate platform versions.
        """
        meta_path = self.pending_dir / f"{submission_id}_meta.json"
        draft_path = self.pending_dir / f"{submission_id}.md"

        if not meta_path.exists() or not draft_path.exists():
            raise FileNotFoundError(f"Submission not found: {submission_id}")

        meta = PublicationMetadata.load(meta_path)
        if meta.state != "awaiting_approval":
            raise ValueError(
                f"Submission {submission_id} is not awaiting approval (state={meta.state})"
            )

        post_dir = self.blog_posts_root / meta.slug
        post_dir.mkdir(parents=True, exist_ok=True)

        new_draft_path = post_dir / "draft.md"
        new_draft_path.write_text(meta.draft_content, encoding="utf-8")

        draft_path.unlink()

        medium_content = format_for_medium(meta.draft_content)
        devto_content = format_for_devto(
            meta.draft_content,
            title=meta.title,
            tags=meta.tags if meta.tags else None,
        )
        substack_content = format_for_substack(meta.draft_content)

        medium_path = post_dir / "medium.md"
        devto_path = post_dir / "devto.md"
        substack_path = post_dir / "substack.md"

        medium_path.write_text(medium_content, encoding="utf-8")
        devto_path.write_text(devto_content, encoding="utf-8")
        substack_path.write_text(substack_content, encoding="utf-8")

        meta.state = "approved"
        meta.approved_at = datetime.utcnow().isoformat()
        meta.save(post_dir / "metadata.json")
        meta_path.unlink()

        logger.info("Submission approved: %s, folder=%s", submission_id, post_dir)

        return ApprovalResult(
            submission_id=submission_id,
            folder_path=post_dir,
            draft_path=new_draft_path,
            medium_path=medium_path,
            devto_path=devto_path,
            substack_path=substack_path,
            message=f"Post approved. Platform versions created in {post_dir}",
        )

    def reject(
        self,
        submission_id: str,
        feedback: str,
        *,
        force_ready_to_revise: bool = False,
    ) -> RejectionResponse:
        """
        Reject the submission with feedback. Uses LLM to ask follow-up questions
        if needed. Returns questions to ask, or ready_to_revise=True when done.
        Set force_ready_to_revise=True to skip follow-up questions and proceed to revision.
        """
        meta_path = self.pending_dir / f"{submission_id}_meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"Submission not found: {submission_id}")

        meta = PublicationMetadata.load(meta_path)
        meta.rejection_feedback.append(feedback.strip())
        meta.state = "collecting_rejection_feedback"
        meta.save(meta_path)

        if force_ready_to_revise:
            feedback_summary = "\n".join(f"- {f}" for f in meta.rejection_feedback)
            return RejectionResponse(
                submission_id=submission_id,
                questions=[],
                ready_to_revise=True,
                collected_feedback_summary=feedback_summary,
            )

        feedback_collected = "\n".join(f"- {f}" for f in meta.rejection_feedback[:-1])
        latest_feedback = meta.rejection_feedback[-1] if meta.rejection_feedback else ""

        prompt = REJECTION_FOLLOW_UP_PROMPT.format(
            feedback_collected=feedback_collected or "(none yet)",
            latest_feedback=latest_feedback,
        )

        data = self.llm.complete_json(prompt, temperature=0.2)

        ready_to_revise = bool(data.get("ready_to_revise", False))
        questions = data.get("questions") or []
        if isinstance(questions, str):
            questions = [q.strip() for q in questions.split("\n") if q.strip()]
        else:
            questions = [str(q).strip() for q in questions if q]

        feedback_summary = (data.get("feedback_summary") or "").strip() or None

        return RejectionResponse(
            submission_id=submission_id,
            questions=questions,
            ready_to_revise=ready_to_revise,
            collected_feedback_summary=feedback_summary,
        )

    def run_revision_loop(
        self,
        submission_id: str,
        *,
        draft_agent: "BlogDraftAgent",
        copy_editor_agent: "BlogCopyEditorAgent",
        research_document: Optional[str] = None,
        outline: Optional[str] = None,
        audience: Optional[str] = None,
        tone_or_purpose: Optional[str] = None,
    ) -> RevisionLoopResult:
        """
        After rejection feedback is collected, run the draft-editor loop to revise
        the post. Uses human feedback + editor feedback. Resets loop count.
        """
        from blog_copy_editor_agent import CopyEditorInput
        from blog_copy_editor_agent.models import FeedbackItem
        from blog_draft_agent import ReviseDraftInput

        meta_path = self.pending_dir / f"{submission_id}_meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"Submission not found: {submission_id}")

        meta = PublicationMetadata.load(meta_path)
        if not meta.rejection_feedback:
            raise ValueError("No rejection feedback collected. Call reject() with feedback first.")

        human_feedback_text = "\n".join(f"- {f}" for f in meta.rejection_feedback)

        data = self.llm.complete_json(
            CONVERT_FEEDBACK_TO_EDITOR_PROMPT.format(feedback=human_feedback_text),
            temperature=0.2,
        )

        feedback_data = data.get("feedback_items") or []
        human_feedback_items: list[FeedbackItem] = []
        for item in feedback_data:
            if isinstance(item, dict) and item.get("issue"):
                human_feedback_items.append(
                    FeedbackItem(
                        category=(item.get("category") or "style").strip(),
                        severity=(item.get("severity") or "must_fix").strip(),
                        location=(item.get("location") or "").strip() or None,
                        issue=item.get("issue", "").strip(),
                        suggestion=(item.get("suggestion") or "").strip() or None,
                    )
                )

        draft = meta.draft_content
        research = research_document or ""
        outline_text = outline or ""
        aud = audience or meta.audience
        tone = tone_or_purpose or meta.tone_or_purpose

        iterations = 0
        for iteration in range(self.max_revision_loops):
            copy_editor_input = CopyEditorInput(
                draft=draft,
                audience=aud,
                tone_or_purpose=tone,
                human_feedback=human_feedback_text if iteration == 0 else None,
            )
            feedback_path = (
                self.pending_dir / f"{submission_id}_editor_feedback_iter_{iteration + 1}.json"
            )
            copy_editor_result = copy_editor_agent.run(
                copy_editor_input, feedback_output_path=feedback_path
            )

            all_feedback = (
                human_feedback_items + copy_editor_result.feedback_items
                if iteration == 0
                else copy_editor_result.feedback_items
            )

            revise_input = ReviseDraftInput(
                draft=draft,
                feedback_items=all_feedback,
                feedback_summary=copy_editor_result.summary,
                research_document=research or None,
                content_plan=_content_plan_from_outline(outline_text),
                audience=aud,
                tone_or_purpose=tone,
            )
            draft_result = draft_agent.revise(revise_input)
            draft = draft_result.draft
            iterations += 1

        meta.draft_content = draft
        meta.rejection_feedback = []
        meta.state = "awaiting_approval"
        meta.save(meta_path)

        draft_path = self.pending_dir / f"{submission_id}.md"
        draft_path.write_text(draft, encoding="utf-8")

        logger.info(
            "Revision loop complete: submission_id=%s, iterations=%s",
            submission_id,
            iterations,
        )

        return RevisionLoopResult(
            submission_id=submission_id,
            revised_draft=draft,
            iterations_completed=iterations,
            message="Draft revised based on your feedback. State reset to awaiting_approval. Review and approve or reject again.",
        )
