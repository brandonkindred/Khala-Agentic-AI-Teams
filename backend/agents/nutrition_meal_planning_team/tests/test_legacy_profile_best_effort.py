"""SPEC-007 W7 — legacy profile without restriction_resolution.

When a profile has raw ``allergies_and_intolerances`` or ``dietary_needs``
but no ``restriction_resolution.resolved`` entries, the pipeline sets
``restrictions_best_effort=True`` so the UI can display a warning.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

pytest.importorskip("strands", reason="orchestrator requires strands-agents")

from nutrition_meal_planning_team.guardrail.violations import (  # noqa: E402
    GuardrailResult,
)
from nutrition_meal_planning_team.models import (  # noqa: E402
    ClientProfile,
    MealRecommendation,
    RestrictionResolution,
)
from nutrition_meal_planning_team.orchestrator.dropped import (  # noqa: E402
    run_guardrail_pipeline,
)


def _passing():
    return GuardrailResult(passed=True, violations=(), flags=(), parsed_ingredients=())


def _meal():
    return MealRecommendation(name="Test Meal", ingredients=["chicken"], meal_type="lunch")


def _stores():
    feedback = MagicMock()
    feedback.record_recommendation.return_value = str(uuid4())
    audit = MagicMock()
    audit.record_rejection.return_value = 1
    agent = MagicMock()
    return feedback, audit, agent


class TestBestEffortFlag:
    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_empty_resolution_with_raw_allergies(self, mock_check):
        mock_check.return_value = _passing()
        feedback, audit, agent = _stores()

        profile = ClientProfile(
            client_id="c1",
            allergies_and_intolerances=["peanuts", "tree nuts"],
            restriction_resolution=RestrictionResolution(),
        )

        result = run_guardrail_pipeline("c1", profile, [_meal()], agent, feedback, audit)

        assert result.restrictions_best_effort is True

    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_empty_resolution_with_dietary_needs(self, mock_check):
        mock_check.return_value = _passing()
        feedback, audit, agent = _stores()

        profile = ClientProfile(
            client_id="c1",
            dietary_needs=["vegan"],
            restriction_resolution=RestrictionResolution(),
        )

        result = run_guardrail_pipeline("c1", profile, [_meal()], agent, feedback, audit)

        assert result.restrictions_best_effort is True

    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_empty_resolution_no_raw_restrictions(self, mock_check):
        mock_check.return_value = _passing()
        feedback, audit, agent = _stores()

        profile = ClientProfile(
            client_id="c1",
            restriction_resolution=RestrictionResolution(),
        )

        result = run_guardrail_pipeline("c1", profile, [_meal()], agent, feedback, audit)

        assert result.restrictions_best_effort is False

    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_resolved_restriction_not_best_effort(self, mock_check):
        from nutrition_meal_planning_team.models import ResolvedRestriction

        mock_check.return_value = _passing()
        feedback, audit, agent = _stores()

        profile = ClientProfile(
            client_id="c1",
            allergies_and_intolerances=["peanuts"],
            restriction_resolution=RestrictionResolution(
                resolved=[
                    ResolvedRestriction(raw="peanuts", allergen_tags=["peanut"]),
                ],
            ),
        )

        result = run_guardrail_pipeline("c1", profile, [_meal()], agent, feedback, audit)

        assert result.restrictions_best_effort is False
