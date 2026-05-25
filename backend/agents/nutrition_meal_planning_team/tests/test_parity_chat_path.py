"""SPEC-007 W7 — flag-off parity: chat path returns same behavior as before.

When NUTRITION_GUARDRAIL=0, ``_record_suggestions`` must produce the
same ``recorded`` list the old single-pass code did — no guardrail
check, no dropped, no flags.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

pytest.importorskip("strands", reason="orchestrator requires strands-agents")

from nutrition_meal_planning_team.models import (  # noqa: E402
    ClientProfile,
    MealRecommendation,
)
from nutrition_meal_planning_team.orchestrator.agent import (  # noqa: E402
    NutritionMealPlanningOrchestrator,
)


def _build_orchestrator():
    orch = object.__new__(NutritionMealPlanningOrchestrator)
    orch.profile_store = None
    orch.nutrition_plan_store = None
    orch.intake_agent = None
    orch.nutritionist_agent = None
    orch.meal_planning_agent = MagicMock()
    orch.chat_agent = None
    orch.meal_feedback_store = MagicMock()
    orch._guardrail_audit_store = MagicMock()
    return orch


def _meal(name="Test Meal"):
    return MealRecommendation(name=name, ingredients=["chicken", "rice"], meal_type="lunch")


class TestFlagOffParity:
    @patch(
        "nutrition_meal_planning_team.orchestrator.agent.is_guardrail_enabled", return_value=False
    )
    def test_legacy_passthrough_records_all(self, _flag):
        orch = _build_orchestrator()
        rec_id = str(uuid4())
        orch.meal_feedback_store.record_recommendation.return_value = rec_id
        profile = ClientProfile(client_id="c1")
        meals = [_meal("Meal A"), _meal("Meal B")]

        result = orch._record_suggestions("c1", profile, meals)

        assert len(result.recorded) == 2
        assert len(result.dropped) == 0
        assert result.flags_by_recommendation == {}
        assert result.restrictions_best_effort is False
        assert orch.meal_feedback_store.record_recommendation.call_count == 2

    @patch(
        "nutrition_meal_planning_team.orchestrator.agent.is_guardrail_enabled", return_value=False
    )
    def test_legacy_no_guardrail_check(self, _flag):
        orch = _build_orchestrator()
        orch.meal_feedback_store.record_recommendation.return_value = str(uuid4())
        profile = ClientProfile(client_id="c1")

        with patch(
            "nutrition_meal_planning_team.orchestrator.dropped.check_recommendation"
        ) as mock_check:
            orch._record_suggestions("c1", profile, [_meal()])
            mock_check.assert_not_called()

    @patch(
        "nutrition_meal_planning_team.orchestrator.agent.is_guardrail_enabled", return_value=False
    )
    def test_recorded_ids_match_store(self, _flag):
        orch = _build_orchestrator()
        ids = [str(uuid4()), str(uuid4())]
        orch.meal_feedback_store.record_recommendation.side_effect = ids
        profile = ClientProfile(client_id="c1")

        result = orch._record_suggestions("c1", profile, [_meal("A"), _meal("B")])

        assert result.recorded[0].recommendation_id == ids[0]
        assert result.recorded[1].recommendation_id == ids[1]
