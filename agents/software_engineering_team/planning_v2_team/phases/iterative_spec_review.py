"""
Iterative Spec Review: 3-phase cycle for Product Requirement Analysis.

Phase 1: Spec Review - Identify gaps, unclear requirements, generate questions with options
Phase 2: Communicate with User - Send questions, wait for answers, apply defaults
Phase 3: Spec Update - Update spec with answers, save to /plan folder

Repeats until no open questions remain.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..models import (
    AnsweredQuestion,
    OpenQuestion,
    QuestionOption,
    SpecReviewResult,
)
from ..prompts import SPEC_UPDATE_PROMPT
from .spec_review_gap import run_spec_review_gap

if TYPE_CHECKING:
    from shared.llm import LLMClient

logger = logging.getLogger(__name__)

OPEN_QUESTIONS_POLL_INTERVAL = 5.0


def run_iterative_spec_review(
    llm: "LLMClient",
    spec_content: str,
    repo_path: Path,
    job_id: Optional[str] = None,
    tool_agents: Optional[Dict[str, Any]] = None,
    max_iterations: int = 5,
    update_job_callback: Optional[callable] = None,
) -> Tuple[SpecReviewResult, str]:
    """
    Run iterative spec review until no open questions remain.

    The cycle repeats:
    1. Spec Review - analyze spec, generate structured questions with options
    2. Communicate with User - send questions, wait for answers, apply defaults
    3. Spec Update - incorporate answers into spec

    Args:
        llm: LLM client for completions
        spec_content: Initial specification content
        repo_path: Path to the repository
        job_id: Job ID for tracking (required for user communication)
        tool_agents: Optional tool agents for spec review
        max_iterations: Maximum number of review cycles
        update_job_callback: Optional callback to update job progress

    Returns:
        Tuple of (final SpecReviewResult, updated spec_content)
    """
    current_spec = spec_content
    iteration = 0
    final_result: Optional[SpecReviewResult] = None

    while iteration < max_iterations:
        iteration += 1
        logger.info(
            "Iterative spec review: Starting iteration %d/%d",
            iteration,
            max_iterations,
        )

        if update_job_callback:
            update_job_callback(
                message=f"Spec review iteration {iteration}",
                progress=10 + (iteration - 1) * 5,
            )

        # Phase 1: Spec Review
        review_result = run_spec_review_gap(
            llm=llm,
            spec_content=current_spec,
            repo_path=repo_path,
            tool_agents=tool_agents,
        )
        final_result = review_result

        logger.info(
            "Iteration %d: Found %d issues, %d gaps, %d open questions",
            iteration,
            len(review_result.issues),
            len(review_result.product_gaps),
            len(review_result.open_questions),
        )

        if not review_result.open_questions:
            logger.info(
                "Iterative spec review: No open questions, exiting after %d iteration(s)",
                iteration,
            )
            return review_result, current_spec

        # Phase 2: Communicate with User
        if not job_id:
            raise RuntimeError(
                "No job_id provided - cannot communicate with user for answers. "
                "A job_id is required to collect user input."
            )
        else:
            answered_questions = _communicate_with_user(
                job_id=job_id,
                open_questions=review_result.open_questions,
                repo_path=repo_path,
                iteration=iteration,
            )

        if not answered_questions:
            logger.warning("No answers received, exiting iteration loop")
            return review_result, current_spec

        # Phase 3: Spec Update
        current_spec = _update_spec_with_answers(
            llm=llm,
            current_spec=current_spec,
            answered_questions=answered_questions,
            repo_path=repo_path,
            iteration=iteration,
        )

        logger.info(
            "Iteration %d complete: Spec updated with %d answers",
            iteration,
            len(answered_questions),
        )

    logger.warning(
        "Iterative spec review: Max iterations (%d) reached, proceeding with current spec",
        max_iterations,
    )
    return final_result or SpecReviewResult(), current_spec


def _communicate_with_user(
    job_id: str,
    open_questions: List[OpenQuestion],
    repo_path: Path,
    iteration: int,
) -> List[AnsweredQuestion]:
    """
    Send questions to user and wait for response.
    Apply defaults for unanswered questions.
    """
    from shared.job_store import (
        add_pending_questions,
        get_submitted_answers,
        is_waiting_for_answers,
        update_job,
    )

    pending = _convert_to_pending_questions(open_questions)
    add_pending_questions(job_id, pending)

    update_job(
        job_id,
        waiting_for_answers=True,
        message=f"Waiting for answers to {len(open_questions)} question(s)",
    )

    logger.info(
        "Communicate with user: Sent %d questions, waiting for response",
        len(open_questions),
    )

    if not _wait_for_answers(job_id):
        raise RuntimeError("Job was cancelled or failed while waiting for user answers")

    submitted = get_submitted_answers(job_id)
    answered = _apply_defaults_for_unanswered(open_questions, submitted)

    update_job(job_id, waiting_for_answers=False)

    _record_answers_to_plan(repo_path, answered, iteration)

    return answered


def _wait_for_answers(job_id: str) -> bool:
    """
    Wait indefinitely for user to submit answers.

    Returns:
        True if answers were submitted, False if job was cancelled/failed.
    """
    from shared.job_store import get_job, is_waiting_for_answers

    while True:
        if not is_waiting_for_answers(job_id):
            return True

        job_data = get_job(job_id)
        if job_data and job_data.get("status") in ("failed", "completed", "cancelled"):
            return False

        time.sleep(OPEN_QUESTIONS_POLL_INTERVAL)


def _convert_to_pending_questions(
    open_questions: List[OpenQuestion],
) -> List[Dict[str, Any]]:
    """Convert OpenQuestion models to PendingQuestion dicts for job store."""
    pending = []
    for q in open_questions:
        options = [
            {"id": opt.id, "label": opt.label, "is_default": opt.is_default}
            for opt in q.options
        ]
        if not options:
            options = [{"id": "other", "label": "Provide answer in text field"}]

        pending.append(
            {
                "id": q.id,
                "question_text": q.question_text,
                "context": q.context,
                "options": options,
                "required": True,
                "source": q.source,
            }
        )
    return pending


def _apply_all_defaults(open_questions: List[OpenQuestion]) -> List[AnsweredQuestion]:
    """Apply default answers to all questions."""
    answered = []
    for q in open_questions:
        default_opt = _get_default_option(q)
        answered.append(
            AnsweredQuestion(
                question_id=q.id,
                question_text=q.question_text,
                selected_option_id=default_opt.id if default_opt else "unknown",
                selected_answer=default_opt.label if default_opt else "No default available",
                was_default=True,
            )
        )
    return answered


def _apply_defaults_for_unanswered(
    open_questions: List[OpenQuestion],
    submitted: List[Dict[str, Any]],
) -> List[AnsweredQuestion]:
    """
    Merge submitted answers with defaults for unanswered questions.
    """
    submitted_by_id = {s.get("question_id"): s for s in submitted}
    answered = []

    for q in open_questions:
        sub = submitted_by_id.get(q.id)
        if sub:
            selected_id = sub.get("selected_option_id", "")
            other_text = sub.get("other_text") or ""

            if selected_id == "other" and other_text:
                selected_answer = other_text
            else:
                opt = next((o for o in q.options if o.id == selected_id), None)
                selected_answer = opt.label if opt else other_text or "Unknown"

            answered.append(
                AnsweredQuestion(
                    question_id=q.id,
                    question_text=q.question_text,
                    selected_option_id=selected_id,
                    selected_answer=selected_answer,
                    was_default=False,
                    other_text=other_text,
                )
            )
        else:
            default_opt = _get_default_option(q)
            answered.append(
                AnsweredQuestion(
                    question_id=q.id,
                    question_text=q.question_text,
                    selected_option_id=default_opt.id if default_opt else "unknown",
                    selected_answer=default_opt.label
                    if default_opt
                    else "No default available",
                    was_default=True,
                )
            )

    return answered


def _get_default_option(q: OpenQuestion) -> Optional[QuestionOption]:
    """Get the default option for a question, or the first option if none marked."""
    default = next((opt for opt in q.options if opt.is_default), None)
    if not default and q.options:
        default = q.options[0]
    return default


def _update_spec_with_answers(
    llm: "LLMClient",
    current_spec: str,
    answered_questions: List[AnsweredQuestion],
    repo_path: Path,
    iteration: int,
) -> str:
    """
    Update the spec to include more detail based on answers.
    """
    answered_text = _format_answered_questions(answered_questions)

    prompt = SPEC_UPDATE_PROMPT.format(
        spec_content=current_spec,
        answered_questions=answered_text,
    )

    try:
        updated_spec = llm.complete_text(prompt)
    except Exception as e:
        logger.error("Failed to update spec with LLM: %s", e)
        return current_spec

    plan_dir = repo_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    spec_file = plan_dir / f"updated_spec_v{iteration}.md"
    spec_file.write_text(updated_spec, encoding="utf-8")
    logger.info("Saved updated spec to %s", spec_file)

    latest_file = plan_dir / "updated_spec.md"
    latest_file.write_text(updated_spec, encoding="utf-8")

    return updated_spec


def _format_answered_questions(answered_questions: List[AnsweredQuestion]) -> str:
    """Format answered questions for the LLM prompt."""
    lines = []
    for aq in answered_questions:
        lines.append(f"Q: {aq.question_text}")
        lines.append(f"A: {aq.selected_answer}")
        if aq.was_default:
            lines.append("(Default applied)")
        lines.append("")
    return "\n".join(lines)


def _record_answers_to_plan(
    repo_path: Path,
    answered_questions: List[AnsweredQuestion],
    iteration: int,
) -> None:
    """Save answered questions to /plan/qa_history.md"""
    plan_dir = repo_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)

    qa_file = plan_dir / "qa_history.md"

    content = f"\n## Iteration {iteration}\n\n"
    for aq in answered_questions:
        content += f"### {aq.question_text}\n"
        content += f"**Answer:** {aq.selected_answer}\n"
        if aq.was_default:
            content += "*(Default applied)*\n"
        if aq.other_text:
            content += f"*Custom text:* {aq.other_text}\n"
        content += "\n"

    mode = "a" if qa_file.exists() else "w"
    if mode == "w":
        content = "# Q&A History\n\nThis file records all questions and answers from iterative spec review.\n" + content

    with open(qa_file, mode, encoding="utf-8") as f:
        f.write(content)

    logger.info("Recorded %d answers to %s", len(answered_questions), qa_file)
