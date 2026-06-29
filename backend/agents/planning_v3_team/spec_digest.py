"""Section-aware map-reduce digestion of brief+spec for planning_v3 phases.

Instead of truncating large specs with ``text[:N]`` (which silently drops the tail
of any spec longer than the slice), this module splits the brief+spec into semantic
sections, runs a per-section extraction (map), and merges the structured results
(reduce). ``compact_text`` is used only as a fallback for a single section that is
still too large for one model call. The passed-in ``LLMClient`` (``.complete_text`` /
``.complete`` / ``.get_max_context_tokens``) is reused throughout; no spec
information is ever silently lost.

Invariants:
    - The full input is always either mapped or compacted, never sliced away.
    - On total LLM failure the caller-supplied ``fallback`` is returned, so callers
      always receive a well-formed result.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Sequence

from llm_service import compact_text

logger = logging.getLogger(__name__)

# Conservative chars-per-token for code/spec text. Mirrors
# software_engineering_team.shared.context_sizing.CHARS_PER_TOKEN without taking a
# runtime cross-team import (that module is imported only inside SE today).
CHARS_PER_TOKEN = 3.5
_RESERVED_PROMPT_TOKENS = 6000  # phase prompt template + headers
_RESERVED_RESPONSE_TOKENS = 4096
_MIN_SECTION_CHARS = 8000
_DEFAULT_CONTEXT_TOKENS = 16384


def compute_section_chars(llm: Any) -> int:
    """Max chars of spec text to feed one map (per-section) prompt.

    Preconditions:
        - ``llm`` either exposes ``get_max_context_tokens() -> int`` or it is
          treated as a small-context model (``_DEFAULT_CONTEXT_TOKENS``).
    Postconditions:
        - Returns an int >= ``_MIN_SECTION_CHARS``.
    """
    # try/except (not just hasattr): a present-but-raising get_max_context_tokens must
    # still degrade to the default rather than crash this critical-path helper, which
    # runs before map_reduce's per-section guard.
    try:
        ctx = llm.get_max_context_tokens()
    except Exception:
        ctx = _DEFAULT_CONTEXT_TOKENS
    available = ctx - _RESERVED_PROMPT_TOKENS - _RESERVED_RESPONSE_TOKENS
    if available < 512:
        available = 512  # leave some room even for tiny models
    return max(_MIN_SECTION_CHARS, int(available * CHARS_PER_TOKEN))


# --- splitter -------------------------------------------------------------

_HEADING_RE = re.compile(r"^#{1,6}\s+.*$", re.MULTILINE)


def split_sections(text: str, max_chars: int) -> List[str]:
    """Split ``text`` into sections at semantic boundaries (no mid-content slicing).

    Strategy: prefer markdown headings as boundaries (each heading is kept with the
    body that follows it), falling back to blank-line boundaries; greedily pack
    consecutive blocks until adding the next would exceed ``max_chars``. A single
    semantic block larger than ``max_chars`` is broken down on blank lines but, if a
    coherent piece is *still* oversized, it is kept WHOLE — ``map_reduce`` hands such a
    section to ``compact_text`` (which condenses it intelligently, chunking internally)
    rather than cutting it mid-content. Nothing is ever truncated here.

    Preconditions:
        - ``max_chars`` > 0.
    Postconditions:
        - Returns ``[]`` for empty text, ``[text]`` when it already fits, else a list
          whose concatenation reproduces ``text`` exactly (no characters dropped).
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    # 1. Cut at heading starts (keep each heading with its following body); fall back
    #    to blank-line pieces when there are no headings.
    starts = [m.start() for m in _HEADING_RE.finditer(text)]
    if starts and starts[0] != 0:
        starts = [0] + starts
    if not starts:
        raw_blocks = _blank_line_pieces(text)
    else:
        starts.append(len(text))
        raw_blocks = [text[starts[i] : starts[i + 1]] for i in range(len(starts) - 1)]

    # 2. Break any oversized heading-block down further on blank lines.
    blocks: List[str] = []
    for b in raw_blocks:
        if len(b) > max_chars:
            blocks.extend(_blank_line_pieces(b))
        else:
            blocks.append(b)

    # 3. Greedily pack blocks into <= max_chars sections; a still-oversized coherent
    #    piece stands alone (compacted later, never sliced).
    sections: List[str] = []
    buf = ""
    for block in blocks:
        if len(block) > max_chars:
            if buf:
                sections.append(buf)
                buf = ""
            sections.append(block)
            continue
        if buf and len(buf) + len(block) > max_chars:
            sections.append(buf)
            buf = block
        else:
            buf += block
    if buf:
        sections.append(buf)
    return sections


def _blank_line_pieces(text: str) -> List[str]:
    """Split on blank lines, retaining the delimiters so concatenation is lossless."""
    parts = re.split(r"(\n\s*\n)", text)
    pieces: List[str] = []
    for i in range(0, len(parts), 2):
        seg = parts[i] + (parts[i + 1] if i + 1 < len(parts) else "")
        if seg:
            pieces.append(seg)
    return pieces


# --- map-reduce driver ----------------------------------------------------


def map_reduce(
    text: str,
    llm: Any,
    *,
    content_description: str,
    map_fn: Callable[[str, Any, int, int], Optional[Dict[str, Any]]],
    reduce_fn: Callable[[Sequence[Dict[str, Any]]], Dict[str, Any]],
    fallback: Dict[str, Any],
) -> Dict[str, Any]:
    """Split ``text`` into sections, ``map_fn`` each, then ``reduce_fn`` the results.

    ``map_fn(section_text, llm, idx, total)`` returns a parsed dict or ``None`` (None
    skips the section). A section still larger than the per-section budget is first run
    through ``compact_text`` (so it fits one call) before ``map_fn``; ``compact_text``
    chunks internally and returns the original on failure, so oversized sections are
    compacted, never sliced.

    Preconditions:
        - ``map_fn`` / ``reduce_fn`` are callables; ``fallback`` is a valid result dict.
    Postconditions:
        - Returns ``fallback`` for empty/blank input or when every map step fails or
          returns falsy; otherwise returns ``reduce_fn`` over the non-empty results.
    """
    if not text or not text.strip():
        return fallback
    section_chars = compute_section_chars(llm)
    sections = split_sections(text, section_chars)
    results: List[Dict[str, Any]] = []
    total = len(sections)
    for idx, section in enumerate(sections):
        chunk = section
        if len(chunk) > section_chars and _can_compact(llm):
            try:
                chunk = compact_text(
                    chunk,
                    max_chars=section_chars,
                    llm=llm,
                    content_description=f"{content_description} (section {idx + 1}/{total})",
                )
            except Exception:  # compact_text should never raise, but keep the fallback safety net
                logger.warning(
                    "compact_text failed for %s section %d/%d; using uncompacted section",
                    content_description,
                    idx + 1,
                    total,
                    exc_info=True,
                )
                chunk = section
        try:
            parsed = map_fn(chunk, llm, idx, total)
        except Exception:  # never let one section kill the whole digest
            logger.warning(
                "map step failed for %s section %d/%d",
                content_description,
                idx + 1,
                total,
                exc_info=True,
            )
            parsed = None
        if parsed:
            results.append(parsed)
    if not results:
        logger.warning("All map steps empty for %s; using fallback", content_description)
        return fallback
    return reduce_fn(results)


def _can_compact(llm: Any) -> bool:
    """True when ``llm`` supports the compaction surface (``.complete`` + ctx size)."""
    return hasattr(llm, "complete") and hasattr(llm, "get_max_context_tokens")


# --- shared JSON parse helper (matches existing phase fence-stripping) -----


def parse_json_response(response: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse an LLM JSON response, tolerating ```json fences.

    Postconditions:
        - Returns a ``dict`` on success, or ``None`` for empty/invalid input OR any
          top-level JSON value that is not an object (e.g. a bare array or string).
          Returning only dicts means callers' reducers can rely on ``.get`` without a
          type guard on the parsed value itself.
    """
    text = (response or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None
