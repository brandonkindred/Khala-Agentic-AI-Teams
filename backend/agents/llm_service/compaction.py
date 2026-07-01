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

import hashlib
import logging
import os
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:
    from .interface import LLMClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Process-global compaction memo cache
# ---------------------------------------------------------------------------
# The same spec/architecture/existing-codebase text is handed to ``compact_text``
# on every task and every review->fix->re-review cycle of a run, so the identical
# (expensive, deterministic) LLM compaction is otherwise recomputed constantly.
# A bounded LRU keyed on (model, budget, content-hash) turns those repeated,
# identical compactions into a single call per distinct input. It is guarded by a
# lock because callers (e.g. the code review coordinator's map phase) may compact
# from worker threads. ``0`` disables the cache (pure passthrough).
DEFAULT_COMPACTION_CACHE_SIZE = 256  # LLM_COMPACTION_CACHE_SIZE, floor 0

_COMPACTION_CACHE: "OrderedDict[str, str]" = OrderedDict()
_COMPACTION_CACHE_LOCK = threading.Lock()


def _compaction_cache_size() -> int:
    """Resolve the compaction cache capacity from the environment.

    Postconditions:
        - Returns ``DEFAULT_COMPACTION_CACHE_SIZE`` when ``LLM_COMPACTION_CACHE_SIZE``
          is unset or unparseable, and never returns a negative value (a
          configured value below 0 is floored to 0, which disables the cache).
    """
    raw = os.getenv("LLM_COMPACTION_CACHE_SIZE")
    if raw is None:
        return DEFAULT_COMPACTION_CACHE_SIZE
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_COMPACTION_CACHE_SIZE
    return max(0, value)


def clear_compaction_cache() -> None:
    """Drop every memoized compaction result.

    Postconditions:
        - The process-global cache is empty; the next ``compact_text`` call for
          any input is a guaranteed miss. Intended for tests (the cache persists
          across calls by design) and callers that must force a cold compaction.
    """
    with _COMPACTION_CACHE_LOCK:
        _COMPACTION_CACHE.clear()


def _model_fingerprint(llm: "LLMClient") -> str:
    """Best-effort stable identifier for the model a compaction will run on.

    Postconditions:
        - Returns a string that changes when the resolved model changes, so a
          result compacted by one model is never served for another (the LLM
          provider list can fail over mid-process). Never raises: any failure to
          resolve a model identifier falls back to the client's type name. The
          value is identity-only — it is hashed into the cache key, never published.
    """
    for attr in ("model_id", "model_name", "model"):
        try:
            value = getattr(llm, attr, None)
        except Exception:
            value = None
        if isinstance(value, str) and value:
            return value
    return type(llm).__name__


def _compaction_cache_key(text: str, max_chars: int, llm: "LLMClient") -> str:
    """Key a compaction by its exact inputs: model + budget + content hash.

    Postconditions:
        - Two calls collide only when their model fingerprint, budget, and raw
          input text are all identical, so a hit is byte-identical to what a
          fresh compaction of the same inputs would return.
    """
    payload = f"{_model_fingerprint(llm)}\x00{max_chars}\x00{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    result = llm.complete(
        prompt, objective=f"compact oversized {content_description}", temperature=0.0
    )
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

    Notes
    -----
    Results are memoized in a bounded, process-global LRU keyed on
    ``(model, max_chars, content-hash)`` (see ``LLM_COMPACTION_CACHE_SIZE``), so a
    repeated call with the same inputs reuses the earlier compaction instead of
    re-invoking the LLM. Compaction is deterministic given those inputs, so a
    cache hit is byte-identical to what a fresh call would return. Only genuine
    full compactions are cached — every fallback path (LLM failure, empty result,
    or a chunked run with any degraded chunk) is retried on the next call rather
    than frozen.
    """
    if not text or len(text) <= max_chars:
        return text or ""

    capacity = _compaction_cache_size()
    if capacity <= 0:
        # Cache disabled — pure passthrough (keeps the number of LLM invocations
        # deterministic for callers/tests that assert on it).
        return _compact_uncached(text, max_chars, llm, content_description)[0]

    key = _compaction_cache_key(text, max_chars, llm)
    with _COMPACTION_CACHE_LOCK:
        hit = _COMPACTION_CACHE.get(key)
        if hit is not None:
            _COMPACTION_CACHE.move_to_end(key)
    if hit is not None:
        return hit

    result, cacheable = _compact_uncached(text, max_chars, llm, content_description)
    if cacheable:
        with _COMPACTION_CACHE_LOCK:
            _COMPACTION_CACHE[key] = result
            _COMPACTION_CACHE.move_to_end(key)
            while len(_COMPACTION_CACHE) > capacity:
                _COMPACTION_CACHE.popitem(last=False)
    return result


def _compact_uncached(
    text: str,
    max_chars: int,
    llm: "LLMClient",
    content_description: str,
) -> Tuple[str, bool]:
    """Compact *text* without consulting the memo cache.

    Preconditions:
        - ``len(text) > max_chars`` — callers handle the fits-as-is case first.

    Postconditions:
        - Returns ``(result, cacheable)``. ``cacheable`` is ``True`` only when
          ``result`` is a genuine LLM compaction of the full input; it is
          ``False`` for every fallback path (LLM failure, empty compaction, or a
          chunked run in which any chunk fell back to a raw slice), so a degraded
          result is retried on the next call rather than frozen in the cache.
    """
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
                return result, True
            logger.warning(
                "Compaction returned empty for %s, returning original", content_description
            )
            return text, False

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
        all_chunks_ok = True
        for i, chunk in enumerate(chunks):
            try:
                part = _compact_single(
                    chunk,
                    per_chunk_target,
                    llm,
                    f"{content_description} (chunk {i + 1}/{num_chunks})",
                )
                if part:
                    compacted_parts.append(part)
                else:
                    # Empty compaction for this chunk — fall back to a raw slice
                    # and mark the aggregate un-cacheable so it is retried.
                    all_chunks_ok = False
                    compacted_parts.append(chunk[:per_chunk_target])
            except Exception:
                logger.warning(
                    "Chunk %d/%d compaction failed for %s, using truncated chunk",
                    i + 1,
                    num_chunks,
                    content_description,
                    exc_info=True,
                )
                all_chunks_ok = False
                compacted_parts.append(chunk[:per_chunk_target])

        result = "\n\n".join(compacted_parts)
        logger.info(
            "Chunked compaction result for %s: %d chars from %d chunks (target %d)",
            content_description,
            len(result),
            num_chunks,
            max_chars,
        )
        return result, all_chunks_ok

    except Exception:
        logger.warning(
            "Compaction failed for %s, returning original text", content_description, exc_info=True
        )
        return text, False
