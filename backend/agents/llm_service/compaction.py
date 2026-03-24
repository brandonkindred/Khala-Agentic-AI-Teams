"""
LLM-powered text compaction for context fitting.

Instead of naively truncating content to fit within an LLM context window,
compact_text() uses the LLM itself to produce a shorter version that preserves
all essential technical detail: code, specs, requirements, architecture, etc.

Usage::

    from llm_service import compact_text

    prompt_body = compact_text(
        large_spec,
        max_chars=budget,
        llm=llm,
        content_description="product specification",
    )
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .interface import LLMClient

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3.5  # conservative estimate for code/spec text


def compact_text(
    text: str,
    max_chars: int,
    llm: "LLMClient",
    content_description: str = "content",
) -> str:
    """Return *text* as-is when it fits, otherwise ask the LLM to compact it.

    Parameters
    ----------
    text:
        The source text that may exceed the budget.
    max_chars:
        Target character budget.  Content at or below this is returned unchanged.
    llm:
        An ``LLMClient`` used to perform the compaction when needed.
    content_description:
        Human-readable label for the content type (e.g. "research document",
        "architecture overview").  Included in the compaction prompt so the LLM
        knows what it is summarising.

    Returns
    -------
    str
        The original text if it fits, or a compacted version produced by the LLM.
        On any LLM failure the original text is returned so callers never lose data.
    """
    if not text or len(text) <= max_chars:
        return text or ""

    overage = len(text) - max_chars
    logger.info(
        "Compacting %s: %d chars over budget (%d chars → target %d chars)",
        content_description,
        overage,
        len(text),
        max_chars,
    )

    # Build a compaction prompt.  We include the full text and ask the LLM to
    # produce a condensed version that fits within the target budget.
    prompt = (
        f"You are a precise technical content compactor.  Condense the following "
        f"{content_description} to approximately {max_chars:,} characters.\n\n"
        f"Rules:\n"
        f"- Preserve ALL code snippets, technical identifiers, file paths, and data values verbatim.\n"
        f"- Preserve ALL requirements, constraints, and specifications.\n"
        f"- Remove redundancy, verbose prose, filler, and repeated information.\n"
        f"- Keep the original structure (headings, lists, sections) where possible.\n"
        f"- Do NOT add commentary, preamble, or explanation — output ONLY the compacted content.\n\n"
        f"--- BEGIN CONTENT ---\n"
        f"{text}\n"
        f"--- END CONTENT ---\n\n"
        f"Compacted version:"
    )

    try:
        result = llm.complete(prompt, temperature=0.0)
        result = result.strip()
        if not result:
            logger.warning(
                "Compaction returned empty result for %s, returning original", content_description
            )
            return text
        logger.info(
            "Compaction result for %s: %d chars (target %d)",
            content_description,
            len(result),
            max_chars,
        )
        return result
    except Exception:
        logger.warning(
            "Compaction failed for %s, returning original text", content_description, exc_info=True
        )
        return text
