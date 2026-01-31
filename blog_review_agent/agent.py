"""
Blog review agent: reviews a list of sources and the original brief to produce
title choices (with success probability) and a detailed blog outline.
"""

from __future__ import annotations

import logging
from typing import List

from blog_research_agent.llm import LLMClient
from blog_research_agent.models import ResearchReference

from .models import BlogReviewInput, BlogReviewOutput, TitleChoice
from .prompts import BLOG_REVIEW_PROMPT

logger = logging.getLogger(__name__)


def _format_references_for_prompt(references: List[ResearchReference]) -> str:
    """Format references as text for the LLM prompt."""
    parts = []
    for i, ref in enumerate(references, start=1):
        parts.append(f"Source {i}: {ref.title}")
        parts.append(f"  URL: {ref.url}")
        parts.append(f"  Summary: {ref.summary}")
        if ref.key_points:
            parts.append("  Key points:")
            for p in ref.key_points:
                parts.append(f"    - {p}")
        parts.append("")
    return "\n".join(parts).strip()


class BlogReviewAgent:
    """
    Agent that reviews a brief and researched sources to produce:
    1. Top 10 catchy title/soundbite choices with probability of success.
    2. A detailed blog post outline with notes useful for a first draft.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        """
        Preconditions:
            - llm_client is not None.
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client

    def run(self, review_input: BlogReviewInput) -> BlogReviewOutput:
        """
        Produce title choices and blog outline from the brief and sources.

        Preconditions:
            - review_input is a valid BlogReviewInput (brief + references).
        Postconditions:
            - Returns BlogReviewOutput with exactly 10 title_choices and a non-empty outline.
        """
        logger.info(
            "Blog review: brief=%s, %s sources",
            review_input.brief[:60] + "..." if len(review_input.brief) > 60 else review_input.brief,
            len(review_input.references),
        )

        refs_text = _format_references_for_prompt(review_input.references)
        context = [
            "BRIEF:",
            review_input.brief,
            "",
        ]
        if review_input.audience:
            context.append(f"Audience: {review_input.audience}\n")
        if review_input.tone_or_purpose:
            context.append(f"Tone/Purpose: {review_input.tone_or_purpose}\n")
        context.append("SOURCES (summaries and key points):")
        context.append(refs_text)

        prompt = BLOG_REVIEW_PROMPT + "\n\n" + "\n".join(context)
        data = self.llm.complete_json(prompt, temperature=0.4)

        title_choices_data = data.get("title_choices") or []
        outline_raw = data.get("outline") or ""

        # Build TitleChoice list (take up to 10, clamp probability)
        title_choices: List[TitleChoice] = []
        for item in title_choices_data[:10]:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or ""
            prob = item.get("probability_of_success")
            if not title:
                continue
            if prob is not None:
                try:
                    p = float(prob)
                    p = max(0.0, min(1.0, p))
                    title_choices.append(TitleChoice(title=title, probability_of_success=p))
                except (TypeError, ValueError):
                    title_choices.append(TitleChoice(title=title, probability_of_success=0.5))
            else:
                title_choices.append(TitleChoice(title=title, probability_of_success=0.5))

        # If we got fewer than 10, pad with placeholders so we always return 10
        while len(title_choices) < 10:
            title_choices.append(
                TitleChoice(
                    title=f"Title option {len(title_choices) + 1} (review suggested)",
                    probability_of_success=0.5,
                )
            )

        outline = outline_raw.strip() or "No outline generated; please try again with more sources."

        logger.info(
            "Blog review complete: %s title choices, outline length=%s",
            len(title_choices),
            len(outline),
        )
        return BlogReviewOutput(title_choices=title_choices[:10], outline=outline)
