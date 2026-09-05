"""
Self-review for blog drafts: a deterministic mechanical pass (banned phrases,
vague citations, em/en dashes, reader address, staccato prose) followed by a
focused LLM review for subjective issues.

Free functions here take an explicit ``call_text`` callback instead of an
agent's bound ``_call_text`` method, so this module has no dependency on
``BlogWriterAgent`` (or on ``agent.py`` at all) and can be adopted by any
caller that can supply a ``(prompt, system_prompt) -> str`` text completion.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

from agents.blogging.shared.text_parsing import (
    extract_draft_after_marker,
    extract_json_array_from_text,
    looks_like_top_level_json_object,
    unwrap_llm_cause,
)

from llm_service import (
    LLMError,
    LLMJsonParseError,
    LLMRateLimitError,
    LLMTemporaryError,
    extract_json_from_response,
)

from .prompts import SELF_REVIEW_PROMPT, WRITING_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# A text-completion callback: ``call_text(prompt, system_prompt) -> response``.
# Mirrors ``BlogWriterAgent._call_text``'s signature.
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


def _split_sentences_for_staccato(para: str) -> list[str]:
    """Split ``para`` into sentence-like units, protecting common abbreviations.

    Preconditions:
        - ``para`` is a non-empty string (caller filters empty paragraphs).
    Postconditions:
        - Returns a list of sentence strings (may be length 1 if no boundary found).
        - Mid-sentence abbreviation/decimal periods are not treated as sentence
          boundaries; a real sentence-ending period after an abbreviation
          (next token capitalized, or end of text) is preserved.
        - Abbreviations and decimal numbers are replaced by internal
          ``_ABBREV_PROTECT`` placeholder tokens (e.g. ``egPLACEHOLDER``,
          ``NUMPLACEHOLDER``) before splitting, to avoid false sentence
          boundaries. The placeholders are NOT restored: returned sentences
          contain them verbatim. Callers only use the results for word-count
          streak detection (``deterministic_self_check``), which is
          unaffected by this substitution.
    """
    protected = para
    for pattern, token in _ABBREV_PROTECT:
        protected = pattern.sub(token, protected)
    return re.split(r"(?<=[.!?])\s+", protected)


def deterministic_self_check(draft: str) -> list[str]:
    """Scan draft for mechanical violations. Returns list of violation descriptions.

    Checks: em/en dashes, banned phrases (``BANNED_PHRASES``), vague citation
    patterns not followed by a source/link within ``CITATION_LOOKAHEAD_CHARS``,
    reader-address (``you``/``your``/``yours``/``yourself``/``yourselves``)
    count below ``MIN_READER_ADDRESS_COUNT``, and staccato prose
    (``STACCATO_MIN_STREAK``+ consecutive sentences with
    ``<= STACCATO_MAX_WORDS`` words).

    Preconditions:
        - ``draft`` is a string (may be empty).
    Postconditions:
        - Returns a list of human-readable violation description strings.
        - Returns an empty list when no mechanical violations are detected.
        - Does not mutate ``draft``.
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


def fix_deterministic_violations(
    draft: str,
    violations: list[str],
    call_text: CallText,
    allowed_claims_section: str = "",
) -> str:
    """Call the LLM once to fix deterministic violations. Returns cleaned draft.

    Preconditions:
        - ``draft`` is a non-empty string when callers intend a real fix (empty is allowed).
        - ``violations`` is a list of human-readable violation strings (may be empty).
        - ``call_text`` is a ``(prompt, system_prompt) -> response`` text-completion
          callback (e.g. ``BlogWriterAgent._call_text``).
        - ``allowed_claims_section`` is the caller's already-rendered allowed-claims
          prompt block (e.g. via ``agent._render_allowed_claims_section``), or ``""``
          when no allowed-claims artifact was supplied. This function embeds its
          text unmodified (surrounded only by blank-line spacing, no added wrapper
          *text*) so the block's own self-contained guidance — tag-and-preserve
          when claims are listed, no tags at all when the artifact is present but
          empty — is never contradicted by a blanket "preserve everything"
          instruction layered on top.
    Postconditions:
        - On success with extractable fixed draft, returns that stripped draft.
        - When ``allowed_claims_section`` is non-empty, the fix prompt includes its
          text unmodified (as its own paragraph, surrounded by blank-line spacing),
          instructing the model to follow the claims policy it describes (this is
          a prompt instruction, not an enforced guarantee: the function does not
          validate or otherwise check that the model's rewrite obeys it).
        - On soft-fail (``LLMError`` excluding types re-raised below, or
          ``json.JSONDecodeError`` / ``TypeError`` / ``ValueError`` / ``AttributeError``),
          logs with traceback via ``logger.exception`` and returns the original ``draft``.
        - ``LLMRateLimitError`` and ``LLMTemporaryError`` (including when wrapped in
          ``EventLoopException``) propagate as the unwrapped cause.
        - Unexpected exceptions propagate unchanged.
    """
    checklist = "\n".join(f"- {v}" for v in violations)
    claims_block = f"\n\n{allowed_claims_section}\n" if allowed_claims_section else ""
    prompt = (
        "Fix ONLY these specific issues in the draft below. Do not change anything else.\n\n"
        f"ISSUES TO FIX:\n{checklist}\n\n"
        "---\nCURRENT DRAFT:\n---\n"
        f"{draft}\n"
        f"{claims_block}\n"
        '---\nUse this format: first line {{"draft": 0}}, then ---DRAFT---, '
        "then the full fixed blog post in Markdown."
    )
    try:
        raw = call_text(prompt, WRITING_SYSTEM_PROMPT)
        fixed = extract_draft_after_marker(raw)
        if fixed and fixed.strip():
            logger.info("Deterministic self-check: fixed %s violations", len(violations))
            return fixed.strip()
    except Exception as e:
        cause = unwrap_llm_cause(e)
        if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
            raise cause
        if isinstance(
            cause, (LLMError, json.JSONDecodeError, TypeError, ValueError, AttributeError)
        ):
            logger.exception("Deterministic fix LLM call failed")
        else:
            raise
    return draft


def llm_self_review(
    draft: str,
    call_text: CallText,
    allowed_claims_section: str = "",
) -> str:
    """Run a focused LLM self-review for subjective violations. Returns cleaned draft.

    Preconditions:
        - ``draft`` is a string (may be empty).
        - ``call_text`` is a ``(prompt, system_prompt) -> response`` text-completion
          callback (e.g. ``BlogWriterAgent._call_text``).
        - ``allowed_claims_section`` is the caller's already-rendered allowed-claims
          prompt block (e.g. via ``agent._render_allowed_claims_section``), or ``""``
          when no allowed-claims artifact was supplied. Its text is embedded
          unmodified (surrounded only by blank-line spacing, no added wrapper
          *text*) so its own self-contained guidance is never contradicted by a
          blanket "preserve everything" instruction layered on top.
    Postconditions:
        - On success, returns the reviewed/fixed draft or the original when no issues.
        - When issues are found and ``allowed_claims_section`` is non-empty, the fix
          prompt includes its text unmodified (as its own paragraph, surrounded by
          blank-line spacing), instructing the model to follow the claims policy it
          describes (a prompt instruction, not an enforced guarantee: the function
          does not validate or otherwise check that the model's rewrite obeys it).
        - Three ways the response can resolve to "issues": (1) it parses to a
          JSON list, used directly; (2) it parses to a genuine top-level JSON
          object (the model's real "no issues" response), which returns the
          original ``draft`` unchanged without further rescanning; (3) it
          parses to anything else (a scalar, a malformed object, or fails to
          parse as JSON at all), in which case a prose-rescan
          (``extract_json_array_from_text``) attempts to salvage an issues
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
        raw = call_text(f"Review this draft:\n\n{draft}", SELF_REVIEW_PROMPT)
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
            issues = extract_json_array_from_text(cleaned, required_keys=("issue",))
            if issues is not None:
                issues = [iss for iss in issues if iss.get("issue")]
        else:
            if isinstance(parsed, list):
                issues = [iss for iss in parsed if isinstance(iss, dict) and iss.get("issue")]
            elif looks_like_top_level_json_object(cleaned):
                logger.info("LLM self-review: no issues found (response was not a JSON array)")
                return draft
            else:
                issues = extract_json_array_from_text(cleaned, required_keys=("issue",))
                if issues is not None:
                    issues = [iss for iss in issues if iss.get("issue")]
        if issues is None:
            logger.info("LLM self-review: no issues found (response was not a JSON array)")
            return draft
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

        claims_block = f"\n\n{allowed_claims_section}\n" if allowed_claims_section else ""
        fix_prompt = (
            "Fix ONLY these issues found during self-review. Do not change anything else.\n\n"
            "ISSUES:\n" + "\n\n".join(issue_lines) + "\n\n"
            "---\nCURRENT DRAFT:\n---\n" + draft + "\n" + claims_block + "\n"
            '---\nUse this format: first line {{"draft": 0}}, then ---DRAFT---, '
            "then the full fixed blog post in Markdown."
        )
        raw_fix = call_text(fix_prompt, WRITING_SYSTEM_PROMPT)
        fixed = extract_draft_after_marker(raw_fix)
        if fixed and fixed.strip():
            logger.info("LLM self-review: applied fixes, new length=%s", len(fixed.strip()))
            return fixed.strip()
    except Exception as e:
        cause = unwrap_llm_cause(e)
        if isinstance(cause, (LLMRateLimitError, LLMTemporaryError)):
            raise cause
        if isinstance(
            cause, (LLMError, json.JSONDecodeError, TypeError, ValueError, AttributeError)
        ):
            logger.exception("LLM self-review failed")
        else:
            raise
    return draft


def self_review(draft: str, call_text: CallText, allowed_claims_section: str = "") -> str:
    """Run deterministic check then LLM self-review. Returns cleaned draft.

    Both sub-steps (``fix_deterministic_violations``, ``llm_self_review``)
    already return their own *input* draft unchanged on a soft-fail, so this
    function has no additional failure handling of its own.

    Preconditions:
        - ``draft`` is a string (may be empty).
        - ``call_text`` is a ``(prompt, system_prompt) -> response`` text-completion
          callback (e.g. ``BlogWriterAgent._call_text``).
        - ``allowed_claims_section`` is the caller's already-rendered allowed-claims
          prompt block, or ``""`` when no allowed-claims artifact was supplied;
          forwarded unchanged to both sub-steps.
    Postconditions:
        - Returns the draft after applying any deterministic fixes and any
          LLM self-review fixes.
        - If ``fix_deterministic_violations`` soft-fails, it returns the
          original ``draft`` unchanged, and ``llm_self_review`` then runs on
          that same original draft.
        - If ``llm_self_review`` soft-fails, it returns its own input
          unchanged — the deterministically-fixed draft when Step 1 ran and
          succeeded, or the original ``draft`` when Step 1 did not run (no
          violations) or itself soft-failed. It is NOT always the original
          ``draft`` passed to ``self_review``.
    """
    # Step 1: Deterministic checks
    violations = deterministic_self_check(draft)
    if violations:
        logger.info("Deterministic self-check found %s violation(s)", len(violations))
        draft = fix_deterministic_violations(draft, violations, call_text, allowed_claims_section)

    # Step 2: LLM self-review for subjective issues
    draft = llm_self_review(draft, call_text, allowed_claims_section)

    return draft
