"""Tests for the LLM Fibonacci-scoring prompt/schema contract (pure, no network/LLM)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from software_engineering_team.github_source.issue_scoring import (
    FIBONACCI_COMPLEXITY_VALUES,
    ScoreBreakdown,
    build_scoring_prompt,
)

VALID_PAYLOAD = {
    "conceptual_score": 3,
    "conceptual_rationale": "Touches one well-understood module.",
    "loc_score": 5,
    "loc_rationale": "A few hundred lines across two files.",
    "code_complexity_score": 2,
    "code_complexity_rationale": "Straightforward branching, no new algorithms.",
    "aggregate_score": 3,
    "aggregate_rationale": "Overall a small, contained change.",
    "suggested_labels": ["good-first-issue"],
}


# ---------------------------------------------------------------------------
# build_scoring_prompt
# ---------------------------------------------------------------------------


def test_build_scoring_prompt_includes_title_body_labels() -> None:
    prompt = build_scoring_prompt(
        "Add retry logic to the job poller",
        "The poller should retry transient failures with backoff.",
        ["backend", "reliability"],
    )
    assert "Add retry logic to the job poller" in prompt
    assert "The poller should retry transient failures with backoff." in prompt
    assert "backend, reliability" in prompt


def test_build_scoring_prompt_asks_for_all_four_dimensions_and_fibonacci_values() -> None:
    prompt = build_scoring_prompt("Title", "Body", [])
    for field in (
        "conceptual_score",
        "loc_score",
        "code_complexity_score",
        "aggregate_score",
        "suggested_labels",
    ):
        assert field in prompt
    for value in FIBONACCI_COMPLEXITY_VALUES:
        assert str(value) in prompt


def test_build_scoring_prompt_handles_empty_body_and_labels() -> None:
    prompt = build_scoring_prompt("Title only", "", [])
    assert "(none)" in prompt


# ---------------------------------------------------------------------------
# ScoreBreakdown -- success
# ---------------------------------------------------------------------------


def test_score_breakdown_accepts_well_formed_payload() -> None:
    result = ScoreBreakdown.model_validate(VALID_PAYLOAD)
    assert result.conceptual_score == 3
    assert result.loc_score == 5
    assert result.suggested_labels == ["good-first-issue"]


def test_score_breakdown_defaults_suggested_labels_when_absent() -> None:
    payload = dict(VALID_PAYLOAD)
    del payload["suggested_labels"]
    result = ScoreBreakdown.model_validate(payload)
    assert result.suggested_labels == []


def test_score_breakdown_accepts_every_fibonacci_value() -> None:
    for value in FIBONACCI_COMPLEXITY_VALUES:
        payload = dict(VALID_PAYLOAD)
        payload["loc_score"] = value
        assert ScoreBreakdown.model_validate(payload).loc_score == value


# ---------------------------------------------------------------------------
# ScoreBreakdown -- validation failures
# ---------------------------------------------------------------------------


def test_score_breakdown_missing_required_field() -> None:
    payload = dict(VALID_PAYLOAD)
    del payload["aggregate_score"]
    with pytest.raises(ValidationError):
        ScoreBreakdown.model_validate(payload)


def test_score_breakdown_non_fibonacci_value_rejected_not_clamped() -> None:
    payload = dict(VALID_PAYLOAD)
    payload["loc_score"] = 4  # not a Fibonacci value; nearest legal values are 3 and 5
    with pytest.raises(ValidationError):
        ScoreBreakdown.model_validate(payload)


def test_score_breakdown_blank_rationale_rejected() -> None:
    payload = dict(VALID_PAYLOAD)
    payload["code_complexity_rationale"] = "   "
    with pytest.raises(ValidationError):
        ScoreBreakdown.model_validate(payload)


def test_score_breakdown_wrong_type_field() -> None:
    payload = dict(VALID_PAYLOAD)
    payload["conceptual_score"] = "five"
    with pytest.raises(ValidationError):
        ScoreBreakdown.model_validate(payload)
