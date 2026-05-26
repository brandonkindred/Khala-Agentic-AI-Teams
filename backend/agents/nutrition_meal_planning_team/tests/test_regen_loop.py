"""SPEC-007 W7 — regeneration loop: rejected → regen → re-check passes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

pytest.importorskip("strands", reason="orchestrator requires strands-agents")

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


def _profile():
    return ClientProfile(client_id="c1")


def _meal(name="Original Meal"):
    return MealRecommendation(name=name, ingredients=["cashew", "rice"], meal_type="dinner")


def _replacement():
    return MealRecommendation(
        name="Replacement Meal", ingredients=["chicken", "rice"], meal_type="dinner"
    )


def _failing():
    v = Violation(
        reason=ViolationReason.allergen,
        ingredient_raw="cashew",
        canonical_id="cashew",
        tag="tree_nut",
        detail="cashew contains active allergen 'tree_nut'",
        severity=Severity.hard_reject,
    )
    return GuardrailResult(passed=False, violations=(v,), flags=(), parsed_ingredients=())


def _passing():
    return GuardrailResult(passed=True, violations=(), flags=(), parsed_ingredients=())


class TestRegenSucceedsOnRetry:
    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_regen_passes_on_second_attempt(self, mock_check):
        mock_check.side_effect = [_failing(), _passing()]

        feedback = MagicMock()
        feedback.record_recommendation.return_value = str(uuid4())
        audit = MagicMock()
        audit.record_rejection.return_value = 1
        agent = MagicMock()
        agent.regenerate_single.return_value = _replacement()

        result = run_guardrail_pipeline("c1", _profile(), [_meal()], agent, feedback, audit)

        assert len(result.recorded) == 1
        assert len(result.dropped) == 0
        assert result.recorded[0].name == "Replacement Meal"
        agent.regenerate_single.assert_called_once()
        audit.record_rejection.assert_called_once()
        feedback.record_recommendation.assert_called_once()

    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_violations_logged_to_audit_store(self, mock_check):
        mock_check.side_effect = [_failing(), _passing()]

        feedback = MagicMock()
        feedback.record_recommendation.return_value = str(uuid4())
        audit = MagicMock()
        audit.record_rejection.return_value = 1
        agent = MagicMock()
        agent.regenerate_single.return_value = _replacement()

        run_guardrail_pipeline("c1", _profile(), [_meal()], agent, feedback, audit)

        audit.record_rejection.assert_called_once()
        rejection_kwargs = audit.record_rejection.call_args.kwargs
        assert rejection_kwargs["guardrail_version"] == GUARDRAIL_VERSION
        assert rejection_kwargs["tag"] == "tree_nut"

    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_regen_none_drops_immediately(self, mock_check):
        mock_check.return_value = _failing()

        feedback = MagicMock()
        audit = MagicMock()
        audit.record_rejection.return_value = 1
        agent = MagicMock()
        agent.regenerate_single.return_value = None

        result = run_guardrail_pipeline("c1", _profile(), [_meal()], agent, feedback, audit)

        assert len(result.recorded) == 0
        assert len(result.dropped) == 1
        assert result.dropped[0].name == "Original Meal"
        agent.regenerate_single.assert_called_once()
