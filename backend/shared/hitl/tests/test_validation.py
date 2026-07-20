"""Tests for shared.hitl.validation.validate_answers — the reconciled strict rule set."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from shared.hitl.models import SubmitAnswersRequest
from shared.hitl.validation import validate_answers


def _job(pending: List[Dict[str, Any]], waiting: bool = True) -> Dict[str, Any]:
    return {"waiting_for_answers": waiting, "pending_questions": pending}


def _req(answers: List[Dict[str, Any]]) -> SubmitAnswersRequest:
    return SubmitAnswersRequest(answers=answers)


_Q1 = {
    "id": "q1",
    "question_text": "Allergen strictness default?",
    "required": True,
    "options": [{"id": "strict", "label": "Strict"}],
}


def test_not_waiting_400():
    with pytest.raises(HTTPException) as ei:
        validate_answers(_job([_Q1], waiting=False), _req([]))
    assert ei.value.status_code == 400
    assert "not waiting" in ei.value.detail


def test_no_pending_400():
    with pytest.raises(HTTPException) as ei:
        validate_answers(_job([]), _req([{"question_id": "q1", "selected_option_id": "strict"}]))
    assert ei.value.status_code == 400
    assert "No pending questions" in ei.value.detail


def test_corrupted_record_missing_id_500():
    bad = [{"question_text": "no id here", "required": True, "options": []}]
    with pytest.raises(HTTPException) as ei:
        validate_answers(_job(bad), _req([{"question_id": "q1", "selected_option_id": "x"}]))
    assert ei.value.status_code == 500
    assert "Corrupted job record" in ei.value.detail


@pytest.mark.parametrize("bad_entry", ["not-a-dict", 123, None, ["nested"]])
def test_corrupted_record_non_dict_500(bad_entry):
    # A non-dict pending entry is a corrupted record: it must hit the controlled 500 path, not raise
    # a bare TypeError from `"id" not in <non-dict>`.
    with pytest.raises(HTTPException) as ei:
        validate_answers(
            _job([bad_entry, _Q1]), _req([{"question_id": "q1", "selected_option_id": "strict"}])
        )
    assert ei.value.status_code == 500
    assert "Corrupted job record" in ei.value.detail


def test_duplicate_question_id_400():
    with pytest.raises(HTTPException) as ei:
        validate_answers(
            _job([_Q1]),
            _req(
                [
                    {"question_id": "q1", "selected_option_id": "strict"},
                    {"question_id": "q1", "selected_option_id": "other", "other_text": "x"},
                ]
            ),
        )
    assert ei.value.status_code == 400
    assert "Duplicate answers" in ei.value.detail
    assert "q1" in ei.value.detail


def test_missing_required_400():
    with pytest.raises(HTTPException) as ei:
        validate_answers(_job([_Q1]), _req([]))
    assert ei.value.status_code == 400
    assert "Missing answers" in ei.value.detail


def test_required_defaults_to_true_when_key_absent():
    # A pending question with no "required" key counts as required.
    q = {"id": "q1", "question_text": "?", "options": []}
    with pytest.raises(HTTPException) as ei:
        validate_answers(_job([q]), _req([]))
    assert ei.value.status_code == 400
    assert "Missing answers" in ei.value.detail


def test_unknown_question_id_400():
    # q1 is not required so missing-required does not preempt the unknown-id check.
    q = {"id": "q1", "question_text": "?", "required": False, "options": []}
    with pytest.raises(HTTPException) as ei:
        validate_answers(_job([q]), _req([{"question_id": "ghost", "selected_option_id": "x"}]))
    assert ei.value.status_code == 400
    assert "Unknown question" in ei.value.detail


def test_other_without_text_400():
    with pytest.raises(HTTPException) as ei:
        validate_answers(
            _job([_Q1]),
            _req([{"question_id": "q1", "selected_option_id": "other", "other_text": None}]),
        )
    assert ei.value.status_code == 400
    assert "no text provided" in ei.value.detail


def test_other_with_whitespace_only_text_400():
    with pytest.raises(HTTPException) as ei:
        validate_answers(
            _job([_Q1]),
            _req([{"question_id": "q1", "selected_option_id": "other", "other_text": "   "}]),
        )
    assert ei.value.status_code == 400
    assert "no text provided" in ei.value.detail


def test_unknown_option_id_400():
    with pytest.raises(HTTPException) as ei:
        validate_answers(_job([_Q1]), _req([{"question_id": "q1", "selected_option_id": "bogus"}]))
    assert ei.value.status_code == 400
    assert "unknown option" in ei.value.detail.lower()


def test_no_option_and_no_text_400():
    with pytest.raises(HTTPException) as ei:
        validate_answers(
            _job([_Q1]),
            _req([{"question_id": "q1", "selected_option_id": "", "other_text": ""}]),
        )
    assert ei.value.status_code == 400
    assert "no option selected" in ei.value.detail.lower()


def test_blank_answer_only_question_id_400():
    with pytest.raises(HTTPException) as ei:
        validate_answers(_job([_Q1]), _req([{"question_id": "q1"}]))
    assert ei.value.status_code == 400
    assert "no option selected" in ei.value.detail.lower()


def test_success_valid_option_carries_question_text():
    out = validate_answers(
        _job([_Q1]), _req([{"question_id": "q1", "selected_option_id": "strict"}])
    )
    assert out == [
        {
            "question_id": "q1",
            "question_text": "Allergen strictness default?",
            "selected_option_id": "strict",
            "other_text": None,
        }
    ]


def test_success_other_with_text_returns_raw_other_text():
    # other_text is stripped only for the emptiness check; the stored value is raw (un-stripped).
    out = validate_answers(
        _job([_Q1]),
        _req([{"question_id": "q1", "selected_option_id": "other", "other_text": "  use mTLS  "}]),
    )
    assert out[0]["other_text"] == "  use mTLS  "
    assert out[0]["selected_option_id"] == "other"


def test_success_free_text_for_optionless_question():
    q = {"id": "q1", "question_text": "Which DB?", "required": True, "options": []}
    out = validate_answers(_job([q]), _req([{"question_id": "q1", "other_text": "Postgres"}]))
    assert out[0]["question_id"] == "q1"
    assert out[0]["question_text"] == "Which DB?"


def test_success_question_text_defaults_to_empty_when_record_lacks_it():
    q = {"id": "q1", "required": False, "options": []}
    out = validate_answers(_job([q]), _req([{"question_id": "q1", "other_text": "x"}]))
    assert out[0]["question_text"] == ""
