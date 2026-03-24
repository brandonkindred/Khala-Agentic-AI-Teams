"""
Auto-answer logic for the Product Requirements Analysis Agent.

Uses LLM to select the best answer option based on industry best practices,
product context, and risk assessment.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List, Optional

from .models import AutoAnswerResult, OpenQuestion, QuestionOption
from .prompts import AUTO_ANSWER_PROMPT

if TYPE_CHECKING:
    from llm_service import LLMClient

logger = logging.getLogger(__name__)


def _format_options_for_prompt(options: List[QuestionOption]) -> str:
    """Format question options for the LLM prompt."""
    lines = []
    for opt in options:
        lines.append(f"- ID: {opt.id}")
        lines.append(f"  Label: {opt.label}")
        if opt.rationale:
            lines.append(f"  Existing rationale: {opt.rationale}")
        if opt.is_default:
            lines.append("  (Marked as recommended default)")
        lines.append("")
    return "\n".join(lines)


def _parse_auto_answer_response(
    raw: Any,
    question: OpenQuestion,
) -> AutoAnswerResult:
    """Parse LLM response into AutoAnswerResult."""
    if not isinstance(raw, dict):
        default_opt = _get_default_option(question)
        return AutoAnswerResult(
            question_id=question.id,
            selected_option_id=default_opt.id if default_opt else "unknown",
            selected_answer=default_opt.label if default_opt else "Unable to auto-answer",
            rationale="Auto-answer parsing failed, using default option.",
            confidence=0.5,
            risks=["Auto-answer may not be optimal"],
        )

    selected_id = str(raw.get("selected_option_id", ""))
    selected_opt = next((o for o in question.options if o.id == selected_id), None)

    if not selected_opt:
        default_opt = _get_default_option(question)
        selected_opt = default_opt
        selected_id = default_opt.id if default_opt else "unknown"

    return AutoAnswerResult(
        question_id=question.id,
        selected_option_id=selected_id,
        selected_answer=selected_opt.label if selected_opt else "Unknown",
        rationale=str(raw.get("rationale", "")),
        confidence=float(raw.get("confidence", 0.7)),
        risks=list(raw.get("risks", [])) if isinstance(raw.get("risks"), list) else [],
        alternatives_considered=str(raw.get("alternatives_considered", "")),
        industry_references=list(raw.get("industry_references", []))
        if isinstance(raw.get("industry_references"), list)
        else [],
    )


def _get_default_option(question: OpenQuestion) -> Optional[QuestionOption]:
    """Get the default option for a question, or highest confidence, or first."""
    default = next((opt for opt in question.options if opt.is_default), None)
    if default:
        return default

    if question.options:
        sorted_by_confidence = sorted(question.options, key=lambda o: o.confidence, reverse=True)
        return sorted_by_confidence[0]

    return None


def auto_answer_question(
    llm: "LLMClient",
    question: OpenQuestion,
    spec_content: str,
    additional_context: Optional[str] = None,
) -> AutoAnswerResult:
    """
    Auto-answer a single question using LLM analysis.

    Args:
        llm: LLM client for completions
        question: The question to auto-answer
        spec_content: Product specification content for context
        additional_context: Optional additional context from the user

    Returns:
        AutoAnswerResult with selected option and rationale
    """
    options_text = _format_options_for_prompt(question.options)

    context = question.context
    if additional_context:
        context = f"{context}\n\nAdditional context: {additional_context}"

    prompt = AUTO_ANSWER_PROMPT.format(
        question_text=question.question_text,
        context=context,
        options=options_text,
        spec_content=spec_content[:8000],
    )

    try:
        raw = llm.complete_json(prompt)
        result = _parse_auto_answer_response(raw, question)
        logger.info(
            "Auto-answered question %s: selected %s with confidence %.2f",
            question.id,
            result.selected_option_id,
            result.confidence,
        )
        return result
    except Exception as e:
        logger.warning("Auto-answer failed for question %s: %s", question.id, e)
        default_opt = _get_default_option(question)
        return AutoAnswerResult(
            question_id=question.id,
            selected_option_id=default_opt.id if default_opt else "unknown",
            selected_answer=default_opt.label if default_opt else "Unable to auto-answer",
            rationale=f"Auto-answer failed: {e}. Using default option.",
            confidence=0.3,
            risks=["Auto-answer failed, manual review recommended"],
        )


def auto_answer_all_questions(
    llm: "LLMClient",
    questions: List[OpenQuestion],
    spec_content: str,
    additional_context: Optional[str] = None,
) -> List[AutoAnswerResult]:
    """
    Auto-answer all provided questions.

    Args:
        llm: LLM client for completions
        questions: List of questions to auto-answer
        spec_content: Product specification content for context
        additional_context: Optional additional context from the user

    Returns:
        List of AutoAnswerResult for each question
    """
    results = []
    for question in questions:
        result = auto_answer_question(
            llm=llm,
            question=question,
            spec_content=spec_content,
            additional_context=additional_context,
        )
        results.append(result)
    return results


def get_auto_answer_for_job(
    llm: "LLMClient",
    job_id: str,
    question_id: str,
    spec_content: str,
    additional_context: Optional[str] = None,
) -> Optional[AutoAnswerResult]:
    """
    Auto-answer a specific question from a job's pending questions.

    Args:
        llm: LLM client for completions
        job_id: Job ID to look up pending questions
        question_id: ID of the question to auto-answer
        spec_content: Product specification content for context
        additional_context: Optional additional context

    Returns:
        AutoAnswerResult or None if question not found
    """
    from software_engineering_team.shared.job_store import get_job

    job_data = get_job(job_id)
    if not job_data:
        logger.warning("Job %s not found", job_id)
        return None

    pending_questions = job_data.get("pending_questions", [])
    question_data = next((q for q in pending_questions if q.get("id") == question_id), None)

    if not question_data:
        logger.warning("Question %s not found in job %s", question_id, job_id)
        return None

    options = [
        QuestionOption(
            id=opt.get("id", f"opt{i}"),
            label=opt.get("label", ""),
            is_default=opt.get("is_default", False),
            rationale=opt.get("rationale", ""),
            confidence=float(opt.get("confidence", 0.0)),
        )
        for i, opt in enumerate(question_data.get("options", []))
    ]

    question = OpenQuestion(
        id=question_data.get("id", question_id),
        question_text=question_data.get("question_text", ""),
        context=question_data.get("context", ""),
        options=options,
        source=question_data.get("source", "unknown"),
        category=question_data.get("category", "general"),
        priority=question_data.get("priority", "medium"),
    )

    return auto_answer_question(
        llm=llm,
        question=question,
        spec_content=spec_content,
        additional_context=additional_context,
    )
