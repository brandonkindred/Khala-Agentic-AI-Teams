"""Shared human-in-the-loop (HITL) schema, validation, and coercion helpers.

The "pending question / answer" contract was independently re-implemented across
teams and had drifted into genuinely different behavior (SE silently accepted
duplicate/contradictory answers and rendered out-of-range progress bars; its
status route dropped ``recommendation``/``allow_multiple``). This package is the
single owner of the reconciled, strictest/superset behavior so every team shares
it via extract-then-shim (the team's old models re-export these; the team's
``_validate_answers``/``_coerce_progress`` become thin wrappers).

Layout:
    - ``models``     — the four schemas (superset fields folded in as Optional).
    - ``validation`` — ``validate_answers(data, request)`` (union of both rule sets).
    - ``progress``   — ``coerce_progress(value)`` (clamped to ``[0, 100]``).
    - ``status``     — ``pending_questions_from_raw(raw)`` (full-fidelity materialization).

Preconditions:
    - ``backend/agents`` is on ``sys.path`` (the ``shared_*`` convention).
Postconditions:
    - Importing has no side effects beyond class/function definition.
"""

from __future__ import annotations

from shared_hitl.models import (
    AnswerSubmission,
    PendingQuestion,
    QuestionOption,
    SubmitAnswersRequest,
)
from shared_hitl.progress import coerce_progress
from shared_hitl.status import pending_questions_from_raw
from shared_hitl.validation import validate_answers

__all__ = [
    "QuestionOption",
    "PendingQuestion",
    "AnswerSubmission",
    "SubmitAnswersRequest",
    "validate_answers",
    "coerce_progress",
    "pending_questions_from_raw",
]
