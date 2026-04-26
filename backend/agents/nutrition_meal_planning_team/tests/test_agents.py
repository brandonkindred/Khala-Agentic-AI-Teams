"""Unit/integration tests for intake, nutritionist, meal planning, and chat agents (mocked LLM or full).

Several tests import paths that transitively touch the real job service.
Marked integration pending follow-up to either fully mock the LLM stack
or split the file.
"""

import pytest

from nutrition_meal_planning_team.agents.chat_agent import NutritionChatAgent
from nutrition_meal_planning_team.agents.chat_agent.agent import (
    _current_phase,
    _profile_completeness,
)
from nutrition_meal_planning_team.agents.intake_profile_agent import IntakeProfileAgent
from nutrition_meal_planning_team.agents.meal_planning_agent import (
    MealPlanningAgent,
    _summarize_history,
)
from nutrition_meal_planning_team.agents.nutritionist_agent import NutritionistAgent
from nutrition_meal_planning_team.models import (
    ClientProfile,
    GoalsInfo,
    HouseholdInfo,
    LifestyleInfo,
    MealHistoryEntry,
    NutritionPlan,
    PreferencesInfo,
)

pytestmark = [pytest.mark.integration]


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


class TestChatAgentHelpers:
    """Tests for chat agent helper functions (no LLM)."""

    def test_profile_completeness_none(self):
        comp = _profile_completeness(None)
        assert not any(comp.values())

    def test_profile_completeness_full(self):
        profile = ClientProfile(
            client_id="c1",
            household=HouseholdInfo(number_of_people=2, description="couple"),
            dietary_needs=["vegetarian"],
            lifestyle=LifestyleInfo(max_cooking_time_minutes=30),
            preferences=PreferencesInfo(cuisines_liked=["Italian"]),
            goals=GoalsInfo(goal_type="lose_weight"),
        )
        comp = _profile_completeness(profile)
        assert comp["household"] is True
        assert comp["dietary_needs"] is True
        assert comp["lifestyle"] is True
        assert comp["preferences"] is True
        assert comp["goals"] is True

    def test_current_phase_no_profile(self):
        assert _current_phase(None, False, False) == "intake"

    def test_current_phase_with_profile_no_plan(self):
        profile = ClientProfile(
            client_id="c1",
            household=HouseholdInfo(number_of_people=2, description="couple"),
            dietary_needs=["vegetarian"],
            lifestyle=LifestyleInfo(max_cooking_time_minutes=30),
        )
        assert _current_phase(profile, False, False) == "nutrition"

    def test_current_phase_with_plan_no_meals(self):
        profile = ClientProfile(
            client_id="c1",
            household=HouseholdInfo(number_of_people=2, description="couple"),
            dietary_needs=["vegetarian"],
        )
        assert _current_phase(profile, True, False) == "meals"

    def test_current_phase_feedback(self):
        profile = ClientProfile(
            client_id="c1",
            household=HouseholdInfo(number_of_people=2, description="couple"),
        )
        assert _current_phase(profile, True, True) == "feedback"


class TestChatAgentWithDummyLLM:
    """Test chat agent with dummy LLM returning structured responses."""

    @pytest.fixture
    def chat_intake_llm(self):
        """LLM that returns an intake-phase chat response with save_profile action."""

        class DummyLLM:
            def complete_json(self, prompt, **kwargs):
                return {
                    "message": "Great, I've noted your info!",
                    "phase": "intake",
                    "action": "save_profile",
                    "extracted_profile": {
                        "household": {"number_of_people": 2, "description": "couple"},
                        "dietary_needs": ["vegetarian"],
                    },
                    "meal_plan_params": None,
                    "feedback_data": None,
                }

        return DummyLLM()

    @pytest.fixture
    def chat_none_llm(self):
        """LLM that returns a no-action chat response (just asking questions)."""

        class DummyLLM:
            def complete_json(self, prompt, **kwargs):
                return {
                    "message": "Tell me about your household!",
                    "phase": "intake",
                    "action": "none",
                }

        return DummyLLM()

    def test_chat_no_action(self, chat_none_llm):
        intake = IntakeProfileAgent(chat_none_llm)
        nutritionist = NutritionistAgent(chat_none_llm)
        meal_planner = MealPlanningAgent(chat_none_llm)
        agent = NutritionChatAgent(chat_none_llm, intake, nutritionist, meal_planner)

        result = agent.run(
            client_id="c1",
            user_message="Hi there",
            conversation_history=[],
            profile=None,
            nutrition_plan=None,
            meal_suggestions=None,
        )
        assert result["action"] == "none"
        assert result["phase"] == "intake"
        assert "Tell me" in result["message"]

    def test_chat_save_profile_action(self, chat_intake_llm):
        intake = IntakeProfileAgent(chat_intake_llm)
        nutritionist = NutritionistAgent(chat_intake_llm)
        meal_planner = MealPlanningAgent(chat_intake_llm)
        agent = NutritionChatAgent(chat_intake_llm, intake, nutritionist, meal_planner)

        result = agent.run(
            client_id="c1",
            user_message="I'm vegetarian, cooking for 2",
            conversation_history=[],
            profile=None,
            nutrition_plan=None,
            meal_suggestions=None,
        )
        assert result["action"] == "save_profile"
        assert result["extracted_profile"] is not None
        assert result["extracted_profile"]["dietary_needs"] == ["vegetarian"]
