"""Tests for shared.hitl.models — the reconciled superset schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.hitl.models import (
    AnswerSubmission,
    PendingQuestion,
    QuestionOption,
    SubmitAnswersRequest,
)


def test_question_option_defaults():
    opt = QuestionOption(id="a", label="A")
    assert opt.is_default is False
    assert opt.rationale is None
    assert opt.confidence is None


def test_question_option_superset_fields():
    opt = QuestionOption(id="a", label="A", is_default=True, rationale="best", confidence=0.9)
    assert opt.rationale == "best"
    assert opt.confidence == 0.9


def test_pending_question_defaults():
    q = PendingQuestion(id="q1", question_text="Pick one?")
    assert q.context is None
    assert q.recommendation is None
    assert q.options == []
    assert q.required is True
    assert q.allow_multiple is False
    # Fallback default (records normally stamp their own source).
    assert q.source == "planning"


def test_pending_question_superset_fields_and_nested_options():
    q = PendingQuestion(
        id="q1",
        question_text="Pick one?",
        recommendation="use strict",
        allow_multiple=True,
        source="engineer:backend",
        options=[{"id": "strict", "label": "Strict", "is_default": True}],
    )
    assert q.recommendation == "use strict"
    assert q.allow_multiple is True
    assert q.source == "engineer:backend"
    assert isinstance(q.options[0], QuestionOption)
    assert q.options[0].is_default is True


def test_pending_question_requires_id_and_text():
    with pytest.raises(ValidationError):
        PendingQuestion(question_text="no id")
    with pytest.raises(ValidationError):
        PendingQuestion(id="q1")


def test_pending_question_json_roundtrip_preserves_superset():
    q = PendingQuestion(
        id="q1",
        question_text="Pick one?",
        recommendation="use strict",
        allow_multiple=True,
        options=[QuestionOption(id="s", label="S", rationale="why", confidence=0.5)],
    )
    dumped = q.model_dump()
    assert dumped["recommendation"] == "use strict"
    assert dumped["allow_multiple"] is True
    assert dumped["options"][0]["rationale"] == "why"
    assert dumped["options"][0]["confidence"] == 0.5
    # Round-trips back to an equal model.
    assert PendingQuestion.model_validate(dumped) == q


def test_answer_submission_defaults():
    ans = AnswerSubmission(question_id="q1")
    assert ans.selected_option_id is None
    assert ans.other_text is None


def test_submit_answers_request_parses_nested_answers():
    req = SubmitAnswersRequest(answers=[{"question_id": "q1", "selected_option_id": "strict"}])
    assert isinstance(req.answers[0], AnswerSubmission)
    assert req.answers[0].question_id == "q1"


def test_submit_answers_request_requires_answers():
    with pytest.raises(ValidationError):
        SubmitAnswersRequest()
