"""Unit/integration tests for intake, nutritionist, and meal planning agents (mocked LLM or full)."""

import pytest

from nutrition_meal_planning_team.agents.intake_profile_agent import IntakeProfileAgent
from nutrition_meal_planning_team.agents.meal_planning_agent import (
    MealPlanningAgent,
    _summarize_history,
)
from nutrition_meal_planning_team.agents.nutritionist_agent import NutritionistAgent
from nutrition_meal_planning_team.models import (
    ClientProfile,
    HouseholdInfo,
    MealHistoryEntry,
    NutritionPlan,
)


class TestMealPlanningAgentHelpers:
    """Test meal planning agent helper (no LLM)."""

    def test_summarize_history_empty(self):
        assert _summarize_history([]) == "No past feedback yet."

    def test_summarize_history_hits_and_misses(self):
        from nutrition_meal_planning_team.models import FeedbackRecord

        entries = [
            MealHistoryEntry(
                recommendation_id="r1",
                client_id="c1",
                meal_snapshot={"name": "Salad"},
                feedback=FeedbackRecord(rating=5, would_make_again=True),
            ),
            MealHistoryEntry(
                recommendation_id="r2",
                client_id="c1",
                meal_snapshot={"name": "Stew"},
                feedback=FeedbackRecord(rating=1, would_make_again=False),
            ),
        ]
        summary = _summarize_history(entries)
        assert "Salad" in summary
        assert "Stew" in summary
        assert "Past hits" in summary
        assert "Past misses" in summary


class TestIntakeProfileAgentWithDummyLLM:
    """Test intake agent with a dummy LLM that returns valid profile JSON."""

    @pytest.fixture
    def dummy_llm(self):
        """LLM that returns a fixed profile JSON."""

        class DummyLLM:
            def complete_json(self, prompt, **kwargs):
                return {
                    "client_id": "test",
                    "household": {
                        "number_of_people": 1,
                        "description": "solo",
                        "ages_if_relevant": [],
                    },
                    "dietary_needs": [],
                    "allergies_and_intolerances": [],
                    "lifestyle": {
                        "max_cooking_time_minutes": 30,
                        "lunch_context": "remote",
                        "equipment_constraints": [],
                        "other_constraints": "",
                    },
                    "preferences": {
                        "cuisines_liked": [],
                        "cuisines_disliked": [],
                        "ingredients_disliked": [],
                        "preferences_free_text": "",
                    },
                    "goals": {"goal_type": "maintain", "notes": ""},
                }

        return DummyLLM()

    def test_run_returns_profile(self, dummy_llm):
        agent = IntakeProfileAgent(dummy_llm)
        profile = agent.run("client1", current_profile=None)
        assert profile.client_id == "client1"
        assert profile.household.number_of_people == 1
        assert profile.lifestyle.max_cooking_time_minutes == 30


class TestNutritionistAgentWithDummyLLM:
    """Test nutritionist agent with dummy LLM."""

    @pytest.fixture
    def dummy_llm(self):
        class DummyLLM:
            def complete_json(self, prompt, **kwargs):
                return {
                    "daily_targets": {"calories_kcal": 2000, "protein_g": 50},
                    "balance_guidelines": ["more vegetables"],
                    "foods_to_emphasize": ["leafy greens"],
                    "foods_to_avoid": [],
                    "notes": "",
                }

        return DummyLLM()

    def test_run_returns_plan(self, dummy_llm):
        profile = ClientProfile(
            client_id="c1",
            household=HouseholdInfo(number_of_people=1),
        )
        agent = NutritionistAgent(dummy_llm)
        plan = agent.run(profile)
        assert plan.daily_targets.calories_kcal == 2000
        assert "more vegetables" in plan.balance_guidelines


class TestMealPlanningAgentWithDummyLLM:
    """Test meal planning agent with dummy LLM."""

    @pytest.fixture
    def dummy_llm(self):
        class DummyLLM:
            def complete_json(self, prompt, **kwargs):
                return {
                    "suggestions": [
                        {
                            "name": "Test Meal",
                            "ingredients": ["a", "b"],
                            "portions_servings": "2",
                            "prep_time_minutes": 10,
                            "cook_time_minutes": 20,
                            "rationale": "Fits plan",
                            "meal_type": "dinner",
                            "suggested_date": None,
                        }
                    ]
                }

        return DummyLLM()

    def test_run_returns_suggestions(self, dummy_llm):
        profile = ClientProfile(client_id="c1", household=HouseholdInfo(number_of_people=1))
        plan = NutritionPlan()
        agent = MealPlanningAgent(dummy_llm)
        suggestions = agent.run(profile, plan, [])
        assert len(suggestions) == 1
        assert suggestions[0].name == "Test Meal"
        assert suggestions[0].meal_type == "dinner"
