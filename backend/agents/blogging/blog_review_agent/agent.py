"""
Blog review agent: reviews a list of sources and the original brief to produce
title choices (with success probability) and a detailed blog outline.
"""

from __future__ import annotations

import ast
import logging
import re
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
    1. Top 5 high-quality title choices with probability of success.
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
            - Returns BlogReviewOutput with up to 5 title_choices and a non-empty outline.
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
        try:
            data = self.llm.complete_json(prompt, temperature=0.4)
        except ValueError as e:
            # LLM may return prose instead of JSON; use raw text as outline
            msg = str(e)
            prefix = "Could not parse JSON from Ollama response: "
            if msg.startswith(prefix):
                try:
                    raw = ast.literal_eval(msg[len(prefix) :])
                    data = {"title_choices": [], "outline": raw}
                except (ValueError, SyntaxError):
                    raise
            else:
                raise

        title_choices_data = data.get("title_choices") or []
        outline_raw = data.get("outline") or ""

        # Build TitleChoice list (take up to 5, clamp probability). Reject placeholder-like titles.
        def _is_placeholder(t: str) -> bool:
            lower = t.lower()
            if "title option" in lower or "placeholder" in lower or "example title" in lower:
                return True
            if re.search(r"\boption\s*\d+\b", lower):  # "option 1", "option 2"
                return True
            return False

        title_choices: List[TitleChoice] = []
        for item in title_choices_data[:5]:
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            prob = item.get("probability_of_success")
            if not title or _is_placeholder(title):
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

        outline = outline_raw.strip() or "No outline generated; please try again with more sources."

        logger.info(
            "Blog review complete: %s title choices, outline length=%s",
            len(title_choices),
            len(outline),
        )
        return BlogReviewOutput(title_choices=title_choices, outline=outline)
