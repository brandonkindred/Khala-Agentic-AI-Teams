"""Direct unit coverage for ``NutritionMealPlanningOrchestrator.get_meal_plan``'s
optional ``cancel_check`` parameter (SPEC-004 meal-plan pipeline).

Bypasses ``__init__`` (and its ``strands`` model requirement) via
``object.__new__``, following the same pattern as
``test_orchestrator_build_plan.py``, then monkeypatches the two expensive
instance methods (``_get_or_generate_nutrition_plan``, ``_record_suggestions``)
so this stays a pure unit test — no Postgres, no LLM.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "strands",
    reason="orchestrator requires strands-agents (install via make install-dev)",
)

from nutrition_meal_planning_team.models import ClientProfile, MealPlanRequest  # noqa: E402
from nutrition_meal_planning_team.orchestrator.agent import (  # noqa: E402
    NutritionMealPlanningOrchestrator,
    OperationCancelled,
)


def _build_orchestrator() -> NutritionMealPlanningOrchestrator:
    """Build an orchestrator with mocked stores/agents (bypasses __init__)."""
    orch = object.__new__(NutritionMealPlanningOrchestrator)
    orch.profile_store = MagicMock()
    orch.profile_store.get_profile.return_value = ClientProfile(client_id="c")
    orch.meal_feedback_store = MagicMock()
    orch.meal_feedback_store.get_meal_history.return_value = []
    orch.meal_planning_agent = MagicMock()
    orch.meal_planning_agent.run.return_value = []
    return orch


def _request() -> MealPlanRequest:
    return MealPlanRequest(client_id="c", period_days=7, meal_types=["lunch", "dinner"])


def test_get_meal_plan_without_cancel_check_runs_to_completion(monkeypatch):
    """Backward compatible: omitting ``cancel_check`` (the pre-PR call shape)
    still runs the full pipeline unchanged."""
    orch = _build_orchestrator()
    monkeypatch.setattr(orch, "_get_or_generate_nutrition_plan", lambda profile: MagicMock())
    monkeypatch.setattr(
        orch,
        "_record_suggestions",
        lambda *a, **kw: MagicMock(
            recorded=[], dropped=[], flags_by_recommendation={}, restrictions_best_effort=False
        ),
    )

    result = orch.get_meal_plan(_request())

    assert result.client_id == "c"
    orch.meal_planning_agent.run.assert_called_once()


def test_get_meal_plan_cancel_check_false_runs_to_completion(monkeypatch):
    orch = _build_orchestrator()
    monkeypatch.setattr(orch, "_get_or_generate_nutrition_plan", lambda profile: MagicMock())
    monkeypatch.setattr(
        orch,
        "_record_suggestions",
        lambda *a, **kw: MagicMock(
            recorded=[], dropped=[], flags_by_recommendation={}, restrictions_best_effort=False
        ),
    )

    result = orch.get_meal_plan(_request(), cancel_check=lambda: False)

    assert result.client_id == "c"
    orch.meal_planning_agent.run.assert_called_once()


def test_get_meal_plan_cancel_check_true_raises_before_meal_planning_llm_call(monkeypatch):
    """The fail-fast checkpoint: if cancel_check() reports cancellation right
    after nutrition-plan generation, OperationCancelled is raised and the
    (expensive) meal-planning LLM call never runs."""
    orch = _build_orchestrator()
    monkeypatch.setattr(orch, "_get_or_generate_nutrition_plan", lambda profile: MagicMock())

    with pytest.raises(OperationCancelled, match="cancelled after nutrition-plan generation"):
        orch.get_meal_plan(_request(), cancel_check=lambda: True)

    orch.meal_planning_agent.run.assert_not_called()


def test_get_meal_plan_still_raises_value_error_for_missing_profile(monkeypatch):
    """The cancel_check checkpoint sits after the profile lookup — a missing
    profile still raises ValueError before cancel_check is even consulted."""
    orch = _build_orchestrator()
    orch.profile_store.get_profile.return_value = None
    cancel_check = MagicMock(return_value=False)

    with pytest.raises(ValueError, match="Profile not found"):
        orch.get_meal_plan(_request(), cancel_check=cancel_check)

    cancel_check.assert_not_called()
