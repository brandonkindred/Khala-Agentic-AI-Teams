"""SPEC-007 W7 — concurrent regeneration: wall-time ≈ single regen latency."""

from __future__ import annotations

import time
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
    run_guardrail_pipeline,
)

REGEN_DELAY_S = 0.15


def _profile():
    return ClientProfile(client_id="c1")


def _meal(name: str):
    return MealRecommendation(name=name, ingredients=["bad_ingredient"], meal_type="lunch")


def _failing():
    v = Violation(
        reason=ViolationReason.allergen,
        ingredient_raw="bad_ingredient",
        canonical_id="bad",
        tag="peanut",
        detail="allergen",
        severity=Severity.hard_reject,
    )
    return GuardrailResult(passed=False, violations=(v,), flags=(), parsed_ingredients=())


def _passing():
    return GuardrailResult(passed=True, violations=(), flags=(), parsed_ingredients=())


def _slow_regen(profile, original, violations):
    time.sleep(REGEN_DELAY_S)
    return MealRecommendation(
        name=f"{original.name} Fixed", ingredients=["safe"], meal_type="lunch"
    )


class TestConcurrentRegeneration:
    @patch("nutrition_meal_planning_team.orchestrator.dropped.check_recommendation")
    def test_three_concurrent_regens_wall_time(self, mock_check):
        """Three rejected suggestions regenerated concurrently should take
        roughly the same wall time as a single regeneration, not 3x."""
        mock_check.side_effect = [
            _failing(),
            _passing(),  # meal 1: fail → regen → pass
            _failing(),
            _passing(),  # meal 2: fail → regen → pass
            _failing(),
            _passing(),  # meal 3: fail → regen → pass
        ]

        agent = MagicMock()
        agent.regenerate_single.side_effect = _slow_regen
        feedback = MagicMock()
        from uuid import uuid4

        feedback.record_recommendation.return_value = str(uuid4())
        audit = MagicMock()
        audit.record_rejection.return_value = 1

        meals = [_meal("A"), _meal("B"), _meal("C")]

        start = time.monotonic()
        result = run_guardrail_pipeline("c1", _profile(), meals, agent, feedback, audit)
        elapsed = time.monotonic() - start

        assert len(result.recorded) == 3
        assert len(result.dropped) == 0
        # Sequential would be ~3 * REGEN_DELAY_S = 0.45s.
        # Concurrent should be ~REGEN_DELAY_S = 0.15s (+ overhead).
        # Use generous 2.5x tolerance to avoid flaky CI.
        assert elapsed < REGEN_DELAY_S * 2.5, (
            f"Expected concurrent execution (~{REGEN_DELAY_S}s) but took {elapsed:.2f}s"
        )
