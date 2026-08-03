"""
Open-question processing for the Product Requirements Analysis Agent.

Between spec review and asking the user, the raw open questions pass through a
pipeline that parses LLM output into typed :class:`OpenQuestion` models, filters out
duplicates of already-answered questions and organizational/process questions,
consolidates semantically-equivalent questions, checks question/option coherence,
and attaches a recommended option. The LLM-backed steps take an explicit Strands
``model`` and fall back to the unmodified list on any failure; the rest are pure.
"""

from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any, List

from strands.models.model import Model

from software_engineering_team.shared.deduplication import dedupe_strings as _dedupe_items

from .llm_io import call_llm_json
from .models import AnsweredQuestion, OpenQuestion, QuestionOption, SpecReviewResult
from .prompts import (
    CONSOLIDATE_QUESTIONS_PROMPT,
    GENERATE_QUESTION_RECOMMENDATIONS_PROMPT,
    REVIEW_QUESTIONS_ALIGNMENT_PROMPT,
)
from .qa_history import content_words

logger = logging.getLogger(__name__)

MAX_ISSUES = 10
MAX_GAPS = 10
MAX_OPEN_QUESTIONS = 10


def cap_open_questions(
    questions: List[OpenQuestion],
    *,
    limit: int = MAX_OPEN_QUESTIONS,
) -> List[OpenQuestion]:
    """Return at most ``limit`` open questions, preserving order.

    Preconditions: ``questions`` is a list of :class:`OpenQuestion`; ``limit`` >= 0.
    Postconditions: returns ``questions`` unchanged when ``len(questions) <= limit``,
        otherwise the first ``limit`` items; never raises.
    """
    assert limit >= 0, f"limit must be >= 0, got {limit}"
    if len(questions) <= limit:
        return list(questions)
    logger.info("Truncated open questions: %d->%d", len(questions), limit)
    return questions[:limit]

ORGANIZATIONAL_PHRASES = [
    "decision process",
    "approval process",
    "who makes",
    "final decision",
    "consensus",
    "product manager",
    "stakeholder approval",
    "organizational structure",
    "who approves",
    "sign-off",
    "sign off",
    "hierarchy",
    "reporting",
]


def _stem_candidates(w: str) -> set[str]:
    """Return plausible root forms of ``w`` for keyword-coverage matching.

    A single-guess suffix strip is ambiguous for silent-e roots (e.g. "based"
    is "base" + d, "stored" is "store" + d) versus regular roots (e.g.
    "documented" is "document" + ed), for "-es" plurals (e.g. "phases" is
    "phase" + s, "classes" is "class" + es), and for "-y"/"-ied" verbs (e.g.
    "studied" is "study" with "y" replaced by "ied") — the surface form alone
    doesn't say which rule applies. Guessing a single stem silently drops the
    correct one (e.g. stemming "based" to "bas" alone would never match
    "base"). Instead this returns every plausible root and lets the caller
    accept a match against any of them. This is a small suffix map covering
    common regular English plural/past-tense inflections, not a linguistically
    complete stemmer/lemmatizer: irregular forms (e.g. "geese" -> "goose")
    are not handled, and it only strips one layer of suffix.

    Preconditions: ``w`` is a lowercase string with no whitespace.
    Postconditions: returns a non-empty set of candidate stems that always
        includes ``w`` itself; a word ending in "ss" (e.g. "address",
        "process") is returned unchanged, since a doubled-s ending is never
        itself a plural/past-tense suffix here; never raises.
    """
    candidates = {w}
    if w.endswith("ss"):
        return candidates
    if w.endswith("ied") and len(w) > 4:
        candidates.add(w[:-3] + "y")  # studied -> study, tried -> try
    if w.endswith("ed") and len(w) > 4:
        candidates.add(w[:-1])  # based -> base, stored -> store
        candidates.add(w[:-2])  # documented -> document
    elif w.endswith("ies") and len(w) > 4:
        candidates.add(w[:-3] + "y")  # policies -> policy
    elif w.endswith("es") and len(w) > 4:
        candidates.add(w[:-1])  # phases -> phase
        candidates.add(w[:-2])  # classes -> class
    elif w.endswith("s") and len(w) > 4:
        candidates.add(w[:-1])  # tokens -> token
    return candidates


def filter_duplicate_questions(
    new_questions: List[OpenQuestion],
    qa_history: str,
) -> tuple[List[OpenQuestion], List[OpenQuestion]]:
    """Filter out questions that appear to be duplicates of answered ones.

    Filters out questions whose keywords share a common stem candidate (see
    :func:`_stem_candidates`) with a word tokenized out of the Q&A history —
    both sides are stemmed the same way so a root-form keyword matches an
    inflected history word and vice versa (e.g. keyword "class" matches
    history "classes", and keyword "classes" matches history "class"). A
    question is considered a duplicate when at least 90% of its keywords have
    such a match. Below-90% coverage is kept for possible consolidation
    elsewhere. This is keyword coverage, not a similarity ratio between the
    question and history.

    Uses :func:`content_words` (stopword-based, not length-based) for the same
    keyword-admission rule as :func:`qa_history.extract_answer_from_qa_history`,
    so a short-keyword question (e.g. one about an acronym) that the extractor
    can now match isn't excluded from ``duplicates`` here first — otherwise it
    would never reach the extractor at all and would be re-asked regardless of
    the extractor's own behavior.

    Returns:
        Tuple of (filtered_questions, duplicate_questions).
        - filtered_questions: Questions that are NOT duplicates (should be asked)
        - duplicate_questions: Questions that ARE duplicates (already answered)

    Preconditions: ``new_questions`` is a list of :class:`OpenQuestion`;
        ``qa_history`` is a string.
    Postconditions: the two returned lists partition ``new_questions`` (order
        preserved within each); never raises.
    """
    qa_history_lower = (qa_history or "").lower()
    filtered = []
    duplicates = []

    for q in new_questions:
        q_text_lower = (q.question_text or "").lower()
        key_words = content_words(q_text_lower)
        if not key_words:
            filtered.append(q)
            continue
        # Tokenize qa_history the same way content_words does (so punctuation
        # can't glue two words together), then expand every history token to
        # its own stem candidates. A keyword matches history when the two
        # candidate sets intersect — stemming both sides identically is what
        # makes the match symmetric: a root-form keyword (e.g. "class")
        # matches an inflected history word ("classes") exactly as readily as
        # an inflected keyword ("classes") matches a root-form history word
        # ("class"). Whole-word only (not substring containment), so a short
        # stem can't false-match inside an unrelated longer word (e.g. "api"
        # inside "capitalizing") now that short content words are admitted as
        # keywords.
        history_stem_pool: set[str] = set()
        for history_word in re.sub(r"[^\w\s]", " ", qa_history_lower).split():
            history_stem_pool |= _stem_candidates(history_word)
        matches = sum(1 for w in key_words if _stem_candidates(w) & history_stem_pool)
        match_ratio = matches / len(key_words)
        # Only treat as duplicate of an answered question when match >= 90%.
        # Below-90% coverage may be consolidated but should not be filtered out.
        if match_ratio >= 0.90:
            logger.info(
                "Filtering duplicate question (%.0f%% match): %s",
                match_ratio * 100,
                q.question_text,
            )
            duplicates.append(q)
            continue
        filtered.append(q)

    if duplicates:
        logger.info(
            "Filtered %d duplicate questions based on qa_history",
            len(duplicates),
        )

    return filtered, duplicates


def filter_organizational_questions(questions: List[OpenQuestion]) -> List[OpenQuestion]:
    """Remove questions about organizational structure, approval processes, or decision hierarchy.

    The client/user is the source of truth; we do not ask who approves, how decisions
    are made, or about org structure. A question is considered organizational if any
    of the configured phrases appear in question_text or (if present) context.

    Preconditions: ``questions`` is a list of :class:`OpenQuestion`.
    Postconditions: returns the sublist that is not organizational, order preserved.
    """
    kept: List[OpenQuestion] = []
    for q in questions:
        text_norm = (q.question_text or "").lower().strip()
        context_norm = (q.context or "").lower().strip() if q.context else ""
        is_org = False
        for phrase in ORGANIZATIONAL_PHRASES:
            if phrase in text_norm or (context_norm and phrase in context_norm):
                is_org = True
                break
        if not is_org:
            kept.append(q)
    removed = len(questions) - len(kept)
    if removed:
        logger.info(
            "Filtered %d organizational/process question(s)",
            removed,
        )
    return kept


def parse_spec_review_response(raw: Any) -> SpecReviewResult:
    """Parse LLM response into SpecReviewResult.

    Applies deduplication and enforces max limits on issues/gaps to prevent runaway
    repetitive output from the LLM.

    Preconditions: ``raw`` is the decoded LLM output (any type).
    Postconditions: returns a valid :class:`SpecReviewResult`; issues/gaps are
        deduped and capped at ``MAX_ISSUES``/``MAX_GAPS``; open questions are
        parsed but not capped here (the agent workflow applies
        ``MAX_OPEN_QUESTIONS`` after semantic consolidation and
        answer-similarity deduplication so near-duplicates do not crowd out
        distinct topics); never raises.
    """
    if not isinstance(raw, dict):
        return SpecReviewResult(summary="Spec review completed (no structured output)")

    raw_issues = raw.get("issues", [])
    raw_gaps = raw.get("gaps", [])
    raw_questions = raw.get("open_questions", [])

    # Deduplicate and limit issues/gaps to prevent repetitive LLM output
    issues = list(raw_issues) if isinstance(raw_issues, list) else []
    gaps = list(raw_gaps) if isinstance(raw_gaps, list) else []

    original_issue_count = len(issues)
    original_gap_count = len(gaps)

    issues = _dedupe_items(issues)[:MAX_ISSUES]
    gaps = _dedupe_items(gaps)[:MAX_GAPS]

    if len(issues) < original_issue_count or len(gaps) < original_gap_count:
        logger.info(
            "Deduplicated spec review results: issues %d->%d, gaps %d->%d",
            original_issue_count,
            len(issues),
            original_gap_count,
            len(gaps),
        )

    open_questions = []
    if isinstance(raw_questions, list):
        for i, q in enumerate(raw_questions):
            try:
                open_questions.append(parse_open_question(q, i))
            except (ValueError, TypeError) as exc:
                logger.warning("Skipping malformed open question at index %d: %s", i, exc)

    return SpecReviewResult(
        issues=issues,
        gaps=gaps,
        open_questions=open_questions,
        summary=str(raw.get("summary") or "Spec review complete"),
    )


def _str_or_default(value: Any, default: str = "") -> str:
    """Coerce an LLM-provided field to str, treating an explicit ``None`` as missing.

    Preconditions: none; ``value`` may be any decoded JSON type.
    Postconditions: returns ``default`` when ``value`` is ``None``, else ``str(value)``.
    """
    return default if value is None else str(value)


def _safe_constraint_layer(value: Any) -> int:
    """Coerce LLM-provided constraint_layer output to int, defaulting to 0.

    Preconditions: none; ``value`` may be any decoded JSON type.
    Postconditions: returns an int; non-numeric or missing input yields 0,
        matching the "not a constraint question" default in :class:`OpenQuestion`.
    """
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def _coerce_list(value: Any) -> list:
    """Coerce LLM-provided list-valued output to a list.

    Preconditions: none; ``value`` may be any decoded JSON type.
    Postconditions: returns a list. None -> []; list/tuple -> list(value);
        any other scalar (str, int, dict, ...) -> [value] (never iterated char-by-char).
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def parse_open_question(q_data: Any, index: int) -> OpenQuestion:
    """Parse a single open question from LLM output.

    Preconditions: ``index`` is a non-negative int; ``q_data`` is the decoded item.
    Postconditions: returns a valid :class:`OpenQuestion`.
        - ``options``, ``section_impact``, and ``asked_via`` are coerced to lists
          (``None`` becomes ``[]``, scalars become single-element lists).
        - ``section_impact`` and ``asked_via`` elements are coerced to ``str``.
        - Option ``confidence`` values are normalized to ``[0.0, 1.0]``,
          defaulting to ``0.5`` when missing or malformed.
        - When ``q_data`` is a dict with options but no default, the
          highest-confidence option is marked default.
        - The coercions above cover every known malformed-input shape from an
          LLM response, but this function has no top-level try/except of its
          own: it does not guarantee never raising in the face of an
          unanticipated input, and every production caller (parse_spec_review_response,
          consolidate_open_questions, review_question_answer_alignment,
          run_context_constraints_discovery) wraps it accordingly.
    """
    if isinstance(q_data, dict):
        raw_options = _coerce_list(q_data.get("options", []))
        options = []
        for i, opt in enumerate(raw_options):
            options.append(parse_question_option(opt, i))

        if options and not any(opt.is_default for opt in options):
            default_idx = max(range(len(options)), key=lambda i: options[i].confidence)
            options[default_idx] = QuestionOption(
                id=options[default_idx].id,
                label=options[default_idx].label,
                is_default=True,
                rationale=options[default_idx].rationale,
                confidence=options[default_idx].confidence,
            )

        raw_depends = q_data.get("depends_on")
        if isinstance(raw_depends, (list, tuple)):
            depends_on = str(raw_depends[0]) if raw_depends else None
        elif isinstance(raw_depends, str):
            depends_on = raw_depends
        else:
            depends_on = None

        section_impact = [str(v) for v in _coerce_list(q_data.get("section_impact", []))]
        asked_via = [str(v) for v in _coerce_list(q_data.get("asked_via", []))]

        return OpenQuestion(
            id=_str_or_default(q_data.get("id"), f"q{index}"),
            question_text=_str_or_default(q_data.get("question_text")),
            context=_str_or_default(q_data.get("context")),
            recommendation=_str_or_default(q_data.get("recommendation")),
            options=options,
            allow_multiple=bool(q_data.get("allow_multiple", False)),
            source=_str_or_default(q_data.get("source"), "spec_review"),
            category=_str_or_default(q_data.get("category"), "general"),
            priority=_str_or_default(q_data.get("priority"), "medium"),
            constraint_domain=_str_or_default(q_data.get("constraint_domain")),
            constraint_layer=_safe_constraint_layer(q_data.get("constraint_layer")),
            depends_on=depends_on,
            blocking=bool(q_data.get("blocking", True)),
            owner=_str_or_default(q_data.get("owner"), "user"),
            section_impact=section_impact,
            due_date=_str_or_default(q_data.get("due_date")),
            status=_str_or_default(q_data.get("status"), "open"),
            asked_via=asked_via,
        )

    return OpenQuestion(
        id=f"q{index}",
        question_text=str(q_data),
        context="This question was identified during spec review.",
        recommendation="",
        options=[
            QuestionOption(id="opt1", label="Yes", is_default=True, rationale="", confidence=0.5),
            QuestionOption(id="opt2", label="No", is_default=False, rationale="", confidence=0.5),
        ],
        allow_multiple=False,
        source="spec_review",
        blocking=True,
        owner="user",
        section_impact=[],
        due_date="",
        status="open",
        asked_via=[],
    )


def _safe_confidence(value: Any) -> float:
    """Coerce LLM-provided confidence output to a valid [0.0, 1.0] float, defaulting to 0.5.

    Preconditions: none; ``value`` may be any decoded JSON type.
    Postconditions: returns a float clamped to [0.0, 1.0]; non-numeric or missing input
        yields 0.5, matching the "no machine-supplied score" default.
    """
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.5
    if result != result:  # NaN check without importing math
        return 0.5
    return max(0.0, min(1.0, result))


def parse_question_option(opt_data: Any, index: int) -> QuestionOption:
    """Parse a single question option from LLM output.

    Preconditions: ``index`` is a non-negative int; ``opt_data`` is the decoded item.
    Postconditions: returns a valid :class:`QuestionOption`; a non-dict becomes a
        label-only option defaulting only at ``index == 0``; a non-numeric, ``None``,
        out-of-range, or overflowing ``confidence`` value defaults to 0.5 (or is
        clamped to ``[0.0, 1.0]``) instead of raising.
    """
    if isinstance(opt_data, dict):
        return QuestionOption(
            id=_str_or_default(opt_data.get("id"), f"opt{index}"),
            label=_str_or_default(opt_data.get("label")),
            is_default=bool(opt_data.get("is_default", False)),
            rationale=_str_or_default(opt_data.get("rationale")),
            confidence=_safe_confidence(opt_data.get("confidence", 0.5)),
        )
    return QuestionOption(
        id=f"opt{index}",
        label=str(opt_data),
        is_default=index == 0,
        rationale="",
        confidence=0.5,
    )


def dedupe_questions_by_answer_similarity(
    open_questions: List[OpenQuestion],
    answered_questions: List[AnsweredQuestion],
) -> List[OpenQuestion]:
    """Drop open questions whose answer we already have.

    Compares answers (selected_answer and other_text from answered_questions) to the
    option labels of each open question. If any option of an open question is
    semantically the same as an answer we already have, we do not ask that question
    again. Preserves order of open_questions.

    Preconditions: both arguments are lists of the respective models.
    Postconditions: returns a sublist of ``open_questions`` (order preserved);
        questions with no options/labels are always kept; never raises.
    """
    if not open_questions:
        return list(open_questions)

    def norm(t: str | None) -> str:
        return " ".join((t or "").lower().split()).strip()

    # Build set of existing answers (normalized) we already have
    existing_answers: set[str] = set()
    for aq in answered_questions:
        s = norm(aq.selected_answer)
        if s:
            existing_answers.add(s)
        if getattr(aq, "other_text", None) and aq.other_text.strip():
            o = norm(aq.other_text)
            if o:
                existing_answers.add(o)

    if not existing_answers:
        return list(open_questions)

    # Same threshold as shared deduplication for "same meaning"
    SIMILARITY_THRESHOLD = 0.85
    kept: List[OpenQuestion] = []

    for q in open_questions:
        if not q.options:
            # No options: we cannot know what answer this would get; keep it
            kept.append(q)
            continue
        option_labels = [norm(opt.label) for opt in q.options if opt.label]
        if not option_labels:
            kept.append(q)
            continue
        # If any option is the same as an answer we already have, skip this question
        already_covered = False
        for opt_label in option_labels:
            if not opt_label:
                continue
            for existing in existing_answers:
                if SequenceMatcher(None, opt_label, existing).ratio() >= SIMILARITY_THRESHOLD:
                    logger.info(
                        "Skipping open question (answer already have): question_id=%s option=%r ~ existing=%r",
                        q.id,
                        opt_label,
                        existing,
                    )
                    already_covered = True
                    break
            if already_covered:
                break
        if not already_covered:
            kept.append(q)

    return kept


def _fetch_llm_list(
    model: Model,
    prompt: str,
    response_key: str,
    operation_name: str,
    allow_empty: bool = False,
) -> List[Any] | None:
    """Call the LLM, parse JSON, and extract a named list field.

    Shared seam for the "call LLM -> validate response shape -> fall back to
    caller's original list" pattern common to the consolidate/align/recommend
    steps below. Per-item parsing and reconciliation stay with each caller.

    Preconditions: ``model`` is a Strands ``Model``; ``prompt`` is a non-empty
        string; ``response_key``/``operation_name`` are non-empty strings.
    Postconditions: returns the list found under ``response_key`` when the LLM
        call succeeds and yields a list that is non-empty, or empty when
        ``allow_empty`` is True; returns ``None`` on any failure (LLM
        exception, non-dict response, a missing/non-list key, or an empty
        list when ``allow_empty`` is False) — callers fall back to their
        original list on ``None``. Never raises.
    """
    try:
        raw = call_llm_json(model, prompt)
    except Exception as e:
        logger.warning("%s failed, using original list: %s", operation_name, str(e))
        return None
    if not isinstance(raw, dict):
        return None
    items = raw.get(response_key)
    if not isinstance(items, list):
        return None
    if not items and not allow_empty:
        return None
    return items


def consolidate_open_questions(
    model: Model, open_questions: List[OpenQuestion]
) -> List[OpenQuestion]:
    """Merge duplicate or semantically equivalent questions before sending to user.

    Uses a single LLM call to identify questions that ask the same thing (e.g. OAuth
    provider asked multiple ways) and consolidate them into one question per distinct
    decision, with merged options.

    Preconditions: ``model`` is a Strands ``Model``; ``open_questions`` a list.
    Postconditions: returns the consolidated list, or the unmodified list on <=1
        input or a full-batch LLM/parse failure; never raises. Items that
        individually fail to parse are skipped and logged rather than discarding
        the whole batch.
    """
    if len(open_questions) <= 1:
        return list(open_questions)

    questions_json = json.dumps(
        [
            {
                "id": q.id,
                "question_text": q.question_text,
                "context": q.context,
                "category": q.category,
                "priority": q.priority,
                "allow_multiple": q.allow_multiple,
                "constraint_domain": q.constraint_domain,
                "constraint_layer": q.constraint_layer,
                "depends_on": q.depends_on,
                "blocking": q.blocking,
                "owner": q.owner,
                "section_impact": q.section_impact,
                "due_date": q.due_date,
                "status": q.status,
                "asked_via": q.asked_via,
                "options": [
                    {
                        "id": o.id,
                        "label": o.label,
                        "is_default": o.is_default,
                        "rationale": o.rationale,
                        "confidence": o.confidence,
                    }
                    for o in q.options
                ],
            }
            for q in open_questions
        ],
        indent=2,
    )
    prompt = CONSOLIDATE_QUESTIONS_PROMPT.format(questions_json=questions_json)
    consolidated = _fetch_llm_list(
        model, prompt, "consolidated_questions", "Question consolidation"
    )
    if consolidated is None:
        return list(open_questions)
    try:
        result = []
        for i, q_data in enumerate(consolidated):
            try:
                result.append(parse_open_question(q_data, i))
            except Exception as e:
                logger.warning("Failed to parse consolidated question %d: %s", i, e)
        return result if result else list(open_questions)
    except Exception as e:
        logger.warning("Question consolidation failed, using original list: %s", str(e))
        return list(open_questions)


def review_question_answer_alignment(
    model: Model, open_questions: List[OpenQuestion]
) -> List[OpenQuestion]:
    """Ensure each question and its options make sense together (e.g. no Yes/No for open-ended questions).

    Preconditions: ``model`` is a Strands ``Model``; ``open_questions`` a list.
    Postconditions: returns the aligned list, or the unmodified list (in its
        original order) on empty input or when no item in the batch parses
        successfully; never raises. This is a per-question review (ids are
        preserved): an item that individually fails to parse or that repeats
        an id already placed in the result (a duplicate) falls back to its
        original (unaligned) question by id, when that original id is not
        already in the result; an item carrying an id not present in
        ``open_questions`` (a hallucinated/unrecognized id) has no original to
        fall back to and is dropped outright. Any original question whose id
        never appears in the result (whether dropped as a duplicate/
        hallucination or simply omitted by the LLM) is appended at the end.
        If no item in the batch parses successfully, the LLM-provided order
        carries no meaning, so the original list is returned unchanged rather
        than in fallback (LLM-provided) order. The result therefore contains
        exactly one entry per original id: no question is ever dropped,
        added, or duplicated.
    """
    if len(open_questions) == 0:
        return []
    original_by_id = {q.id: q for q in open_questions}
    questions_payload = [
        {
            "id": q.id,
            "question_text": q.question_text,
            "context": q.context,
            "category": q.category,
            "priority": q.priority,
            "allow_multiple": q.allow_multiple,
            "constraint_domain": q.constraint_domain,
            "constraint_layer": q.constraint_layer,
            "depends_on": q.depends_on,
            "blocking": q.blocking,
            "owner": q.owner,
            "section_impact": q.section_impact,
            "due_date": q.due_date,
            "status": q.status,
            "asked_via": q.asked_via,
            "options": [
                {
                    "id": o.id,
                    "label": o.label,
                    "is_default": o.is_default,
                    "rationale": o.rationale,
                    "confidence": o.confidence,
                }
                for o in q.options
            ],
        }
        for q in open_questions
    ]
    questions_json = json.dumps(questions_payload, indent=2)
    prompt = REVIEW_QUESTIONS_ALIGNMENT_PROMPT.format(questions_json=questions_json)
    aligned = _fetch_llm_list(
        model, prompt, "aligned_questions", "Question-answer alignment review"
    )
    if aligned is None:
        return list(open_questions)
    try:
        result = []
        seen_ids = set()
        any_parsed = False
        for i, q_data in enumerate(aligned):
            try:
                parsed = parse_open_question(q_data, i)
                if parsed.id not in original_by_id:
                    raise ValueError(
                        f"aligned question id {parsed.id!r} does not match any original question"
                    )
                if parsed.id in seen_ids:
                    raise ValueError(f"aligned question id {parsed.id!r} is a duplicate")
                result.append(parsed)
                seen_ids.add(parsed.id)
                any_parsed = True
            except Exception as e:
                logger.warning("Failed to parse aligned question %d: %s", i, e)
                fallback_id = q_data.get("id") if isinstance(q_data, dict) else None
                original = original_by_id.get(fallback_id) if isinstance(fallback_id, str) else None
                if original is not None and original.id not in seen_ids:
                    result.append(original)
                    seen_ids.add(original.id)
        if not any_parsed:
            # Nothing in the batch was genuinely realigned, so the LLM-provided
            # order (which any fallbacks above were assembled in) carries no
            # meaning — return the original list in its original order instead.
            return list(open_questions)
        for q in open_questions:
            if q.id not in seen_ids:
                result.append(q)
        return result
    except Exception as e:
        logger.warning(
            "Question-answer alignment review failed, using original list: %s",
            str(e),
        )
        return list(open_questions)


def add_recommendations(
    model: Model, open_questions: List[OpenQuestion], spec_content: str
) -> List[OpenQuestion]:
    """Add a short recommendation (which option and why) to each question.

    Preconditions: ``model`` is a Strands ``Model``; ``open_questions`` a list;
        ``spec_content`` a string.
    Postconditions: returns the list with ``recommendation`` populated where the LLM
        supplied one, or the unmodified list on empty input or any failure; never raises.
    """
    if len(open_questions) == 0:
        return list(open_questions)
    questions_payload = [
        {
            "id": q.id,
            "question_text": q.question_text,
            "context": q.context,
            "options": [
                {
                    "id": o.id,
                    "label": o.label,
                    "rationale": o.rationale,
                }
                for o in q.options
            ],
        }
        for q in open_questions
    ]
    questions_json = json.dumps(questions_payload, indent=2)
    spec_content_str = spec_content or ""
    prompt = GENERATE_QUESTION_RECOMMENDATIONS_PROMPT.format(
        spec_content=spec_content_str,
        questions_json=questions_json,
    )
    recs = _fetch_llm_list(
        model, prompt, "recommendations", "Recommendation generation", allow_empty=True
    )
    if recs is None:
        return list(open_questions)
    try:
        rec_by_id = {
            r.get("id"): str(r["recommendation"])
            for r in recs
            if (
                isinstance(r, dict)
                and isinstance(r.get("id"), str)
                and r.get("recommendation") is not None
            )
        }
        result = []
        for q in open_questions:
            rec = rec_by_id.get(q.id)
            result.append(q.model_copy(update={"recommendation": rec}) if rec else q)
        return result
    except Exception as e:
        logger.warning(
            "Recommendation generation failed, leaving recommendations empty: %s",
            str(e),
        )
        return list(open_questions)
