"""
Blog draft agent: takes a research document and an outline and generates
a blog post draft that complies with a brand and writing style guide.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from blog_research_agent.llm import LLMClient

from .models import DraftInput, DraftOutput, ReviseDraftInput
from .prompts import DRAFT_SYSTEM_REMINDER, MINIMAL_STYLE_REMINDER, REVISE_DRAFT_PROMPT

logger = logging.getLogger(__name__)


def _load_style_guide(path: str | Path) -> str:
    """Load style guide text from a file. Raises OSError if file cannot be read."""
    return Path(path).read_text().strip()


class BlogDraftAgent:
    """
    Agent that generates a blog post draft from a research document and outline,
    following a provided brand and writing style guide.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        default_style_guide_path: Optional[str | Path] = None,
    ) -> None:
        """
        Preconditions:
            - llm_client is not None.
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self.default_style_guide_path = Path(default_style_guide_path) if default_style_guide_path else None

    def run(self, draft_input: DraftInput) -> DraftOutput:
        """
        Generate a blog post draft from the research document and outline.

        Preconditions:
            - draft_input is a valid DraftInput (research_document and outline non-empty).
        Postconditions:
            - Returns DraftOutput with draft (Markdown string) compliant with the style guide.
        """
        research = draft_input.research_document.strip()
        outline = draft_input.outline.strip()
        if not research or not outline:
            logger.warning("Empty research_document or outline; returning minimal draft.")
            return DraftOutput(draft="# Draft\n\nAdd research document and outline to generate a draft.")

        # Resolve style guide text
        if draft_input.style_guide:
            style_guide_text = draft_input.style_guide.strip()
        elif self.default_style_guide_path and self.default_style_guide_path.exists():
            try:
                style_guide_text = _load_style_guide(self.default_style_guide_path)
            except OSError as e:
                logger.warning("Could not load default style guide from %s: %s", self.default_style_guide_path, e)
                style_guide_text = MINIMAL_STYLE_REMINDER
        else:
            style_guide_text = MINIMAL_STYLE_REMINDER

        logger.info(
            "Generating draft: research len=%s, outline len=%s, style_guide len=%s",
            len(research),
            len(outline),
            len(style_guide_text),
        )

        prompt_parts = [
            DRAFT_SYSTEM_REMINDER,
            "",
            "---",
            "STYLE GUIDE (you must follow every applicable rule):",
            "---",
            style_guide_text,
            "",
            "---",
            "RESEARCH DOCUMENT (use this for facts, examples, and substance):",
            "---",
            research,
            "",
            "---",
            "OUTLINE (follow this structure):",
            "---",
            outline,
        ]
        if draft_input.audience:
            prompt_parts.append("")
            prompt_parts.append(f"Audience: {draft_input.audience}")
        if draft_input.tone_or_purpose:
            prompt_parts.append(f"Tone/Purpose: {draft_input.tone_or_purpose}")
        prompt_parts.append("")
        prompt_parts.append("---")
        prompt_parts.append("Return JSON with a single key \"draft\" whose value is the full blog post in Markdown.")
        prompt = "\n".join(prompt_parts)

        data = self.llm.complete_json(prompt, temperature=0.3)
        raw_draft = data.get("draft")
        if isinstance(raw_draft, str):
            draft = raw_draft.strip()
        else:
            draft = ""

        if not draft:
            logger.warning("LLM returned no draft content; returning placeholder.")
            draft = "# Draft\n\nNo draft was generated. Check the model response or try again."

        logger.info("Draft generated: length=%s", len(draft))
        return DraftOutput(draft=draft)

    def revise(self, revise_input: ReviseDraftInput) -> DraftOutput:
        """
        Revise a draft based on copy editor feedback.

        Preconditions:
            - revise_input has non-empty draft and feedback_items.
        Postconditions:
            - Returns DraftOutput with the revised draft.
        """
        draft = revise_input.draft.strip()
        if not draft:
            logger.warning("Empty draft in revise; returning as-is.")
            return DraftOutput(draft=revise_input.draft)
        if not revise_input.feedback_items:
            logger.info("No feedback items; returning draft unchanged.")
            return DraftOutput(draft=draft)

        # Resolve style guide
        if revise_input.style_guide:
            style_guide_text = revise_input.style_guide.strip()
        elif self.default_style_guide_path and self.default_style_guide_path.exists():
            try:
                style_guide_text = _load_style_guide(self.default_style_guide_path)
            except OSError as e:
                logger.warning(
                    "Could not load default style guide from %s: %s",
                    self.default_style_guide_path,
                    e,
                )
                style_guide_text = MINIMAL_STYLE_REMINDER
        else:
            style_guide_text = MINIMAL_STYLE_REMINDER

        # Format feedback for prompt
        feedback_lines = []
        if revise_input.feedback_summary:
            feedback_lines.append(f"Overall summary: {revise_input.feedback_summary}\n")
        for i, item in enumerate(revise_input.feedback_items, 1):
            loc = f" [{item.location}]" if item.location else ""
            feedback_lines.append(f"{i}. [{item.severity}] {item.category}{loc}: {item.issue}")
            if item.suggestion:
                feedback_lines.append(f"   Suggestion: {item.suggestion}")
            feedback_lines.append("")

        prompt_parts = [
            REVISE_DRAFT_PROMPT,
            "",
            "---",
            "STYLE GUIDE:",
            "---",
            style_guide_text,
            "",
            "---",
            "COPY EDITOR FEEDBACK:",
            "---",
            "\n".join(feedback_lines).strip(),
            "",
            "---",
            "CURRENT DRAFT:",
            "---",
            draft,
        ]
        if revise_input.audience:
            prompt_parts.insert(0, f"Audience: {revise_input.audience}\n")
        if revise_input.tone_or_purpose:
            prompt_parts.insert(0, f"Tone/Purpose: {revise_input.tone_or_purpose}\n")
        if revise_input.research_document:
            research = revise_input.research_document.strip()
            research_snippet = research[:3000] + ("..." if len(research) > 3000 else "")
            prompt_parts.extend([
                "",
                "---",
                "RESEARCH (for context; preserve facts):",
                "---",
                research_snippet,
            ])
        prompt_parts.extend([
            "",
            "---",
            "Return JSON with a single key \"draft\" whose value is the full revised blog post in Markdown.",
        ])
        prompt = "\n".join(prompt_parts)

        data = self.llm.complete_json(prompt, temperature=0.2)
        raw_draft = data.get("draft")
        if isinstance(raw_draft, str):
            revised = raw_draft.strip()
        else:
            revised = draft

        if not revised:
            logger.warning("LLM returned no revised content; keeping original draft.")
            revised = draft

        logger.info("Draft revised: length=%s", len(revised))
        return DraftOutput(draft=revised)
