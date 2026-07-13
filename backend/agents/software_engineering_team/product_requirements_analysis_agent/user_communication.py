"""
User-communication seam for the Product Requirements Analysis Agent.

The one place that pushes open questions to the job store (and Slack), blocks until
the user submits answers, then merges those answers with per-question defaults into
typed :class:`AnsweredQuestion` models and records them in the Q&A history. It is the
agent's only human-in-the-loop step and makes no LLM calls — it is job-store and
notification I/O plus deterministic answer merging.

Shared across both SOP Phase 1 (``sop_engine.run_sop_phase1``, per-sub-phase question
rounds) and the spec-review workflow's own Communicate-with-User phase — not tied to
a single numbered phase.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import AnsweredQuestion, OpenQuestion, QuestionOption
from .qa_history import record_answers

logger = logging.getLogger(__name__)

OPEN_QUESTIONS_POLL_INTERVAL = 5.0


def communicate_with_user(
    job_id: Optional[str],
    open_questions: List[OpenQuestion],
    repo_path: Path,
    iteration: int,
) -> List[AnsweredQuestion]:
    """Send questions to user and wait for response.

    Preconditions: ``job_id`` must be truthy (a job is required to collect input);
        ``open_questions`` is a list.
    Postconditions: returns the answered questions (submitted answers merged with
        defaults) and records them to qa_history. Raises ``RuntimeError`` when
        ``job_id`` is missing or the job is cancelled/fails while waiting.
    """
    if not job_id:
        raise RuntimeError(
            "No job_id provided - cannot communicate with user for answers. "
            "A job_id is required to collect user input."
        )

    from software_engineering_team.shared.job_store import (
        add_pending_questions,
        get_submitted_answers,
        update_job,
    )

    pending = convert_to_pending_questions(open_questions)
    add_pending_questions(job_id, pending)
    try:
        from unified_api.slack_notifier import notify_open_questions

        notify_open_questions(job_id, pending, source="product-analysis")
    except ImportError:
        pass

    update_job(
        job_id,
        waiting_for_answers=True,
        message=f"Waiting for answers to {len(open_questions)} question(s)",
    )

    logger.info(
        "Communicate with user: Sent %d questions, waiting for response",
        len(open_questions),
    )

    if not wait_for_answers(job_id):
        raise RuntimeError("Job was cancelled or failed while waiting for user answers")

    submitted = get_submitted_answers(job_id)
    answered = apply_answers(open_questions, submitted)

    update_job(job_id, waiting_for_answers=False)
    record_answers(repo_path, answered, iteration)

    return answered


def wait_for_answers(job_id: str) -> bool:
    """Wait indefinitely for user to submit answers.

    Preconditions: ``job_id`` identifies a live job.
    Postconditions: returns ``True`` once the job is no longer waiting for answers,
        ``False`` if it reaches a failed/completed/cancelled status first. Polls every
        ``OPEN_QUESTIONS_POLL_INTERVAL`` seconds.
    """
    from software_engineering_team.shared.job_store import get_job, is_waiting_for_answers

    while True:
        if not is_waiting_for_answers(job_id):
            return True

        job_data = get_job(job_id)
        if job_data and job_data.get("status") in ("failed", "completed", "cancelled"):
            return False

        time.sleep(OPEN_QUESTIONS_POLL_INTERVAL)


def convert_to_pending_questions(
    open_questions: List[OpenQuestion],
) -> List[Dict[str, Any]]:
    """Convert OpenQuestion models to pending question dicts for job store.

    Preconditions: ``open_questions`` is a list of :class:`OpenQuestion`.
    Postconditions: returns one dict per question; questions with no options get a
        single free-text "other" option.
    """
    pending = []
    for q in open_questions:
        options = [
            {
                "id": opt.id,
                "label": opt.label,
                "is_default": opt.is_default,
                "rationale": opt.rationale,
                "confidence": opt.confidence,
            }
            for opt in q.options
        ]
        if not options:
            options = [{"id": "other", "label": "Provide answer in text field"}]

        rec = getattr(q, "recommendation", None) or ""
        pending.append(
            {
                "id": q.id,
                "question_text": q.question_text,
                "context": q.context,
                "recommendation": rec if rec else None,
                "options": options,
                "allow_multiple": q.allow_multiple,
                "required": True,
                "source": q.source,
                "category": q.category,
                "priority": q.priority,
                "constraint_domain": q.constraint_domain,
                "constraint_layer": q.constraint_layer,
                "depends_on": q.depends_on,
                "blocking": q.blocking,
                "owner": q.owner,
                "section_impact": q.section_impact,
                "due_date": q.due_date,
                "status": q.status,
                "asked_via": q.asked_via,
            }
        )
    return pending


def apply_all_defaults(
    open_questions: List[OpenQuestion],
) -> List[AnsweredQuestion]:
    """Apply default answers to all questions.

    Preconditions: ``open_questions`` is a list of :class:`OpenQuestion`.
    Postconditions: returns one default-flagged :class:`AnsweredQuestion` per input.
    """
    answered = []
    for q in open_questions:
        default_opt = get_default_option(q)
        answered.append(
            AnsweredQuestion(
                question_id=q.id,
                question_text=q.question_text,
                selected_option_id=default_opt.id if default_opt else "unknown",
                selected_answer=default_opt.label if default_opt else "No default available",
                was_default=True,
                rationale=default_opt.rationale if default_opt else "",
                confidence=default_opt.confidence if default_opt else 0.0,
            )
        )
    return answered


def apply_answers(
    open_questions: List[OpenQuestion],
    submitted: List[Dict[str, Any]],
) -> List[AnsweredQuestion]:
    """Merge submitted answers with defaults for unanswered questions.

    Preconditions: ``open_questions`` is a list; ``submitted`` is a list of answer
        dicts keyed by ``question_id``.
    Postconditions: returns one :class:`AnsweredQuestion` per open question — using
        the submitted answer where present (single- or multi-select, honoring
        free-text "other"), otherwise the question's default option.
    """
    submitted_by_id = {s.get("question_id"): s for s in submitted}
    answered = []

    for q in open_questions:
        sub = submitted_by_id.get(q.id)
        if sub:
            other_text = sub.get("other_text") or ""
            was_auto = sub.get("was_auto_answered", False)

            # Handle multi-select questions
            selected_ids = sub.get("selected_option_ids", [])
            selected_id = sub.get("selected_option_id", "")

            if selected_ids:
                # Multi-select: build combined answer from all selected options
                selected_labels = []
                for opt_id in selected_ids:
                    if opt_id == "other" and other_text:
                        selected_labels.append(other_text)
                    else:
                        opt = next((o for o in q.options if o.id == opt_id), None)
                        if opt:
                            selected_labels.append(opt.label)
                selected_answer = "; ".join(selected_labels) if selected_labels else "Unknown"
                # Use first selected ID for backward compatibility
                primary_selected_id = selected_ids[0] if selected_ids else ""
            else:
                # Single-select: use the single selected option
                selected_ids = [selected_id] if selected_id else []
                primary_selected_id = selected_id
                if selected_id == "other" and other_text:
                    selected_answer = other_text
                else:
                    opt = next((o for o in q.options if o.id == selected_id), None)
                    selected_answer = opt.label if opt else other_text or "Unknown"

            answered.append(
                AnsweredQuestion(
                    question_id=q.id,
                    question_text=q.question_text,
                    selected_option_id=primary_selected_id,
                    selected_option_ids=selected_ids,
                    selected_answer=selected_answer,
                    was_auto_answered=was_auto,
                    was_default=False,
                    rationale=sub.get("rationale") or "",
                    confidence=float(sub.get("confidence") or 0.0),
                    other_text=other_text,
                )
            )
        else:
            default_opt = get_default_option(q)
            answered.append(
                AnsweredQuestion(
                    question_id=q.id,
                    question_text=q.question_text,
                    selected_option_id=default_opt.id if default_opt else "unknown",
                    selected_option_ids=[default_opt.id] if default_opt else [],
                    selected_answer=default_opt.label if default_opt else "No default available",
                    was_default=True,
                    rationale=default_opt.rationale if default_opt else "",
                    confidence=default_opt.confidence if default_opt else 0.0,
                )
            )

    return answered


def get_default_option(q: OpenQuestion) -> Optional[QuestionOption]:
    """Get the default option for a question.

    Preconditions: ``q`` is an :class:`OpenQuestion`.
    Postconditions: returns the option flagged default; else the highest-confidence
        option; ``None`` when the question has no options.
    """
    default = next((opt for opt in q.options if opt.is_default), None)
    if default:
        return default

    if q.options:
        sorted_by_confidence = sorted(q.options, key=lambda o: o.confidence, reverse=True)
        return sorted_by_confidence[0]

    return None
