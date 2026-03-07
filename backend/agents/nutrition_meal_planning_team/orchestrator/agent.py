"""Orchestrator: routes profile/plan/meals/feedback to the right agents and stores."""

from __future__ import annotations

import logging
from typing import Optional

from ..agents.intake_profile_agent import IntakeProfileAgent
from ..agents.meal_planning_agent import MealPlanningAgent
from ..agents.nutritionist_agent import NutritionistAgent
from ..models import (
    ClientProfile,
    FeedbackRequest,
    FeedbackResponse,
    MealHistoryResponse,
    MealPlanRequest,
    MealPlanResponse,
    NutritionPlanRequest,
    NutritionPlanResponse,
    ProfileUpdateRequest,
)
from ..shared.client_profile_store import ClientProfileStore
from ..shared.meal_feedback_store import MealFeedbackStore
from ..shared.llm import get_llm_client

logger = logging.getLogger(__name__)


class NutritionMealPlanningOrchestrator:
    """
    Single entry point: load profile/history as needed, delegate to intake/nutritionist/meal_planning agents.
    API routes can call orchestrator methods or wire stores/agents directly.
    """

    def __init__(
        self,
        profile_store: Optional[ClientProfileStore] = None,
        meal_feedback_store: Optional[MealFeedbackStore] = None,
    ) -> None:
        self.profile_store = profile_store or ClientProfileStore()
        self.meal_feedback_store = meal_feedback_store or MealFeedbackStore()
        llm = get_llm_client()
        self.intake_agent = IntakeProfileAgent(llm)
        self.nutritionist_agent = NutritionistAgent(llm)
        self.meal_planning_agent = MealPlanningAgent(llm)

    def get_profile(self, client_id: str) -> Optional[ClientProfile]:
        """Get client profile or None if not found."""
        return self.profile_store.get_profile(client_id)

    def update_profile(
        self, client_id: str, update: ProfileUpdateRequest
    ) -> ClientProfile:
        """Validate/complete profile with intake agent and save."""
        current = self.profile_store.get_profile(client_id)
        if current is None:
            current = self.profile_store.create_profile(client_id)
        profile = self.intake_agent.run(client_id, update=update, current_profile=current)
        self.profile_store.save_profile(client_id, profile)
        return profile

    def get_nutrition_plan(self, request: NutritionPlanRequest) -> NutritionPlanResponse:
        """Load profile, run nutritionist agent, return plan."""
        profile = self.profile_store.get_profile(request.client_id)
        if profile is None:
            raise ValueError("Profile not found")
        plan = self.nutritionist_agent.run(profile)
        return NutritionPlanResponse(client_id=request.client_id, plan=plan)

    def get_meal_plan(self, request: MealPlanRequest) -> MealPlanResponse:
        """Load profile, nutrition plan, meal history; run meal planning agent; record each suggestion; return with recommendation_ids."""
        from ..models import MealRecommendationWithId

        profile = self.profile_store.get_profile(request.client_id)
        if profile is None:
            raise ValueError("Profile not found")
        nutrition_plan = self.nutritionist_agent.run(profile)
        meal_history = self.meal_feedback_store.get_meal_history(
            request.client_id, limit=50
        )
        suggestions = self.meal_planning_agent.run(
            profile,
            nutrition_plan,
            meal_history,
            period_days=request.period_days,
            meal_types=request.meal_types,
        )
        with_ids: list[MealRecommendationWithId] = []
        for s in suggestions:
            rec_id = self.meal_feedback_store.record_recommendation(
                request.client_id, s.model_dump()
            )
            with_ids.append(
                MealRecommendationWithId(**s.model_dump(), recommendation_id=rec_id)
            )
        return MealPlanResponse(client_id=request.client_id, suggestions=with_ids)

    def submit_feedback(self, request: FeedbackRequest) -> FeedbackResponse:
        """Record feedback for a recommendation."""
        ok = self.meal_feedback_store.record_feedback(
            request.recommendation_id,
            rating=request.rating,
            would_make_again=request.would_make_again,
            notes=request.notes,
        )
        return FeedbackResponse(
            recommendation_id=request.recommendation_id, recorded=ok
        )

    def get_meal_history(self, client_id: str, limit: int = 100) -> MealHistoryResponse:
        """Return past recommendations and feedback for the client."""
        entries = self.meal_feedback_store.get_meal_history(client_id, limit=limit)
        return MealHistoryResponse(client_id=client_id, entries=entries)
