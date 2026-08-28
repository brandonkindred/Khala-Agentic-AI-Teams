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
import json
import logging
import os
from typing import TYPE_CHECKING, Any, Callable, List, Tuple

from shared.cache import get_shared_cache, with_cache_build_id

from .interface import (
    LLMTruncatedError,
    observer_turn_started,
    take_complete_json_turns,
)

if TYPE_CHECKING:
    from .interface import LLMClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared compaction memo cache
# ---------------------------------------------------------------------------
# The same spec/architecture/existing-codebase text is handed to ``compact_text``
# on every task and every review->fix->re-review cycle of a run, so the identical
# (expensive, deterministic) LLM compaction is otherwise recomputed constantly.
# A bounded LRU keyed on (model, budget, description, content-hash) turns those
# repeated, identical compactions into a single call per distinct input. Backed by
# ``shared.cache`` (Redis when configured). ``0`` disables the cache (pure
# passthrough).
DEFAULT_COMPACTION_CACHE_SIZE = 256  # LLM_COMPACTION_CACHE_SIZE, floor 0
# Base stem; ``_compaction_cache_namespace()`` appends build id when configured.
_COMPACTION_CACHE_NAMESPACE = "llm:compact:v1"


def _compaction_cache_namespace() -> str:
    """Shared-cache namespace for compaction memos (includes build id)."""
    return with_cache_build_id(_COMPACTION_CACHE_NAMESPACE)


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
        - A best-effort attempt is made to empty the shared compaction cache
          namespace. Under normal operation the namespace is empty after this call,
          but because the shared cache is fail-open, a backend failure may leave
          entries behind (and never raises into the caller). Intended for tests
          and callers that prefer a cold compaction when clearing succeeds.
    """
    try:
        get_shared_cache(_compaction_cache_namespace()).clear()
    except Exception:
        logger.warning("Failed to clear compaction cache", exc_info=True)


def _model_fingerprint(llm: "LLMClient") -> str:
    """Best-effort stable identifier for the model a compaction will run on.

    Delegates to ``llm_service.strands_model.model_fingerprint`` — the
    canonical attribute-probing tail — imported lazily (not at module level)
    to avoid a circular import: this module is imported eagerly by
    ``llm_service/__init__.py``, before ``strands_model``'s own top-level
    ``from llm_service import get_strands_model`` could resolve.
    ``model_fingerprint`` is a strict superset of this function's old
    inline probe (it additionally falls back to a dict-shaped ``.config``),
    so this is behavior-preserving for every ``LLMClient`` that has no such
    attribute — true of every concrete client in this package.

    Postconditions:
        - Returns a string that changes when the *currently preferred* model
          changes, so switching the configured provider/model (e.g. Ollama →
          Claude) does not serve a cache entry across grossly different models.
          Never raises: any failure to resolve a model identifier falls back to
          the client's type name. The value is identity-only — it is hashed into
          the cache key, never published.
        - Best-effort only for a failover client: the identifier is read from the
          most-preferred provider at call time, which may differ from the entry
          that ultimately answers if the preferred one 429s mid-call and a
          fallback fills in. This is deliberately tolerated — every configured
          provider produces a valid, budget-bounded compaction of the same input,
          so reusing one within a configured provider list is acceptable.
    """
    from llm_service.strands_model import (
        model_fingerprint as _model_fingerprint_tail,  # noqa: PLC0415
    )

    return _model_fingerprint_tail(llm)


def _compaction_cache_key(
    text: str, max_chars: int, content_description: str, llm: "LLMClient"
) -> str:
    """Key a compaction by its exact inputs: model + budget + label + content hash.

    Postconditions:
        - Two calls collide only when their model fingerprint, budget,
          ``content_description``, and raw input text are all identical, so a hit
          is byte-identical to what a fresh compaction of the same inputs would
          return. ``content_description`` is part of the key because it is
          interpolated into the compaction prompt (``_compact_single``), so the
          same text under a different label can produce a different summary.
        - Components are JSON-array serialized so embedded delimiters (including
          NUL) in ``content_description`` / ``text`` cannot create collisions.
    """
    payload = json.dumps(
        [_model_fingerprint(llm), max_chars, content_description, text],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Very conservative chars-per-token for chunk sizing.  Web-fetched content,
# HTML residue, and non-English text can tokenize at <2 chars/token.
# Using 1.0 ensures chunks never exceed the model context even in worst cases.
_CHUNK_CHARS_PER_TOKEN = 1.0

# Reserve tokens for the compaction prompt template + response.
_PROMPT_OVERHEAD_TOKENS = 4000
_RESPONSE_RESERVE_TOKENS = 8000


def _get_model_chunk_chars(llm: "LLMClient") -> int:
    """Max source chars safe for one call across every possible LLM consumer."""
    min_context = getattr(llm, "get_min_context_tokens", None)
    if callable(min_context):
        # A failover client can switch providers after a 429 within this call, so
        # size the chunk for its smallest candidate rather than only the preferred
        # provider exposed through get_max_context_tokens().
        ctx = min_context()
    else:
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


def _invoke_on_attempt(
    on_attempt: Callable[[str, str], None] | None, prompt: str, response: str
) -> None:
    """Best-effort compaction observer; never raises."""
    if on_attempt is None:
        return
    try:
        on_attempt(prompt, response)
    except Exception:  # noqa: BLE001 - observer must never break compaction
        logger.warning("compact_text: on_attempt callback failed", exc_info=True)


def _observe_complete_turns(
    on_attempt: Callable[[str, str], None] | None,
    prompt: str,
    fallback_response: str,
) -> None:
    """Notify ``on_attempt`` for each recorded ``complete`` continuation turn.

    Preconditions:
        ``prompt`` is the compaction user message. ``fallback_response`` is
        used when the provider recorded no inner turns.
    Postconditions:
        Inner continuation turns, when present, are each observed in record
        order with that turn's start time bound. Otherwise ``on_attempt`` is
        invoked once with ``(prompt, fallback_response)``. Never raises.
    """
    turns = take_complete_json_turns()
    if turns:
        for turn_prompt, turn_response, started in turns:
            with observer_turn_started(started):
                _invoke_on_attempt(on_attempt, turn_prompt, turn_response)
        return
    _invoke_on_attempt(on_attempt, prompt, fallback_response)


def _compact_single(
    text: str,
    target_chars: int,
    llm: "LLMClient",
    content_description: str,
    on_attempt: Callable[[str, str], None] | None = None,
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
    try:
        result = llm.complete(
            prompt, objective=f"compact oversized {content_description}", temperature=0.0
        )
    except LLMTruncatedError as exc:
        _observe_complete_turns(on_attempt, prompt, exc.partial_content or "")
        raise
    except Exception:
        _observe_complete_turns(on_attempt, prompt, "")
        raise
    _observe_complete_turns(on_attempt, prompt, result)
    return result.strip()


def supports_compaction(llm: Any) -> bool:
    """True when ``llm`` exposes the surface ``compact_text`` requires.

    Preconditions:
        - ``llm`` may be any object (including ``None``); attribute lookup must not
          raise for the checked name (``getattr`` with default is used).
    Postconditions:
        - Returns True iff ``getattr(llm, "complete", None)`` is callable.
        - Does not require ``get_max_context_tokens`` or model fingerprint attributes
          (those are optional inside ``compact_text``).
    """
    return callable(getattr(llm, "complete", None))


def compact_text(
    text: str,
    max_chars: int,
    llm: "LLMClient",
    content_description: str = "content",
    *,
    on_attempt: Callable[[str, str], None] | None = None,
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
    on_attempt:
        Optional observer invoked with ``(prompt, response)`` for each
        ``llm.complete`` this call actually makes (not on cache hits). A
        truncated reply is reported with its ``partial_content`` rather than
        an empty string. Observer exceptions are swallowed.

    Returns
    -------
    str
        The original text if it fits, or a compacted version produced by the LLM.
        On any LLM failure the original text is returned so callers never lose data.

    Notes
    -----
    Results are memoized in a bounded shared LRU keyed on
    ``(model, max_chars, content_description, content-hash)`` (see
    ``LLM_COMPACTION_CACHE_SIZE``), so a
    repeated call with the same inputs reuses the earlier compaction instead of
    re-invoking the LLM. Concurrent callers for the same key share one compute
    via ``shared.cache.single_flight`` (Redis NX lock + waiter poll when Redis
    is configured; in-process lock otherwise). Capacity is passed as
    ``max_entries`` on each ``single_flight`` call (read from
    ``LLM_COMPACTION_CACHE_SIZE`` each invocation so env changes apply without
    restart). Value TTL comes from ``REDIS_CACHE_TTL_S``; compose Redis uses
    ``maxmemory``/``noeviction`` with app-side ZSET trim (not Redis LRU) — not a
    separate compaction TTL. Compaction is
    deterministic given those inputs, so a cache hit is byte-identical to what a
    fresh call would return. Only genuine full compactions are cached — every
    fallback path (LLM failure, empty result, or a chunked run with any
    degraded chunk) is retried on the next call rather than frozen.
    """
    if not text or len(text) <= max_chars:
        return text or ""

    capacity = _compaction_cache_size()
    if capacity <= 0:
        # Cache disabled — pure passthrough (keeps the number of LLM invocations
        # deterministic for callers/tests that assert on it).
        return _compact_uncached(text, max_chars, llm, content_description, on_attempt=on_attempt)[
            0
        ]

    key = _compaction_cache_key(text, max_chars, content_description, llm)
    cache = get_shared_cache(_compaction_cache_namespace())

    def _compute() -> Tuple[bytes, bool]:
        result, cacheable = _compact_uncached(
            text, max_chars, llm, content_description, on_attempt=on_attempt
        )
        return result.encode("utf-8"), cacheable

    # SharedCache.single_flight takes compute → (payload_bytes, cacheable) and
    # returns only the payload bytes (durable-stores when cacheable is True).
    raw = cache.single_flight(key, _compute, max_entries=capacity)
    try:
        return raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        logger.warning(
            "corrupt compaction cache entry for %s; evicting and recomputing",
            key,
            exc_info=True,
        )
        try:
            cache.delete(key)
        except Exception:
            logger.warning(
                "Failed to evict corrupt compaction cache entry for %s",
                key,
                exc_info=True,
            )
        result, cacheable = _compact_uncached(
            text, max_chars, llm, content_description, on_attempt=on_attempt
        )
        if cacheable:
            try:
                cache.set(key, result.encode("utf-8"), max_entries=capacity)
            except Exception:
                logger.warning(
                    "Failed to re-store compaction after corrupt eviction for %s",
                    key,
                    exc_info=True,
                )
        return result


def _compact_uncached(
    text: str,
    max_chars: int,
    llm: "LLMClient",
    content_description: str,
    on_attempt: Callable[[str, str], None] | None = None,
) -> Tuple[str, bool]:
    """Compact *text* without consulting the memo cache.

    Preconditions:
        - ``len(text) > max_chars`` — callers handle the fits-as-is case first.

    Postconditions:
        - Returns ``(result, cacheable)``. ``cacheable`` is ``True`` only when
          ``result`` is a genuine LLM compaction of the full input; it is
          ``False`` for every fallback path (LLM failure, empty compaction, or a
          chunked run in which any chunk failed or returned empty). On every
          fallback path ``result`` is the original ``text`` so callers never
          receive a silently truncated aggregate.
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
            result = _compact_single(
                text, max_chars, llm, content_description, on_attempt=on_attempt
            )
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
        # Per-chunk targets sum to at most ``max_chars`` (plus join separators),
        # so a floored minimum cannot blow the overall budget.
        chunks = _split_into_chunks(text, chunk_chars)
        num_chunks = len(chunks)
        sep_overhead = 2 * max(0, num_chunks - 1)  # "\n\n" between parts
        budget = max(1, max_chars - sep_overhead)
        per_chunk_target = max(1, budget // num_chunks)
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
                    on_attempt=on_attempt,
                )
                if part:
                    compacted_parts.append(part)
                else:
                    # Empty compaction for any chunk — return the original full
                    # text so callers never receive a silently truncated join.
                    logger.warning(
                        "Chunk %d/%d compaction returned empty for %s, returning original",
                        i + 1,
                        num_chunks,
                        content_description,
                    )
                    return text, False
            except Exception:
                logger.warning(
                    "Chunk %d/%d compaction failed for %s, returning original text",
                    i + 1,
                    num_chunks,
                    content_description,
                    exc_info=True,
                )
                return text, False

        result = "\n\n".join(compacted_parts)
        if len(result) > max_chars:
            # Models can overshoot the per-chunk target; tighten once against
            # the overall budget before accepting the join.
            try:
                tightened = _compact_single(
                    result, max_chars, llm, content_description, on_attempt=on_attempt
                )
                if tightened:
                    result = tightened
            except Exception:
                logger.warning(
                    "Re-compaction of joined chunks failed for %s; keeping join",
                    content_description,
                    exc_info=True,
                )
        logger.info(
            "Chunked compaction result for %s: %d chars from %d chunks (target %d)",
            content_description,
            len(result),
            num_chunks,
            max_chars,
        )
        return result, True

    except Exception:
        logger.warning(
            "Compaction failed for %s, returning original text", content_description, exc_info=True
        )
        return text, False
