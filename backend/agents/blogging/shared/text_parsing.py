"""
Shared LLM-response text-parsing helpers.

This is the single implementation of five helpers that were previously
duplicated (and, in the case of ``extract_json_array_from_text``, allowed to
drift) across ``blog_writer_agent/agent.py``, ``blog_writer_agent/revision.py``,
``blog_writer_agent/self_review.py``, and
``agent_implementations/pipeline/_common.py``. All four modules import from
here and hold no private copy of any of these functions.

``extract_json_array_from_text`` is the fixed variant: it resumes scanning at
the decoded value's end rather than one character past the opening bracket,
so a non-matching array that itself contains a nested ``[`` cannot be
re-entered and salvaged as if it were the real payload. Before this module
existed, ``blog_writer_agent/agent.py``'s private copy ran the drifted
(one-character-past-the-bracket) variant; its one caller,
``identify_uncertainty_questions``, now runs the fixed scanner instead — the
sole intended behavior change from the migration.

``format_feedback_item_line`` also rejects a ``bool`` index
(``isinstance(index, bool)``), tightening a precondition the two prior
duplicate copies did not enforce (``bool`` subclasses ``int``, so they
silently accepted ``True``/``False`` as a positive index). Both call sites
feed the index from ``enumerate(..., start=1)``, so no caller can produce a
bool in practice — this is a precondition tightened, not a reachable
behavior change.

A related duplicate, ``_unwrap_event_loop_exception``, still exists in
``shared/json_retry.py`` and is out of scope here.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from strands.types.exceptions import EventLoopException

from llm_service import LLMJsonParseError, extract_json_from_response


def unwrap_llm_cause(exc: BaseException) -> BaseException:
    """Return the underlying model error when strands wraps it in EventLoopException.

    Preconditions:
        - ``exc`` is the exception caught at an LLM call boundary.
    Postconditions:
        - If ``exc`` is an ``EventLoopException`` whose ``original_exception``
          is a ``BaseException`` instance, returns that original exception.
        - Otherwise (not an ``EventLoopException``, or its ``original_exception``
          is ``None`` or not a ``BaseException``) returns ``exc`` unchanged.
    """
    if isinstance(exc, EventLoopException):
        original = getattr(exc, "original_exception", None)
        if isinstance(original, BaseException):
            return original
    return exc


def extract_draft_after_marker(raw_response: Optional[str]) -> str:
    """
    Extract draft content from model output that uses the hybrid format:
    first line {\"draft\": 0}, then ---DRAFT---, then the full blog post in Markdown.
    Falls back to scanning the response for extractable JSON (whole-response,
    fenced, or prose-wrapped, via ``extract_json_from_response``) and returning
    the value of its \"draft\" key, but only when that value is a non-empty
    string; a non-string value (including the literal ``0`` sentinel used in
    the hybrid marker line) or an empty string is treated the same as no
    fallback match and yields ``\"\"``.

    Preconditions:
        - ``raw_response`` is ``None`` or a string.
    Postconditions:
        - Returns the text after the first ``---DRAFT---`` marker found (tried
          in order across the four marker/whitespace variants), stripped, when
          that text is non-empty after stripping.
        - Otherwise, extracts JSON from ``raw_response`` and returns its
          stripped ``\"draft\"`` value, but only when that value is a string
          that is non-empty after stripping.
        - Returns ``\"\"`` when ``raw_response`` is ``None``/not a string, no
          marker yields non-empty text, JSON extraction fails
          (``LLMJsonParseError``), the extracted JSON is not a dict, or its
          ``\"draft\"`` value is missing, non-string, or empty/whitespace-only.
    """
    if not raw_response or not isinstance(raw_response, str):
        return ""
    text = raw_response.strip()
    for marker in ("\n---DRAFT---\n", "\n---DRAFT---", "---DRAFT---\n", "---DRAFT---"):
        if marker in text:
            after = text.split(marker, 1)[1].strip()
            if after:
                return after
    try:
        data = extract_json_from_response(text)
        if isinstance(data, dict):
            d = data.get("draft")
            if isinstance(d, str) and d.strip():
                return d.strip()
    except LLMJsonParseError:
        pass
    return ""


def extract_json_array_from_text(
    text: str, *, required_keys: tuple[str, ...] = ()
) -> Optional[list[dict[str, Any]]]:
    """Parse a JSON array of objects from ``text``, including when prefixed by prose.

    Preconditions:
        - ``text`` is a string (may be empty).
        - ``required_keys``, if given, are the keys used to recognize the real
          payload (e.g. ``("issue",)`` for self-review issues, ``("question",)``
          for uncertainty questions): at least one element of a candidate array
          must contain all of them. This rejects an unrelated dict array (e.g. a
          ``references`` list salvaged from surrounding prose) that would
          otherwise pass a bare "is it a list of dicts" check, while still
          tolerating a real payload where some items are individually malformed
          (the caller's own per-item validation skips those).
    Postconditions:
        - Returns the dict elements of the first decoded JSON array containing at
          least one dict with every key in ``required_keys``, found by scanning
          for ``[`` and using ``json.JSONDecoder.raw_decode``. Non-dict elements
          in that array (e.g. a stray string) are dropped rather than rejecting
          the whole array — callers already tolerate individually malformed dict
          items via their own per-item validation.
        - A syntactically valid but schema-mismatched non-empty array (e.g. a
          numeric citation like ``[1]``, or a dict array none of whose elements
          have ``required_keys``) does not short-circuit the scan; scanning
          continues past it toward the real payload.
        - If no matching array of dicts is found, returns the first syntactically
          valid empty ``[]`` encountered — this cannot be distinguished from a
          literally empty Markdown link ``[]()`` (an empty pair of brackets is
          valid JSON), so a response containing only such a link and no real
          array-of-dicts payload also returns ``[]`` here. A Markdown link with
          non-empty text, e.g. ``[label](url)``, is not valid JSON at that
          ``[`` and is simply skipped like any other non-match. Returns
          ``None`` if no array matched at all.

    Limitation: the scan looks for a literal ``[`` anywhere in ``text``,
    including inside a JSON string value (e.g. an object field whose value is
    the literal text ``"[{...}]"``), so it can extract an array nested inside
    a string rather than only a true top-level/prose array. This has not been
    observed in practice for the reviewer/uncertainty response shapes this is
    used for, but is a known edge case if a future prompt's schema puts
    JSON-looking text inside a string field.
    """
    decoder = json.JSONDecoder()
    search_from = 0
    empty_fallback = None
    while True:
        i = text.find("[", search_from)
        if i == -1:
            break
        try:
            value, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            search_from = i + 1
            continue
        if isinstance(value, list):
            dict_elements = [el for el in value if isinstance(el, dict)]
            if dict_elements and any(all(k in el for k in required_keys) for el in dict_elements):
                return dict_elements
            if not value and empty_fallback is None:
                empty_fallback = value
        # Resume scanning past the decoded value's end, not from i + 1: a
        # non-matching value can itself contain a nested "[" (e.g. a sub-array
        # or a string literal that reads as one) that would otherwise be
        # re-entered and salvaged as if it were a real top-level match.
        search_from = end
    return empty_fallback


def looks_like_top_level_json_object(text: str) -> bool:
    """Return True when ``text``'s JSON payload appears to be a top-level object.

    Preconditions:
        - ``text`` is a string (may be empty).
    Postconditions:
        - Returns True only when the entire stripped response is a JSON object;
          prose and fenced snippets are not treated as top-level objects.
    """
    candidate = text.strip()
    if not candidate.startswith("{"):
        return False
    try:
        value, end = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError:
        return False
    return isinstance(value, dict) and not candidate[end:].strip()


def format_feedback_item_line(item: Any, index: int) -> str:
    """One numbered feedback line (+ optional suggestion) for batch revise prompts.

    Preconditions:
        ``index`` is a positive int. ``item`` exposes ``severity``, ``category``,
        and ``issue`` (via attribute or duck typing); empty/missing values are
        rejected. ``location`` and ``suggestion`` are optional.
    Postconditions:
        Returns a numbered feedback line; includes a location bracket and a
        suggestion sub-line when those optional fields are present.
    Raises:
        ValueError: if ``index`` is not a positive int, or required item
            fields are missing.
    """
    if isinstance(index, bool) or not isinstance(index, int) or index <= 0:
        raise ValueError(f"index must be a positive int, got {index!r}")
    severity = getattr(item, "severity", None)
    category = getattr(item, "category", None)
    issue = getattr(item, "issue", None)
    if not all([severity, category, issue]):
        raise ValueError(f"Feedback item missing required fields: {item!r}")
    location = getattr(item, "location", None)
    loc = f" [{location}]" if location else ""
    line = f"{index}. [{severity}] {category}{loc}: {issue}"
    suggestion = getattr(item, "suggestion", None)
    if suggestion:
        line += f"\n   Suggestion: {suggestion}"
    return line
