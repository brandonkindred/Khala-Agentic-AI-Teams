"""Tests for the LLM Fibonacci-scoring prompt/parse contract (pure, no network/LLM)."""

from __future__ import annotations

import json

from software_engineering_team.github_source.issue_scoring import (
    FIBONACCI_COMPLEXITY_VALUES,
    ScoreBreakdown,
    ScoreParseFailure,
    ScoreParseFailureReason,
    build_scoring_prompt,
    parse_score_response,
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
# parse_score_response -- success
# ---------------------------------------------------------------------------


def test_parse_score_response_well_formed_json() -> None:
    result = parse_score_response(json.dumps(VALID_PAYLOAD))
    assert isinstance(result, ScoreBreakdown)
    assert result.conceptual_score == 3
    assert result.loc_score == 5
    assert result.suggested_labels == ["good-first-issue"]


def test_parse_score_response_handles_markdown_fenced_json() -> None:
    raw = f"```json\n{json.dumps(VALID_PAYLOAD)}\n```"
    result = parse_score_response(raw)
    assert isinstance(result, ScoreBreakdown)
    assert result.aggregate_score == 3


def test_parse_score_response_defaults_suggested_labels_when_absent() -> None:
    payload = dict(VALID_PAYLOAD)
    del payload["suggested_labels"]
    result = parse_score_response(json.dumps(payload))
    assert isinstance(result, ScoreBreakdown)
    assert result.suggested_labels == []


# ---------------------------------------------------------------------------
# parse_score_response -- failure paths
# ---------------------------------------------------------------------------


def test_parse_score_response_malformed_json() -> None:
    result = parse_score_response("not json at all, no braces")
    assert isinstance(result, ScoreParseFailure)
    assert result.reason == ScoreParseFailureReason.MALFORMED_JSON


def test_parse_score_response_missing_required_field() -> None:
    payload = dict(VALID_PAYLOAD)
    del payload["aggregate_score"]
    result = parse_score_response(json.dumps(payload))
    assert isinstance(result, ScoreParseFailure)
    assert result.reason == ScoreParseFailureReason.VALIDATION_ERROR


def test_parse_score_response_non_fibonacci_value_rejected_not_clamped() -> None:
    payload = dict(VALID_PAYLOAD)
    payload["loc_score"] = 4  # not a Fibonacci value; nearest legal values are 3 and 5
    result = parse_score_response(json.dumps(payload))
    assert isinstance(result, ScoreParseFailure)
    assert result.reason == ScoreParseFailureReason.VALIDATION_ERROR
    assert not isinstance(result, ScoreBreakdown)


def test_parse_score_response_blank_rationale_rejected() -> None:
    payload = dict(VALID_PAYLOAD)
    payload["code_complexity_rationale"] = "   "
    result = parse_score_response(json.dumps(payload))
    assert isinstance(result, ScoreParseFailure)
    assert result.reason == ScoreParseFailureReason.VALIDATION_ERROR


def test_parse_score_response_non_dict_json_shape() -> None:
    result = parse_score_response(json.dumps([1, 2, 3]))
    assert isinstance(result, ScoreParseFailure)
    assert result.reason == ScoreParseFailureReason.INVALID_SHAPE


def test_parse_score_response_wrong_type_field() -> None:
    payload = dict(VALID_PAYLOAD)
    payload["conceptual_score"] = "five"
    result = parse_score_response(json.dumps(payload))
    assert isinstance(result, ScoreParseFailure)
    assert result.reason == ScoreParseFailureReason.VALIDATION_ERROR
