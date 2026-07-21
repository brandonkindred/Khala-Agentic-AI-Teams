"""Tests for shared.hitl.status.pending_questions_from_raw — full-fidelity materialization."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.hitl.models import PendingQuestion
from shared.hitl.status import pending_questions_from_raw


def test_empty_returns_empty():
    assert pending_questions_from_raw([]) == []


def test_preserves_superset_fields():
    raw = [
        {
            "id": "q1",
            "question_text": "Pick one?",
            "recommendation": "use strict",
            "allow_multiple": True,
            "required": True,
            "source": "tech_lead",
            "options": [
                {
                    "id": "strict",
                    "label": "Strict",
                    "is_default": True,
                    "rationale": "safer",
                    "confidence": 0.8,
                }
            ],
        }
    ]
    out = pending_questions_from_raw(raw)
    assert len(out) == 1
    q = out[0]
    assert isinstance(q, PendingQuestion)
    # The field-drop bug fixed by construction: recommendation/allow_multiple survive.
    assert q.recommendation == "use strict"
    assert q.allow_multiple is True
    assert q.source == "tech_lead"
    # Nested option superset fields survive too.
    assert q.options[0].rationale == "safer"
    assert q.options[0].confidence == 0.8


def test_skips_non_dict_entries():
    raw = [
        "not-a-dict",
        123,
        None,
        {"id": "q1", "question_text": "ok"},
    ]
    out = pending_questions_from_raw(raw)
    assert len(out) == 1
    assert out[0].id == "q1"


def test_missing_required_field_raises_validation_error():
    with pytest.raises(ValidationError):
        pending_questions_from_raw([{"question_text": "no id"}])
