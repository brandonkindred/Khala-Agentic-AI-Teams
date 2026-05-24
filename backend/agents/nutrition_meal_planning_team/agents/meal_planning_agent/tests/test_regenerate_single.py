"""Tests for MealPlanningAgent.regenerate_single (SPEC-007 W5).

Uses a lightweight dummy LLM client injected via ``agent._client`` to
control ``complete_json`` responses without touching the real LLM.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agents.nutrition_meal_planning_team.agents.meal_planning_agent.agent import (
    MealPlanningAgent,
)
from agents.nutrition_meal_planning_team.guardrail.tests._fixtures import (
    profile_with,
    recipe,
)
from agents.nutrition_meal_planning_team.guardrail.violations import (
    Severity,
    Violation,
    ViolationReason,
)
from agents.nutrition_meal_planning_team.ingredient_kb.taxonomy import AllergenTag
from agents.nutrition_meal_planning_team.models import MealRecommendation

from llm_service.clients.dummy import DummyLLMClient
from llm_service.interface import (
    LLMError,
    LLMJsonParseError,
    LLMPermanentError,
    LLMSchemaValidationError,
)

# ---------------------------------------------------------------------------
# Test-only dummy client
# ---------------------------------------------------------------------------


class _RegenDummyClient:
    """Minimal ``LLMClient``-compatible stub for regeneration tests."""

    def __init__(
        self,
        response: Optional[Dict[str, Any]] = None,
        error: Optional[Exception] = None,
    ):
        self._response = response
        self._error = error
        self.last_prompt: Optional[str] = None
        self.last_kwargs: Dict[str, Any] = {}

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.last_prompt = prompt
        self.last_kwargs = kwargs
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


_VALID_RESPONSE: Dict[str, Any] = {
    "name": "Grilled Tofu Bowl",
    "ingredients": ["tofu", "rice", "broccoli", "soy sauce"],
    "portions_servings": "2",
    "prep_time_minutes": 10,
    "cook_time_minutes": 20,
    "rationale": "Avoids tree nuts; high protein",
    "meal_type": "dinner",
    "suggested_date": None,
}


def _make_agent(client: _RegenDummyClient) -> MealPlanningAgent:
    agent = MealPlanningAgent(DummyLLMClient())
    agent._client = client
    return agent


def _violation(
    ingredient: str = "almonds",
    tag: str | None = "tree_nut",
) -> Violation:
    return Violation(
        reason=ViolationReason.allergen,
        ingredient_raw=ingredient,
        canonical_id=None,
        tag=tag,
        detail=f"test: {ingredient}",
        severity=Severity.hard_reject,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_returns_meal_recommendation(self):
        client = _RegenDummyClient(response=_VALID_RESPONSE)
        agent = _make_agent(client)
        result = agent.regenerate_single(
            profile_with(allergens=[AllergenTag.tree_nut]),
            recipe("almonds", name="Almond Cake"),
            [_violation("almonds", "tree_nut")],
        )
        assert isinstance(result, MealRecommendation)
        assert result.name == "Grilled Tofu Bowl"
        assert result.meal_type == "dinner"

    def test_returned_meal_has_ingredients(self):
        client = _RegenDummyClient(response=_VALID_RESPONSE)
        agent = _make_agent(client)
        result = agent.regenerate_single(
            profile_with(),
            recipe("item"),
            [_violation("item", "tree_nut")],
        )
        assert result is not None
        assert len(result.ingredients) > 0


# ---------------------------------------------------------------------------
# Returns None on errors
# ---------------------------------------------------------------------------


class TestReturnsNone:
    def test_returns_none_on_json_parse_error(self):
        client = _RegenDummyClient(error=LLMJsonParseError("bad json"))
        agent = _make_agent(client)
        result = agent.regenerate_single(
            profile_with(),
            recipe("item"),
            [_violation()],
        )
        assert result is None

    def test_returns_none_on_schema_validation_error(self):
        client = _RegenDummyClient(error=LLMSchemaValidationError("bad schema"))
        agent = _make_agent(client)
        result = agent.regenerate_single(
            profile_with(),
            recipe("item"),
            [_violation()],
        )
        assert result is None

    def test_returns_none_on_permanent_error(self):
        client = _RegenDummyClient(error=LLMPermanentError("4xx"))
        agent = _make_agent(client)
        result = agent.regenerate_single(
            profile_with(),
            recipe("item"),
            [_violation()],
        )
        assert result is None

    def test_returns_none_on_transient_error(self):
        client = _RegenDummyClient(error=LLMError("timeout"))
        agent = _make_agent(client)
        result = agent.regenerate_single(
            profile_with(),
            recipe("item"),
            [_violation()],
        )
        assert result is None

    def test_returns_none_on_unexpected_exception(self):
        client = _RegenDummyClient(error=RuntimeError("unexpected"))
        agent = _make_agent(client)
        result = agent.regenerate_single(
            profile_with(),
            recipe("item"),
            [_violation()],
        )
        assert result is None


# ---------------------------------------------------------------------------
# Prompt wiring
# ---------------------------------------------------------------------------


class TestPromptWiring:
    def test_forbidden_ingredients_in_prompt(self):
        client = _RegenDummyClient(response=_VALID_RESPONSE)
        agent = _make_agent(client)
        agent.regenerate_single(
            profile_with(allergens=[AllergenTag.tree_nut]),
            recipe("walnuts", name="Walnut Salad"),
            [_violation("walnuts", "tree_nut")],
        )
        assert client.last_prompt is not None
        assert "walnuts (tag: tree_nut)" in client.last_prompt

    def test_multiple_violations_all_in_prompt(self):
        client = _RegenDummyClient(response=_VALID_RESPONSE)
        agent = _make_agent(client)
        agent.regenerate_single(
            profile_with(allergens=[AllergenTag.tree_nut]),
            recipe("walnuts", "milk"),
            [
                _violation("walnuts", "tree_nut"),
                _violation("milk", "dairy"),
            ],
        )
        assert client.last_prompt is not None
        assert "walnuts (tag: tree_nut)" in client.last_prompt
        assert "milk (tag: dairy)" in client.last_prompt

    def test_system_prompt_passed(self):
        client = _RegenDummyClient(response=_VALID_RESPONSE)
        agent = _make_agent(client)
        agent.regenerate_single(
            profile_with(),
            recipe("item"),
            [_violation()],
        )
        from agents.nutrition_meal_planning_team.agents.meal_planning_agent.regeneration_prompt import (
            REGENERATION_SYSTEM_PROMPT,
        )

        assert client.last_kwargs.get("system_prompt") == REGENERATION_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------


class TestSchemaContract:
    def test_schema_is_single_meal_recommendation(self):
        """The prompt instructs the LLM to return a single object, not a list."""
        client = _RegenDummyClient(response=_VALID_RESPONSE)
        agent = _make_agent(client)
        agent.regenerate_single(
            profile_with(),
            recipe("item"),
            [_violation()],
        )
        assert client.last_prompt is not None
        assert '"MealRecommendation"' in client.last_prompt
        assert '"suggestions"' not in client.last_prompt
