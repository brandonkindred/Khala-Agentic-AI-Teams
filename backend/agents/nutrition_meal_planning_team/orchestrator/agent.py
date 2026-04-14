"""Orchestrator: routes profile/plan/meals/feedback/chat to the right agents and stores."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from llm_service import get_strands_model

from ..agents.chat_agent import NutritionChatAgent
from ..agents.intake_profile_agent import IntakeProfileAgent
from ..agents.meal_planning_agent import MealPlanningAgent
from ..agents.nutritionist_agent import NutritionistAgent
from ..models import (
    ChatRequest,
    ChatResponse,
    ClientProfile,
    FeedbackRequest,
    FeedbackResponse,
    MealHistoryResponse,
    MealPlanRequest,
    MealPlanResponse,
    MealRecommendationWithId,
    NutritionPlan,
    NutritionPlanRequest,
    NutritionPlanResponse,
    ProfileUpdateRequest,
)
from ..shared.client_profile_store import ClientProfileStore
from ..shared.meal_feedback_store import MealFeedbackStore
from ..shared.nutrition_plan_store import NutritionPlanStore

logger = logging.getLogger(__name__)


class NutritionMealPlanningOrchestrator:
    """
    Single entry point: load profile/history as needed, delegate to intake/nutritionist/meal_planning agents.
    All API routes should delegate to this class.
    """

    def __init__(
        self,
        profile_store: Optional[ClientProfileStore] = None,
        meal_feedback_store: Optional[MealFeedbackStore] = None,
        nutrition_plan_store: Optional[NutritionPlanStore] = None,
        llm_model: Optional[Any] = None,
    ) -> None:
        self.profile_store = profile_store or ClientProfileStore()
        self.meal_feedback_store = meal_feedback_store or MealFeedbackStore()
        self.nutrition_plan_store = nutrition_plan_store or NutritionPlanStore()
        model = llm_model or get_strands_model("nutrition_meal_planning")
        self.intake_agent = IntakeProfileAgent(model)
        self.nutritionist_agent = NutritionistAgent(model)
        self.meal_planning_agent = MealPlanningAgent(model)
        self.chat_agent = NutritionChatAgent(
            model, self.intake_agent, self.nutritionist_agent, self.meal_planning_agent
        )

    def get_profile(self, client_id: str) -> Optional[ClientProfile]:
        """Get client profile or None if not found."""
        return self.profile_store.get_profile(client_id)

    def update_profile(self, client_id: str, update: ProfileUpdateRequest) -> ClientProfile:
        """Validate/complete profile with intake agent and save."""
        current = self.profile_store.get_profile(client_id)
        if current is None:
            current = self.profile_store.create_profile(client_id)
        profile = self.intake_agent.run(client_id, update=update, current_profile=current)
        self.profile_store.save_profile(client_id, profile)
        return profile

    def _get_or_generate_nutrition_plan(self, profile: ClientProfile) -> NutritionPlan:
        """Return cached nutrition plan if profile unchanged, otherwise generate and cache."""
        cached = self.nutrition_plan_store.get_cached_plan(profile.client_id, profile)
        if cached is not None:
            return cached
        plan = self.nutritionist_agent.run(profile)
        self.nutrition_plan_store.save_plan(profile.client_id, profile, plan)
        return plan

    def get_nutrition_plan(self, request: NutritionPlanRequest) -> NutritionPlanResponse:
        """Load profile, run nutritionist agent, return plan."""
        profile = self.profile_store.get_profile(request.client_id)
        if profile is None:
            raise ValueError("Profile not found")
        plan = self._get_or_generate_nutrition_plan(profile)
        return NutritionPlanResponse(client_id=request.client_id, plan=plan)

    def get_meal_plan(self, request: MealPlanRequest) -> MealPlanResponse:
        """Load profile, nutrition plan, meal history; run meal planning agent; record each suggestion."""
        profile = self.profile_store.get_profile(request.client_id)
        if profile is None:
            raise ValueError("Profile not found")
        nutrition_plan = self._get_or_generate_nutrition_plan(profile)
        meal_history = self.meal_feedback_store.get_meal_history(request.client_id, limit=50)
        suggestions = self.meal_planning_agent.run(
            profile,
            nutrition_plan,
            meal_history,
            period_days=request.period_days,
            meal_types=request.meal_types,
        )
        with_ids = self._record_suggestions(request.client_id, suggestions)
        return MealPlanResponse(client_id=request.client_id, suggestions=with_ids)

    def submit_feedback(self, request: FeedbackRequest) -> FeedbackResponse:
        """Record feedback for a recommendation."""
        ok = self.meal_feedback_store.record_feedback(
            request.recommendation_id,
            rating=request.rating,
            would_make_again=request.would_make_again,
            notes=request.notes,
        )
        return FeedbackResponse(recommendation_id=request.recommendation_id, recorded=ok)

    def get_meal_history(self, client_id: str, limit: int = 100) -> MealHistoryResponse:
        """Return past recommendations and feedback for the client."""
        entries = self.meal_feedback_store.get_meal_history(client_id, limit=limit)
        return MealHistoryResponse(client_id=client_id, entries=entries)

    # --- Chat ---

    def handle_chat(self, body: ChatRequest) -> ChatResponse:
        """Process one chat turn: run chat agent, then execute any triggered action."""
        client_id = body.client_id.strip()
        profile = self.profile_store.get_profile(client_id)

        # Only load nutrition plan and history when profile exists and has meaningful data
        nutrition_plan: Optional[NutritionPlan] = None
        meal_history: List = []
        if profile is not None:
            try:
                nutrition_plan = self._get_or_generate_nutrition_plan(profile)
            except Exception:
                nutrition_plan = None
            meal_history = self.meal_feedback_store.get_meal_history(client_id, limit=50)

        history_dicts = [{"role": m.role, "content": m.content} for m in body.conversation_history]

        result = self.chat_agent.run(
            client_id=client_id,
            user_message=body.message,
            conversation_history=history_dicts,
            profile=profile,
            nutrition_plan=nutrition_plan,
            meal_suggestions=None,
            meal_history=meal_history or None,
        )

        action = result.get("action", "none")
        response = ChatResponse(
            message=result.get("message", ""),
            phase=result.get("phase", "intake"),
            action=action,
        )

        if action == "save_profile":
            response.profile = self._handle_save_profile(client_id, result, profile)
        elif action == "generate_nutrition_plan":
            response.nutrition_plan = self._handle_generate_nutrition_plan(client_id, profile)
        elif action == "generate_meals":
            response.meal_suggestions = self._handle_generate_meals(client_id, result, profile)
        elif action == "submit_feedback":
            response.feedback_recorded = self._handle_submit_feedback(result, meal_history)

        return response

    # --- Private helpers ---

    def _record_suggestions(
        self, client_id: str, suggestions: list
    ) -> list[MealRecommendationWithId]:
        """Record each suggestion in the store and attach recommendation IDs."""
        with_ids: list[MealRecommendationWithId] = []
        for s in suggestions:
            rec_id = self.meal_feedback_store.record_recommendation(client_id, s.model_dump())
            with_ids.append(MealRecommendationWithId(**s.model_dump(), recommendation_id=rec_id))
        return with_ids

    def _handle_save_profile(
        self, client_id: str, result: Dict[str, Any], profile: Optional[ClientProfile]
    ) -> Optional[ClientProfile]:
        extracted = result.get("extracted_profile") or {}
        update_data: dict = {}
        for key in (
            "household",
            "dietary_needs",
            "allergies_and_intolerances",
            "lifestyle",
            "preferences",
            "goals",
        ):
            if key in extracted:
                update_data[key] = extracted[key]

        if update_data:
            update_req = ProfileUpdateRequest.model_validate(update_data)
            current = self.profile_store.get_profile(client_id)
            if current is None:
                current = self.profile_store.create_profile(client_id)
            saved_profile = self.intake_agent.run(
                client_id, update=update_req, current_profile=current
            )
            self.profile_store.save_profile(client_id, saved_profile)
            return saved_profile
        return profile

    def _handle_generate_nutrition_plan(
        self, client_id: str, profile: Optional[ClientProfile]
    ) -> Optional[NutritionPlan]:
        p = profile or self.profile_store.get_profile(client_id)
        if p:
            try:
                return self.nutritionist_agent.run(p)
            except Exception as e:
                logger.warning("Nutrition plan generation failed during chat: %s", e)
        return None

    def _handle_generate_meals(
        self, client_id: str, result: Dict[str, Any], profile: Optional[ClientProfile]
    ) -> list[MealRecommendationWithId]:
        p = profile or self.profile_store.get_profile(client_id)
        if not p:
            return []
        try:
            params = result.get("meal_plan_params") or {}
            period_days = params.get("period_days", 7)
            meal_types = params.get("meal_types", ["lunch", "dinner"])
            np = self._get_or_generate_nutrition_plan(p)
            mh = self.meal_feedback_store.get_meal_history(client_id, limit=50)
            suggestions = self.meal_planning_agent.run(
                p, np, mh, period_days=period_days, meal_types=meal_types
            )
            return self._record_suggestions(client_id, suggestions)
        except Exception as e:
            logger.warning("Meal plan generation failed during chat: %s", e)
            return []

    def _handle_submit_feedback(self, result: Dict[str, Any], meal_history: list) -> bool:
        fb = result.get("feedback_data") or {}
        meal_name = fb.get("meal_name", "").strip().lower()
        rating = fb.get("rating")
        would_make_again = fb.get("would_make_again")
        notes = fb.get("notes", "")

        if meal_name and meal_history:
            for entry in meal_history:
                snap = entry.meal_snapshot or {}
                name = (snap.get("name") or "").strip().lower()
                if name and (meal_name in name or name in meal_name):
                    self.meal_feedback_store.record_feedback(
                        entry.recommendation_id,
                        rating=rating,
                        would_make_again=would_make_again,
                        notes=notes,
                    )
                    return True
        return False
