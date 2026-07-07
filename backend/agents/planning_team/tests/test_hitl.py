"""Tests for Planning fail-closed answer resolution and the handoff question fields."""

from __future__ import annotations

import pytest

from planning_team.models import AnsweredQuestion, HandoffPackage, OpenQuestion
from planning_team.orchestrator import _resolve_pra_answers

_Q = [
    {
        "id": "q1",
        "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B", "is_default": True}],
    }
]


def test_resolve_uses_answer_callback_when_present():
    out = _resolve_pra_answers(
        _Q,
        answer_callback=lambda qs: [{"question_id": "q1", "selected_option_id": "a"}],
        auto_answer_questions=True,
    )
    assert out == [{"question_id": "q1", "selected_option_id": "a"}]


def test_resolve_auto_answers_default_option_when_enabled():
    out = _resolve_pra_answers(_Q, answer_callback=None, auto_answer_questions=True)
    assert out == [{"question_id": "q1", "selected_option_id": "b"}]  # the is_default option


def test_resolve_auto_answers_first_option_when_no_default():
    qs = [{"id": "q1", "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}]
    out = _resolve_pra_answers(qs, answer_callback=None, auto_answer_questions=True)
    assert out == [{"question_id": "q1", "selected_option_id": "a"}]


def test_resolve_fails_closed_when_gated_without_callback():
    with pytest.raises(RuntimeError, match="decisions must be made by the user"):
        _resolve_pra_answers(_Q, answer_callback=None, auto_answer_questions=False)


def test_resolve_no_questions_is_noop():
    assert _resolve_pra_answers([], answer_callback=None, auto_answer_questions=False) == []
    assert _resolve_pra_answers([], answer_callback=None, auto_answer_questions=True) == []


def test_handoff_package_carries_questions():
    hp = HandoffPackage(
        open_questions=[OpenQuestion(id="q1", question_text="Allergen default?").model_dump()],
        resolved_questions=[
            AnsweredQuestion(question_id="q2", selected_answer="strict").model_dump()
        ],
    )
    dumped = hp.model_dump()
    assert dumped["open_questions"][0]["question_text"] == "Allergen default?"
    assert dumped["resolved_questions"][0]["selected_answer"] == "strict"
    # Round-trips through validation.
    assert HandoffPackage.model_validate(dumped).open_questions[0]["id"] == "q1"


def test_handoff_package_defaults_empty():
    hp = HandoffPackage()
    assert hp.open_questions == []
    assert hp.resolved_questions == []
