"""
Self-review for blog writer drafts: deterministic mechanical checks plus a
focused LLM-driven subjective review, as free functions.

These are extracted from ``BlogWriterAgent``'s self-review methods, adapted
to take an explicit ``call_text`` callback (``(prompt: str, system_prompt:
str = "") -> str``) in place of ``self._call_text``. Callers typically pass a
bound ``BlogWriterAgent._call_text`` method as ``call_text``.

This module intentionally carries local copies of a few small parsing/error
helpers that also live in ``agent.py`` (``_unwrap_llm_cause``,
``_extract_draft_after_marker``, ``_extract_json_array_from_text``,
``_looks_like_top_level_json_object``, ``_split_sentences_for_staccato``).
``agent.py`` still uses its own copies for drafting/revision paths that are
out of scope for this module, so the duplication is deliberate and short-lived.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

from strands.types.exceptions import EventLoopException

from llm_service import (
    LLMError,
    LLMJsonParseError,
    LLMRateLimitError,
    LLMTemporaryError,
    extract_json_from_response,
)

from .prompts import SELF_REVIEW_PROMPT, WRITING_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# A callback with the same shape as ``BlogWriterAgent._call_text``:
# ``(prompt: str, system_prompt: str = "") -> str``. A two-argument Callable
# (rather than ``Callable[..., str]``) so a mismatched callback signature is
# a type error, not silently accepted.
CallText = Callable[[str, str], str]

# ---------------------------------------------------------------------------
# Deterministic compliance constants
# ---------------------------------------------------------------------------

BANNED_PHRASES = [
    "In today's fast-paced world",
    "In the ever-evolving landscape of",
    "In an era where",
    "Now more than ever",
    "As we navigate",
    "With the rise of",
    "As technology continues to evolve",
    "It's worth noting that",
    "It's important to understand that",
    "It bears mentioning",
    "It's no secret that",
    "Needless to say",
    "Of course,",
    "As mentioned above",
    "This is a game-changer",
    "This is incredibly important",
    "This is essential for success",
    "Harnessing the power of",
    "Furthermore,",
    "Moreover,",
    "Additionally,",
    "In conclusion,",
    "To summarize,",
]

VAGUE_CITATION_PATTERNS = [
    r"[Ss]tudies show",
    r"[Rr]esearch indicates",
    r"[Ee]xperts agree",
    r"[Ii]t'?s well[- ]known that",
    r"[Dd]ata suggests",
    r"[Mm]any organizations have found",
    r"[Tt]eams often discover",
    r"[Aa]ccording to industry best practices",
    r"[Ss]tatistics show",
    r"[Ii]t'?s widely recognized",
]

# Deterministic self-check thresholds (named so rules stay tunable in one place).
CITATION_LOOKAHEAD_CHARS = 150
STACCATO_MAX_WORDS = 7
STACCATO_MIN_STREAK = 3
MIN_READER_ADDRESS_COUNT = 3

# Source/link markers that clear a vague-citation flag within the lookahead window.
_CITATION_SOURCE_RE = re.compile(r"\[CLAIM:|https?://|\]\(https?://")

# Reader-address forms counted toward the minimum (includes plural reflexive).
_READER_ADDRESS_RE = re.compile(r"\byou(?:r|rs|rself|rselves)?\b")

# Paragraph split: one-or-more blank lines, tolerant of ``\r\n`` line endings.
_PARAGRAPH_SPLIT_RE = re.compile(r"\r?\n\s*\r?\n")

# Protect common abbreviations / decimals before staccato sentence splitting.
# Abbreviation matching is case-insensitive via ``(?i:...)``, but the sentence-
# boundary lookahead keeps a case-sensitive ``[A-Z]`` so mid-sentence forms like
# ``e.g. tracing`` stay protected while ``e.g. Tracing`` (new sentence) does not.
# The same continuation rule applies to ``U.S.`` and titles: protect only when
# the following token continues the sentence (not whitespace + uppercase, and
# not end-of-text), so genuine sentence ends keep their terminal period.
_ABBREV_PROTECT: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i:\be\.g\.)(?!\s+[A-Z]|\s*$)"), "egPLACEHOLDER"),
    (re.compile(r"(?i:\bi\.e\.)(?!\s+[A-Z]|\s*$)"), "iePLACEHOLDER"),
    (re.compile(r"(?i:\betc\.)(?!\s+[A-Z]|\s*$)"), "etcPLACEHOLDER"),
    (re.compile(r"(?i:\bU\.S\.)(?!\s+[A-Z]|\s*$)"), "USPLACEHOLDER"),
    (re.compile(r"(?i:\bDr\.)(?!\s+[A-Z]|\s*$)"), "DrPLACEHOLDER"),
    (re.compile(r"(?i:\bMr\.)(?!\s+[A-Z]|\s*$)"), "MrPLACEHOLDER"),
    (re.compile(r"(?i:\bMrs\.)(?!\s+[A-Z]|\s*$)"), "MrsPLACEHOLDER"),
    (re.compile(r"(?i:\bMs\.)(?!\s+[A-Z]|\s*$)"), "MsPLACEHOLDER"),
    (re.compile(r"\d+\.\d+"), "NUMPLACEHOLDER"),
)

# Precompiled banned-phrase patterns: leading word boundary; trailing boundary only
# when the phrase ends in an alphanumeric (phrases that end in punctuation, e.g.
# ``"Of course,"``, keep the punctuation and skip a trailing ``\b``).
_BANNED_PHRASE_PATTERNS: list[tuple[str, re.Pattern[str]]] = []
for _phrase in BANNED_PHRASES:
    _escaped = re.escape(_phrase.lower())
    if _phrase[-1].isalnum():
        _BANNED_PHRASE_PATTERNS.append((_phrase, re.compile(rf"\b{_escaped}\b")))
    else:
        _BANNED_PHRASE_PATTERNS.append((_phrase, re.compile(rf"\b{_escaped}")))
del _phrase, _escaped


# ---------------------------------------------------------------------------
# Local helper copies (see module docstring — duplicated from agent.py)
# ---------------------------------------------------------------------------


def _split_sentences_for_staccato(para: str) -> list[str]:
    """Split ``para`` into sentence-like units, protecting common abbreviations.

    Preconditions:
        - ``para`` is a non-empty string (caller filters empty paragraphs).
    Postconditions:
        - Returns a list of sentence strings (may be length 1 if no boundary found).
        - Mid-sentence abbreviation/decimal periods are not treated as sentence
          boundaries; a real sentence-ending period after an abbreviation
          (next token capitalized, or end of text) is preserved.
        - Returned sentences are not restored to their original text: wherever
          an abbreviation/decimal was protected, the sentence contains the
          placeholder token (e.g. ``egPLACEHOLDER``, ``USPLACEHOLDER``) instead
          of the original ``e.g.``/``U.S.``/etc. Word counts are preserved
          (placeholders are single tokens), which is all the caller (staccato
          word-count detection) relies on; the literal text is not.
    """
    protected = para
    for pattern, token in _ABBREV_PROTECT:
        protected = pattern.sub(token, protected)
    return re.split(r"(?<=[.!?])\s+", protected)


def _unwrap_llm_cause(exc: BaseException) -> BaseException:
    """Return the underlying model error when strands wraps it in EventLoopException.

    Preconditions:
        - ``exc`` is the exception caught at an LLM call boundary.
    Postconditions:
        - If ``exc`` is an ``EventLoopException`` whose ``original_exception``
          is itself a ``BaseException``, returns that original exception.
        - Otherwise (not an ``EventLoopException``, or its ``original_exception``
          is ``None`` or not a ``BaseException``) returns ``exc`` unchanged.
    """
    if isinstance(exc, EventLoopException):
        original = getattr(exc, "original_exception", None)
        if isinstance(original, BaseException):
            return original
    return exc


def _extract_draft_after_marker(raw_response: Optional[str]) -> str:
    """
    Extract draft content from model output that uses the hybrid format:
    first line {\"draft\": 0}, then ---DRAFT---, then the full blog post in Markdown.
    Falls back to scanning the response for extractable JSON (whole-response,
    fenced, or prose-wrapped, via ``extract_json_from_response``) and returning
    the string value of its \"draft\" key. Returns \"\" if no marker is present
    and the JSON fallback's \"draft\" value is missing, non-string, or
    whitespace-only (a number/bool/null/blank-string \"draft\" value is treated
    the same as no usable draft, not surfaced to the caller).
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


def _extract_json_array_from_text(
    text: str, *, required_keys: tuple[str, ...] = ()
) -> Optional[list]:
    """Parse a JSON array of objects from ``text``, including when prefixed by prose.

    Preconditions:
        - ``text`` is a string (may be empty).
        - ``required_keys``, if given, are the keys used to recognize the real
          payload (e.g. ``("issue",)`` for self-review issues): at least one
          element of a candidate array must contain all of them. This rejects
          an unrelated dict array (e.g. a ``references`` list salvaged from
          surrounding prose) that would otherwise pass a bare "is it a list of
          dicts" check, while still tolerating a real payload where some items
          are individually malformed (the caller's own per-item validation
          skips those).
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


def _looks_like_top_level_json_object(text: str) -> bool:
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


# ---------------------------------------------------------------------------
# Self-review: deterministic + LLM review
# ---------------------------------------------------------------------------


def _deterministic_self_check(draft: str) -> list[str]:
    """Scan draft for mechanical violations. Returns list of violation descriptions.

    Checks: em/en dashes, banned phrases (``BANNED_PHRASES``), vague citation
    patterns not followed by a source/link within ``CITATION_LOOKAHEAD_CHARS``,
    reader-address (``you``/``your``/``yours``/``yourself``/``yourselves``)
    count below ``MIN_READER_ADDRESS_COUNT``, and staccato prose
    (``STACCATO_MIN_STREAK``+ consecutive sentences with
    ``<= STACCATO_MAX_WORDS`` words).

    Preconditions:
        - ``draft`` is a string (may be empty).
    Raises:
        TypeError: if ``draft`` is not a string.
    """
    if not isinstance(draft, str):
        raise TypeError(f"draft must be a string, got {type(draft).__name__}")
    violations: list[str] = []
    draft_lower = draft.lower()
    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(draft) if p.strip()]

    # 1. Em/en dashes
    for i, para in enumerate(paragraphs, 1):
        if "\u2014" in para or "\u2013" in para:
            violations.append(f"Em/en dash found in paragraph {i}")

    # 2. Banned phrases (word-boundary aware; see ``_BANNED_PHRASE_PATTERNS``)
    for phrase, pattern in _BANNED_PHRASE_PATTERNS:
        if pattern.search(draft_lower):
            violations.append(f"Banned phrase found: '{phrase}'")

    # 3. Vague citation patterns — only flag if NOT followed by a source/link
    for pattern in VAGUE_CITATION_PATTERNS:
        for match in re.finditer(pattern, draft):
            after = draft[match.end() : match.end() + CITATION_LOOKAHEAD_CHARS]
            if _CITATION_SOURCE_RE.search(after):
                continue
            violations.append(
                f"Vague citation: '{match.group()}' — add an inline link or name a specific source"
            )

    # 4. Reader address count
    you_count = len(_READER_ADDRESS_RE.findall(draft_lower))
    if you_count < MIN_READER_ADDRESS_COUNT:
        violations.append(
            f"Reader address 'you/your' appears only {you_count} time(s) — "
            f"need at least {MIN_READER_ADDRESS_COUNT}"
        )

    # 5. Staccato detection — consecutive short sentences (once per paragraph streak)
    for i, para in enumerate(paragraphs, 1):
        if para.startswith("#"):
            continue
        sentences = _split_sentences_for_staccato(para)
        streak = 0
        flagged = False
        for sent in sentences:
            word_count = len(sent.split())
            if word_count <= STACCATO_MAX_WORDS:
                streak += 1
                if streak >= STACCATO_MIN_STREAK and not flagged:
                    violations.append(
                        f"Staccato prose in paragraph {i}: {streak}+ consecutive short sentences"
                    )
                    flagged = True
            else:
                streak = 0
                flagged = False

    return violations


def _fix_deterministic_violations(draft: str, violations: list[str], call_text: CallText) -> str:
    """Call LLM once to fix deterministic violations. Returns cleaned draft.

    Preconditions:
        - ``draft`` is a non-empty string when callers intend a real fix (empty is allowed).
        - ``violations`` is a list of human-readable violation strings (may be empty).
        - ``call_text`` is a callable of the form
          ``(prompt: str, system_prompt: str = "") -> str``.
    Postconditions:
        - On success with extractable fixed draft, returns that stripped draft.
        - On soft-fail (``LLMError`` excluding types re-raised below, or
          ``json.JSONDecodeError`` / ``TypeError`` / ``ValueError`` / ``AttributeError``),
          logs with traceback via ``logger.exception`` and returns the original ``draft``.
        - ``LLMRateLimitError`` and ``LLMTemporaryError`` (including when wrapped in
          ``EventLoopException``) propagate as the unwrapped cause.
        - Unexpected exceptions propagate unchanged.
    """
    checklist = "\n".join(f"- {v}" for v in violations)
    prompt = (
        "Fix ONLY these specific issues in the draft below. Do not change anything else.\n\n"
        f"ISSUES TO FIX:\n{checklist}\n\n"
        "---\nCURRENT DRAFT:\n---\n"
        f"{draft}\n\n"
        '---\nUse this format: first line {{"draft": 0}}, then ---DRAFT---, '
        "then the full fixed blog post in Markdown."
    )
    try:
        raw = call_text(prompt, system_prompt=WRITING_SYSTEM_PROMPT)
        fixed = _extract_draft_after_marker(raw)
        if fixed and fixed.strip():
            logger.info("Deterministic self-check: fixed %s violations", len(violations))
            return fixed.strip()
    except Exception as e:
        cause = _unwrap_llm_cause(e)
        if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
            raise cause
        if isinstance(
            cause, (LLMError, json.JSONDecodeError, TypeError, ValueError, AttributeError)
        ):
            logger.exception("Deterministic fix LLM call failed")
        else:
            raise
    return draft


def _llm_self_review(draft: str, call_text: CallText) -> str:
    """Run a focused LLM self-review for subjective violations. Returns cleaned draft.

    Preconditions:
        - ``draft`` is a string (may be empty).
        - ``call_text`` is a callable of the form
          ``(prompt: str, system_prompt: str = "") -> str``.
    Postconditions:
        - On success, returns the reviewed/fixed draft or the original when no issues.
        - Three ways the response can resolve to "issues": (1) it parses to a
          JSON list, used directly; (2) it parses to a genuine top-level JSON
          object (the model's real "no issues" response), which returns the
          original ``draft`` unchanged without further rescanning; (3) it
          parses to anything else (a scalar, a malformed object, or fails to
          parse as JSON at all), in which case a prose-rescan
          (``_extract_json_array_from_text``) attempts to salvage an issues
          array from the raw text, returning the original ``draft`` unchanged
          only if no array is recoverable that way either.
        - Whichever path above produces the list, elements lacking a truthy
          ``"issue"`` key are discarded before use; if none remain, returns the
          original ``draft`` unchanged.
        - On soft-fail (``LLMError`` excluding types re-raised below, or
          ``json.JSONDecodeError`` / ``TypeError`` / ``ValueError`` / ``AttributeError``),
          logs with traceback via ``logger.exception`` and returns the original ``draft``.
        - ``LLMRateLimitError`` and ``LLMTemporaryError`` (including when wrapped in
          ``EventLoopException``) propagate as the unwrapped cause.
        - Unexpected exceptions propagate unchanged.
    """
    try:
        raw = call_text(f"Review this draft:\n\n{draft}", system_prompt=SELF_REVIEW_PROMPT)
        cleaned = raw.strip()
        # Prefer the shared extractor for fenced / whole-response JSON. It can
        # raise (extraction fails entirely) or, on success, return a non-list
        # value in two different situations that must be told apart: a
        # genuine top-level JSON object (the model's real "no issues"
        # response) vs. a dict salvaged from prose that isn't the actual
        # top-level structure (e.g. it snagged the one object inside an
        # issues array). Only the latter is worth rescanning for a real
        # array; a genuine top-level object must not be rescanned.
        issues: Optional[list] = None
        try:
            parsed = extract_json_from_response(cleaned)
        except LLMJsonParseError:
            issues = _extract_json_array_from_text(cleaned, required_keys=("issue",))
        else:
            if isinstance(parsed, list):
                issues = parsed
            elif _looks_like_top_level_json_object(cleaned):
                logger.info("LLM self-review: no issues found (response was not a JSON array)")
                return draft
            else:
                issues = _extract_json_array_from_text(cleaned, required_keys=("issue",))
        if issues is None:
            logger.info("LLM self-review: no issues found (response was not a JSON array)")
            return draft
        # Applied uniformly regardless of which path above produced ``issues``:
        # ``_extract_json_array_from_text`` only requires that SOME element carry
        # the required keys, so a malformed sibling dict without a truthy "issue"
        # can otherwise survive into the fix prompt below as a blank issue line.
        issues = [iss for iss in issues if isinstance(iss, dict) and iss.get("issue")]
        if not issues:
            logger.info("LLM self-review: draft passed all checks")
            return draft

        logger.info("LLM self-review found %s issue(s); applying fixes", len(issues))
        issue_lines = []
        for i, iss in enumerate(issues, 1):
            loc = iss.get("location", "")
            desc = iss.get("issue", "")
            fix = iss.get("fix", "")
            issue_lines.append(f"{i}. [{loc}] {desc}\n   Fix: {fix}")

        fix_prompt = (
            "Fix ONLY these issues found during self-review. Do not change anything else.\n\n"
            "ISSUES:\n" + "\n\n".join(issue_lines) + "\n\n"
            "---\nCURRENT DRAFT:\n---\n" + draft + "\n\n"
            '---\nUse this format: first line {{"draft": 0}}, then ---DRAFT---, '
            "then the full fixed blog post in Markdown."
        )
        raw_fix = call_text(fix_prompt, system_prompt=WRITING_SYSTEM_PROMPT)
        fixed = _extract_draft_after_marker(raw_fix)
        if fixed and fixed.strip():
            logger.info("LLM self-review: applied fixes, new length=%s", len(fixed.strip()))
            return fixed.strip()
    except Exception as e:
        cause = _unwrap_llm_cause(e)
        if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
            raise cause
        if isinstance(
            cause, (LLMError, json.JSONDecodeError, TypeError, ValueError, AttributeError)
        ):
            logger.exception("LLM self-review failed")
        else:
            raise
    return draft


def _self_review(draft: str, call_text: CallText) -> str:
    """Run deterministic check then LLM self-review. Returns cleaned draft.

    Both sub-steps (``_fix_deterministic_violations``, ``_llm_self_review``)
    already return the original draft on their own soft-fail paths, so this
    function has no additional failure handling of its own.

    Preconditions:
        - ``draft`` is a string (may be empty).
        - ``call_text`` is a callable of the form
          ``(prompt: str, system_prompt: str = "") -> str``.
    """
    # Step 1: Deterministic checks
    violations = _deterministic_self_check(draft)
    if violations:
        logger.info("Deterministic self-check found %s violation(s)", len(violations))
        draft = _fix_deterministic_violations(draft, violations, call_text)

    # Step 2: LLM self-review for subjective issues
    draft = _llm_self_review(draft, call_text)

    return draft
