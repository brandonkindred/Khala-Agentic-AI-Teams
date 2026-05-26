"""SPEC-007 W7 — MAX_REGEN_RETRIES exhausted → DroppedSuggestion."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("strands", reason="orchestrator requires strands-agents")

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
    MAX_REGEN_RETRIES,
    run_guardrail_pipeline,
)


def _profile():
    return ClientProfile(client_id="c1")


def _meal():
    return MealRecommendation(
        name="Nut Stir Fry", ingredients=["cashew", "tofu"], meal_type="dinner"
    )


def _bad_replacement(n: int):
    return MealRecommendation(
        name=f"Bad Replacement {n}", ingredients=["walnut", "tofu"], meal_type="dinner"
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


class TestRetriesExhausted:
    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_all_retries_fail_produces_dropped(self, mock_check):
        mock_check.return_value = _failing()

        agent = MagicMock()
        agent.regenerate_single.side_effect = [
            _bad_replacement(i) for i in range(MAX_REGEN_RETRIES)
        ]
        feedback = MagicMock()
        audit = MagicMock()
        audit.record_rejection.return_value = 1

        result = run_guardrail_pipeline("c1", _profile(), [_meal()], agent, feedback, audit)

        assert len(result.recorded) == 0
        assert len(result.dropped) == 1
        dropped = result.dropped[0]
        assert dropped.name == "Nut Stir Fry"
        assert any("allergen" in r for r in dropped.reasons)
        assert any("tree_nut" in d for d in dropped.detail)
        feedback.record_recommendation.assert_not_called()

    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_retry_count_matches_constant(self, mock_check):
        mock_check.return_value = _failing()

        agent = MagicMock()
        agent.regenerate_single.side_effect = [
            _bad_replacement(i) for i in range(MAX_REGEN_RETRIES)
        ]
        feedback = MagicMock()
        audit = MagicMock()
        audit.record_rejection.return_value = 1

        run_guardrail_pipeline("c1", _profile(), [_meal()], agent, feedback, audit)

        assert agent.regenerate_single.call_count == MAX_REGEN_RETRIES
        # initial check + MAX_REGEN_RETRIES re-checks
        assert mock_check.call_count == 1 + MAX_REGEN_RETRIES

    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_max_regen_retries_is_two(self, mock_check):
        assert MAX_REGEN_RETRIES == 2
