"""Tests for ``github_source.issue_scorer`` -- the heuristic/LLM mode facade.

Covers: ``heuristic_only`` mode never calls the LLM scorer; ``auto`` mode
prefers the LLM scorer and falls back to heuristics on ``LLMError``; a
non-``LLMError`` exception from the LLM path is never swallowed; and
``resolve_scoring_mode``'s env-var/explicit-param precedence. No live
LLM/network calls anywhere (LLM path is monkeypatched directly).
"""

from __future__ import annotations

import logging

import pytest

from llm_service import LLMJsonParseError, LLMNotConfiguredError, LLMSchemaValidationError
from software_engineering_team.github_source import issue_scorer as scorer_mod
from software_engineering_team.github_source.issue_scoring import (
    FIBONACCI_COMPLEXITY_VALUES,
    ScoreBreakdown,
)

VALID_LLM_PAYLOAD = {
    "conceptual_score": 8,
    "conceptual_rationale": "LLM: novel domain with several unknowns.",
    "loc_score": 5,
    "loc_rationale": "LLM: moderate diff size expected.",
    "code_complexity_score": 3,
    "code_complexity_rationale": "LLM: some branching but no new algorithms.",
    "aggregate_score": 5,
    "aggregate_rationale": "LLM: overall moderate complexity.",
    "suggested_labels": ["needs-design"],
}


@pytest.fixture(autouse=True)
def _clear_scoring_mode_env(monkeypatch):
    monkeypatch.delenv("ISSUE_GROOMING_SCORING_MODE", raising=False)


# ---------------------------------------------------------------------------
# heuristic_only mode
# ---------------------------------------------------------------------------


def test_heuristic_only_never_calls_llm_scorer(monkeypatch) -> None:
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("score_issue_via_llm must not be called in heuristic_only mode")

    monkeypatch.setattr(scorer_mod, "score_issue_via_llm", _fail_if_called)

    result = scorer_mod.score_issue("Title", "Body", ["a"], mode="heuristic_only")

    assert isinstance(result, ScoreBreakdown)
    for score in (
        result.conceptual_score,
        result.loc_score,
        result.code_complexity_score,
        result.aggregate_score,
    ):
        assert score in FIBONACCI_COMPLEXITY_VALUES


# ---------------------------------------------------------------------------
# auto mode -- success path uses the LLM result
# ---------------------------------------------------------------------------


def test_auto_mode_returns_llm_result_on_success(monkeypatch) -> None:
    monkeypatch.setattr(
        scorer_mod,
        "score_issue_via_llm",
        lambda *args, **kwargs: ScoreBreakdown(**VALID_LLM_PAYLOAD),
    )

    result = scorer_mod.score_issue("Title", "Body", [], mode="auto")

    assert result.aggregate_score == VALID_LLM_PAYLOAD["aggregate_score"]
    assert result.suggested_labels == ["needs-design"]


def test_auto_is_the_default_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        scorer_mod,
        "score_issue_via_llm",
        lambda *args, **kwargs: ScoreBreakdown(**VALID_LLM_PAYLOAD),
    )

    result = scorer_mod.score_issue("Title", "Body", [])

    assert result.aggregate_score == VALID_LLM_PAYLOAD["aggregate_score"]


# ---------------------------------------------------------------------------
# auto mode -- fallback to heuristic on LLMError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        LLMNotConfiguredError("no provider configured"),
        LLMJsonParseError("bad json", correction_attempts_used=1),
        LLMSchemaValidationError("bad schema", correction_attempts_used=1),
    ],
)
def test_auto_mode_falls_back_to_heuristic_on_llm_error(monkeypatch, exc) -> None:
    def _raise(*args, **kwargs):
        raise exc

    monkeypatch.setattr(scorer_mod, "score_issue_via_llm", _raise)

    result = scorer_mod.score_issue("Title", "Body", [], mode="auto")

    assert isinstance(result, ScoreBreakdown)
    assert "Heuristic" in result.loc_rationale


def test_auto_mode_fallback_logs_a_warning(monkeypatch, caplog) -> None:
    def _raise(*args, **kwargs):
        raise LLMNotConfiguredError("no provider configured")

    monkeypatch.setattr(scorer_mod, "score_issue_via_llm", _raise)

    with caplog.at_level(logging.WARNING, logger=scorer_mod.__name__):
        scorer_mod.score_issue("Title", "Body", [], mode="auto")

    assert any("falling back to heuristic" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# auto mode -- non-LLMError bugs are never swallowed
# ---------------------------------------------------------------------------


def test_auto_mode_does_not_swallow_non_llm_errors(monkeypatch) -> None:
    def _raise(*args, **kwargs):
        raise ValueError("unrelated bug, not an LLM failure")

    monkeypatch.setattr(scorer_mod, "score_issue_via_llm", _raise)

    with pytest.raises(ValueError, match="unrelated bug"):
        scorer_mod.score_issue("Title", "Body", [], mode="auto")


# ---------------------------------------------------------------------------
# resolve_scoring_mode precedence
# ---------------------------------------------------------------------------


def test_resolve_scoring_mode_defaults_to_auto_when_unset() -> None:
    assert scorer_mod.resolve_scoring_mode() == "auto"


def test_resolve_scoring_mode_explicit_param_wins_over_env(monkeypatch) -> None:
    monkeypatch.setenv("ISSUE_GROOMING_SCORING_MODE", "heuristic_only")

    assert scorer_mod.resolve_scoring_mode("auto") == "auto"


def test_resolve_scoring_mode_reads_env_var_when_no_param(monkeypatch) -> None:
    monkeypatch.setenv("ISSUE_GROOMING_SCORING_MODE", "heuristic_only")

    assert scorer_mod.resolve_scoring_mode() == "heuristic_only"


def test_resolve_scoring_mode_is_case_and_whitespace_insensitive(monkeypatch) -> None:
    monkeypatch.setenv("ISSUE_GROOMING_SCORING_MODE", "  Heuristic_Only  ")

    assert scorer_mod.resolve_scoring_mode() == "heuristic_only"


def test_resolve_scoring_mode_unrecognized_env_value_warns_and_defaults(
    monkeypatch, caplog
) -> None:
    monkeypatch.setenv("ISSUE_GROOMING_SCORING_MODE", "bogus")

    with caplog.at_level(logging.WARNING, logger=scorer_mod.__name__):
        result = scorer_mod.resolve_scoring_mode()

    assert result == "auto"
    assert any("Unrecognized" in record.message for record in caplog.records)


def test_resolve_scoring_mode_unrecognized_param_warns_and_defaults(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=scorer_mod.__name__):
        result = scorer_mod.resolve_scoring_mode("bogus")

    assert result == "auto"
    assert any("Unrecognized" in record.message for record in caplog.records)
