"""Tests for ``github_source.issue_heuristic_scorer.score_issue_heuristically``.

Pure logic, no LLM/network -- covers Fibonacci-membership, determinism, and
that keyword/length signals move scores in the expected direction.
"""

from __future__ import annotations

from software_engineering_team.github_source.issue_heuristic_scorer import (
    score_issue_heuristically,
)
from software_engineering_team.github_source.issue_scoring import (
    FIBONACCI_COMPLEXITY_VALUES,
    ScoreBreakdown,
)

_SHORT_BODY = "Fix a typo."
_LONG_BODY = (
    "This requires a distributed architecture redesign with a cross-team migration, "
    "a new state machine, a custom parser, and careful handling of race condition and "
    "concurrency edge cases across the retry protocol. " * 20
)


def test_returns_score_breakdown_with_fibonacci_values() -> None:
    result = score_issue_heuristically("Add retry logic", "Some body text.", ["backend"])

    assert isinstance(result, ScoreBreakdown)
    for score in (
        result.conceptual_score,
        result.loc_score,
        result.code_complexity_score,
        result.aggregate_score,
    ):
        assert score in FIBONACCI_COMPLEXITY_VALUES


def test_deterministic_for_same_input() -> None:
    first = score_issue_heuristically("Title", "Body text here.", ["a", "b"])
    second = score_issue_heuristically("Title", "Body text here.", ["a", "b"])

    assert first.model_dump() == second.model_dump()


def test_empty_body_and_labels_do_not_raise() -> None:
    result = score_issue_heuristically("Title only", "", [])

    assert isinstance(result, ScoreBreakdown)
    assert result.loc_score == min(FIBONACCI_COMPLEXITY_VALUES)


def test_longer_keyword_dense_body_scores_at_least_as_high_as_short_body() -> None:
    short = score_issue_heuristically("Trivial fix", _SHORT_BODY, [])
    long = score_issue_heuristically("Large redesign", _LONG_BODY, [])

    assert long.loc_score >= short.loc_score
    assert long.conceptual_score >= short.conceptual_score
    assert long.code_complexity_score >= short.code_complexity_score
    assert long.aggregate_score >= short.aggregate_score
    # At least one dimension should meaningfully separate the two inputs.
    assert (
        long.loc_score > short.loc_score
        or long.conceptual_score > short.conceptual_score
        or long.code_complexity_score > short.code_complexity_score
    )


def test_rationales_are_nonblank_and_reference_signals() -> None:
    result = score_issue_heuristically("Title", "A body with some content.", ["x", "y"])

    assert "chars" in result.loc_rationale
    assert "keyword" in result.conceptual_rationale
    assert "keyword" in result.code_complexity_rationale
    assert result.aggregate_rationale.strip()


def test_suggested_labels_defaults_to_empty() -> None:
    result = score_issue_heuristically("Title", "Body", [])

    assert result.suggested_labels == []
