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

logger = logging.getLogger(__name__)

MAX_ISSUES = 10
MAX_GAPS = 10


def filter_duplicate_questions(
    new_questions: List[OpenQuestion],
    qa_history: str,
) -> tuple[List[OpenQuestion], List[OpenQuestion]]:
    """Filter out questions that appear to be duplicates of answered ones.

    Uses normalized word stems (e.g. token/tokens, store/stored). Only filters as
    duplicate when match to qa_history is >= 90%; 50–90% similar questions are kept
    and may be consolidated elsewhere. Treats spec + Q&A as source of truth.

    Returns:
        Tuple of (filtered_questions, duplicate_questions).
        - filtered_questions: Questions that are NOT duplicates (should be asked)
        - duplicate_questions: Questions that ARE duplicates (already answered)

    Preconditions: ``new_questions`` is a list of :class:`OpenQuestion`;
        ``qa_history`` is a string.
    Postconditions: the two returned lists partition ``new_questions`` (order
        preserved within each); never raises.
    """
    qa_history_lower = qa_history.lower()
    filtered = []
    duplicates = []

    def _stem(w: str) -> str:
        """Normalize word for matching (e.g. tokens->token, stored->store)."""
        w = w.strip()
        if len(w) <= 3:
            return w
        if w.endswith("ed") and len(w) > 4:
            return w[:-2]  # stored -> store
        if w.endswith("s") and not w.endswith("ss") and len(w) > 4:
            return w[:-1]  # tokens -> token
        return w

    for q in new_questions:
        q_text_lower = q.question_text.lower()
        # Key words: length > 3, normalized to stems for plural/tense
        words = [w for w in q_text_lower.split() if len(w) > 3]
        key_stems = set(_stem(w) for w in words)
        if not key_stems:
            filtered.append(q)
            continue
        # Count how many stems (or their plural) appear in qa_history
        matches = sum(
            1
            for stem in key_stems
            if stem in qa_history_lower
            or (stem + "s") in qa_history_lower
            or (stem + "ed") in qa_history_lower
        )
        match_ratio = matches / len(key_stems)
        # Only treat as duplicate of an answered question when match >= 90%.
        # Lower similarity (50–90%) may be consolidated but should not be filtered out.
        if match_ratio >= 0.90:
            logger.info(
                "Filtering duplicate question (%.0f%% match): %s",
                match_ratio * 100,
                q.question_text[:60],
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
        deduped and capped at ``MAX_ISSUES``/``MAX_GAPS``; never raises.
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
            open_questions.append(parse_open_question(q, i))

    return SpecReviewResult(
        issues=issues,
        gaps=gaps,
        open_questions=open_questions,
        summary=str(raw.get("summary", "") or "Spec review complete"),
    )


def parse_open_question(q_data: Any, index: int) -> OpenQuestion:
    """Parse a single open question from LLM output.

    Preconditions: ``index`` is a non-negative int; ``q_data`` is the decoded item.
    Postconditions: returns a valid :class:`OpenQuestion`. When ``q_data`` is a dict
        with options but no default, the highest-confidence option is marked default.
    """
    if isinstance(q_data, dict):
        raw_options = q_data.get("options", [])
        options = []
        for i, opt in enumerate(raw_options):
            options.append(parse_question_option(opt, i))

        if options and not any(opt.is_default for opt in options):
            sorted_opts = sorted(options, key=lambda o: o.confidence, reverse=True)
            sorted_opts[0] = QuestionOption(
                id=sorted_opts[0].id,
                label=sorted_opts[0].label,
                is_default=True,
                rationale=sorted_opts[0].rationale,
                confidence=sorted_opts[0].confidence,
            )
            options = sorted_opts

        raw_depends = q_data.get("depends_on")
        if isinstance(raw_depends, (list, tuple)):
            depends_on = str(raw_depends[0]) if raw_depends else None
        elif isinstance(raw_depends, str):
            depends_on = raw_depends
        else:
            depends_on = None

        return OpenQuestion(
            id=str(q_data.get("id", f"q{index}")),
            question_text=str(q_data.get("question_text", "")),
            context=str(q_data.get("context", "")),
            recommendation=str(q_data.get("recommendation", "") or ""),
            options=options,
            allow_multiple=bool(q_data.get("allow_multiple", False)),
            source=str(q_data.get("source", "spec_review")),
            category=str(q_data.get("category", "general")),
            priority=str(q_data.get("priority", "medium")),
            constraint_domain=str(q_data.get("constraint_domain", "")),
            constraint_layer=int(q_data.get("constraint_layer", 0) or 0),
            depends_on=depends_on,
            blocking=bool(q_data.get("blocking", True)),
            owner=str(q_data.get("owner", "user")),
            section_impact=list(q_data.get("section_impact", []) or []),
            due_date=str(q_data.get("due_date", "")),
            status=str(q_data.get("status", "open")),
            asked_via=list(q_data.get("asked_via", []) or []),
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


def parse_question_option(opt_data: Any, index: int) -> QuestionOption:
    """Parse a single question option from LLM output.

    Preconditions: ``index`` is a non-negative int; ``opt_data`` is the decoded item.
    Postconditions: returns a valid :class:`QuestionOption`; a non-dict becomes a
        label-only option defaulting only at ``index == 0``.
    """
    if isinstance(opt_data, dict):
        return QuestionOption(
            id=str(opt_data.get("id", f"opt{index}")),
            label=str(opt_data.get("label", "")),
            is_default=bool(opt_data.get("is_default", False)),
            rationale=str(opt_data.get("rationale", "")),
            confidence=float(opt_data.get("confidence", 0.5)),
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

    Compares answers (selected_answer from answered_questions) to the option labels
    of each open question. If any option of an open question is semantically the same
    as an answer we already have, we do not ask that question again. Preserves order
    of open_questions.

    Preconditions: both arguments are lists of the respective models.
    Postconditions: returns a sublist of ``open_questions`` (order preserved);
        questions with no options/labels are always kept; never raises.
    """
    if not open_questions:
        return list(open_questions)

    def norm(t: str) -> str:
        return " ".join((t or "").lower().split()).strip()

    # Build set of existing answers (normalized) we already have
    existing_answers: List[str] = []
    for aq in answered_questions:
        s = norm(aq.selected_answer)
        if s:
            existing_answers.append(s)
        if getattr(aq, "other_text", None) and aq.other_text.strip():
            o = norm(aq.other_text)
            if o and o not in existing_answers:
                existing_answers.append(o)

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
                        opt_label[:50],
                        existing[:50],
                    )
                    already_covered = True
                    break
            if already_covered:
                break
        if not already_covered:
            kept.append(q)

    return kept


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
                "question_text": q.question_text,
                "context": q.context,
                "category": q.category,
                "priority": q.priority,
                "allow_multiple": q.allow_multiple,
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
    try:
        raw = call_llm_json(model, prompt)
        if not isinstance(raw, dict):
            return list(open_questions)
        consolidated = raw.get("consolidated_questions", [])
        if not isinstance(consolidated, list) or len(consolidated) == 0:
            return list(open_questions)
        result = []
        for i, q_data in enumerate(consolidated):
            try:
                result.append(parse_open_question(q_data, i))
            except Exception as e:
                logger.warning("Failed to parse consolidated question %d: %s", i, e)
        return result if result else list(open_questions)
    except Exception as e:
        logger.warning(
            "Question consolidation failed, using original list: %s",
            str(e),
        )
        return list(open_questions)


def review_question_answer_alignment(
    model: Model, open_questions: List[OpenQuestion]
) -> List[OpenQuestion]:
    """Ensure each question and its options make sense together (e.g. no Yes/No for open-ended questions).

    Preconditions: ``model`` is a Strands ``Model``; ``open_questions`` a list.
    Postconditions: returns the aligned list, or the unmodified list on empty input
        or a full-batch LLM/parse failure; never raises. This is a per-question
        review (ids are preserved), so an item that individually fails to parse
        falls back to its original (unaligned) question by id, and any original
        question whose id never appears in the result (e.g. the failed item's id
        was missing or unrecognized) is appended at the end — no original
        question is ever dropped from the batch.
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
    try:
        raw = call_llm_json(model, prompt)
        if not isinstance(raw, dict):
            return list(open_questions)
        aligned = raw.get("aligned_questions", [])
        if not isinstance(aligned, list) or len(aligned) == 0:
            return list(open_questions)
        result = []
        seen_ids = set()
        for i, q_data in enumerate(aligned):
            try:
                parsed = parse_open_question(q_data, i)
                result.append(parsed)
                seen_ids.add(parsed.id)
            except Exception as e:
                logger.warning("Failed to parse aligned question %d: %s", i, e)
                fallback_id = q_data.get("id") if isinstance(q_data, dict) else None
                original = original_by_id.get(fallback_id) if fallback_id else None
                if original is not None:
                    result.append(original)
                    seen_ids.add(original.id)
        if not result:
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
    spec_excerpt = (spec_content or "")
    prompt = GENERATE_QUESTION_RECOMMENDATIONS_PROMPT.format(
        spec_excerpt=spec_excerpt,
        questions_json=questions_json,
    )
    try:
        raw = call_llm_json(model, prompt)
        if not isinstance(raw, dict):
            return list(open_questions)
        recs = raw.get("recommendations", [])
        if not isinstance(recs, list):
            return list(open_questions)
        rec_by_id = {
            r.get("id"): str(r.get("recommendation", "") or "")
            for r in recs
            if isinstance(r, dict) and "id" in r
        }
        result = []
        for q in open_questions:
            rec = rec_by_id.get(q.id, "")
            result.append(q.model_copy(update={"recommendation": rec}))
        return result
    except Exception as e:
        logger.warning(
            "Recommendation generation failed, leaving recommendations empty: %s",
            str(e),
        )
        return list(open_questions)
