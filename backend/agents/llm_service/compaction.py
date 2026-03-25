"""
LLM-powered text compaction for context fitting.

Instead of naively truncating content to fit within an LLM context window,
compact_text() uses the LLM itself to produce a shorter version that preserves
all essential technical detail: code, specs, requirements, architecture, etc.

When the input is too large for a single compaction call, it is split into
chunks that each fit within the model's context, compacted independently,
and concatenated.

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
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from .interface import LLMClient

logger = logging.getLogger(__name__)

# Very conservative chars-per-token for chunk sizing.  Web-fetched content,
# HTML residue, and non-English text can tokenize at <2 chars/token.
# Using 1.0 ensures chunks never exceed the model context even in worst cases.
_CHUNK_CHARS_PER_TOKEN = 1.0

# Reserve tokens for the compaction prompt template + response.
_PROMPT_OVERHEAD_TOKENS = 4000
_RESPONSE_RESERVE_TOKENS = 8000


def _get_model_chunk_chars(llm: "LLMClient") -> int:
    """Max chars of source text that fit in one compaction call."""
    ctx = llm.get_max_context_tokens() if hasattr(llm, "get_max_context_tokens") else 16384
    available = ctx - _PROMPT_OVERHEAD_TOKENS - _RESPONSE_RESERVE_TOKENS
    return max(4000, int(available * _CHUNK_CHARS_PER_TOKEN))


def _split_into_chunks(text: str, chunk_chars: int) -> List[str]:
    """Split text into chunks of approximately *chunk_chars*, breaking at newlines."""
    if len(text) <= chunk_chars:
        return [text]
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_chars
        if end >= len(text):
            chunks.append(text[start:])
            break
        # Try to break at a newline within the last 20% of the chunk.
        search_start = start + int(chunk_chars * 0.8)
        nl = text.rfind("\n", search_start, end)
        if nl > search_start:
            end = nl + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def _compact_single(
    text: str,
    target_chars: int,
    llm: "LLMClient",
    content_description: str,
) -> str:
    """Compact a single chunk that fits within the model's context window."""
    prompt = (
        f"You are a precise technical content compactor.  Condense the following "
        f"{content_description} to approximately {target_chars:,} characters.\n\n"
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
    result = llm.complete(prompt, temperature=0.0)
    return result.strip()


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

    try:
        chunk_chars = _get_model_chunk_chars(llm)

        # If the text fits in one compaction call, do it directly.
        if len(text) <= chunk_chars:
            result = _compact_single(text, max_chars, llm, content_description)
            if result:
                logger.info(
                    "Compaction result for %s: %d chars (target %d)",
                    content_description,
                    len(result),
                    max_chars,
                )
                return result
            logger.warning(
                "Compaction returned empty for %s, returning original", content_description
            )
            return text

        # Text is too large for one call — chunk, compact each, concatenate.
        chunks = _split_into_chunks(text, chunk_chars)
        num_chunks = len(chunks)
        per_chunk_target = max(1000, max_chars // num_chunks)
        logger.info(
            "Chunked compaction for %s: %d chunks, %d chars per chunk target",
            content_description,
            num_chunks,
            per_chunk_target,
        )

        compacted_parts: List[str] = []
        for i, chunk in enumerate(chunks):
            try:
                part = _compact_single(
                    chunk,
                    per_chunk_target,
                    llm,
                    f"{content_description} (chunk {i + 1}/{num_chunks})",
                )
                compacted_parts.append(part if part else chunk[:per_chunk_target])
            except Exception:
                logger.warning(
                    "Chunk %d/%d compaction failed for %s, using truncated chunk",
                    i + 1,
                    num_chunks,
                    content_description,
                    exc_info=True,
                )
                compacted_parts.append(chunk[:per_chunk_target])

        result = "\n\n".join(compacted_parts)
        logger.info(
            "Chunked compaction result for %s: %d chars from %d chunks (target %d)",
            content_description,
            len(result),
            num_chunks,
            max_chars,
        )
        return result

    except Exception:
        logger.warning(
            "Compaction failed for %s, returning original text", content_description, exc_info=True
        )
        return text
