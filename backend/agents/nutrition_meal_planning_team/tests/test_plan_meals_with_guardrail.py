"""SPEC-007 W7 — guardrail pipeline integration (flag ON).

Tests that when NUTRITION_GUARDRAIL=1:
- Safe meals pass through and are recorded.
- Unsafe meals are rejected and appear in ``dropped``.
- Flags are populated in ``flags_by_recommendation``.
- ``guardrail_version`` is set on the response.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

pytest.importorskip(
    "strands",
    reason="orchestrator requires strands-agents",
)

from nutrition_meal_planning_team.guardrail.version import GUARDRAIL_VERSION  # noqa: E402
from nutrition_meal_planning_team.guardrail.violations import (  # noqa: E402
    GuardrailResult,
    Severity,
    Violation,
    ViolationReason,
)
from nutrition_meal_planning_team.models import (  # noqa: E402
    ClientProfile,
    MealRecommendation,
)
from nutrition_meal_planning_team.orchestrator.dropped import (  # noqa: E402
    run_guardrail_pipeline,
)


def _profile(**kw) -> ClientProfile:
    return ClientProfile(client_id="test-client", **kw)


def _meal(
    name: str = "Grilled Chicken Salad", ingredients: list | None = None
) -> MealRecommendation:
    return MealRecommendation(
        name=name,
        ingredients=ingredients or ["chicken breast", "romaine lettuce", "olive oil"],
        meal_type="lunch",
    )


def _passing_result() -> GuardrailResult:
    return GuardrailResult(passed=True, violations=(), flags=(), parsed_ingredients=())


def _passing_result_with_flags() -> GuardrailResult:
    flag = Violation(
        reason=ViolationReason.interaction_flag,
        ingredient_raw="kale",
        canonical_id="kale",
        tag="vitamin_k_high",
        detail="kale may interact with warfarin",
        severity=Severity.flag,
    )
    return GuardrailResult(passed=True, violations=(), flags=(flag,), parsed_ingredients=())


def _failing_result() -> GuardrailResult:
    violation = Violation(
        reason=ViolationReason.allergen,
        ingredient_raw="peanut butter",
        canonical_id="peanut",
        tag="peanut",
        detail="peanut butter contains active allergen 'peanut'",
        severity=Severity.hard_reject,
    )
    return GuardrailResult(passed=False, violations=(violation,), flags=(), parsed_ingredients=())


def _mock_stores():
    feedback = MagicMock()
    feedback.record_recommendation.return_value = str(uuid4())
    audit = MagicMock()
    audit.record_rejection.return_value = 1
    agent = MagicMock()
    agent.regenerate_single.return_value = None
    return feedback, audit, agent


class TestSafeMealPasses:
    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_safe_meal_recorded(self, mock_check):
        mock_check.return_value = _passing_result()
        feedback, audit, agent = _mock_stores()
        profile = _profile()
        meal = _meal()

        result = run_guardrail_pipeline("c1", profile, [meal], agent, feedback, audit)

        assert len(result.recorded) == 1
        assert len(result.dropped) == 0
        assert result.recorded[0].name == "Grilled Chicken Salad"
        feedback.record_recommendation.assert_called_once()
        audit.record_rejection.assert_not_called()

    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_flags_populated(self, mock_check):
        mock_check.return_value = _passing_result_with_flags()
        feedback, audit, agent = _mock_stores()

        result = run_guardrail_pipeline("c1", _profile(), [_meal()], agent, feedback, audit)

        assert len(result.recorded) == 1
        rec_id = result.recorded[0].recommendation_id
        assert rec_id in result.flags_by_recommendation
        assert "interaction_flag:vitamin_k_high" in result.flags_by_recommendation[rec_id]
        assert result.recorded[0].clinical_flags == ["interaction_flag:vitamin_k_high"]


class TestUnsafeMealDropped:
    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_unsafe_meal_dropped(self, mock_check):
        mock_check.return_value = _failing_result()
        feedback, audit, agent = _mock_stores()

        result = run_guardrail_pipeline("c1", _profile(), [_meal()], agent, feedback, audit)

        assert len(result.recorded) == 0
        assert len(result.dropped) == 1
        assert result.dropped[0].name == "Grilled Chicken Salad"
        assert "allergen:peanut" in result.dropped[0].reasons
        feedback.record_recommendation.assert_not_called()
        audit.record_rejection.assert_called()


class TestGuardrailVersionSet:
    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_guardrail_version_on_stored_recommendation(self, mock_check):
        mock_check.return_value = _passing_result()
        feedback, audit, agent = _mock_stores()

        run_guardrail_pipeline("c1", _profile(), [_meal()], agent, feedback, audit)

        call_kwargs = feedback.record_recommendation.call_args
        assert call_kwargs.kwargs["guardrail_version"] == GUARDRAIL_VERSION


class TestMixedBatch:
    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_mixed_safe_and_unsafe(self, mock_check):
        mock_check.side_effect = [_passing_result(), _failing_result()]
        feedback, audit, agent = _mock_stores()

        meals = [_meal("Safe Salad"), _meal("Peanut Bowl")]
        result = run_guardrail_pipeline("c1", _profile(), meals, agent, feedback, audit)

        assert len(result.recorded) == 1
        assert result.recorded[0].name == "Safe Salad"
        assert len(result.dropped) == 1
        assert result.dropped[0].name == "Peanut Bowl"
